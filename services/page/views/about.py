"""How it works: the pipeline and the four investigation steps, for a first-time viewer."""
from html import escape

import streamlit as st

from services.page import ui

PIPELINE = [
    ("01", "Replay", "Synthetic IBM AML transfers are replayed in dataset order."),
    ("02", "Score", "XGBoost ranks each transfer. A score orders attention; it is not proof."),
    ("03", "Connect", "Flagged transfers are linked into candidate rings; a GNN re-scores them."),
    ("04", "Explain", "A template or Granite explanation is fact-checked against the case facts."),
    ("05", "Decide", "An investigator records a freeze, a request for more information, or a dismissal."),
]

CHAPTERS = [
    ("cases", "01", "Choose a case", "A ranked queue of flagged flows with the first reason for each flag.", "What needs my attention?"),
    ("investigate", "02", "Follow the money", "A directional graph from origin to final recipient, with every transfer listed.", "How are these accounts connected?"),
    ("evidence", "03", "Weigh the evidence", "Supplied reasons, model scores, computed observations and a labelled explanation.", "Why was this flagged?"),
    ("decide", "04", "Record a decision", "An audit-log instruction with a note, confirmed only once the service acknowledges it.", "What can I do next?"),
]


def pipeline():
    ui.section("How a transfer becomes a case")
    ui.reveal('<div class="pipeline">' + "".join(
        '<div class="stage"><div class="n">%s</div><strong>%s</strong><span>%s</span></div>' % (
            n, escape(title), escape(text)) for n, title, text in PIPELINE) + "</div>")
    st.caption("Replay progress and model internals are not shown in the workspace. The queue lists whatever the ring service has supplied so far.")


def chapters():
    ui.section("The investigation, step by step")
    cols = st.columns(4, gap="medium")
    for n, (key, number, title, text, question) in enumerate(CHAPTERS):
        with cols[n]:
            with st.container(key="chapter-" + key):
                ui.reveal('<div class="chapter"><div class="n">%s</div><h3>%s</h3><p>%s</p><span class="q">%s</span></div>' % (
                    number, escape(title), escape(text), escape(question)), delay=n)
                st.page_link(ui.PAGES[key][0], label="Open " + ui.PAGES[key][1], icon=":material/arrow_forward:", width="stretch")


st.markdown("## How It Works")
st.caption("Transaction replay → suspicious-payment scoring → connected-account investigation → evidence-based explanation → human decision.")
with st.container(key="about-pipeline"):
    pipeline()
with st.container(key="about-chapters"):
    chapters()
with st.container(key="about-boundaries"):
    ui.section("Boundaries")
    boundaries = [
        ("01", "Simulated data", "Synthetic IBM AML transactions, replayed in historical dataset time."),
        ("02", "Scores guide attention", "A 0–1 ranking of suspicion, not a calibrated probability of wrongdoing."),
        ("03", "Roles are inferred", "Origin and recipient roles come from the case’s transfers, not dataset facts."),
        ("04", "People decide", "Decisions create audit records. They do not freeze bank accounts."),
    ]
    ui.html('<div class="boundaries">' + ''.join(
        '<div class="boundary"><span class="n">%s</span><div><h3>%s</h3><p>%s</p></div></div>' %
        (n, escape(title), escape(text)) for n, title, text in boundaries) + '</div>')
