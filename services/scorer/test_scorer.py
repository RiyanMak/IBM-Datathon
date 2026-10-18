"""
Tests for the scorer (services/scorer/scorer.py) and the reason phrases.
Run from the repo root:
    python -m pytest -q services/scorer
Redis tests use fakeredis (pip install -r requirements-dev.txt); model tests need
xgboost. Both are skipped if the package is missing.
"""
import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from common import bus
from common.clean_transactions import write_clean_csv
from common.features import FEATURE_NAMES, FORMATS, ClueBuilder
from common.streams import TXN_FIELDS, message_to_txn, now_sent_at, txn_to_message
from services.scorer import reasons
from services.scorer.scorer import (
    GROUP, STATS_KEY, Model, Stats, ensure_group, main, run, score_batch,
    warm_up_from_file, warm_up_from_stream,
)

T0 = datetime(2022, 9, 8, 16, 12)
ACCTS = [f"{bank}_{i:03X}" for bank in (10, 27, 70) for i in range(12)]


# ---------------------------------------------------------------- helpers

def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis(decode_responses=True)


def transfers(n, seed=0, start=T0, step_minutes=1):
    """n synthetic transfers in time order, a few minutes apart, as dicts with the clean.csv columns."""
    rng = np.random.default_rng(seed)
    rows = []
    t = start
    for i in range(n):
        t = t + timedelta(minutes=int(rng.integers(0, step_minutes + 1)))
        src, dst = rng.choice(ACCTS, 2, replace=False)
        amount = round(float(rng.lognormal(5, 1.2)), 2)
        rows.append({"txn_id": i, "ts": t, "from_acct": str(src), "to_acct": str(dst),
                     "amount_usd": amount, "amt_paid": amount, "ccy_paid": "US Dollar",
                     "amt_recv": amount, "ccy_recv": "US Dollar", "fmt": str(rng.choice(FORMATS))})
    return rows


def messages(rows):
    return [txn_to_message(row, now_sent_at()) for row in rows]


def push(r, msgs, stream=bus.TXNS):
    return [r.xadd(stream, m) for m in msgs]


def offline_clues(rows):
    """The notebook's path: a fresh builder over the rows, text timestamps."""
    b = ClueBuilder()
    return np.asarray([list(b.compute({**row, "ts": row["ts"].strftime("%Y-%m-%dT%H:%M:%S"),
                                        "amount_usd": str(row["amount_usd"])}).values()) for row in rows],
                      dtype=np.float32)


@pytest.fixture(scope="module")
def models_dir(tmp_path_factory):
    """A tiny real XGBoost model trained on synthetic clues, saved the way the notebook saves it."""
    xgb = pytest.importorskip("xgboost")
    rows = transfers(3000, seed=1)
    X = offline_clues(rows)
    rng = np.random.default_rng(2)
    # label: pass-through to strangers is suspicious, plus noise
    names = list(FEATURE_NAMES)
    y = ((X[:, names.index("new_payee")] > 0.5) & (X[:, names.index("pass_through")] > 0.2)
         & (rng.random(len(X)) < 0.9)).astype(int)
    model = xgb.XGBClassifier(n_estimators=30, max_depth=3, learning_rate=0.3, tree_method="hist",
                              scale_pos_weight=float((y == 0).sum() / max(1, (y == 1).sum())))
    model.fit(pd.DataFrame(X, columns=names), y)
    folder = tmp_path_factory.mktemp("models")
    model.save_model(folder / "scorer.json")
    (folder / "feature_list.json").write_text(json.dumps(names))
    scores = model.predict_proba(pd.DataFrame(X, columns=names))[:, 1]
    alert, ring = float(np.quantile(scores, 0.9)), float(np.quantile(scores, 0.7))
    (folder / "thresholds.json").write_text(json.dumps({"alert": alert, "ring": ring, "chosen_on": "validation"}))
    return folder


# ---------------------------------------------------------------- model

def test_model_refuses_a_different_feature_order(models_dir, tmp_path):
    for name in ("scorer.json", "thresholds.json"):
        (tmp_path / name).write_bytes((models_dir / name).read_bytes())
    swapped = list(FEATURE_NAMES)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    (tmp_path / "feature_list.json").write_text(json.dumps(swapped))
    with pytest.raises(ValueError, match="feature_list.json"):
        Model(tmp_path)


def test_model_scores_and_contributions_have_the_right_shape(models_dir):
    m = Model(models_dir)
    X = offline_clues(transfers(50, seed=3))
    s = m.scores(X)
    c = m.contributions(X)
    assert s.shape == (50,) and np.all((s >= 0) & (s <= 1))
    assert c.shape == (50, len(FEATURE_NAMES))
    assert 0 <= m.ring <= m.alert <= 1


# ---------------------------------------------------------------- scoring

