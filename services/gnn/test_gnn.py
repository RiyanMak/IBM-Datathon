"""Automatic checks for the GNN re-scorer.

    python -m pytest -q services/gnn          # with the rest of the repo's tests
    python -m services.gnn.test_gnn           # one PASS/FAIL line per check

Makes its own fake data in a temp folder, so it needs nothing from the team.
"""
import json
import os
import sys
import tempfile

import numpy as np
import torch

from services.gnn import make_fake_data, train
from services.gnn.evaluate import match, parse_patterns
from services.gnn.graphdata import EDGE_FEATURES, NODE_FEATURES, build_subgraph, parse_ts, to_iso
from services.gnn.model import RingGNN, load_weights_json, ring_score_from_probs, save_weights_json
from services.gnn.rescorer import (Rescorer, normalise_edges, parse_stream_ring, ring_to_stream_fields,
                                   run_service)
from services.gnn.transactions import TxnIndex, load_transactions, time_split

TMP = tempfile.mkdtemp(prefix="gnn-test-")
FAKE = os.path.join(TMP, "fake")
WEIGHTS = os.path.join(TMP, "weights.json")
_state = {}

SAMPLE = [
    {"txn_id": "a", "from": "1_AAA", "to": "2_BBB", "ts": "2022-09-03T10:00:00", "amount_usd": 5000,
     "ccy_paid": "US Dollar", "ccy_recv": "US Dollar", "fmt": "ACH", "score": 0.8},
    {"txn_id": "b", "from": "2_BBB", "to": "3_CCC", "ts": "2022/09/03 12:30", "amount_usd": 4800, "fmt": "ACH"},
    {"txn_id": "c", "from": "2_BBB", "to": "4_DDD", "ts": 1662208200, "amount_usd": 100, "fmt": "Cash"},
]


def _fake():
    if "fake" not in _state:
        _state["fake"] = make_fake_data.build(FAKE, seed=0)
    return _state["fake"]


def _weights():
    if not os.path.exists(WEIGHTS):
        _fake()
        train.main(["--txns", os.path.join(FAKE, "fake_txns.csv"), "--out", WEIGHTS, "--epochs", "3",
                    "--max-seeds", "4000", "--hidden", "32"])
    return WEIGHTS


# ---- data -------------------------------------------------------------------

def test_fake_data_generator_writes_the_three_files():
    s = _fake()
    for f in ("fake_txns.csv", "fake_patterns.txt", "fake_rings.json"):
        assert os.path.exists(os.path.join(FAKE, f)), f"missing {f}"
    assert s["true_rings"] >= 8 and s["rings"] - s["true_rings"] >= 8


def test_raw_csv_loads_with_team_account_ids_and_splits_by_time():
    _fake()
    df = load_transactions(os.path.join(FAKE, "fake_txns.csv"))
    assert list(df.columns)[:4] == ["txn_id", "ts", "from", "to"]
    assert "_" in df["from"].iloc[0] and ":" not in df["from"].iloc[0], df["from"].iloc[0]
    assert not df["from"].str.match(r"^0").any(), "bank codes must lose their leading zeros"
    assert (df["ts"].diff().dropna() >= 0).all(), "not sorted by time"
    tr, va, te = time_split(df)
    assert tr["ts"].max() <= va["ts"].min() <= va["ts"].max() <= te["ts"].min(), "split leaks time"


def _write_csv(name, text):
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(text)
    return path


def test_clean_csv_timestamps_load_as_epoch_seconds():
    path = _write_csv("clean_ts.csv",
                      "txn_id,ts,from_acct,to_acct,amount_usd\n"
                      "0,2022-09-01,1_A,2_B,10\n"                      # older files' midnight form
                      "1,2022-09-08T14:00:00,2_B,3_C,20\n")
    df = load_transactions(path)
    assert df["ts"].tolist() == [parse_ts("2022-09-01T00:00:00"), 1662645600.0]


def test_unparseable_timestamp_is_an_error_not_year_1677():
    path = _write_csv("bad_ts.csv", "txn_id,ts,from_acct,to_acct,amount_usd\n0,not-a-time,1_A,2_B,10\n")
    try:
        load_transactions(path)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_raw_csv_txn_ids_follow_time_order_like_the_cleaning_script():
    path = _write_csv("raw_order.csv",
                      "Timestamp,From Bank,Account,To Bank,Account.1,Amount Paid\n"
                      "2022/09/01 00:20,010,A,020,B,1\n"
                      "2022/09/01 00:05,010,C,020,D,2\n")
    df = load_transactions(path)
    assert df["txn_id"].tolist() == ["0", "1"] and df["amount_paid"].tolist() == [2.0, 1.0]


def test_missing_timestamp_is_an_error_not_1970():
    for value in (None, ""):
        try:
            parse_ts(value)
        except ValueError:
            continue
        raise AssertionError(f"parse_ts({value!r}) should raise")


def test_timestamps_in_all_formats_agree():
    a = parse_ts("2022-09-03T10:00:00"); b = parse_ts("2022/09/03 10:00"); c = parse_ts(a)
    assert a == b == c
    assert to_iso(a) == "2022-09-03T10:00:00"


