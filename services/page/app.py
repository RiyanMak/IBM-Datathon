"""Step 6: the investigator page. The AI recommends and explains; a person decides.

    streamlit run services/page/app.py --server.port 8501 --server.address 0.0.0.0

This is the only file that imports Streamlit. It holds no business logic: it reads
one PageState from state.py, draws rings with graph.py, writes with decisions.py.

Layout:
  top bar (Redis banner, counters, latency, Granite)     live, every 2 s
  left:  ring list with sort and "open only"             live, every 2 s
  right: ring header, picture, reasons, case summary     live, every 2 s
         decision form                                   NOT live, so typing is never lost
"""
import os
import sys
import time

# `streamlit run` puts only this file's folder on sys.path; the repo root is two up.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402
from redis.backoff import NoBackoff  # noqa: E402
from redis.retry import Retry  # noqa: E402

from common import bus  # noqa: E402
from services.page import decisions, fmt, graph, state  # noqa: E402
from services.page.models import PageState, decision_label, shape_label  # noqa: E402

REFRESH_S = 2
SORTS = {"Newest": "newest", "Highest score": "score"}
DECISION_COLOURS = {"freeze": "red", "dismiss": "gray", "more_info": "orange"}
UPGRADE_HIGHLIGHT_S = 20

st.set_page_config(page_title="Mule Ring Hunter", layout="wide")


# --- data ---------------------------------------------------------------------

@st.cache_resource
def redis_client():
    # Fail fast: a down Redis shows a banner within ~1 s instead of stalling the page.
    return bus.connect(socket_connect_timeout=1, socket_timeout=2, retry=Retry(NoBackoff(), 0))


@st.cache_data(ttl=1.0, show_spinner=False)
def snapshot() -> PageState:
    """One read of Redis per second, however many live areas ask for it."""
    return state.load(redis_client())


def theme() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except AttributeError:   # Streamlit older than 1.46
        return "light"


def badge(text: str, colour: str, help_text: str = ""):
    """Callers pass plain data; it is escaped here."""
    text = fmt.md(text)
    help_text = fmt.md(help_text) if help_text else None
    if hasattr(st, "badge"):   # Streamlit 1.44+
        st.badge(text, color=colour, help=help_text)
    else:
        st.markdown(":%s-background[%s]" % (colour, text), help=help_text)


def selected_id():
    return st.session_state.get("selected")


# --- live areas -----------------------------------------------------------------

@st.cache_data(ttl=REFRESH_S, show_spinner=False)
def history(ring_id: str):
    try:
        return state.case_history(redis_client(), ring_id)
    except Exception:   # Redis down: the banner already says so
        return []


def recently_upgraded(ring_id: str) -> bool:
    at = st.session_state.get("upgraded_at", {}).get(ring_id)
    return at is not None and time.time() - at < UPGRADE_HIGHLIGHT_S


@st.fragment(run_every=REFRESH_S)
def top_bar():
    snap = snapshot()
    # Watch every ring, not only the selected one, for a template -> Granite upgrade.
    seen = st.session_state.setdefault("seen_sources", {})
    for rid in state.detect_upgrades(seen, snap.cases):
        st.session_state.setdefault("upgraded_at", {})[rid] = time.time()
        st.toast("Granite summary arrived for %s." % fmt.md(rid))
    if not snap.redis_ok:
        st.warning("Waiting for Redis at %s:%d. The page will fill in once it is reachable."
                   % (bus.redis_host(), bus.redis_port()))
    for err in snap.errors if snap.redis_ok else []:
        st.error(err)

    with st.container(border=True):
        tiles = fmt.top_bar(snap.counters)
        for col, tile in zip(st.columns(len(tiles)), tiles):
            col.metric(tile.label, tile.value, help=tile.help)
        notes = ["Updated %s" % time.strftime("%H:%M:%S UTC", time.gmtime())]
        if snap.skipped:
            notes.append("skipped malformed messages: " + ", ".join(
                "%s %d" % (k, v) for k, v in sorted(snap.skipped.items())))
        st.caption(fmt.md(" · ".join(notes)))


def ring_caption(snap: PageState, ring) -> str:
    bits = [fmt.plural(ring.n_accounts, "account"),
            "XGB " + fmt.score(ring.xgb_score),
            "GNN " + fmt.score(ring.gnn_score)]
    d = snap.decisions.get(ring.ring_id)
    bits.append(d.label if d else "Open")
    return fmt.md(" · ".join(bits))