def test_scored_messages_match_the_offline_path(models_dir):
    rows = transfers(400, seed=4)
    entries = list(zip([f"1-{i}" for i in range(len(rows))], messages(rows)))
    model = Model(models_dir)
    stats = Stats()
    results = score_batch(model, ClueBuilder(), entries, stats)

    X = offline_clues(rows)
    expected = model.scores(X)
    assert len(results) == len(rows)
    n_reasons = 0
    for (msg_id, out, latency), score, x, row in zip(results, expected, X, rows):
        assert out is not None and latency is not None and latency >= 0
        assert set(out) == set(TXN_FIELDS) | {"score", "flagged", "reasons", "clues", "scored_at"}
        assert out["txn_id"] == str(row["txn_id"])
        assert abs(float(out["score"]) - float(score)) < 1e-5          # live == offline
        assert out["flagged"] == ("1" if score >= model.alert else "0")
        clues = json.loads(out["clues"])
        assert list(clues) == list(FEATURE_NAMES)
        assert np.allclose(list(clues.values()), x, atol=1e-4)
        phrases = json.loads(out["reasons"])
        assert isinstance(phrases, list) and len(phrases) <= 3
        if score < model.ring:
            assert phrases == []                                        # reasons only for ring candidates
        n_reasons += bool(phrases)
    assert n_reasons > 0
    assert stats.scored == len(rows) and stats.flagged == sum(1 for _, o, _ in results if o["flagged"] == "1")
    assert stats.candidates >= stats.flagged


def test_malformed_message_is_skipped_but_still_acknowledged(models_dir):
    rows = transfers(5, seed=5)
    msgs = messages(rows)
    msgs[2] = {"txn_id": "2", "ts": "garbage"}                         # missing fields
    entries = list(zip([f"1-{i}" for i in range(5)], msgs))
    stats = Stats()
    results = score_batch(Model(models_dir), ClueBuilder(), entries, stats)
    assert [out is None for _, out, _ in results] == [False, False, True, False, False]
    assert stats.skipped == 1 and stats.scored == 4


# ---------------------------------------------------------------- the loop, with a fake Redis

def test_run_end_to_end(models_dir):
    r = fake_redis()
    rows = transfers(250, seed=6)
    push(r, messages(rows))
    model = Model(models_dir)
    stats = run(r, model, ClueBuilder(), "scorer-test", once=True, progress=None)

    assert stats.scored == 250
    scored = r.xrange(bus.SCORED, "-", "+")
    assert [f["txn_id"] for _, f in scored] == [str(row["txn_id"]) for row in rows]   # order kept
    assert r.xpending(bus.TXNS, GROUP)["pending"] == 0                                  # everything acked
    h = r.hgetall(STATS_KEY)
    assert int(h["scored"]) == 250 and int(h["flagged"]) == stats.flagged
    assert h["consumer"] == "scorer-test" and h["p50_ms"] != ""
    for _, f in scored:
        assert float(f["scored_at"]) >= float(f["sent_at"])


def test_pending_messages_are_scored_first_after_a_crash(models_dir):
    r = fake_redis()
    rows = transfers(30, seed=7)
    push(r, messages(rows))
    ensure_group(r)
    # deliver 10 to this consumer without acknowledging: the "crash"
    r.xreadgroup(GROUP, "scorer-a", {bus.TXNS: ">"}, count=10)
    assert r.xpending(bus.TXNS, GROUP)["pending"] == 10
    stats = run(r, Model(models_dir), ClueBuilder(), "scorer-a", once=True, progress=None)
    assert stats.scored == 30
    assert r.xpending(bus.TXNS, GROUP)["pending"] == 0
    assert [f["txn_id"] for _, f in r.xrange(bus.SCORED, "-", "+")] == [str(i) for i in range(30)]


def test_two_consumers_share_the_queue(models_dir):
    r = fake_redis()
    rows = transfers(40, seed=8)
    push(r, messages(rows))
    model = Model(models_dir)
    a = run(r, model, ClueBuilder(), "scorer-a", max_messages=15, batch=5, once=True, progress=None)
    b = run(r, model, ClueBuilder(), "scorer-b", once=True, batch=5, progress=None)
    assert a.scored == 15 and b.scored == 25
    assert sorted(int(f["txn_id"]) for _, f in r.xrange(bus.SCORED, "-", "+")) == list(range(40))
    assert r.xpending(bus.TXNS, GROUP)["pending"] == 0


def test_ensure_group_twice_is_fine():
    r = fake_redis()
    ensure_group(r)
    ensure_group(r)
    assert [g["name"] for g in r.xinfo_groups(bus.TXNS)] == [GROUP]


# ---------------------------------------------------------------- warm-up

