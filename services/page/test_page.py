"""Checks for the page's data layer. Uses fakeredis and builds its own messages;
it never imports services.page.fake, so deleting that folder cannot break it."""
import json
from datetime import datetime, timezone

import pytest

fakeredis = pytest.importorskip("fakeredis")
import redis

from common import bus
from services.page import decisions, state
from services.page.models import PageState, normalise_shape, part_label, source_label


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def ring_msg(ring_id="R-1", version=1, **over):
    msg = {
        "ring_id": ring_id, "version": str(version), "shape": "Fan-out", "n_accounts": "3",
        "accounts": json.dumps(["1_A", "2_B", "3_C"]),
        "edges": json.dumps([
            {"txn_id": "t1", "from": "1_A", "to": "2_B", "amount_usd": 100.0,
             "ts": "2022-09-08T10:00:00", "score": 0.9},
            ["t2", "1_A", "3_C", "50.5", "2022-09-08T10:05:00", "0.8"],
        ]),
        "xgb_score": "0.85", "gnn_score": "0.7",
        "reasons": json.dumps(["a", "b", "c", "d"]),
        "first_seen": "2022-09-08T10:00:00", "last_seen": "2022-09-08T10:05:00",
    }
    msg.update(over)
    return msg


def case_msg(ring_id="R-1", version=1, source="template", **over):
    msg = {
        "ring_id": ring_id, "version": str(version),
        "summary": json.dumps({"summary": "s", "what_happened": "w"}),
        "source": source, "check_note": "granite not used",
        "written_at": "2026-10-17T10:00:00", "facts": json.dumps({"key_account": "2_B"}),
    }
    msg.update(over)
    return msg


def scored_msg(i, flagged=0, latency_ms=4.0):
    sent = 1_760_000_000.0 + i
    return {"txn_id": "t%d" % i, "flagged": str(flagged),
            "sent_at": datetime.fromtimestamp(sent, timezone.utc).isoformat(timespec="milliseconds"),
            "scored_at": datetime.fromtimestamp(sent + latency_ms / 1000, timezone.utc)
                         .isoformat(timespec="milliseconds")}


# --- empty and broken input ---------------------------------------------------

def test_empty_redis_gives_empty_state(r):
    snap = state.load(r)
    assert snap.redis_ok and not snap.errors
    assert snap.rings == [] and snap.cases == {} and snap.decisions == {}
    c = snap.counters
    assert (c.scored_total, c.flagged, c.open_rings, c.p50_ms) == (0, 0, 0, None)


def test_redis_down_is_reported_not_raised():
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2,
                       retry=Retry(NoBackoff(), 0), decode_responses=True)
    snap = state.load(dead)
    assert snap.redis_ok is False
    assert len(snap.errors) == 1 and "not reachable" in snap.errors[0]


def test_bad_messages_are_skipped(r):
    r.xadd(bus.RINGS, ring_msg("R-1"))
    r.xadd(bus.RINGS, {"version": "1"})                         # no ring_id
    r.xadd(bus.RINGS, ring_msg("R-2", edges=json.dumps([42])))  # unrecognised edge
    r.xadd(bus.CASES, {"summary": "x"})                          # no ring_id
    snap = state.load(r)
    assert [x.ring_id for x in snap.rings] == ["R-1"]
    assert snap.skipped == {bus.RINGS: 2, bus.CASES: 1}
    assert not snap.errors


def test_broken_fields_fall_back(r):
    r.xadd(bus.RINGS, ring_msg(gnn_score="", xgb_score="nan", reasons="not json", accounts="[oops"))
    ring = state.load(r).rings[0]
    assert ring.gnn_score is None and ring.xgb_score is None
    assert ring.reasons == [] and ring.accounts == []


# --- rings --------------------------------------------------------------------

def test_ring_parsing(r):
    r.xadd(bus.RINGS, ring_msg(shape="SCATTER_GATHER"))
    ring = state.load(r).rings[0]
    assert ring.shape == "scatter-gather" and ring.shape_label == "Scatter-gather"
    assert [e.txn_id for e in ring.edges] == ["t1", "t2"]   # dict and list edges
    assert ring.edges[1].amount_usd == 50.5 and ring.edges[1].score == 0.8
    assert ring.total_usd == 150.5
    assert ring.reasons == ["a", "b", "c"]                  # top 3 only
    assert ring.received_ms > 0


