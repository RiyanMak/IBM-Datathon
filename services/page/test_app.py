"""Renders the real Streamlit page against an in-memory Redis with Streamlit's own
test harness. Builds its own messages; never imports services.page.fake."""
import json
import os

import pytest

pytest.importorskip("streamlit")
fakeredis = pytest.importorskip("fakeredis")
from streamlit.testing.v1 import AppTest  # noqa: E402

from common import bus  # noqa: E402
from services.page import fmt  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")


@pytest.fixture
def r(monkeypatch):
    import streamlit as st
    st.cache_resource.clear()
    st.cache_data.clear()
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(bus, "connect", lambda **kw: client)
    return client


def add_ring(r, ring_id="R-1", version=1, reasons=("money left 40 minutes after it arrived",)):
    r.xadd(bus.RINGS, {
        "ring_id": ring_id, "version": str(version), "shape": "fan-out", "n_accounts": "2",
        "accounts": json.dumps(["1_A", "2_B"]),
        "edges": json.dumps([{"txn_id": "t1", "from": "1_A", "to": "2_B", "amount_usd": 9800,
                              "ts": "2022-09-08T10:00:00", "score": 0.9}]),
        "xgb_score": "0.9", "gnn_score": "", "reasons": json.dumps(list(reasons)),
        "first_seen": "2022-09-08T10:00:00", "last_seen": "2022-09-08T12:00:00",
    })


def add_case(r, ring_id="R-1", version=1, summary=None, source="template"):
    r.xadd(bus.CASES, {
        "ring_id": ring_id, "version": str(version),
        "summary": json.dumps(summary or {"summary": "s"}), "source": source,
        "check_note": "granite not used", "written_at": "2026-10-17T10:00:00",
        "facts": json.dumps({"key_account": "1_A"}),
    })


def run():
    return AppTest.from_file(APP, default_timeout=30).run()


def texts(at):
    return [m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info]


def test_empty_redis_renders(r):
    at = run()
    assert not at.exception
    assert any("No rings yet" in t for t in texts(at))


def test_redis_down_shows_banner(monkeypatch):
    import redis
    import streamlit as st
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    st.cache_resource.clear()
    st.cache_data.clear()
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2,
                       retry=Retry(NoBackoff(), 0), decode_responses=True)
    monkeypatch.setattr(bus, "connect", lambda **kw: dead)
    at = run()
    assert not at.exception
    assert any("Waiting for Redis" in w.value for w in at.warning)


def test_ring_without_case_says_summary_coming(r):
    add_ring(r)
    at = run()
    assert not at.exception
    assert at.subheader[0].value == "R-1 · Fan-out"
    assert any("Summary coming" in t for t in texts(at))
    gnn = [m for m in at.metric if m.label == "GNN score"][0]
    assert gnn.value == fmt.DASH


def test_dollar_amounts_are_escaped(r):
    add_ring(r, reasons=["amounts just over $1,000 and $2,000"])
    add_case(r, summary={"summary": "moved $29,350 in 13 transfers; received $10,000"})
    at = run()
    assert not at.exception
    shown = texts(at)
    assert any("\\$29,350" in t and "\\$10,000" in t for t in shown)
    assert any("\\$1,000" in t for t in shown)


def test_unknown_summary_part_is_shown(r):
    add_ring(r)
    add_case(r, summary={"summary": "s", "draft_report": "d"})
    at = run()
    assert any(t.startswith("**Draft report**") for t in texts(at))


def test_freeze_writes_a_decision(r):
    add_ring(r, "R-1", version=3)
    add_case(r, "R-1", version=3)
    at = run()
    at.text_area[0].input("passes money on within the hour")
    [b for b in at.button if b.label == "Freeze accounts"][0].click().run()
    assert not at.exception
    (_id, d), = r.xrange(bus.DECISIONS)
    assert (d["ring_id"], d["version"], d["decision"], d["note"]) == (
        "R-1", "3", "freeze", "passes money on within the hour")
    assert at.toast and "Frozen" in at.toast[0].value


def test_selecting_another_ring_switches_detail(r):
    add_ring(r, "R-1")
    add_ring(r, "R-2")
    at = run()
    first = at.subheader[0].value
    other = "R-1" if "R-2" in first else "R-2"
    at.radio[0].set_value(other).run()
    assert other in at.subheader[0].value
    assert any(other in m.value for m in at.markdown if m.value.startswith("**Your decision"))