def test_restart_continuity_from_the_stream(models_dir):
    """Scores after a restart equal the scores of one uninterrupted run."""
    rows = transfers(120, seed=9)
    msgs = messages(rows)
    model = Model(models_dir)

    r1 = fake_redis()
    push(r1, msgs)
    run(r1, model, ClueBuilder(), "s", once=True, progress=None)
    uninterrupted = [f["score"] for _, f in r1.xrange(bus.SCORED, "-", "+")]

    r2 = fake_redis()
    push(r2, msgs[:70])
    run(r2, model, ClueBuilder(), "s", once=True, progress=None)       # first life
    push(r2, msgs[70:])
    fresh = ClueBuilder()                                              # second life: empty memory
    fed = warm_up_from_stream(fresh, r2)
    assert fed == 70
    run(r2, model, fresh, "s", once=True, progress=None)
    restarted = [f["score"] for _, f in r2.xrange(bus.SCORED, "-", "+")]
    assert restarted == uninterrupted

    # without the stream warm-up the second life would drift
    r3 = fake_redis()
    push(r3, msgs[:70])
    run(r3, model, ClueBuilder(), "s", once=True, progress=None)
    push(r3, msgs[70:])
    run(r3, model, ClueBuilder(), "s", once=True, progress=None)
    drifted = [f["score"] for _, f in r3.xrange(bus.SCORED, "-", "+")]
    assert drifted != uninterrupted


def test_warm_up_from_file_feeds_only_rows_before_the_start(tmp_path):
    rows = transfers(60, seed=10, start=T0 - timedelta(minutes=60))
    start = rows[40]["ts"]
    df = pd.DataFrame(rows)
    df["label"] = 0
    df["split"] = "train"
    write_clean_csv(df, tmp_path / "clean.csv")

    b = ClueBuilder()
    fed = warm_up_from_file(b, tmp_path / "clean.csv", start.strftime("%Y-%m-%dT%H:%M:%S"),
                            chunk_rows=7, progress=None)
    before = [row for row in rows if row["ts"] < start]
    assert fed == len(before)

    # identical memory to feeding the same rows directly: the next clue row matches exactly
    direct = ClueBuilder()
    for row in before:
        direct.compute(txn_to_message(row, "0"))
    nxt = rows[len(before)]
    msg = txn_to_message(nxt, "0")
    assert b.compute(msg) == direct.compute(msg)


def test_main_with_fake_redis_and_no_warmup(models_dir):
    r = fake_redis()
    push(r, messages(transfers(20, seed=11)))
    stats = main(["--models", str(models_dir), "--no-warmup", "--once", "--consumer", "c1"], r=r)
    assert stats.scored == 20
    assert r.xlen(bus.SCORED) == 20


# ---------------------------------------------------------------- phrases

def test_every_clue_has_a_phrase_and_binary_zeros_are_skipped():
    names = list(FEATURE_NAMES)
    values = np.zeros(len(names), dtype=np.float32)
    contribs = np.zeros(len(names))
    values[names.index("pass_through")] = 0.98
    values[names.index("mins_since_snd_last_in")] = 0.0
    values[names.index("snd_payees_24h")] = 6
    contribs[names.index("fmt_Wire")] = 3.0            # the largest, but fmt_Wire is 0: skipped
    contribs[names.index("pass_through")] = 2.0
    contribs[names.index("mins_since_snd_last_in")] = 1.5
    contribs[names.index("snd_payees_24h")] = 1.0
    contribs[names.index("log_amount")] = 0.5          # fourth largest positive: not in the top 3
    contribs[names.index("new_payee")] = -1.0          # negative: never shown
    out = reasons.phrases(values, contribs, amount_usd=10000.0)
    assert out == ["98% of money received was passed straight on",
                   "money left within a minute of arriving",
                   "paid 6 different accounts in 24 hours"]


def test_phrase_wording_samples():
    t = reasons.TEMPLATES
    assert t["fmt_ACH"](1, 0) == "paid by ACH"
    assert t["fmt_Credit_Card"](1, 0) == "paid by Credit Card"
    assert t["log_amount"](4.1, 12500.0) == "unusually large: $12,500"
    assert t["mins_since_snd_last_in"](40, 0) == "money left 40 minutes after it arrived"
    assert t["mins_since_snd_last_in"](-1, 0) == "sender has never received money"
    assert t["pass_through"](5.0, 0) == "more than 100% of money received was passed straight on"
    assert t["rcv_payers_24h"](6, 0) == "receiver was paid by 6 different accounts in 24 hours"
    assert t["snd_age_days"](0.02, 0) == "sender account first seen today"
    assert t["weekday"](3, 0) == "sent on a Thursday"
    assert reasons.phrases(np.ones(len(FEATURE_NAMES)), np.zeros(len(FEATURE_NAMES)), 1.0) == []