def test_latest_version_per_ring(r):
    r.xadd(bus.RINGS, ring_msg("R-1", 1))
    r.xadd(bus.RINGS, ring_msg("R-1", 3))
    r.xadd(bus.RINGS, ring_msg("R-1", 2))   # late, older version
    r.xadd(bus.RINGS, ring_msg("R-2", 1))
    snap = state.load(r)
    assert {x.ring_id: x.version for x in snap.rings} == {"R-1": 3, "R-2": 1}


def test_sort_rings(r):
    r.xadd(bus.RINGS, ring_msg("R-old", last_seen="2022-09-07T00:00:00", xgb_score="0.99"))
    r.xadd(bus.RINGS, ring_msg("R-new", last_seen="2022-09-09T00:00:00", xgb_score="0.5"))
    r.xadd(bus.RINGS, ring_msg("R-none", last_seen="2022-09-08T00:00:00", xgb_score=""))
    rings = state.load(r).rings
    assert [x.ring_id for x in rings] == ["R-new", "R-none", "R-old"]
    assert [x.ring_id for x in state.sort_rings(rings, "score")] == ["R-old", "R-new", "R-none"]


# --- cases --------------------------------------------------------------------

def test_case_ordering_granite_beats_template_at_same_version(r):
    r.xadd(bus.CASES, case_msg("R-1", 1, "template"))
    r.xadd(bus.CASES, case_msg("R-1", 1, "granite", check_note="passed fact check"))
    r.xadd(bus.CASES, case_msg("R-1", 1, "template"))   # a late template re-send
    assert state.load(r).cases["R-1"].source == "granite"
    assert state.latest_case(r, "R-1").source == "granite"


def test_case_ordering_newer_version_wins(r):
    r.xadd(bus.CASES, case_msg("R-1", 1, "granite"))
    r.xadd(bus.CASES, case_msg("R-1", 2, "template"))
    case = state.load(r).cases["R-1"]
    assert (case.version, case.source) == (2, "template")


def test_summary_parts_follow_the_data(r):
    parts = {"summary": "s", "what_happened": "w", "draft_report": "d", "empty": ""}
    r.xadd(bus.CASES, case_msg(summary=json.dumps(parts)))
    case = state.load(r).cases["R-1"]
    assert [(p.key, p.label) for p in case.parts] == [
        ("summary", "Summary"), ("what_happened", "What happened"), ("draft_report", "Draft report")]
    assert case.facts == {"key_account": "2_B"}


def test_plain_text_summary_and_missing_facts(r):
    r.xadd(bus.CASES, case_msg(summary="just text", facts=""))
    case = state.load(r).cases["R-1"]
    assert [(p.key, p.text) for p in case.parts] == [("summary", "just text")]
    assert case.facts is None


def test_granite_counts_from_cases(r):
    r.xadd(bus.CASES, case_msg("R-1", source="granite"))
    r.xadd(bus.CASES, case_msg("R-2", check_note="granite reply rejected: number 12"))
    r.xadd(bus.CASES, case_msg("R-3", check_note="granite error: timeout"))
    r.xadd(bus.CASES, case_msg("R-4", check_note="granite not used"))
    c = state.load(r).counters
    assert (c.granite_passed, c.granite_tried, c.granite_from_stats) == (1, 3, False)


def test_granite_counts_prefer_casewriter_stats(r):
    r.xadd(bus.CASES, case_msg("R-1", source="granite"))
    r.hset(state.CASEWRITER_STATS, mapping={"cases_written": 9, "granite_tried": 8, "granite_passed": 6})
    c = state.load(r).counters
    assert (c.granite_passed, c.granite_tried, c.granite_from_stats) == (6, 8, True)


def test_case_history_oldest_first(r):
    r.xadd(bus.CASES, case_msg("R-1", 1))
    r.xadd(bus.CASES, case_msg("R-2", 1))
    r.xadd(bus.CASES, case_msg("R-1", 2))
    assert [c.version for c in state.case_history(r, "R-1")] == [1, 2]


# --- scored -------------------------------------------------------------------

def test_scored_counters_and_latency(r):
    for i in range(100):
        r.xadd(bus.SCORED, scored_msg(i, flagged=1 if i % 10 == 0 else 0, latency_ms=i + 1))
    c = state.load(r).counters
    assert (c.scored_total, c.flagged, c.flagged_window, c.counts_exact) == (100, 10, 100, False)
    assert c.latency_samples == 100
    assert c.p50_ms == pytest.approx(50, abs=0.01)
    assert c.p99_ms == pytest.approx(99, abs=0.01)


