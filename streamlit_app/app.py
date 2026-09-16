"""
Scientist-review UI entrypoint -- a review workstation, not a dashboard.
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
