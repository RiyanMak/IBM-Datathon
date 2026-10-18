"""Entry page: the product in one screen, then one action to enter the workspace."""
from html import escape

import streamlit as st

from services.page import fmt, ui

HERO_FLOW = """
<svg class="hero-flow" viewBox="0 0 440 170" role="img" aria-label="Money leaves an origin account, spreads across several accounts, then converges on a final recipient.">
  <g class="wires">
    <path id="p1" class="wire draw" d="M58 85 L140 85"/>
    <path id="p2" class="wire draw" d="M178 85 Q230 85 262 40"/>
    <path id="p3" class="wire draw" d="M178 85 L262 85"/>
    <path id="p4" class="wire draw" d="M178 85 Q230 85 262 130"/>
    <path id="p5" class="wire draw" d="M298 40 Q350 40 382 85"/>
    <path id="p6" class="wire draw" d="M298 85 L382 85"/>
    <path id="p7" class="wire draw" d="M298 130 Q350 130 382 85"/>
  </g>
  <g>%s</g>
  <g>
    <circle class="acct origin" cx="40" cy="85" r="18"/><text class="lbl origin" x="40" y="118">Origin</text>
    <circle class="acct" cx="160" cy="85" r="18"/><text class="lbl" x="160" y="118">Mule account</text>
    <circle class="acct" cx="280" cy="40" r="16"/><circle class="acct" cx="280" cy="85" r="16"/><circle class="acct" cx="280" cy="130" r="16"/>
    <text class="lbl" x="280" y="160">Intermediaries</text>
    <circle class="acct end" cx="400" cy="85" r="18"/><text class="lbl end" x="400" y="118">Final recipient</text>
  </g>
</svg>""" % "".join(
    '<circle class="pulse" r="%s" opacity="0"><set attributeName="opacity" to="1" begin="%ss"/>'
    '<animateMotion dur="2.6s" repeatCount="indefinite" begin="%ss"><mpath href="#p%d"/></animateMotion></circle>' % (r, b, b, n)
    for n, r, b in [(1, 3.5, 0.4), (2, 3, 1.3), (3, 3, 1.6), (4, 3, 1.9), (5, 3, 2.9), (6, 3, 3.2), (7, 3, 3.5)])


@st.fragment(run_every=ui.REFRESH_S)
def foot():
    snap = ui.current_snapshot()
    known = snap.redis_ok or "last_good_snapshot" in st.session_state
    c = snap.counters
    counts = [(str(c.open_rings), "cases awaiting a decision"), (str(c.total_rings), "cases in the current window"),
              (fmt.count(c.flagged), "flagged in the latest %s scored" % fmt.count(c.flagged_window))]
    ui.html('<div class="entry-foot">' + "".join('<div><strong>%s</strong>%s</div>' % (escape(v if known else fmt.DASH), escape(l)) for v, l in counts)
            + '<div style="margin-left:auto;display:flex;gap:10px;align-items:center"><span class="chip">%s</span>%s</div></div>'
            % (escape(ui.mode_label(snap)), ui.status_chip(snap)))
    if not snap.redis_ok:
        st.warning("Connection unavailable. The last available data is shown. See System status to retry.")


entry = st.container(key="entry")
left, right = entry.columns([4.6, 7.4], gap="large", vertical_alignment="center")
with left:
    ui.html('<div class="eyebrow">IBM Z Datathon · UT Dallas</div>'
            '<h1 class="display">Mule Ring <em>Hunter</em></h1>'
            '<p class="lede">Investigation workspace for connected suspicious transfers.</p>'
            '<p class="sub">Replayed transactions are scored, linked into candidate rings and explained. '
            'Choose a case, follow the money, weigh the evidence and record a decision.</p>')
    with st.container(key="cta", horizontal=True):
        if st.button("Start investigating", type="primary", icon=":material/arrow_forward:", key="enter"):
            st.switch_page(ui.PAGES["cases"][0])
        st.page_link(ui.PAGES["about"][0], label="How it works", icon=":material/arrow_forward:")
with right:
    ui.html(HERO_FLOW)
foot()
