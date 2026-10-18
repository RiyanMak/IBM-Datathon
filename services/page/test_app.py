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


PAGES = {"cases": "views/cases.py", "investigate": "views/investigate.py",
         "evidence": "views/evidence.py", "decide": "views/decide.py"}


def run(page=None):
    """The overview opens first; a step page is reached the way a user reaches it."""
    at = AppTest.from_file(APP, default_timeout=30)
    if page:
        at.switch_page(PAGES[page])
    return at.run()


def case_title(at):
    return next((m.value for m in at.markdown if 'class="casebar"' in m.value), "")


def texts(at):
    return [m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info]


def test_empty_redis_renders(r):
    at = run()
    assert not at.exception
    assert any("Find the accounts" in m.value for m in at.markdown)
    at = run("cases")
    assert not at.exception
    assert any("No cases yet" in t for t in texts(at))


def test_overview_links_every_step(r):
    at = run()
    assert not at.exception
    assert any("How a transfer becomes a case" in m.value for m in at.markdown)


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
    assert any("Connection unavailable" in w.value for w in at.warning)
    at.switch_page("views/status.py").run()
    assert any(b.label == "Retry connection" for b in at.button)


def test_ring_without_case_says_summary_coming(r):
    add_ring(r)
    at = run("evidence")
    assert not at.exception
    assert "R-1 · Fan-out" in case_title(at)
    assert any("Summary coming" in t for t in texts(at))
    gnn = [m for m in at.metric if m.label == "GNN score"][0]
    assert gnn.value == fmt.DASH


def test_dollar_amounts_are_escaped(r):
    add_ring(r, reasons=["amounts just over $1,000 and $2,000"])
    add_case(r, summary={"summary": "moved $29,350 in 13 transfers; received $10,000"})
    at = run("evidence")
    assert not at.exception
    shown = texts(at)
    assert any("\\$29,350" in t and "\\$10,000" in t for t in shown)
    assert any("$1,000" in t for t in shown)


def test_unknown_summary_part_is_shown(r):
    add_ring(r)
    add_case(r, summary={"summary": "s", "draft_report": "d"})
    at = run("evidence")
    assert any(t.startswith("**Draft report**") for t in texts(at))


def test_freeze_writes_a_decision(r):
    add_ring(r, "R-1", version=3)
    add_case(r, "R-1", version=3)
    at = run("decide")
    at.text_area[0].input("passes money on within the hour")
    [b for b in at.button if b.label == "Record freeze decision"][0].click().run()
    assert not at.exception
    (_id, d), = r.xrange(bus.DECISIONS)
    assert (d["ring_id"], d["version"], d["decision"], d["note"]) == (
        "R-1", "3", "freeze", "passes money on within the hour")
    assert at.success and "Freeze recorded" in at.success[0].value


def test_selecting_another_ring_switches_detail(r):
    add_ring(r, "R-1")
    add_ring(r, "R-2")
    at = run("cases")
    first = case_title(at)
    other = "R-1" if "R-2" in first else "R-2"
    at.radio[0].set_value(other).run()
    assert other in case_title(at)
    at.switch_page("views/decide.py").run()
    assert any(other in m.value for m in at.markdown if m.value.startswith("**Your decision"))


def test_facts_panel_and_history(r):
    add_ring(r, "R-1", version=1)
    add_case(r, "R-1", version=1)
    add_case(r, "R-1", version=1, source="granite")
    at = run("evidence")
    assert not at.exception
    assert any(m.value.startswith("| Fact | Value |") and "1\\_A" in m.value for m in at.markdown)
    assert "Explanation history (2)" in [e.label for e in at.expander]


def test_upgrade_provenance_does_not_claim_a_check_without_evidence(r):
    import streamlit as st
    add_ring(r, "R-1")
    add_case(r, "R-1", source="template")
    at = run("evidence")
    add_case(r, "R-1", source="granite")
    st.cache_data.clear()
    at.run()
    assert not at.exception
    assert any("Granite explanation" in t for t in texts(at))
    assert any("passing fact check was not supplied" in t for t in texts(at))


