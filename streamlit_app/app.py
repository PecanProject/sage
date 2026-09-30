"""
app.py
================
Scientist-review UI entrypoint -- a review workstation, not a dashboard.

Two pages only:
    - library:  upload PDFs, run Marker processing, pick a paper to open
    - review:   the two-pane workspace (real PDF left, extracted data right)

No entity sidebar, no per-entity nav buttons, no card-heavy layout. All
data comes through api_client.py, which reads real Sage artifacts
(src/results/, src/paper/, src/pipeline/corrections_store) --
see api_client.py's own docstring for the architecture boundary this UI
must not cross (no extraction/business logic in components).
"""

import streamlit as st

import state
import styles
from components import library, workspace

st.set_page_config(
    page_title="Sage Scientist Review",
    page_icon="\U0001f9ea",
    layout="wide",
)

styles.inject_base_css()
state.init_state()

styles.masthead("Scientist Review")

if st.session_state.nav != "review" or not st.session_state.paper_id:
    library.render()
else:
    workspace.render(st.session_state.paper_id)
