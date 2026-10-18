"""
Tests for the ring service (services/rings/rings.py).
Run from the repo root:
    python -m pytest -q services/rings
Redis tests use fakeredis and are skipped without it; the Louvain test needs networkx.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from common import bus
from services.rings.rings import (
    GROUP, STATS_KEY, Edge, RingService, Window, connected_groups, has_cycle, hub_accounts,
    is_bipartite, is_gather_scatter, is_scatter_gather, is_stack, main, make_groups, match_ring,
    name_shape, parse_scored, ring_fields, run, adjacency,
)

T0 = datetime(2022, 9, 8, 16, 12)
FAKE_RINGS = Path(__file__).resolve().parents[1] / "casewriter" / "fake_rings.json"


# ---------------------------------------------------------------- helpers

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


_counter = [7_000_000]


def scored(src, dst, score, minutes=0, amount=1000.0, reasons=(), txn_id=None, base=T0):
    """A `scored` message as the scorer writes it (all text)."""
    _counter[0] += 1
    txn_id = _counter[0] if txn_id is None else txn_id
    ts = iso(base + timedelta(minutes=minutes))
    return {"txn_id": str(txn_id), "ts": ts, "from_acct": src, "to_acct": dst,
            "amount_usd": repr(float(amount)), "amt_paid": repr(float(amount)), "ccy_paid": "US Dollar",
            "amt_recv": repr(float(amount)), "ccy_recv": "US Dollar", "fmt": "ACH",
            "sent_at": "1792000000.000", "score": "%.6f" % score, "flagged": "1" if score >= 0.9 else "0",
            "reasons": json.dumps(list(reasons)), "clues": "{}", "scored_at": "1792000000.010"}


def edges(*specs, score=0.9, base=T0):
    """Edge objects from (src, dst) or (src, dst, minutes) tuples."""
    out = []
    for i, spec in enumerate(specs):
        src, dst = spec[0], spec[1]
        minutes = spec[2] if len(spec) > 2 else i
        msg = scored(src, dst, score, minutes, base=base)
        out.append(parse_scored(msg))
    return out


def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis(decode_responses=True)


def fake_ring_edges(ring):
    """The edges of one ring from casewriter/fake_rings.json, as Edge objects."""
    out = []
    for e in ring["edges"]:
        if isinstance(e, dict):
            txn, src, dst, amt, ts, score = (e["txn_id"], e["from"], e["to"], e["amount_usd"], e["ts"], e["score"])
        else:
            txn, src, dst, amt, ts, score = e
        out.append(parse_scored({"txn_id": txn, "ts": ts, "from_acct": src, "to_acct": dst,
                                 "amount_usd": str(amt), "score": str(score), "reasons": "[]"}))
    return out


# ---------------------------------------------------------------- parsing

def test_parse_scored_reads_what_the_scorer_writes():
    e = parse_scored(scored("10_A", "27_B", 0.95, minutes=5, amount=1234.5, reasons=["paid by ACH"]))
    assert (e.src, e.dst, e.amount, e.score, e.reasons) == ("10_A", "27_B", 1234.5, 0.95, ["paid by ACH"])
    assert e.ts == "2022-09-08T16:17:00" and e.ts_min % 60 == 17


@pytest.mark.parametrize("bad", [
    {"txn_id": "1"},
    {**scored("a", "b", 0.5), "score": "1.5"},
    {**scored("a", "b", 0.5), "ts": "yesterday"},
    {**scored("a", "b", 0.5), "amount_usd": "nan"},
    {**scored("a", "b", 0.5), "from_acct": ""},
])
def test_parse_scored_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_scored(bad)


# ---------------------------------------------------------------- shapes

def test_shapes_of_the_case_writers_fake_rings():
    rings = json.loads(FAKE_RINGS.read_text())
    want = {"R-0001": "scatter-gather", "R-0002": "fan-out", "R-0003": "cycle"}
    for ring in rings:
        es = fake_ring_edges(ring)
        accounts = {e.src for e in es} | {e.dst for e in es}
        assert name_shape(accounts, es) == want[ring["ring_id"]], ring["ring_id"]


def test_each_shape_rule():
    def shape(*specs):
        es = edges(*specs)
        return name_shape({e.src for e in es} | {e.dst for e in es}, es)

    assert shape(("A", "B"), ("A", "C"), ("A", "D")) == "fan-out"
    assert shape(("B", "A"), ("C", "A"), ("D", "A")) == "fan-in"
    assert shape(("A", "B"), ("B", "C"), ("C", "A")) == "cycle"
    assert shape(("A", "B"), ("B", "A"), ("B", "C")) == "other"          # a 2-step loop is not a cycle
    assert shape(("A", "B"), ("B", "C")) == "other"
    assert shape(("S1", "H"), ("S2", "H"), ("H", "R1"), ("H", "R2")) == "gather-scatter"
    assert shape(("A", "M1"), ("A", "M2"), ("M1", "B"), ("M2", "B")) == "scatter-gather"
    assert shape(("S1", "T1"), ("S1", "T2"), ("S2", "T1"), ("S2", "T2")) == "bipartite"
    assert shape(("M", "P1"), ("M", "P2"), ("M", "P3"), ("Q", "P1")) == "fan-out"   # one extra sender is not a layer
    assert shape(("S1", "M1"), ("S2", "M2"), ("S1", "M2"), ("M1", "T1"), ("M2", "T2"), ("M2", "T1")) == "stack"
    # scatter-gather wins over fan-out when both apply
    assert shape(("A", "M1"), ("A", "M2"), ("A", "M3"), ("M1", "B"), ("M2", "B")) == "scatter-gather"


def test_shape_helpers_directly():
    es = edges(("A", "B"), ("B", "C"), ("C", "A"), ("A", "D"))
    out, inn = adjacency(es)
    assert has_cycle(out) and not is_bipartite({"A", "B", "C", "D"}, out, inn)
    assert not is_stack({"A", "B", "C", "D"}, out, inn)
    assert not is_scatter_gather(out, inn) and not is_gather_scatter(out, inn)


# ---------------------------------------------------------------- grouping

def test_connected_groups_and_size_filter():
    es = edges(("A", "B"), ("B", "C"), ("X", "Y"), ("P", "Q"), ("Q", "R"), ("R", "S"))
    groups, hubs = make_groups(es)
    assert hubs == set()
    assert sorted(sorted(g) for g in groups) == [["A", "B", "C"], ["P", "Q", "R", "S"]]   # X-Y is too small


def test_hub_is_left_out_and_keeps_rings_apart():
    specs = [("A", "B"), ("B", "C"), ("P", "Q"), ("Q", "R")]
    specs += [("HUB", f"x{i}") for i in range(301)] + [("A", "HUB"), ("P", "HUB")]
    es = edges(*specs)
    assert hub_accounts(es, 300) == {"HUB"}
    groups, hubs = make_groups(es, hub_limit=300)
    assert hubs == {"HUB"}
    assert sorted(sorted(g) for g in groups) == [["A", "B", "C"], ["P", "Q", "R"]]
    # with a higher limit the hub glues everything into one giant group, which is then split or dropped
    glued = connected_groups(es)
    assert len(glued) == 1 and len(glued[0]) > 300


def test_large_group_is_split_with_louvain():
    pytest.importorskip("networkx")
    specs = []
    for cluster in ("L", "R"):
        for i in range(30):
            for step in (1, 2, 3, 5, 7, 11):     # each account pays 6 others in its cluster
                specs.append((f"{cluster}{i}", f"{cluster}{(i + step) % 30}"))
    specs.append(("L0", "R0"))      # one bridge between two dense clusters of 30
    es = edges(*specs)
    groups, _ = make_groups(es, max_size=50)
    assert sorted(len(g) for g in groups) == [30, 30]
    assert all(len({a[0] for a in g}) == 1 for g in groups)   # each piece is one cluster


def test_window_prunes_old_transfers():
    w = Window(minutes=60)
    old, new = edges(("A", "B", 0), ("C", "D", 61))
    assert w.add(old) and w.add(new) and not w.add(new)
    assert w.prune(new.ts_min) == 1
    assert [e.src for e in w.edges] == ["C"] and old.txn_id not in w.ids


# ---------------------------------------------------------------- the service

def test_regroups_every_10_data_minutes_and_keeps_ids_stable():
    s = RingService(threshold=0.5, regroup_minutes=10)
    # a fan-out forming over 15 minutes
    assert s.offer(scored("M", "P1", 0.95, 0)) == []
    assert s.offer(scored("M", "P2", 0.95, 4)) == []
    assert s.offer(scored("M", "P3", 0.95, 9)) == []          # 9 min: not yet
    out = s.offer(scored("M", "P4", 0.95, 10))                # 10 min: regroup
    assert len(out) == 1
    first = out[0]
    assert first["ring_id"] == "R-0001" and first["version"] == "1" and first["shape"] == "fan-out"
    assert json.loads(first["accounts"]) == ["M", "P1", "P2", "P3", "P4"]
    assert len(json.loads(first["edges"])) == 4

    # nothing changed by the next regroup: nothing published
    assert s.offer(scored("X", "Y", 0.1, 20)) == []           # below threshold, but it moves the clock
    assert s.stats["regroups"] == 2 and s.stats["published"] == 1

    # the ring grows: same id, version 2
    s.offer(scored("P1", "B", 0.93, 25))
    s.offer(scored("P2", "B", 0.93, 26))
    out = s.offer(scored("P3", "B", 0.93, 30))
    assert [(o["ring_id"], o["version"], o["shape"]) for o in out] == [("R-0001", "2", "scatter-gather")]

    # an unrelated group gets a new id
    s.offer(scored("Q1", "Q2", 0.9, 31)); s.offer(scored("Q2", "Q3", 0.9, 32))
    out = s.offer(scored("Q3", "Q1", 0.9, 40))
    assert [(o["ring_id"], o["version"], o["shape"]) for o in out] == [("R-0002", "1", "cycle")]


def test_members_weaker_transfers_join_the_ring():
    """Grouping uses transfers at or above the ring threshold; once a group exists, its members'
    transfers at or above the expand threshold join it, one hop out."""
    s = RingService(threshold=0.9, expand_threshold=0.5, regroup_minutes=10)
    s.offer(scored("M", "P1", 0.95, 0)); s.offer(scored("M", "P2", 0.96, 1))     # the core
    s.offer(scored("M", "P3", 0.60, 2)); s.offer(scored("M", "P4", 0.70, 3))     # weaker, from a member: join
    s.offer(scored("M", "P5", 0.20, 4))                                           # too weak: stays out
    s.offer(scored("Q", "P1", 0.55, 5))                                           # into a member: joins (one hop)
    s.offer(scored("Z1", "Z2", 0.65, 6))                                          # weak and unrelated: no ring
    out = s.offer(scored("X", "Y", 0.1, 12))
    assert len(out) == 1
    f = out[0]
    assert json.loads(f["accounts"]) == ["M", "P1", "P2", "P3", "P4", "Q"]
    assert len(json.loads(f["edges"])) == 5
    assert f["shape"] == "fan-out"
    assert s.stats["candidates_in_window"] == 2 and len(s.window) == 6

    # the size cap: expansion adds the highest-scoring transfers first and stops at max_size
    s = RingService(threshold=0.9, expand_threshold=0.5, regroup_minutes=10, max_size=4)
    s.offer(scored("M", "P1", 0.95, 0)); s.offer(scored("M", "P2", 0.96, 1))
    for i, sc in enumerate((0.6, 0.8, 0.7)):
        s.offer(scored("M", f"W{i}", sc, 2 + i))
    out = s.offer(scored("X", "Y", 0.1, 12))
    assert json.loads(out[0]["accounts"]) == ["M", "P1", "P2", "W1"]              # W1 scored 0.8

    # expansion never pulls in a hub or a self-transfer
    s = RingService(threshold=0.9, expand_threshold=0.5, regroup_minutes=10, hub_limit=2)
    s.offer(scored("A", "B", 0.95, 0)); s.offer(scored("B", "C", 0.95, 1))
    for i in range(3):
        s.offer(scored("HUB", f"h{i}", 0.95, 2))
    s.offer(scored("A", "HUB", 0.7, 3)); s.offer(scored("A", "A", 0.99, 4))
    out = s.offer(scored("X", "Y", 0.1, 12))
    assert json.loads(out[0]["accounts"]) == ["A", "B", "C"]


def test_ring_message_fields_and_reasons():
    es = edges(("M", "P1", 0), ("M", "P2", 1), ("P1", "B", 5))
    es[0].reasons = ["paid by ACH", "new payee"]
    es[1].reasons = ["paid by ACH", "money left within a minute of arriving"]
    es[2].reasons = ["paid by ACH", "new payee", "large"]
    from services.rings.rings import Ring
    ring = Ring("R-0042", 0)
    ring.version, ring.accounts, ring.shape = 3, {"M", "P1", "P2", "B"}, "scatter-gather"
    f = ring_fields(ring, es)
    assert f["ring_id"] == "R-0042" and f["version"] == "3" and f["n_accounts"] == "4"
    assert json.loads(f["accounts"]) == ["B", "M", "P1", "P2"]
    e0 = json.loads(f["edges"])[0]
    assert set(e0) == {"txn_id", "from", "to", "amount_usd", "ts", "score"}
    assert f["xgb_score"] == "0.9000" and f["gnn_score"] == ""
    assert json.loads(f["reasons"]) == ["paid by ACH", "new payee", "money left within a minute of arriving"]
    assert f["first_seen"] == es[0].ts and f["last_seen"] == es[2].ts
    assert all(isinstance(v, str) for v in f.values())


def test_match_ring_needs_half_the_accounts():
    from services.rings.rings import Ring
    r = Ring("R-0001", 0); r.core = {"A", "B", "C", "D"}; r.accounts = set(r.core) | {"E"}; r.version = 1
    rings = {"R-0001": r}
    assert match_ring({"A", "B", "X"}, rings, set()) is r          # 2 of 3 shared
    assert match_ring({"A", "X", "Y", "Z"}, rings, set()) is None  # 1 of 4
    assert match_ring({"A", "B", "X"}, rings, {"R-0001"}) is None  # already claimed this round


def test_duplicates_and_self_transfers_are_ignored():
    s = RingService(threshold=0.5)
    m = scored("A", "B", 0.9, 0, txn_id=1)
    s.offer(m); s.offer(m); s.offer(scored("C", "C", 0.99, 1))
    assert s.stats["kept"] == 1 and s.stats["duplicates"] == 1 and len(s.window) == 1


# ---------------------------------------------------------------- Redis loop, and the consumers downstream

def test_run_end_to_end_and_the_case_writer_and_page_read_the_result():
    from services.casewriter.facts import build_facts, parse_ring
    from services.casewriter.template import template_summary
    from services.page import state

    r = fake_redis()
    rings = json.loads(FAKE_RINGS.read_text())
    msgs = []
    for ring in rings:
        for e in fake_ring_edges(ring):
            msgs.append(scored(e.src, e.dst, e.score, 0, e.amount, ["paid by ACH"], txn_id=e.txn_id))
            msgs[-1]["ts"] = e.ts
    msgs.sort(key=lambda m: m["ts"])
    for i in range(40):       # normal traffic that must not end up in a ring
        msgs.append(scored(f"70_N{i}", f"70_N{i + 1}", 0.05, i))
    for m in msgs:
        r.xadd(bus.SCORED, m)

    service = RingService(threshold=0.5, new_id=lambda: "R-%04d" % r.incr("rings:next_id"))
    stats = run(r, service, "rings-test", once=True, progress=None)
    assert stats["seen"] == len(msgs) and stats["skipped"] == 0
    assert r.xpending(bus.SCORED, GROUP)["pending"] == 0

    published = [f for _, f in r.xrange(bus.RINGS, "-", "+")]
    latest = {}
    for f in published:
        latest[f["ring_id"]] = f
    assert sorted(f["shape"] for f in latest.values()) == ["cycle", "fan-out", "scatter-gather"]
    assert all(f["ring_id"].startswith("R-") for f in latest.values())
    assert r.hgetall(STATS_KEY)["rings"] == "3"

    for f in latest.values():
        ring = parse_ring(f)                       # the case writer's parser
        facts = build_facts(ring)
        summary = template_summary(facts)
        assert facts["ring_id"] == f["ring_id"] and facts["gnn_score"] is None
        assert summary["summary"]
        view = state.parse_ring("1-0", f)          # the page's parser
        assert view.n_accounts == int(f["n_accounts"]) and view.gnn_score is None
        assert view.reasons == ["paid by ACH"]


def test_main_with_fake_redis_and_a_thresholds_file(tmp_path):
    r = fake_redis()
    (tmp_path / "thresholds.json").write_text(json.dumps({"alert": 0.9, "ring": 0.5}))
    for i, spec in enumerate([("A", "B"), ("B", "C"), ("C", "A"), ("A", "D")]):
        r.xadd(bus.SCORED, scored(spec[0], spec[1], 0.8, i))
    service = main(["--models", str(tmp_path), "--once", "--consumer", "t"], r=r)
    assert service.threshold == 0.5
    rings = [f for _, f in r.xrange(bus.RINGS, "-", "+")]
    assert len(rings) == 1 and rings[0]["shape"] == "cycle" and rings[0]["ring_id"] == "R-0001"
    assert r.get("rings:next_id") == "1"