# ---- graph + model ----------------------------------------------------------

def test_subgraph_has_the_right_shapes_and_no_nans():
    g = build_subgraph(SAMPLE)
    assert g.num_nodes == 4 and g.num_edges == 3
    assert g.x.shape == (4, len(NODE_FEATURES)) and g.edge_attr.shape == (3, len(EDGE_FEATURES))
    assert not torch.isnan(g.x).any() and not torch.isnan(g.edge_attr).any()
    bbb = g.node_index["2_BBB"]
    assert g.x[bbb, NODE_FEATURES.index("passthrough")] > 0.9, "BBB passes nearly everything on"
    assert g.edge_attr[0, EDGE_FEATURES.index("cross_bank")] == 1.0, "1_AAA -> 2_BBB is cross-bank"
    assert g.labels is None


def test_model_gives_one_score_per_transfer_and_topk_ring_score():
    g = build_subgraph(SAMPLE)
    m = RingGNN(d=16)
    assert m(g).shape == (3,)
    probs = m.edge_probs(g)
    assert ((probs >= 0) & (probs <= 1)).all()
    assert abs(ring_score_from_probs(torch.tensor([0.1, 0.9, 0.8, 0.2]), k=2) - 0.85) < 1e-6


def test_weights_save_to_json_and_load_back_identically():
    m = RingGNN(d=16)
    p = os.path.join(TMP, "w.json")
    save_weights_json(m, p, extra={"note": "test"})
    m2 = load_weights_json(p)
    g = build_subgraph(SAMPLE)
    assert torch.allclose(m(g), m2(g), atol=1e-6)
    assert json.load(open(p))["extra"]["note"] == "test"


# ---- training ---------------------------------------------------------------

def test_training_runs_loss_falls_and_validation_auc_is_high():
    hist = json.load(open(_weights()))["extra"]["history"]
    assert hist[-1]["train_loss"] < hist[0]["train_loss"], "loss did not go down"
    assert hist[-1]["auc"] > 0.9, f"val AUC too low: {hist[-1]}"


# ---- rescoring --------------------------------------------------------------

def test_ring_edges_accepted_as_dicts_lists_or_json_text():
    as_dicts = normalise_edges(SAMPLE)
    as_lists = normalise_edges([[e["txn_id"], e["from"], e["to"], e["amount_usd"], e["ts"], e.get("score")] for e in SAMPLE])
    as_text = normalise_edges(json.dumps(SAMPLE))
    for edges in (as_dicts, as_lists, as_text):
        assert [e["txn_id"] for e in edges] == ["a", "b", "c"]
        assert edges[0]["from"] == "1_AAA" and edges[0]["amount_usd"] == 5000.0 and edges[0]["score"] == 0.8
        assert edges[1]["score"] is None
    old_style = normalise_edges([{"txn_id": 7, "src": "1_A", "dst": "2_B", "amount_paid": 10, "ts": "2022-09-03T10:00:00", "xgb_score": 0.3}])
    assert old_style[0]["from"] == "1_A" and old_style[0]["amount_usd"] == 10.0 and old_style[0]["score"] == 0.3
    assert old_style[0]["txn_id"] == "7"


def test_stream_fields_round_trip_like_the_case_writer():
    ring = {"ring_id": "R-1", "version": 2, "shape": "cycle", "n_accounts": 4, "accounts": ["1_A", "2_B"],
            "edges": SAMPLE, "xgb_score": 0.7, "gnn_score": None, "reasons": ["x"],
            "first_seen": "2022-09-03T10:00:00", "last_seen": "2022-09-03T12:30:00", "gnn_kept": True}
    fields = ring_to_stream_fields(ring)
    assert all(isinstance(v, str) for v in fields.values()), "stream values must be text"
    assert fields["gnn_score"] == "" and "gnn_kept" not in fields, "only the guide's fields go on the stream"
    back = parse_stream_ring(fields)
    assert back["accounts"] == ["1_A", "2_B"] and back["version"] == 2 and back["gnn_score"] is None
    assert back["edges"][0]["txn_id"] == "a"


def test_a_ring_scores_even_with_no_transaction_file():
    rs = Rescorer(_weights(), txns_path="", quiet=True)
    ring = {"ring_id": "R-x", "version": 1, "accounts": ["1_AAA", "2_BBB", "3_CCC", "4_DDD"], "edges": SAMPLE}
    out = rs.rescore(ring)
    assert 0 <= out["gnn_score"] <= 1 and isinstance(out["gnn_kept"], bool)
    assert set(out["gnn_meta"]["edge_scores"]) == {"a", "b", "c"}
    assert out["reasons"] and "GNN" in out["reasons"][-1]
    assert "gnn_score" not in ring, "input ring must not be modified"
    assert set(out["edges"][0]) >= {"txn_id", "from", "to", "amount_usd", "ts", "score"}


