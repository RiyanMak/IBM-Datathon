"""System status: connection, windows, counters and honest replay diagnostics."""
import time
from html import escape

import streamlit as st

from services.page import fmt, state, ui


@st.fragment(run_every=ui.REFRESH_S)
def diagnostics():
    snap = ui.current_snapshot()
    known = snap.redis_ok or "last_good_snapshot" in st.session_state
    ui.html('<div class="status-row"><span class="chip">%s</span>%s</div>' % (escape(ui.mode_label(snap)), ui.status_chip(snap)))
    if not snap.redis_ok:
        st.warning("Connection unavailable. Your notes are kept. Decisions require a connection.")
        if st.button("Retry connection"):
            ui.snapshot.clear()
            st.rerun(scope="app")
    for error in snap.errors if snap.redis_ok else []:
        st.warning(fmt.md("Some data could not be loaded: " + error))
    st.caption("Synthetic transactions only. Dataset time describes historical transfers. "
               "Connection checks describe this session; replay progress is not supplied.")
    if snap.loaded_ms and known:
        st.caption("Last successful read: %s UTC" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(snap.loaded_ms / 1000)))
    age = ui.read_age(snap)
    if age is not None:
        st.caption("Most recent scored/ring message arrived %s seconds ago. An idle feed may be complete or paused." % fmt.count(age))
    st.caption("Queue: latest versions in the last %d ring messages. Summary window: %d messages." %
               (state.RINGS_WINDOW, state.CASES_WINDOW))
    ui.section("Pipeline counters")
    tiles = fmt.top_bar(snap.counters)
    cols = st.columns(3)
    for n, tile in enumerate(tiles):
        with cols[n % 3]:
            ui.html('<div class="tile"><div class="l">%s</div><strong>%s</strong><div class="h">%s</div></div>' % (
                escape(tile.label), escape(tile.value if known else fmt.DASH), escape(tile.help)))
    if snap.skipped:
        st.warning(fmt.md("Malformed messages skipped: " + str(snap.skipped)))


st.markdown("## System status")
st.caption("Everything here describes this session and the stream windows it reads. Nothing here is a model result.")
diagnostics()
