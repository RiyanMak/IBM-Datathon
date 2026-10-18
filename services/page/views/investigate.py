"""Step 02: the money-flow graph, inspector and transfer list for the selected case."""
from html import escape

import streamlit as st

from services.page import fmt, ui, workspace as ws


def inspector(ring, selection):
    edge = ws.selected_edge(ring, selection)
    if edge:
        ui.html('<div class="inspector"><strong>%s</strong><p>%s</p></div>' % (
            escape("Transfer " + (edge.txn_id or "without an identifier")), escape(edge.src + " → " + edge.dst)))
        st.code(edge.src + " → " + edge.dst, language=None)
        a, b = st.columns(2)
        a.metric("Estimated USD", ws.volume([edge]))
        b.metric("Transfer score", fmt.score(edge.score))
        st.caption(fmt.md("Dataset time: %s · Paid: %s · Received: %s" % (
            fmt.data_time(edge.ts), ws.native_amount(edge.amt_paid, edge.ccy_paid),
            ws.native_amount(edge.amt_recv, edge.ccy_recv))))
        if edge.src == edge.dst:
            st.info("This is a self-transfer. It does not establish a multi-account ring.")
        return
    if selection.startswith("account:"):
        account = selection[len("account:"):]
        st.markdown("**Selected account**")
        st.code(account, language=None)
        incoming = [e for e in ring.edges if e.dst == account]
        outgoing = [e for e in ring.edges if e.src == account]
        a, b = st.columns(2)
        a.metric("Incoming transfer volume", ws.volume(incoming))
        b.metric("Outgoing transfer volume", ws.volume(outgoing))
        st.caption("%d incoming · %d outgoing transfers within this case. Self-transfers appear in both counts." % (len(incoming), len(outgoing)))
        return
    st.caption("Select a graph account, arrow, or inspection-list item to connect the flow to its evidence.")


def transfer_list(ring, selection):
    rows = ws.related_transfers(ring, selection)
    if not rows:
        st.info("No transfers in this selection.")
        return
    ui.section("Transfers / dataset time")
    page_count = (len(rows) + ws.PAGE_SIZE - 1) // ws.PAGE_SIZE
    key = "page-" + ring.ring_id + "-" + selection
    page = st.selectbox("Transfer page", range(page_count), format_func=lambda n: "%d of %d" % (n + 1, page_count), key=key) if page_count > 1 else 0
    shown = rows[page * ws.PAGE_SIZE:(page + 1) * ws.PAGE_SIZE]
    st.caption("Showing %d–%d of %d transfers. USD values are estimates; volume may count the same funds at multiple hops." %
               (page * ws.PAGE_SIZE + 1, page * ws.PAGE_SIZE + len(shown), len(rows)))
    st.markdown("| Transfer | Dataset time | Sender → recipient | Est. USD |\n|---|---|---|---:|\n" + "\n".join(
        "| %s | %s | %s | %s |" % (fmt.md(e.txn_id or "Unavailable"), fmt.md(fmt.data_time(e.ts)),
                                   fmt.md(e.src + " → " + e.dst), fmt.md(ws.volume([e]))) for _, e in shown))


@st.fragment(run_every=ui.REFRESH_S)
def investigation():
    snap, ss = ui.current_snapshot(), st.session_state
    ui.ensure_selection(snap)
    ring, incoming, outside = ui.resolve_ring(snap)
    if not ring:
        ui.no_case()
        return
    if outside:
        st.warning("This selected case is outside the current stream window. Showing the last reviewed version.")
    if incoming != ring:
        st.info("New case data is available. The flow stays on version %d until you apply the update." % ring.version)
        if st.button("Review updated case", key="apply-case-update"):
            ss["reviewed_ring"] = incoming
            st.rerun(scope="app")
    ui.case_bar(ring, snap)
    # Granite starts writing while the investigator follows the money, so the
    # explanation is usually ready by the time they reach the evidence step.
    ui.ask_granite(snap, ring, ui.case_for(snap, ring))
    a, b, c = st.columns(3)
    a.metric("Connected accounts", len(ws.accounts(ring)))
    b.metric("Transfers in this case", len(ring.edges))
    c.metric("Est. transfer volume · USD", ws.volume(ring.edges), help="Sum of supplied USD estimates. Funds may be counted again at each hop; this is not unique money or loss.")
    st.markdown(fmt.md(ws.flow_description(ring)))
    select_key = "inspect-" + ring.ring_id
    choices = ws.options(ring)
    if ss.get(select_key, "overview") not in choices:
        ss[select_key] = "overview"
    selections = ss.setdefault("inspections", {})
    if select_key not in ss and selections.get(ring.ring_id) in choices:
        ss[select_key] = selections[ring.ring_id]
    selection = ss.get(select_key, "overview")
    positions_key = "positions-" + ring.ring_id
    data = ws.graph_data(ring, selection, ss.get(positions_key))
    ss[positions_key] = data["positions"]
    event = ui.money_flow(graph=data, theme=ui.theme(), key="flow-" + ring.ring_id, default=None)
    event_key = "flow-event-" + ring.ring_id
    if isinstance(event, dict) and event.get("nonce") != ss.get(event_key):
        ss[event_key] = event.get("nonce")
        if event.get("key") in choices:
            ss[select_key] = event["key"]
            selections[ring.ring_id] = event["key"]
    selection = st.selectbox("Inspect an account or transfer", choices, key=select_key,
                             format_func=lambda key: ws.selection_label(ring, key),
                             help="Keyboard alternative to the graph. Account selection filters transfers.")
    selections[ring.ring_id] = selection
    if data["limited"]:
        st.caption(fmt.md(data["scope"] + ". Largest transfers and your selection are prioritized; all transfers remain in the inspection list."))
    inspector(ring, selection)
    transfer_list(ring, selection)


ui.step_rail("investigate")
ui.section("Follow the money", "02")
st.markdown("## Money flow")
st.caption("The origin is the first sender; the final recipient never sends on. Both roles are inferred from this case, not dataset facts.")
investigation()
ui.step_links(previous="cases", next_step="evidence")