@st.fragment(run_every=REFRESH_S)
def ring_list():
    snap = snapshot()
    ss = st.session_state
    active = sum([bool(ss.get("open_only")), bool(ss.get("shapes")), bool(ss.get("min_score"))])
    title_col, filter_col = st.columns([3, 2], vertical_alignment="center")
    title_col.markdown("**Rings**")
    with filter_col.popover("Filters (%d)" % active if active else "Filters", width="stretch",
                            key="filters"):
        st.toggle("Open only", key="open_only", help="Hide rings that already have a decision.")
        # Offer the shapes present now, plus any already chosen, so a choice never vanishes.
        shapes = sorted({x.shape for x in snap.rings} | set(ss.get("shapes", [])),
                        key=lambda s: shape_label(s))
        st.multiselect("Shapes", shapes, key="shapes", format_func=shape_label,
                       placeholder="All shapes")
        st.slider("Minimum XGBoost score", 0.0, 1.0, step=0.05, key="min_score",
                  help="Rings with no score are hidden when this is above 0.")
    sort = st.segmented_control("Sort", list(SORTS), default="Newest", key="sort",
                                label_visibility="collapsed") or "Newest"

    rings = state.sort_rings(snap.rings, SORTS[sort])
    rings = state.filter_rings(rings, ss.get("shapes"), ss.get("min_score") or 0.0,
                               bool(ss.get("open_only")), snap.decisions)
    if not rings:
        st.info("No rings match the filters." if snap.rings else
                "No rings yet. They appear here as the ring service finds them.")
        return
    if len(rings) < len(snap.rings):
        st.caption("Showing %d of %d rings." % (len(rings), len(snap.rings)))

    ids = [x.ring_id for x in rings]
    by_id = {x.ring_id: x for x in rings}
    current = selected_id()
    if current not in by_id:
        current = ids[0]
        st.session_state["selected"] = current

    with st.container(height=560, border=False):
        choice = st.radio(
            "Rings", ids, index=ids.index(current), label_visibility="collapsed",
            format_func=lambda rid: "**%s** · %s" % (fmt.md(rid), fmt.md(by_id[rid].shape_label)),
            captions=[ring_caption(snap, by_id[rid]) for rid in ids],
        )
    if choice != selected_id():
        st.session_state["selected"] = choice
        st.rerun(scope="app")   # the decision form below must follow the selection


