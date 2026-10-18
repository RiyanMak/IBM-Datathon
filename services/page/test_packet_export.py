"""Tests for the case packet download (packet_export.py). Uses fakeredis; cases are made by
the real case writer (template only) from its fake rings, the way the live service makes them."""
import json
from pathlib import Path

import pytest

fakeredis = pytest.importorskip("fakeredis")

from common import bus
from services.casewriter.casewriter import ring_to_stream_fields, write_case
from services.notifier import packet
from services.page import decisions, packet_export

FAKE_RINGS = Path(__file__).resolve().parents[1] / "casewriter" / "fake_rings.json"
PART_LABELS = ("Summary", "What happened", "Why it is suspicious", "Possible victim", "Suggested next step")


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def fake_ring(i=0):
    with open(FAKE_RINGS) as fh:
        return ring_to_stream_fields(json.load(fh)[i])


def stocked(r):
    """One ring, its template case and two decisions, as the stack would leave them."""
    ring = fake_ring()
    r.xadd(bus.RINGS, ring)
    case = write_case(ring, use_granite=False)
    r.xadd(bus.CASES, {k: str(v) for k, v in case.items()})
    decisions.write(r, ring["ring_id"], ring["version"], "more_info", "check the collector")
    decisions.write(r, ring["ring_id"], ring["version"], "freeze", "passes money on within the hour")
    return ring, case


def test_fetch_returns_the_case_ring_and_decisions_in_time_order(r):
    ring, case = stocked(r)
    r.xadd(bus.CASES, {"ring_id": "R-OTHER", "version": "1", "summary": "{}"})
    got_case, got_ring, got_decisions = packet_export.fetch(r, ring["ring_id"], int(ring["version"]))
    assert got_case["ring_id"] == ring["ring_id"] and got_case["summary"] == case["summary"]
    assert got_ring == ring
    assert [d["decision"] for d in got_decisions] == ["more_info", "freeze"]


def test_fetch_prefers_the_reviewed_version_and_granite_at_equal_version(r):
    ring = fake_ring()
    rid = ring["ring_id"]
    r.xadd(bus.RINGS, dict(ring, version="1"))
    r.xadd(bus.RINGS, dict(ring, version="2"))
    r.xadd(bus.CASES, {"ring_id": rid, "version": "1", "source": "template", "summary": "{}"})
    r.xadd(bus.CASES, {"ring_id": rid, "version": "1", "source": "granite", "summary": "{}"})
    r.xadd(bus.CASES, {"ring_id": rid, "version": "1", "source": "template", "summary": "{}"})
    r.xadd(bus.CASES, {"ring_id": rid, "version": "2", "source": "template", "summary": "{}"})
    case, ring_f, _ = packet_export.fetch(r, rid, 1)
    assert (case["version"], case["source"], ring_f["version"]) == ("1", "granite", "1")
    case, ring_f, _ = packet_export.fetch(r, rid)
    assert (case["version"], ring_f["version"]) == ("2", "2")
    case, _, _ = packet_export.fetch(r, rid, 7)   # no case for v7 yet: the newest is used
    assert case["version"] == "2"


def test_build_gives_the_filename_and_a_full_packet(r):
    ring, _ = stocked(r)
    name, markdown = packet_export.build(r, ring["ring_id"], ring["version"])
    assert name == "case_%s_v%s.md" % (ring["ring_id"], ring["version"])
    assert ring["ring_id"] in markdown
    for label in PART_LABELS:
        assert "### " + label in markdown
    for edge in json.loads(ring["edges"]):
        assert edge["txn_id"] in markdown
    assert "Frozen" in markdown and "check the collector" in markdown
    assert packet.NO_TRANSFERS not in markdown


def test_no_case_gives_none(r):
    r.xadd(bus.RINGS, fake_ring())
    assert packet_export.build(r, fake_ring()["ring_id"]) is None
    assert packet_export.fetch(r, "R-NONE") == (None, None, [])
    assert packet_export.build(r, "") is None


def test_malformed_fields_never_raise(r):
    rid = fake_ring()["ring_id"]
    r.xadd(bus.RINGS, {"ring_id": rid, "version": "x", "edges": "[not json", "accounts": "{"})
    r.xadd(bus.CASES, {"ring_id": rid, "version": "1", "summary": "{broken", "facts": "[1,"})
    r.xadd(bus.DECISIONS, {"ring_id": rid, "decision": "freeze", "version": "abc"})
    name, markdown = packet_export.build(r, rid, "not a number")
    assert name == "case_%s_v1.md" % rid
    assert "(not available)" in markdown and "Frozen" in markdown


def test_redis_down_gives_none():
    import redis

    class Down:
        def __getattr__(self, name):
            raise redis.exceptions.ConnectionError("down")

    assert packet_export.build(Down(), "R-0001") is None
