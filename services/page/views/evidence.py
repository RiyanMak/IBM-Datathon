"""Step 03: reasons, scores, computed observations and the labelled explanation."""
import time
from html import escape

import streamlit as st

from services.page import fmt, granite_requests, state, ui, workspace as ws

WAIT_NOTE_S = 90   # how long after a request the page keeps saying Granite is writing


def reasons_and_scores(ring, case):
    st.markdown("**Why this case was flagged**")
    st.caption("Reasons supplied with the case. These are model judgments, not proof of wrongdoing.")
    for reason in ring.reasons:
        ui.html('<div class="reason">%s</div>' % escape(reason))
    if not ring.reasons:
        st.info("No flag reasons were supplied.")
    a, b = st.columns(2)
    a.metric("XGBoost score", fmt.score(ring.xgb_score))
    b.metric("GNN score", fmt.score(ring.gnn_score))
    st.caption("Scores rank suspicion on a 0–1 scale, not calibrated probabilities. A dash means unavailable.")
    with st.expander("Computed observations"):
        if case and case.version == ring.version and case.facts:
            st.markdown("| Fact | Value |\n|---|---|\n" + "\n".join(
                "| %s | %s |" % (fmt.md(label), fmt.md(value)) for label, value in fmt.fact_rows(case.facts)))
            st.caption("Computed from this case. Account roles are inferred; USD amounts are estimates.")
        else:
            st.info("Computed observations for this version are not available yet.")


def granite_status(case, asked_at):
    """What to say while a template case is on screen: Granite writing, or why not."""
    waiting = asked_at is not None and time.time() - asked_at < WAIT_NOTE_S
    if granite_requests.granite_expected(case) or (waiting and case.check_note.strip().lower() == "granite pending"):
        if asked_at is not None:
            return "Granite is writing an explanation · requested %d s ago. The computer's observations are shown meanwhile." % max(0, int(time.time() - asked_at))
        return "Granite is writing an explanation. The computer's observations are shown meanwhile."
    return None


def observations(case):
    """The facts the case writer computed, as a table: what Granite is given, and all a
    reader has while Granite writes."""
    rows = fmt.fact_rows(case.facts) if case.facts else []
    if not rows:
        st.info("Computed observations for this version are not available yet.")
        return
    st.markdown("| Observation | Value |\n|---|---|\n" + "\n".join(
        "| %s | %s |" % (fmt.md(label), fmt.md(value)) for label, value in rows))
    st.caption("Computed by our code from this case. Account roles are inferred; USD amounts are estimates. "
               "The fact check rejects any Granite text that uses a number not in this set.")


def explanation(ring, case, asked_at=None):
    st.markdown("**Case explanation**")
    if not case:
        if asked_at is not None:
            st.info("Summary coming. Granite was asked %d s ago; review the transfers while the case writer prepares an explanation." % max(0, int(time.time() - asked_at)))
        else:
            st.info("Summary coming. Review the transfers while the case writer prepares an explanation.")
        return
    status = granite_status(case, asked_at) if not case.is_granite else None
    if status:
        st.info(status)
    st.caption(fmt.md(ws.provenance(case)))
    seen_key = "summary-seen-" + ring.ring_id
    before = st.session_state.get(seen_key)
    now = (case.version, case.source)
    if before is not None and before != now and case.is_granite:
        st.session_state["summary-updated-" + ring.ring_id] = time.time()
    st.session_state[seen_key] = now
    if time.time() - st.session_state.get("summary-updated-" + ring.ring_id, 0) < 15:
        st.caption(fmt.md("Explanation updated · " + ws.provenance(case)))
    if case.version != ring.version:
        st.warning("This explanation describes version %d. You are reviewing version %d. Use the current transfers until a matching explanation arrives." % (case.version, ring.version))
    st.caption(fmt.md("Written %s · %s" % (fmt.wall_time(case.written_at), case.check_note or "No check result supplied")))
    if case.fact_checked:
        st.caption("The check validates numbers, account references, and dates against supplied facts. It does not verify wrongdoing or every inference.")
    elif case.is_granite and not case.check_note.upper().startswith("FAKE"):
        st.caption("A passing fact check was not supplied for this explanation.")
    if not case.is_granite and case.version == ring.version:
        st.markdown("**Computer observations**")
        observations(case)
        st.markdown("**Template explanation** · written from the observations above")
    for part in case.parts:
        label = "Account hypothesis" if part.key == "possible_victim" else part.label
        st.markdown("**%s**  \n%s" % (fmt.md(label), fmt.md(part.text)))
    if not case.parts:
        st.info("The case writer supplied an empty explanation.")


def explanation_history(ring):
    if not ui.current_snapshot().redis_ok:
        st.caption("Reconnect to retrieve the explanation history.")
        return
    try:
        past_cases = state.case_history(ui.redis_client(), ring.ring_id)
    except Exception:
        st.caption("Explanation history is unavailable. The next refresh will retry.")
        return
    with st.expander("Explanation history (%d)" % len(past_cases)):
        for case in reversed(past_cases):
            st.caption(fmt.md("v%d · %s · %s" % (case.version, ws.provenance(case), fmt.wall_time(case.written_at))))
        st.caption("History window: latest %d explanations across all cases." % (state.CASES_WINDOW * 4))


@st.fragment(run_every=ui.REFRESH_S)
def evidence():
    snap = ui.current_snapshot()
    ui.ensure_selection(snap)
    ring, incoming, outside = ui.resolve_ring(snap)
    if not ring:
        ui.no_case("Choose a case to see why it was flagged.")
        return
    if incoming != ring:
        st.info("New case data is available. Apply it from the Investigate step; this evidence describes version %d." % ring.version)
    ui.case_bar(ring, snap)
    left, right = st.columns([1, 1.35], gap="large")
    case = ui.case_for(snap, ring)
    asked_at = ui.ask_granite(snap, ring, case)
    with left:
        reasons_and_scores(ring, case)
    with right:
        explanation(ring, case, asked_at)
        explanation_history(ring)


ui.step_rail("evidence")
ui.section("Weigh the evidence", "03")
st.markdown("## Evidence and explanation")
st.caption("Observations, model judgments, generated explanations and human decisions are labelled separately.")
evidence()
ui.step_links(previous="investigate", next_step="decide")
