"""Streamlit entry point: one coherent frame, then one page per investigation step."""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Mule Ring Hunter", page_icon="◎", layout="wide")

from services.page import ui  # noqa: E402

ui.page_frame()
pages = [st.Page(path, title=title, default=(key == "home"), url_path="" if key == "home" else key)
         for key, (path, title) in ui.PAGES.items()]
st.navigation(pages, position="top").run()