@st.fragment(run_every=REFRESH_S)
def ring_detail():
    snap = snapshot()
    ring = snap.ring(selected_id()) if selected_id() else None
    if ring is None:
        if snap.rings:
            st.info("Select a ring on the left.")
        return
    case = snap.cases.get(ring.ring_id)
    dec = snap.decisions.get(ring.ring_id)

    # Header
    head, tag = st.columns([4, 1], vertical_alignment="center")
    head.subheader("%s · %s" % (fmt.md(ring.ring_id), fmt.md(ring.shape_label)), anchor=False)
    with tag:
        if dec:
            badge(dec.label, DECISION_COLOURS.get(dec.decision, "gray"),
                  "Decided %s" % fmt.wall_time(dec.decided_at))
        else:
            badge("Open", "blue")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("XGBoost score", fmt.score(ring.xgb_score))
    m2.metric("GNN score", fmt.score(ring.gnn_score),
              help=None if ring.gnn_score is not None else "No GNN score sent for this ring.")
    m3.metric("Accounts", ring.n_accounts)
    m4.metric("Money moved", graph.money(ring.total_usd))
    span = fmt.span_hours(ring.first_seen, ring.last_seen)
    st.caption("Version %d · data time %s to %s%s" % (
        ring.version, fmt.data_time(ring.first_seen), fmt.data_time(ring.last_seen),
        " (%s)" % span if span else ""))

    # Picture
    pic = graph.build(ring, case.facts if case else None, theme())
    with st.container(border=True):
        st.graphviz_chart(pic.dot, width="content")
        notes = []
        if pic.caption:
            notes.append(pic.caption)
        if case is None or not pic.roles:
            notes.append("Key account, origin and collector are highlighted once the case arrives.")
        notes.append("Hover an account or arrow for details.")
        st.caption(fmt.md(" ".join(notes)))

    # Why and summary
    why, summary = st.columns([2, 3], gap="large")
    with why:
        st.markdown("**Why it was flagged**")
        if ring.reasons:
            st.markdown("\n".join("%d. %s" % (i, fmt.md(r)) for i, r in enumerate(ring.reasons, 1)))
        else:
            st.caption("No reasons were sent with this ring.")
        rows = fmt.fact_rows(case.facts if case else None)
        if rows:
            st.markdown("**Numbers behind the summary**")
            st.markdown("| Fact | Value |\n|---|---|\n" + "\n".join(
                "| %s | %s |" % (fmt.md(label), fmt.md(value)) for label, value in rows))
            st.caption("Computed by our code from the ring. The fact check rejects any Granite "
                       "summary that uses a number not in this set.")
    with summary:
        title, src = st.columns([3, 2], vertical_alignment="center")
        title.markdown("**Case summary**")
        if case is None:
            st.info("Summary coming. The case writer has not written this ring yet.")
        else:
            with src:
                badge(case.source_label, "green" if case.is_granite else "gray",
                      "%s · written %s" % (case.check_note or "no note", fmt.wall_time(case.written_at)))
            if recently_upgraded(ring.ring_id):
                st.success("Granite summary arrived just now. It replaced the template summary.")
            if case.version != ring.version:
                st.caption("Written for version %d; the ring is now at version %d."
                           % (case.version, ring.version))
            for part in case.parts:
                st.markdown("**%s**  \n%s" % (fmt.md(part.label), fmt.md(part.text)))
            past = history(ring.ring_id)
            if past:
                with st.expander("Case history (%d)" % len(past)):
                    for c in reversed(past):   # newest first
                        st.caption(fmt.md("Version %d · %s · %s · %s" % (
                            c.version, c.source_label, fmt.wall_time(c.written_at),
                            c.check_note or "no note")))


# --- decision form (not live) -----------------------------------------------------

def decision_form():
    rid = selected_id()
    snap = snapshot()
    ring = snap.ring(rid) if rid else None
    if ring is None:
        return
    dec = snap.decisions.get(rid)

    with st.form("decide-%s" % rid, clear_on_submit=True):
        st.markdown("**Your decision on %s**" % fmt.md(rid))
        if dec:
            st.caption(fmt.md("Last decision: %s, %s%s" % (
                dec.label, fmt.wall_time(dec.decided_at), (". Note: " + dec.note) if dec.note else "")))
        note = st.text_area("Note", key="note-%s" % rid, max_chars=decisions.MAX_NOTE,
                            placeholder="Optional. Why? This goes into the audit log.",
                            height=80)
        c1, c2, c3 = st.columns(3)
        pressed = None
        if c1.form_submit_button("Freeze accounts", type="primary", width="stretch"):
            pressed = "freeze"
        if c2.form_submit_button("Needs more info", width="stretch"):
            pressed = "more_info"
        if c3.form_submit_button("Dismiss", width="stretch"):
            pressed = "dismiss"

    if pressed:
        snapshot.clear()
        fresh = snapshot().ring(rid)   # record the version on screen now, not at page load
        version = fresh.version if fresh else ring.version
        try:
            decisions.write(redis_client(), rid, version, pressed, note)
        except decisions.DecisionError as exc:
            st.error(str(exc))
            return
        except Exception as exc:   # Redis down between load and click
            st.error("Could not save the decision: %s" % exc)
            return
        snapshot.clear()
        st.session_state["flash"] = fmt.md("Saved: %s for %s (version %d)." % (
            decision_label(pressed), rid, version))
        st.rerun()


# --- page -----------------------------------------------------------------------

def main():
    # Less empty space above the title; nothing else is restyled.
    st.markdown("<style>.block-container{padding-top:2.5rem}</style>", unsafe_allow_html=True)
    st.title("Mule Ring Hunter", anchor=False)
    st.caption("Investigator page. The AI recommends and explains; a person decides, and every "
               "decision is logged.")
    if "flash" in st.session_state:
        st.toast(st.session_state.pop("flash"))

    top_bar()
    left, right = st.columns([1, 2.6], gap="large")
    with left:
        ring_list()
    with right:
        ring_detail()
        decision_form()


main()
