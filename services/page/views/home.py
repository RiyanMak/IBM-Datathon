"""Overview: what the product does, the live status, the pipeline, and the way in."""
from html import escape

import streamlit as st

from services.page import fmt, ui

HERO_FLOW = """
<svg class="hero-flow" viewBox="0 0 440 170" role="img" aria-label="Money leaves an origin account, spreads across several accounts, then converges on a final recipient.">
  <defs><marker id="hm" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5 0 10z" fill="currentColor"/></marker></defs>
  <g class="wires" style="color:var(--border-strong)">
    <path id="p1" class="wire draw" d="M58 85 L140 85"/>
    <path id="p2" class="wire draw" d="M178 85 Q230 85 262 40"/>
    <path id="p3" class="wire draw" d="M178 85 L262 85"/>
    <path id="p4" class="wire draw" d="M178 85 Q230 85 262 130"/>
    <path id="p5" class="wire draw" d="M298 40 Q350 40 382 85"/>
    <path id="p6" class="wire draw" d="M298 85 L382 85"/>
    <path id="p7" class="wire draw" d="M298 130 Q350 130 382 85"/>
  </g>
  <g>
    <circle class="pulse" r="3.5" opacity="0"><set attributeName="opacity" to="1" begin="0.4s"/><animateMotion dur="2.6s" repeatCount="indefinite" begin="0.4s"><mpath href="#p1"/></animateMotion></circle>
    <circle class="pulse" r="3" opacity="0"><set attributeName="opacity" to="1" begin="1.3s"/><animateMotion dur="2.6s" repeatCount="indefinite" begin="1.3s"><mpath href="#p2"/></animateMotion></circle>
    <circle class="pulse" r="3" opacity="0"><set attributeName="opacity" to="1" begin="1.6s"/><animateMotion dur="2.6s" repeatCount="indefinite" begin="1.6s"><mpath href="#p3"/></animateMotion></circle>
    <circle class="pulse" r="3" opacity="0"><set attributeName="opacity" to="1" begin="1.9s"/><animateMotion dur="2.6s" repeatCount="indefinite" begin="1.9s"><mpath href="#p4"/></animateMotion></circle>
    <circle class="pulse" r="3" opacity="0"><set attributeName="opacity" to="1" begin="2.9s"/><animateMotion dur="2.6s" repeatCount="indefinite" begin="2.9s"><mpath href="#p5"/></animateMotion></circle>
    <circle class="pulse" r="3" opacity="0"><set attributeName="opacity" to="1" begin="3.2s"/><animateMotion dur="2.6s" repeatCount="indefinite" begin="3.2s"><mpath href="#p6"/></animateMotion></circle>
    <circle class="pulse" r="3" opacity="0"><set attributeName="opacity" to="1" begin="3.5s"/><animateMotion dur="2.6s" repeatCount="indefinite" begin="3.5s"><mpath href="#p7"/></animateMotion></circle>
  </g>
  <g>
    <circle class="acct origin" cx="40" cy="85" r="18"/><text class="lbl origin" x="40" y="122">Origin</text>
    <circle class="acct" cx="160" cy="85" r="18"/><text class="lbl" x="160" y="122">Mule</text>
    <circle class="acct" cx="280" cy="40" r="16"/><circle class="acct" cx="280" cy="85" r="16"/><circle class="acct" cx="280" cy="130" r="16"/>
    <circle class="acct end" cx="400" cy="85" r="18"/><text class="lbl end" x="400" y="122">Final recipient</text>
  </g>
</svg>"""

PIPELINE = [
    ("01", "Replay", "Synthetic IBM AML transfers are replayed in dataset order.", False),
    ("02", "Score", "XGBoost ranks each transfer. A score orders attention; it is not proof.", False),
    ("03", "Connect", "Flagged transfers are linked into candidate rings; a GNN re-scores them.", False),
    ("04", "Explain", "A template or Granite explanation is fact-checked against the case facts.", False),
    ("05", "Decide", "An investigator records a freeze, a request for more information, or a dismissal.", False),
]

CHAPTERS = [
    ("cases", "01", "Choose a case", "A ranked queue of flagged flows with the first reason for each flag.", "What needs my attention?"),
    ("investigate", "02", "Follow the money", "A directional graph from origin to final recipient, with every transfer listed.", "How are these accounts connected?"),
    ("evidence", "03", "Weigh the evidence", "Supplied reasons, model scores, computed observations and a labelled explanation.", "Why was this flagged?"),
    ("decide", "04", "Record a decision", "An audit-log instruction with a note, confirmed only once the service acknowledges it.", "What can I do next?"),
]


@st.fragment(run_every=ui.REFRESH_S)
def live_strip():
    snap = ui.current_snapshot()
    known = snap.redis_ok or "last_good_snapshot" in st.session_state
    c = snap.counters
    ui.html('<div class="status-row"><span class="chip">%s</span>%s</div>' % (escape(ui.mode_label(snap)), ui.status_chip(snap)))
    values = [(str(c.open_rings), "cases awaiting a decision"),
              (str(c.total_rings), "cases in the current window"),
              (fmt.count(c.flagged), ("flagged / %s total scored" if c.counts_exact else "flagged in latest %s scored") % fmt.count(c.flagged_window))]
    ui.html('<div class="summary-strip">' + "".join(
        '<div><strong>%s</strong><span>%s</span></div>' % (escape(v if known else fmt.DASH), escape(label))
        for v, label in values) + "</div>")
    if not snap.redis_ok:
        st.warning("Connection unavailable. The last available data is shown. See System status to retry.")


def hero():
    ui.html('<div class="hero"><div><div class="eyebrow">IBM Z Datathon · UT Dallas · synthetic AML data</div>'
            '<h1 class="hero-title">Find the accounts that <em>move money for someone else.</em></h1>'
            '<p class="hero-lede">Mule Ring Hunter replays transfers, scores the suspicious ones, links them into connected flows, '
            'and gives an investigator the evidence to record a decision.</p></div><div>%s</div></div>' % HERO_FLOW)
    with st.container(key="cta", horizontal=True):
        st.page_link(ui.PAGES["cases"][0], label="Start investigating", icon=":material/arrow_forward:")
        st.page_link(ui.PAGES["status"][0], label="Data status and diagnostics", icon=":material/monitoring:")


def pipeline():
    ui.section("How a transfer becomes a case")
    ui.reveal('<div class="pipeline">' + "".join(
        '<div class="stage%s"><div class="n">%s</div><strong>%s</strong><span>%s</span></div>' % (
            " soft" if soft else "", n, escape(title), escape(text)) for n, title, text, soft in PIPELINE) + "</div>")
    st.caption("Replay progress and model internals are not shown here. The queue lists whatever the ring service has supplied so far.")


def chapters():
    ui.section("The investigation, step by step")
    cols = st.columns(4, gap="medium")
    for n, (key, number, title, text, question) in enumerate(CHAPTERS):
        with cols[n]:
            ui.reveal('<div class="chapter"><div class="n">%s</div><h3>%s</h3><p>%s</p><span class="q">%s</span></div>' % (
                number, escape(title), escape(text), escape(question)), delay=n)
            st.page_link(ui.PAGES[key][0], label="Open", icon=":material/arrow_forward:")


hero()
live_strip()
pipeline()
chapters()
