"""Streamlit investigation workspace. Decisions stay outside polling fragments."""
import sys
import time
import uuid
from dataclasses import replace
from html import escape
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402
from streamlit.components.v1 import declare_component  # noqa: E402
from redis.backoff import NoBackoff  # noqa: E402
from redis.retry import Retry  # noqa: E402
from common import bus  # noqa: E402
from services.page import decisions, fmt, state, workspace as ws  # noqa: E402
from services.page.models import decision_label, shape_label  # noqa: E402

REFRESH_S = 2
SORTS = {"Highest score": "score", "Newest data": "newest"}
HERE = Path(__file__).resolve().parent
st.set_page_config(page_title="Mule Ring Hunter · Investigate", page_icon="◎", layout="wide")
money_flow = declare_component("money_flow", path=str(HERE / "flow_component"))


@st.cache_resource
def redis_client():
    return bus.connect(socket_connect_timeout=1, socket_timeout=2, retry=Retry(NoBackoff(), 0))


@st.cache_data(ttl=1, show_spinner=False)
def snapshot():
    return state.load(redis_client())


def current_snapshot():
    snap = snapshot()
    if snap.redis_ok and not snap.errors:
        st.session_state["last_good_snapshot"] = snap
    elif not snap.redis_ok and "last_good_snapshot" in st.session_state:
        return replace(st.session_state["last_good_snapshot"], redis_ok=False, errors=snap.errors)
    return snap


def theme():
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except AttributeError:
        return "light"


def html(text):
    st.markdown(text, unsafe_allow_html=True)


def section(text):
    html('<div class="section">%s</div>' % escape(text))


def selected_id():
    return st.session_state.get("selected")


def clear_filters():
    for key, value in {"open_only": False, "shapes": [], "min_score": 0.0, "search": ""}.items():
        st.session_state[key] = value
    st.session_state.pop("queue_order", None)


@st.fragment(run_every=REFRESH_S)
def orientation():
    snap = current_snapshot()
    mode = "Fixture simulation" if snap.feed_mode == "fixture" else "Synthetic-data simulation"
    status = "Connected · checking every 2 s" if snap.redis_ok else "Disconnected · last available data"
    html('<div class="status"><span class="chip">%s</span><span>%s</span></div>' %
         (escape(mode), escape(status)))
    if not snap.redis_ok:
        st.warning("Connection unavailable. Your notes are kept. Decisions require a connection.")
        if st.button("Retry connection"):
            snapshot.clear()
            st.rerun(scope="app")
    for error in snap.errors if snap.redis_ok else []:
        st.warning(fmt.md("Some data could not be loaded: " + error))
    c = snap.counters
    known = snap.redis_ok or "last_good_snapshot" in st.session_state
    values = [(str(c.open_rings), "cases awaiting a decision"),
              (str(c.total_rings), "cases in the current window"),
              (fmt.count(c.flagged), ("flagged / %s total scored" if c.counts_exact else "flagged in latest %s scored") % fmt.count(c.flagged_window))]
    html('<div class="summary-strip">' + ''.join(
        '<div><strong>%s</strong><span>%s</span></div>' % (escape(v if known else fmt.DASH), escape(label))
        for v, label in values) + '</div>')
    with st.expander("Data status & diagnostics"):
        st.caption("Synthetic transactions only. Dataset time describes historical transfers. "
                   "Connection checks describe this session; replay progress is not supplied.")
        if snap.loaded_ms and known:
            st.caption("Last successful read: %s UTC" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.gmtime(snap.loaded_ms / 1000)))
        if snap.last_event_ms:
            age = max(0, int(time.time() - snap.last_event_ms / 1000))
            st.caption("Most recent scored/ring message arrived %s seconds ago. An idle feed may be complete or paused." % fmt.count(age))
        st.caption("Queue: latest versions in the last %d ring messages. Summary window: %d messages." %
                   (state.RINGS_WINDOW, state.CASES_WINDOW))
        for tile in fmt.top_bar(c):
            st.markdown("**%s:** %s" % (fmt.md(tile.label), fmt.md(tile.value if known else fmt.DASH)))
            st.caption(fmt.md(tile.help))
        if snap.skipped:
            st.warning(fmt.md("Malformed messages skipped: " + str(snap.skipped)))


