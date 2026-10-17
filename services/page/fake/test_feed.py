"""Checks for the fake feed. Lives in fake/ so deleting the folder removes it too."""
import json
import random

import pytest

fakeredis = pytest.importorskip("fakeredis")

from common import bus
from services.page.fake import feed


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def test_scored_rows_match_the_handoff_fields(r):
    feed.push_scored(r, random.Random(1), 50)
    rows = [f for _id, f in r.xrange(bus.SCORED)]
    assert len(rows) == 50
    want = {"txn_id", "ts", "from_acct", "to_acct", "amount_usd", "amt_paid", "ccy_paid",
            "amt_recv", "ccy_recv", "fmt", "sent_at", "score", "flagged", "reasons",
            "clues", "scored_at"}
    for row in rows:
        assert set(row) == want
        assert row["txn_id"].startswith(feed.FAKE_TXN_PREFIX)
        assert row["flagged"] in ("0", "1")
        assert isinstance(json.loads(row["reasons"]), list)
        assert isinstance(json.loads(row["clues"]), dict)


def test_scored_numbering_continues(r):
    rng = random.Random(2)
    feed.push_scored(r, rng, 3)
    feed.push_scored(r, rng, 2)
    ids = [f["txn_id"] for _id, f in r.xrange(bus.SCORED)]
    assert ids == ["FAKE-%06d" % i for i in range(1, 6)]


def test_cases_and_decisions_use_fake_rings(r):
    rings = feed.load_rings(feed.RINGS_FILE)
    assert feed.push_cases(r, rings) == len(rings)
    assert feed.push_decisions(r, rings) == 2
    for _id, case in r.xrange(bus.CASES):
        assert case["ring_id"].startswith(feed.FAKE_RING_PREFIXES)
        assert case["source"] == "template"
        assert len(json.loads(case["summary"])) == 5
    for _id, d in r.xrange(bus.DECISIONS):
        assert d["decision"] in ("freeze", "dismiss", "more_info")
        assert d["note"].startswith(feed.FAKE_NOTE_PREFIX)


def test_fake_upgrade_is_labelled_fake(r):
    ring = feed.load_rings(feed.RINGS_FILE)[0]
    feed.push_fake_upgrade(r, ring)
    (_id, case), = r.xrange(bus.CASES)
    assert case["source"] == "granite"
    assert case["check_note"].startswith(feed.FAKE_NOTE_PREFIX)


def test_reset_refuses_on_real_data(r):
    r.xadd(bus.RINGS, {"ring_id": "R-4242", "version": "1"})
    assert feed.reset(r) is False
    assert r.xlen(bus.RINGS) == 1


def test_reset_clears_fake_data(r):
    rings = feed.load_rings(feed.RINGS_FILE)
    feed.push_cases(r, rings)
    feed.push_scored(r, random.Random(3), 5)
    assert feed.reset(r) is True
    assert not any(r.exists(s) for s in bus.STREAMS)
