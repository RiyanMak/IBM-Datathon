"""Step 01: the review queue. Order holds until the investigator applies an update."""
from html import escape

import streamlit as st

from services.page import fmt, state, ui, workspace as ws
from services.page.models import shape_label


@st.fragment(run_every=ui.REFRESH_S)
def queue():
    snap, ss = ui.current_snapshot(), st.session_state
    ui.html('<div class="status-row"><span class="chip">%s</span>%s</div>' % (escape(ui.mode_label(snap)), ui.status_chip(snap)))
    st.text_input("Find a case or account", key="search", placeholder="Case ID or account…")
    f2, f3 = st.columns(2, vertical_alignment="bottom")
    with f2.popover("Filter cases", width="stretch"):
        st.toggle("Awaiting a decision", key="open_only")
        shapes = sorted({r.shape for r in snap.rings} | set(ss.get("shapes", [])))
        st.multiselect("Flow shape", shapes, key="shapes", format_func=shape_label)
        st.slider("Minimum XGBoost score", 0.0, 1.0, step=.05, key="min_score",
                  help="A model score ranks cases; it is not a probability of wrongdoing.")
        st.button("Clear filters", on_click=ui.clear_filters, width="stretch")
    sort = f3.selectbox("Order", list(ui.SORTS), key="sort")
    available = list(snap.rings)
    pinned = ss.get("reviewed_ring")
    if pinned and pinned.ring_id == ui.selected_id() and snap.ring(pinned.ring_id) is None:
        available.append(pinned)
    rings = state.filter_rings(state.sort_rings(available, ui.SORTS[sort]), ss.get("shapes"),
                               ss.get("min_score", 0), ss.get("open_only", False), snap.decisions)
    query = ss.get("search", "").strip().casefold()
    rings = [r for r in rings if not query or query in r.ring_id.casefold()
             or any(query in a.casefold() for a in ws.accounts(r))]
    signature = (sort, tuple(ss.get("shapes", [])), ss.get("min_score"), ss.get("open_only"), query)
    if ss.get("queue_filter") != signature:
        ss["queue_filter"] = signature
        ss.pop("queue_order", None)
    ranked = [r.ring_id for r in rings]
    order, pending = ws.stable_queue(ss.get("queue_order"), ranked)
    if pending or order != ranked:
        if st.button("Update queue%s" % (" · %d new" % pending if pending else " order"), width="stretch"):
            order = ranked
    ss["queue_order"] = order
    if not ranked:
        st.info("No cases match your filters." if snap.rings else
                "No cases yet. Cases appear when the ring service supplies a flagged flow.")
        if ss.get("reviewed_ring"):
            st.caption("Your selected case stays open while you adjust the filters.")
        if snap.rings:
            st.button("Reset filters", on_click=ui.clear_filters)
        return
    if not order:
        st.info("New cases are available. Update the queue to review them.")
        return
    by_id = {r.ring_id: r for r in rings}
    st.caption("Showing %d of %d cases · order held while you review" % (len(order), len(available)))
    if ui.selected_id() not in order:
        ss["selected"], ss["case_choice"] = order[0], order[0]
        if ss.get("reviewed_ring") and ss["reviewed_ring"].ring_id != order[0]:
            st.rerun(scope="app")
    captions = []
    for rid in order:
        ring, dec = by_id[rid], snap.decisions.get(rid)
        captions.append(fmt.md("Score %s · %s" % (fmt.score(ring.xgb_score), dec.label if dec else "Requires review"))
                        + "  \n" + fmt.md(ring.reasons[0] if ring.reasons else "Flag reason not supplied"))
    with st.container(key="queue"):
        choice = st.radio("Select a case", order, key="case_choice",
                          label_visibility="collapsed", captions=captions, width="stretch",
                          format_func=lambda rid: fmt.md("%s · %s" % (rid, by_id[rid].shape_label)))
    if choice != ui.selected_id():
        ss["selected"] = choice
        st.rerun(scope="app")


def preview():
    snap = ui.current_snapshot()
    ring = snap.ring(ui.selected_id()) or st.session_state.get("reviewed_ring")
    if not ring:
        ui.html('<div class="empty"><strong>Nothing selected yet.</strong><p>Pick a case from the queue to preview it here.</p></div>')
        return
    ui.case_bar(ring, snap)
    st.markdown(fmt.md(ws.flow_description(ring)))
    st.markdown("**Why it was flagged**")
    for reason in ring.reasons[:3]:
        ui.html('<div class="reason">%s</div>' % escape(reason))
    if not ring.reasons:
        st.info("No flag reasons were supplied.")
    if st.button("Investigate this case", type="primary", icon=":material/arrow_forward:", width="stretch"):
        st.switch_page(ui.PAGES["investigate"][0])


ui.step_rail("cases")
ui.section("Choose a case", "01")
st.markdown("## Review queue")
st.caption("Cases are ranked by model score. The order holds while you review; new arrivals wait for **Update queue**.")
with st.container(key="case-workspace"):
    left, right = st.columns([1, 1.45], gap="medium")
    with left:
        with st.container(key="queue-panel"):
            ui.section("Case queue")
            queue()
    with right:
        with st.container(key="case-preview"):
            ui.section("Selected case")
            preview()