@st.fragment(run_every=REFRESH_S)
def case_queue():
    snap, ss = current_snapshot(), st.session_state
    section("01 / Choose a case")
    st.markdown("**Review queue**")
    st.text_input("Find a case or account", key="search", placeholder="Case ID or account…")
    with st.popover("Filter cases", width="stretch"):
        st.toggle("Awaiting a decision", key="open_only")
        shapes = sorted({r.shape for r in snap.rings} | set(ss.get("shapes", [])))
        st.multiselect("Flow shape", shapes, key="shapes", format_func=shape_label)
        st.slider("Minimum XGBoost score", 0.0, 1.0, step=.05, key="min_score",
                  help="A model score ranks cases; it is not a probability of wrongdoing.")
        st.button("Clear filters", on_click=clear_filters, width="stretch")
    sort = st.selectbox("Order", list(SORTS), key="sort")
    available = list(snap.rings)
    pinned = ss.get("reviewed_ring")
    if pinned and pinned.ring_id == selected_id() and snap.ring(pinned.ring_id) is None:
        available.append(pinned)
    rings = state.filter_rings(state.sort_rings(available, SORTS[sort]), ss.get("shapes"),
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
            st.button("Reset filters", on_click=clear_filters)
        return
    if not order:
        st.info("New cases are available. Update the queue to review them.")
        return
    by_id = {r.ring_id: r for r in rings}
    st.caption("Showing %d of %d cases · order held while you review" % (len(order), len(available)))
    if selected_id() not in order:
        ss["selected"], ss["case_choice"] = order[0], order[0]
        if ss.get("reviewed_ring") and ss["reviewed_ring"].ring_id != order[0]:
            st.rerun(scope="app")
    captions = []
    for rid in order:
        ring, dec = by_id[rid], snap.decisions.get(rid)
        captions.append(fmt.md("Score %s · %s\n%s" % (
            fmt.score(ring.xgb_score), dec.label if dec else "Requires review",
            ring.reasons[0] if ring.reasons else "Flag reason not supplied")))
    choice = st.radio("Select a case", order, key="case_choice",
                      label_visibility="collapsed", captions=captions,
                      format_func=lambda rid: fmt.md("%s · %s" % (rid, by_id[rid].shape_label)))
    if choice != selected_id():
        ss["selected"] = choice
        st.rerun(scope="app")


def inspector(ring, selection):
    edge = ws.selected_edge(ring, selection)
    if edge:
        html('<div class="inspector"><strong>%s</strong><p>%s</p></div>' % (
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
    elif selection.startswith("account:"):
        account = selection[len("account:"):]
        st.markdown("**Selected account**")
        st.code(account, language=None)
        incoming = [e for e in ring.edges if e.dst == account]
        outgoing = [e for e in ring.edges if e.src == account]
        a, b = st.columns(2)
        a.metric("Incoming transfer volume", ws.volume(incoming))
        b.metric("Outgoing transfer volume", ws.volume(outgoing))
        st.caption("%d incoming · %d outgoing transfers within this case. Self-transfers appear in both counts." % (len(incoming), len(outgoing)))
    else:
        st.caption("Select a graph account, arrow, or inspection-list item to connect the flow to its evidence.")


def transfer_list(ring, selection):
    rows = ws.related_transfers(ring, selection)
    if not rows:
        st.info("No transfers in this selection.")
        return
    section("Transfers / dataset time")
    page_count = (len(rows) + ws.PAGE_SIZE - 1) // ws.PAGE_SIZE
    key = "page-" + ring.ring_id + "-" + selection
    page = st.selectbox("Transfer page", range(page_count), format_func=lambda n: "%d of %d" % (n + 1, page_count), key=key) if page_count > 1 else 0
    shown = rows[page * ws.PAGE_SIZE:(page + 1) * ws.PAGE_SIZE]
    st.caption("Showing %d–%d of %d transfers. USD values are estimates; volume may count the same funds at multiple hops." %
               (page * ws.PAGE_SIZE + 1, page * ws.PAGE_SIZE + len(shown), len(rows)))
    st.markdown("| Transfer | Dataset time | Sender → recipient | Est. USD |\n|---|---|---|---:|\n" + "\n".join(
        "| %s | %s | %s | %s |" % (fmt.md(e.txn_id or "Unavailable"), fmt.md(fmt.data_time(e.ts)),
                                   fmt.md(e.src + " → " + e.dst), fmt.md(ws.volume([e]))) for _, e in shown))


def evidence(ring, case):
    section("03 / Weigh the evidence")
    left, right = st.columns([1, 1.35], gap="large")
    with left:
        st.markdown("**Why this case was flagged**")
        st.caption("Reasons supplied with the case. These are model judgments, not proof of wrongdoing.")
        for reason in ring.reasons:
            html('<div class="reason">%s</div>' % escape(reason))
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
    with right:
        st.markdown("**Case explanation**")
        if not case:
            st.info("Summary coming. Review the transfers while the case writer prepares an explanation.")
            return
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
        for part in case.parts:
            label = "Account hypothesis" if part.key == "possible_victim" else part.label
            st.markdown("**%s**  \n%s" % (fmt.md(label), fmt.md(part.text)))
        if not case.parts:
            st.info("The case writer supplied an empty explanation.")


@st.fragment(run_every=REFRESH_S)
def investigation():
    snap, ss = current_snapshot(), st.session_state
    incoming = snap.ring(selected_id())
    if incoming is None and ss.get("reviewed_ring") and ss["reviewed_ring"].ring_id == selected_id():
        incoming = ss["reviewed_ring"]
        st.warning("This selected case is outside the current stream window. Showing the last reviewed version.")
    if not incoming:
        html('<div class="empty"><strong>Follow the money, one case at a time.</strong><p>Choose a case to inspect its transfers and evidence.</p></div>')
        return
    if ss.get("reviewed_ring") is None or ss["reviewed_ring"].ring_id != incoming.ring_id:
        ss["reviewed_ring"] = incoming
    ring = ss["reviewed_ring"]
    if incoming != ring:
        st.info("New case data is available. The flow stays on version %d until you apply the update." % ring.version)
        if st.button("Review updated case", key="apply-case-update"):
            ss["reviewed_ring"] = incoming
            st.rerun(scope="app")
    section("02 / Follow the money")
    head, status = st.columns([3, 1])
    head.subheader("%s · %s" % (fmt.md(ring.ring_id), fmt.md(ring.shape_label)), anchor=False)
    dec = snap.decisions.get(ring.ring_id)
    status.caption(fmt.md(dec.label if dec else "● Requires review"))
    if dec and dec.version != ring.version:
        st.caption("The latest decision applies to version %s; you are reviewing version %d." % (dec.version, ring.version))
    st.caption(fmt.md("Version %d · Dataset time %s → %s" % (ring.version, fmt.data_time(ring.first_seen), fmt.data_time(ring.last_seen))))
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
    event = money_flow(graph=data, theme=theme(), key="flow-" + ring.ring_id, default=None)
    event_key = "flow-event-" + ring.ring_id
    if isinstance(event, dict) and event.get("nonce") != ss.get(event_key):
        ss[event_key] = event.get("nonce")
        if event.get("key") in choices:
            ss[select_key] = event["key"]
            selections[ring.ring_id] = event["key"]
            # No rerun needed: the component already highlights this event locally.
            # The next timed refresh supplies its new selection back to the graph.
    selection = st.selectbox("Inspect an account or transfer", choices, key=select_key,
                             format_func=lambda key: ws.selection_label(ring, key),
                             help="Keyboard alternative to the graph. Account selection filters transfers.")
    selections[ring.ring_id] = selection
    if data["limited"]:
        st.caption(fmt.md(data["scope"] + ". Largest transfers and your selection are prioritized; all transfers remain in the inspection list."))
    inspector(ring, selection)
    transfer_tab, evidence_tab, audit_tab = st.tabs(["Transfers", "Evidence & explanation", "History"])
    with transfer_tab:
        transfer_list(ring, selection)
    with evidence_tab:
        evidence(ring, snap.cases.get(ring.ring_id))
    with audit_tab:
        if not snap.redis_ok:
            st.info("Reconnect to retrieve the audit history.")
        else:
            try:
                past_decisions = state.decision_history(redis_client(), ring.ring_id)
                past_cases = state.case_history(redis_client(), ring.ring_id)
            except Exception:
                st.warning("History is unavailable. The next refresh will retry.")
            else:
                st.markdown("**Recorded decisions**")
                if not past_decisions:
                    st.caption("No decision recorded in the current history window.")
                for d in past_decisions:
                    st.markdown("**%s · v%s**  \n%s" % (fmt.md(d.label), fmt.md(d.version), fmt.md(d.note or "No note")))
                    st.caption(fmt.md(fmt.wall_time(d.decided_at)))
                with st.expander("Explanation history (%d)" % len(past_cases)):
                    for case in reversed(past_cases):
                        st.caption(fmt.md("v%d · %s · %s" % (case.version, ws.provenance(case), fmt.wall_time(case.written_at))))
                st.caption("History window: latest %d decisions and %d explanations across all cases." % (state.DECISIONS_WINDOW, state.CASES_WINDOW * 4))


def decision_form():
    ring = st.session_state.get("reviewed_ring")
    if not ring or ring.ring_id != selected_id():
        return
    rid = ring.ring_id
    section("04 / Record your decision")
    request_key = "request-" + rid
    st.session_state.setdefault(request_key, str(uuid.uuid4()))
    with st.form("decide-" + rid, clear_on_submit=False):
        st.markdown("**Your decision on %s · version %d**" % (fmt.md(rid), ring.version))
        st.caption("This records an instruction in the audit log. It does not freeze bank accounts.")
        note = st.text_area("Investigation note", key="note-" + rid, max_chars=decisions.MAX_NOTE,
                            placeholder="What did you observe, and why did you choose this action?", height=90)
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
            decisions.write(redis_client(), rid, ring.version, pressed, note,
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
    snapshot.clear()
    st.rerun()


def main():
    css = (HERE / "styles.css").read_text()
    if theme() == "dark":
        css += ":root{--canvas:#131f1b;--surface:#1c2a25;--ink:#e6eee9;--muted:#a9bcb2;--border:#40574b;--accent:#81d5b2;--tint:#27463a;--review:#f2c478;--review-bg:#3b3020}"
    html("<style>" + css + "</style>")
    html('<div class="masthead"><div class="brand"><svg class="mark" viewBox="0 0 36 36" aria-hidden="true"><path d="M7 8L27 18 7 28 7 8M7 8L18 18 7 28" fill="none" stroke="currentColor" stroke-width="1.5"/><g fill="currentColor"><circle cx="7" cy="8" r="3"/><circle cx="27" cy="18" r="3"/><circle cx="7" cy="28" r="3"/><circle cx="18" cy="18" r="3"/></g></svg><div><h1 class="name">Mule Ring Hunter</h1><div class="eyebrow">Investigation workspace</div></div></div><div class="eyebrow">IBM Z Datathon / UT Dallas</div></div>')
    st.caption("Find connected suspicious transfers, review the evidence, and record a decision.")
    if "flash" in st.session_state:
        st.success(fmt.md(st.session_state.pop("flash")))
    orientation()
    with st.container(key="workspace"):
        left, right = st.columns([1, 3], gap="large")
        with left, st.container(key="queue"):
            case_queue()
        with right:
            investigation()
            decision_form()


main()
