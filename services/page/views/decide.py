"""Step 04: the decision form (outside any polling fragment) and the audit history."""
import uuid

import streamlit as st

from services.page import decisions, fmt, packet_export, state, ui
from services.page.models import decision_label


def decision_form(ring):
    rid = ring.ring_id
    request_key = "request-" + rid
    st.session_state.setdefault(request_key, str(uuid.uuid4()))
    with st.form("decide-" + rid, clear_on_submit=False):
        st.markdown("**Your decision on %s · version %d**" % (fmt.md(rid), ring.version))
        st.caption("This records an instruction in the audit log. It does not freeze bank accounts.")
        note = st.text_area("Investigation note", key="note-" + rid, max_chars=decisions.MAX_NOTE,
                            placeholder="What did you observe, and why did you choose this action?", height=100)
        a, b, c = st.columns(3)
        pressed = None
        if a.form_submit_button("Record freeze decision", type="primary", width="stretch"):
            pressed = "freeze"
        if b.form_submit_button("Request more information", width="stretch"):
            pressed = "more_info"
        if c.form_submit_button("Dismiss case", width="stretch"):
            pressed = "dismiss"
    if not pressed:
        return
    signature = (ring.version, pressed, note)
    if st.session_state.get("saved-" + rid) == signature:
        st.info("This decision has already been recorded. Edit the note or choose another action to record a new decision.")
        return
    try:
        with st.spinner("Recording decision…"):
            decisions.write(ui.redis_client(), rid, ring.version, pressed, note,
                            request_id=st.session_state[request_key])
    except decisions.DecisionError as exc:
        st.error(fmt.md(str(exc)))
        return
    except Exception:
        st.error("The decision could not be confirmed. Your note is kept; retry when the connection is available.")
        return
    st.session_state["saved-" + rid] = signature
    st.session_state[request_key] = str(uuid.uuid4())
    st.session_state["flash"] = "%s for %s · version %d. Recorded in the audit log." % (decision_label(pressed), rid, ring.version)
    ui.snapshot.clear()
    st.rerun()


PACKET_CAPTION = ("A Markdown draft of the case for filing: summary, facts, accounts, transfers with "
                  "transaction IDs, decision log. Every number traces to clean.csv.")
PACKET_PENDING = "The case packet is available once the case writer has published an explanation."


@st.fragment(run_every=ui.REFRESH_S)
def case_packet(ring):
    """The download for the reviewed case. Refreshes with the history, so a Granite
    upgrade or a new decision is in the file without leaving the page."""
    st.markdown("**Case packet**")
    found = None
    if ui.current_snapshot().redis_ok:
        try:
            found = packet_export.build(ui.redis_client(), ring.ring_id, ring.version)
        except Exception:
            found = None
    if found:
        name, markdown = found
        st.download_button("Download case packet (.md)", data=markdown, file_name=name, mime="text/markdown",
                           type="secondary", width="stretch", on_click="ignore", key="packet-" + ring.ring_id)
        st.caption(PACKET_CAPTION)
    else:
        st.button("Download case packet (.md)", disabled=True, width="stretch", key="packet-off-" + ring.ring_id)
        st.caption(PACKET_PENDING)


@st.fragment(run_every=ui.REFRESH_S)
def history(ring):
    st.markdown("**Recorded decisions**")
    if not ui.current_snapshot().redis_ok:
        st.info("Reconnect to retrieve the audit history.")
        return
    try:
        past = state.decision_history(ui.redis_client(), ring.ring_id)
    except Exception:
        st.warning("History is unavailable. The next refresh will retry.")
        return
    if not past:
        st.caption("No decision recorded in the current history window.")
    for d in past:
        st.markdown("**%s · v%s**  \n%s" % (fmt.md(d.label), fmt.md(d.version), fmt.md(d.note or "No note")))
        st.caption(fmt.md(fmt.wall_time(d.decided_at)))
    st.caption("History window: latest %d decisions across all cases." % state.DECISIONS_WINDOW)


ui.step_rail("decide")
ui.section("Record your decision", "04")
st.markdown("## Decision")
st.caption("Decisions are audit records for the reviewed version. They are confirmed only once the service acknowledges them.")
snapshot_now = ui.current_snapshot()
ui.ensure_selection(snapshot_now)
reviewed, incoming_ring, _outside = ui.resolve_ring(snapshot_now)
if not reviewed:
    ui.no_case("Choose a case before recording a decision.")
else:
    if incoming_ring != reviewed:
        st.info("New case data is available. Apply it from the Investigate step; this decision records version %d." % reviewed.version)
    ui.case_bar(reviewed, snapshot_now)
    form_col, history_col = st.columns([1.5, 1], gap="large")
    with form_col:
        decision_form(reviewed)
    with history_col:
        case_packet(reviewed)
        history(reviewed)
ui.step_links(previous="evidence")
