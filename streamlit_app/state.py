"""
state.py
================
Central Streamlit session-state management for the review workflow.

Deliberately small: this UI's data (records, corrections, PDF geometry)
lives in real Sage artifacts on disk (via api_client.py / provenance_adapter.py),
re-read on demand -- session_state here only tracks transient UI state that
has no other home: which page is open, which records are expanded, which
locators the PDF viewer should currently highlight.

Expansion is tracked per RECORD (entity_type, record_id), not per field
(Phase: two-pane evidence-review redesign) -- the review pane's primary
scannable unit is now one compact row per record, with every one of that
record's fields shown in full once the record itself is opened. Multiple
records may be expanded at once (a set, not a single value) so a scientist
can keep two Species open side by side while scrolling, rather than being
forced back to one at a time as the old single-field design was.
"""

from __future__ import annotations

import streamlit as st

DEFAULT_PAPER_ID = None  # no paper selected -> library page shows first


def init_state():
    if "paper_id" not in st.session_state:
        st.session_state.paper_id = DEFAULT_PAPER_ID
    if "nav" not in st.session_state:
        st.session_state.nav = "library"  # "library" | "review"
    if "expanded_records" not in st.session_state:
        st.session_state.expanded_records = set()  # {(entity_type, record_id), ...}
    if "active_locators" not in st.session_state:
        st.session_state.active_locators = []  # list of resolved provenance_adapter locators
    if "active_locator_index" not in st.session_state:
        st.session_state.active_locator_index = 0
    if "last_message" not in st.session_state:
        st.session_state.last_message = None
    if "marker_run_result" not in st.session_state:
        st.session_state.marker_run_result = None


def open_paper(paper_id: str):
    st.session_state.paper_id = paper_id
    st.session_state.nav = "review"
    st.session_state.expanded_records = set()
    st.session_state.active_locators = []
    st.session_state.active_locator_index = 0


def go_to_library():
    st.session_state.nav = "library"
    st.session_state.paper_id = None


def is_record_expanded(key: tuple[str, str]) -> bool:
    return key in st.session_state.expanded_records


def toggle_expanded_record(key: tuple[str, str]):
    if key in st.session_state.expanded_records:
        st.session_state.expanded_records.discard(key)
    else:
        st.session_state.expanded_records.add(key)


def set_active_locators(locators: list[dict]):
    st.session_state.active_locators = locators or []
    st.session_state.active_locator_index = 0


def get_active_locator() -> dict | None:
    locators = st.session_state.active_locators
    if not locators:
        return None
    idx = min(st.session_state.active_locator_index, len(locators) - 1)
    return locators[idx]


def set_active_locator_index(idx: int):
    st.session_state.active_locator_index = idx


def set_message(message: str):
    st.session_state.last_message = message