def test_shape_filter(r):
    add_ring(r, "R-1")
    r.xadd(bus.RINGS, {"ring_id": "R-2", "version": "1", "shape": "cycle", "n_accounts": "0",
                       "accounts": "[]", "edges": "[]", "xgb_score": "0.5", "gnn_score": "",
                       "reasons": "[]", "first_seen": "", "last_seen": ""})
    at = run("cases")
    assert len(at.radio[0].options) == 2
    at.multiselect(key="shapes").set_value(["cycle"]).run()
    assert not at.exception
    assert len(at.radio[0].options) == 1 and "R-2" in at.radio[0].options[0]
    assert any("Showing 1 of 2 cases" in c.value for c in at.caption)
    at.slider(key="min_score").set_value(0.8).run()
    assert any("No cases match" in i.value for i in at.info)


def test_diagnostic_counts_move(r):
    import streamlit as st
    add_ring(r)
    for i in range(3):
        r.xadd(bus.SCORED, {"txn_id": str(i), "flagged": "0"})
    at = run()
    at.switch_page("views/status.py").run()
    assert not at.exception
    assert any("**Transfers scored:** 3" in t for t in texts(at))
    r.xadd(bus.SCORED, {"txn_id": "4", "flagged": "0"})
    st.cache_data.clear()
    at.run()
    assert any("**Transfers scored:** 4" in t for t in texts(at))


def test_selected_version_is_not_silently_upgraded_on_submit(r):
    import streamlit as st
    add_ring(r, version=1)
    at = run("decide")
    add_ring(r, version=2)
    st.cache_data.clear()
    at.text_area[0].input("Reviewed version one")
    [b for b in at.button if b.label == "Record freeze decision"][0].click().run()
    assert not at.exception
    assert r.xrange(bus.DECISIONS)[0][1]["version"] == "1"
    assert any("New case data" in i.value for i in at.info)


def test_failed_decision_retains_note(r, monkeypatch):
    from services.page import decisions
    add_ring(r)
    at = run("decide")
    def fail(*args, **kwargs):
        raise ConnectionError("unavailable")
    monkeypatch.setattr(decisions, "write", fail)
    at.text_area[0].input("Keep this investigation note")
    [b for b in at.button if b.label == "Record freeze decision"][0].click().run()
    assert not at.exception
    assert at.text_area[0].value == "Keep this investigation note"
    assert at.error and "could not be confirmed" in at.error[0].value
    assert r.xlen(bus.DECISIONS) == 0


def test_selection_survives_new_case_ranking(r):
    import streamlit as st
    add_ring(r, "R-1")
    at = run("cases")
    add_ring(r, "R-2")
    st.cache_data.clear()
    at.run()
    assert at.radio[0].value == "R-1"
    assert "R-1" in case_title(at)
    assert any("Update queue" in b.label for b in at.button)


def test_stale_summary_observations_are_not_presented_as_current(r):
    add_ring(r, version=2)
    add_case(r, version=1)
    at = run("evidence")
    assert not at.exception
    assert any("explanation describes version 1" in w.value for w in at.warning)
    assert any("observations for this version are not available" in i.value for i in at.info)


def test_identical_double_submission_is_not_duplicated(r):
    add_ring(r)
    at = run("decide")
    button = lambda: [b for b in at.button if b.label == "Record freeze decision"][0]
    button().click().run()
    button().click().run()
    assert not at.exception
    assert r.xlen(bus.DECISIONS) == 1


def test_investigate_marks_origin_and_final_recipient(r):
    add_ring(r)
    at = run("investigate")
    assert not at.exception
    assert "R-1 · Fan-out" in case_title(at)
    assert any("Connected accounts" == m.label for m in at.metric)
    assert any("Inspect an account or transfer" == box.label for box in at.selectbox)


def test_step_pages_without_a_selection_point_back_to_the_queue(r):
    at = run("decide")
    assert not at.exception
    assert any("Choose a case before recording" in m.value for m in at.markdown)