def test_scored_window_is_bounded(r, monkeypatch):
    monkeypatch.setattr(state, "SCORED_WINDOW", 20)
    for i in range(50):
        r.xadd(bus.SCORED, scored_msg(i, flagged=1))
    c = state.load(r).counters
    assert (c.scored_total, c.flagged, c.flagged_window) == (50, 20, 20)


def test_scorer_stats_make_counts_exact(r):
    r.xadd(bus.SCORED, scored_msg(0, flagged=1))
    r.hset(state.SCORER_STATS, mapping={"scored": 5000, "flagged": 42})
    c = state.load(r).counters
    assert (c.scored_total, c.flagged, c.counts_exact) == (5000, 42, True)


def test_wall_seconds_formats():
    iso = state.wall_seconds("2026-10-17T10:00:00.250+00:00")
    assert state.wall_seconds("2026-10-17T10:00:00.250Z") == iso
    assert state.wall_seconds("2026-10-17T10:00:00.250") == iso       # no zone read as UTC
    assert state.wall_seconds(str(iso)) == iso                        # epoch seconds
    assert state.wall_seconds(str(int(iso * 1000))) == pytest.approx(iso)  # epoch ms
    assert state.wall_seconds("yesterday") is None and state.wall_seconds("") is None


# --- decisions ----------------------------------------------------------------

def test_write_decision_and_read_back(r):
    r.xadd(bus.RINGS, ring_msg("R-1", 2))
    r.xadd(bus.RINGS, ring_msg("R-2", 1))
    decisions.write(r, "R-1", 2, "more_info", "  check the collector  ")
    decisions.write(r, "R-1", 2, "Freeze", "confirmed")
    (_id, msg), = r.xrevrange(bus.DECISIONS, count=1)
    assert set(msg) == {"ring_id", "version", "decision", "note", "decided_at"}
    assert msg["decided_at"].endswith("+00:00")
    snap = state.load(r)
    d = snap.decisions["R-1"]
    assert (d.decision, d.version, d.note, d.label) == ("freeze", 2, "confirmed", "Frozen")
    assert (snap.counters.total_rings, snap.counters.open_rings) == (2, 1)


def test_decision_note_is_trimmed():
    msg = decisions.build("R-1", 1, "dismiss", "  ok  ")
    assert msg["note"] == "ok" and msg["version"] == "1"


@pytest.mark.parametrize("ring_id,decision,note", [
    ("", "freeze", ""),
    ("R-1", "delete", ""),
    ("R-1", "freeze", "x" * (decisions.MAX_NOTE + 1)),
])
def test_bad_decisions_are_refused(r, ring_id, decision, note):
    with pytest.raises(decisions.DecisionError):
        decisions.write(r, ring_id, 1, decision, note)
    assert not r.exists(bus.DECISIONS)


def test_decision_without_version_is_read(r):
    r.xadd(bus.DECISIONS, {"ring_id": "R-1", "decision": "dismiss", "note": "", "decided_at": "x"})
    assert state.load(r).decisions["R-1"].version is None


# --- labels and extension -----------------------------------------------------

def test_labels_fall_back_for_unknown_values():
    assert normalise_shape(" Gather scatter ") == "gather-scatter"
    assert normalise_shape("") == "other"
    assert source_label("granite") == "Granite, fact-checked"
    assert source_label("llama") == "llama"
    assert part_label("next_steps_for_bank") == "Next steps for bank"


def test_a_new_reader_plugs_in(r):
    def read_extra(_r, snap):
        snap.errors.append("extra ran")
    snap = state.load(r, readers=state.READERS + [read_extra])
    assert snap.errors == ["extra ran"]


def test_a_failing_reader_does_not_stop_the_others(r):
    r.xadd(bus.RINGS, ring_msg("R-1"))

    def boom(_r, _snap):
        raise RuntimeError("bad day")
    snap = state.load(r, readers=[boom] + state.READERS)
    assert [x.ring_id for x in snap.rings] == ["R-1"]
    assert snap.errors == ["boom failed: bad day"]


def test_state_is_json_ready(r):
    r.xadd(bus.RINGS, ring_msg())
    r.xadd(bus.CASES, case_msg())
    json.dumps(state.load(r).to_dict())
    assert isinstance(PageState().to_dict(), dict)
