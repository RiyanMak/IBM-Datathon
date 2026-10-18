"""Shared Streamlit helpers for every view: snapshot access, theme, HTML blocks, motion,
case context and the step rail. Only app.py, this module and views/ import Streamlit."""
import time
from dataclasses import replace
from html import escape
from pathlib import Path

import streamlit as st
from streamlit.components.v1 import declare_component
from redis.backoff import NoBackoff
from redis.retry import Retry

from common import bus
from services.page import fmt, state, workspace as ws

HERE = Path(__file__).resolve().parent
REFRESH_S = 2
SORTS = {"Highest score": "score", "Newest data": "newest"}
DARK_TOKENS = (":root{--canvas:#161616;--surface:#262626;--surface-2:#393939;--ink:#f4f4f4;--muted:#c6c6c6;"
               "--faint:#8d8d8d;--border:#393939;--border-strong:#525252;--accent:#4589ff;--accent-ink:#161616;"
               "--accent-strong:#78a9ff;--tint:#001d6c;--review:#f1c21b;--review-bg:#2d2500;--origin:#ff832b;--origin-fill:#ff832b;"
               "--origin-bg:#3a1e00;--ok:#42be65;--ok-bg:#0a2f16;--focus:#ffffff}")
# Streamlit keys the stored choice by page path ("stActiveTheme-<pathname>-v2"), so the switch
# writes every page's key; otherwise the choice would apply to one page only.
THEME_STORE_KEY = "stActiveTheme-%s-v2"
THEME_STORE_VALUES = {"light": '"Light"', "dark": '"Dark"'}

PAGES = {
    "home": ("views/home.py", "Start"),
    "cases": ("views/cases.py", "Cases"),
    "investigate": ("views/investigate.py", "Investigate"),
    "evidence": ("views/evidence.py", "Evidence"),
    "decide": ("views/decide.py", "Decide"),
    "status": ("views/status.py", "System status"),
    "about": ("views/about.py", "How it works"),
}
STEPS = [("cases", "Choose a case"), ("investigate", "Follow the money"),
         ("evidence", "Weigh the evidence"), ("decide", "Record a decision")]
PILL_CLASSES = {"freeze": "freeze", "dismiss": "dismiss", "more_info": "info"}

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


def is_dark_color(hex_color):
    """True for a dark #rrggbb background. Unreadable values count as light."""
    text = str(hex_color or "").strip().lstrip("#")
    if len(text) != 6:
        return False
    try:
        r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return False
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128


def theme():
    """'dark' only when the active variant really draws a dark canvas. The repository config
    pins both variants to the light palette, so this is 'light' unless that config is removed."""
    try:
        variant = st.context.theme.type
    except AttributeError:
        return "light"
    try:
        background = st.get_option("theme.dark.backgroundColor" if variant == "dark" else "theme.backgroundColor")
    except RuntimeError:  # the variant section is not configured in this deployment
        background = None
    if background is None:
        return "dark" if variant == "dark" else "light"
    return "dark" if is_dark_color(background) else "light"


def html(text):
    st.markdown(text, unsafe_allow_html=True)


def section(text, number=None):
    label = ("<b>%s</b> / " % escape(number) if number else "") + escape(text)
    html('<div class="section">%s</div>' % label)


def reveal(inner_html, delay=0):
    """Wrap static HTML so it fades in when scrolled into view (static without JS)."""
    html('<div class="reveal%s">%s</div>' % (" delay-%d" % delay if delay else "", inner_html))


def page_frame():
    """Stylesheet, brand mark, theme switch and the motion script. Called once per run from app.py."""
    active = theme()
    css = (HERE / "styles.css").read_text()
    if active == "dark":
        css += DARK_TOKENS
    html("<style>" + css + "</style>")
    mark = "wordmark-dark.svg" if active == "dark" else "wordmark.svg"
    st.logo(str(HERE / "assets" / mark), icon_image=str(HERE / "assets" / "mark.svg"), size="large")
    with st.container(key="motion"):
        st.iframe("<script>%s</script>" % (HERE / "motion.js").read_text(), height=1)
    theme_switch(active)
    if "flash" in st.session_state:
        st.success(fmt.md(st.session_state.pop("flash")))


