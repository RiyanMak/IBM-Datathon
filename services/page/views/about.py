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
            ui.reveal('<div class="chapter"><div class="n">%s</div><h3>%s</h3><p>%s</p><span class="q">%s</span></div>' % (
                number, escape(title), escape(text), escape(question)), delay=n)
            st.page_link(ui.PAGES[key][0], label="Open", icon=":material/arrow_forward:")


st.markdown("## How it works")
st.caption("Transaction replay → suspicious-payment scoring → connected-account investigation → evidence-based explanation → human decision.")
pipeline()
chapters()
ui.section("Boundaries")
st.markdown("- Transactions are synthetic IBM AML data replayed as a simulation; dataset time is historical.\n"
            "- Scores rank suspicion on a 0–1 scale and are not calibrated probabilities of wrongdoing.\n"
            "- Account roles in the graph are inferred from the case's transfers, not dataset facts.\n"
            "- Decisions are audit records. They do not freeze bank accounts.")