def test_true_fake_rings_score_high_and_innocent_ones_get_dropped():
    df = load_transactions(os.path.join(FAKE, "fake_txns.csv"))
    rs = Rescorer(_weights(), txn_index=TxnIndex(df), threshold=0.5, quiet=True)
    rings = json.load(open(os.path.join(FAKE, "fake_rings.json")))
    scored = [rs.rescore(r) for r in rings]
    true_scores = [r["gnn_score"] for r in scored if r["_truth"]]
    innocent = [r["gnn_score"] for r in scored if not r["_truth"]]
    assert np.mean(true_scores) > np.mean(innocent) + 0.3, (np.mean(true_scores), np.mean(innocent))
    assert np.mean([r["gnn_kept"] for r in scored if r["_truth"]]) >= 0.8
    assert np.mean([not r["gnn_kept"] for r in scored if not r["_truth"]]) >= 0.6
    assert all(r["gnn_meta"]["neighbourhood_transfers"] >= len(r["edges"]) for r in scored)
    json.dump(scored, open(os.path.join(TMP, "rescored.json"), "w"))
    _state["scored"] = scored


def test_patterns_file_parses_into_typed_attempts_with_team_ids():
    _fake()
    att = parse_patterns(os.path.join(FAKE, "fake_patterns.txt"))
    assert len(att) == 60, len(att)
    assert all("_" in a and ":" not in a for a in att[0]["accounts"])
    assert {"scatter-gather", "cycle", "fan-in"} <= {a["type"] for a in att}


def test_ring_precision_and_recall_math():
    attempts = [{"type": "cycle", "accounts": {"a", "b", "c"}}, {"type": "fan-in", "accounts": {"x", "y", "z", "w"}}]
    rings = [{"accounts": ["a", "b", "c"]}, {"accounts": ["a", "b", "q"]}, {"accounts": ["m", "n"]}]
    p, r, per, _ = match(rings, attempts)
    assert abs(p - 2 / 3) < 1e-9 and abs(r - 0.5) < 1e-9
    assert per["cycle"] == [1, 1] and per["fan-in"] == [0, 1]


def test_gnn_raises_ring_precision_on_the_fake_rings():
    if "scored" not in _state:
        test_true_fake_rings_score_high_and_innocent_ones_get_dropped()
    rings = _state["scored"]
    attempts = parse_patterns(os.path.join(FAKE, "fake_patterns.txt"))
    p0, _, _, _ = match(rings, attempts)
    p1, _, _, _ = match([r for r in rings if r["gnn_kept"]], attempts)
    assert p1 > p0, f"precision before {p0:.2f}, after {p1:.2f}"


def test_service_mode_forwards_kept_rings_on_a_fake_redis():
    try:
        import fakeredis
    except ImportError:
        import pytest
        pytest.skip("fakeredis not installed (pip install -r requirements-dev.txt)")
    r = fakeredis.FakeRedis(decode_responses=True)
    rings = json.load(open(os.path.join(FAKE, "fake_rings.json")))
    for ring in rings[:6]:
        r.xadd("candidates", ring_to_stream_fields(ring))
    df = load_transactions(os.path.join(FAKE, "fake_txns.csv"))
    rs = Rescorer(_weights(), txn_index=TxnIndex(df), quiet=True)
    handled = run_service(rs, "candidates", "rings", drop=True, redis_client=r, max_messages=6)
    assert handled == 6
    out = r.xrange("rings", "-", "+")
    assert 0 < len(out) <= 6
    fields = out[0][1]
    assert fields["gnn_score"] != "" and float(fields["gnn_score"]) >= 0.5
    assert json.loads(fields["edges"])[0]["from"]
    assert r.hget("stats:gnn", "scored") == "6"


def test_service_mode_leaves_a_ring_pending_when_forwarding_fails():
    try:
        import fakeredis
    except ImportError:
        import pytest
        pytest.skip("fakeredis not installed (pip install -r requirements-dev.txt)")

    class BrokenOutput(fakeredis.FakeRedis):
        def xadd(self, name, *a, **kw):
            if name == "rings":
                raise ConnectionError("redis went away")
            return super().xadd(name, *a, **kw)

    r = BrokenOutput(decode_responses=True)
    _fake()
    ring = json.load(open(os.path.join(FAKE, "fake_rings.json")))[0]
    r.xadd("candidates", ring_to_stream_fields(ring))
    rs = Rescorer(_weights(), txn_index=None, quiet=True)
    run_service(rs, "candidates", "rings", drop=False, redis_client=r, max_messages=1)
    assert r.xpending("candidates", "gnn")["pending"] == 1


# ---- plain runner -----------------------------------------------------------

if __name__ == "__main__":
    import traceback
    names = [n for n in dir() if n.startswith("test_")]
    fails = skips = 0
    for n in names:
        try:
            globals()[n]()
            print(f"PASS  {n}")
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ in ("Skipped", "skip", "SkipTest") or "skip" in str(e).lower():
                skips += 1
                print(f"SKIP  {n} ({e})")
            else:
                fails += 1
                print(f"FAIL  {n}\n      {e}")
                traceback.print_exc(limit=2)
    print(f"\n{len(names) - fails - skips} passed, {skips} skipped, {fails} failed")
    print("All tests passed" if not fails else "SOME TESTS FAILED")
    sys.exit(1 if fails else 0)