def theme_switch(active):
    """One button that stores the other theme the way Streamlit's own menu does, then reloads."""
    wanted = "light" if active == "dark" else "dark"
    with st.container(key="utility", horizontal=True, horizontal_alignment="right"):
        if st.button("Switch to %s mode" % wanted, key="theme-switch",
                     icon=":material/light_mode:" if wanted == "light" else ":material/dark_mode:"):
            st.session_state["theme_request"] = wanted
    requested = st.session_state.pop("theme_request", None)
    if requested:
        keys = [THEME_STORE_KEY % path for path in theme_store_paths()]
        with st.container(key="themeio"):
            st.iframe("<script>try{var s=window.parent.localStorage;%s;window.parent.location.reload();}catch(e){}</script>"
                      % ";".join("s.setItem(%r,%r)" % (key, THEME_STORE_VALUES[requested]) for key in keys), height=1)


def theme_store_paths():
    """One local-storage path per page: '/' for the entry page, '/<key>' for the others."""
    return ["/"] + ["/" + key for key in PAGES if key != "home"]


def selected_id():
    return st.session_state.get("selected")


def clear_filters():
    for key, value in {"open_only": False, "shapes": [], "min_score": 0.0, "search": ""}.items():
        st.session_state[key] = value
    st.session_state.pop("queue_order", None)


def mode_label(snap):
    return "Fixture simulation" if snap.feed_mode == "fixture" else "Synthetic-data simulation"


def status_chip(snap):
    live = snap.redis_ok
    return ('<span class="chip %s"><span class="dot"></span>%s</span>' %
            ("live" if live else "off", "Connected · checking every 2 s" if live else "Disconnected · last available data"))


def pill(decision):
    if decision is None:
        return '<span class="pill review">Requires review</span>'
    return '<span class="pill %s">%s</span>' % (PILL_CLASSES.get(decision.decision, "dismiss"), escape(decision.label))


def ensure_selection(snap):
    """Jumping straight into a step still lands on the highest-scored case."""
    if selected_id() is None and snap.rings:
        top = state.sort_rings(list(snap.rings), "score")[0].ring_id
        st.session_state["selected"] = st.session_state["case_choice"] = top


def resolve_ring(snap):
    """The version under review, the incoming stream version, and whether it left the window."""
    ss = st.session_state
    incoming = snap.ring(selected_id())
    outside = False
    if incoming is None and ss.get("reviewed_ring") and ss["reviewed_ring"].ring_id == selected_id():
        incoming, outside = ss["reviewed_ring"], True
    if not incoming:
        return None, None, False
    if ss.get("reviewed_ring") is None or ss["reviewed_ring"].ring_id != incoming.ring_id:
        ss["reviewed_ring"] = incoming
    return ss["reviewed_ring"], incoming, outside


def step_rail(current):
    done = True
    parts = []
    for n, (key, label) in enumerate(STEPS, 1):
        if key == current:
            done = False
        cls = "current" if key == current else ("done" if done else "")
        parts.append('<span class="step %s"><i>%s</i>%s</span>' % (cls, "✓" if cls == "done" else "%02d" % n, escape(label)))
    html('<div class="steps">' + "".join(parts) + "</div>")


def case_bar(ring, snap):
    dec = snap.decisions.get(ring.ring_id)
    html('<div class="casebar"><div><p class="id">%s · %s</p><p class="meta">Version %d · %d accounts · %d transfers · dataset time %s → %s</p></div>'
         '<div class="right"><span class="score" title="XGBoost ranking score, 0–1. Not a probability of wrongdoing.">XGBoost %s</span>%s</div></div>' % (
             escape(ring.ring_id), escape(ring.shape_label), ring.version, len(ws.accounts(ring)), len(ring.edges),
             escape(fmt.data_time(ring.first_seen)), escape(fmt.data_time(ring.last_seen)), escape(fmt.score(ring.xgb_score)), pill(dec)))
    if dec and dec.version != ring.version:
        st.caption("The latest decision applies to version %s; you are reviewing version %d." % (dec.version, ring.version))


def no_case(message="Choose a case to inspect its transfers and evidence."):
    html('<div class="empty"><strong>Follow the money, one case at a time.</strong><p>%s</p></div>' % escape(message))
    st.page_link(PAGES["cases"][0], label="Open the case queue", icon=":material/arrow_forward:")


def step_links(previous=None, next_step=None):
    cols = st.columns([1, 1, 2])
    if previous:
        cols[0].page_link(PAGES[previous][0], label="Back · " + PAGES[previous][1], icon=":material/arrow_back:")
    if next_step:
        cols[1].page_link(PAGES[next_step][0], label="Next · " + PAGES[next_step][1], icon=":material/arrow_forward:")


def read_age(snap):
    if not snap.last_event_ms:
        return None
    return max(0, int(time.time() - snap.last_event_ms / 1000))
