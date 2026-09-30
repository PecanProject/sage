"""Focused tests for state.py's record-level expand tracking (Phase:
two-pane evidence-review redesign) -- multiple records may be expanded at
once, unlike the old single-field-at-a-time design."""

from __future__ import annotations

import streamlit as st

import state


def _reset():
    for k in ("paper_id", "nav", "expanded_records", "active_locators",
              "active_locator_index", "last_message", "marker_run_result"):
        if k in st.session_state:
            del st.session_state[k]


def test_init_state_starts_with_no_expanded_records():
    _reset()
    state.init_state()
    assert st.session_state.expanded_records == set()


def test_toggle_expanded_record_opens_and_closes_independently():
    _reset()
    state.init_state()
    key_a = ("Species", "sp_a")
    key_b = ("Species", "sp_b")

    state.toggle_expanded_record(key_a)
    state.toggle_expanded_record(key_b)
    assert state.is_record_expanded(key_a)
    assert state.is_record_expanded(key_b)

    state.toggle_expanded_record(key_a)
    assert not state.is_record_expanded(key_a)
    assert state.is_record_expanded(key_b)  # untouched by closing the other


def test_open_paper_clears_expanded_records():
    _reset()
    state.init_state()
    state.toggle_expanded_record(("Species", "sp_a"))
    state.open_paper("some_paper")
    assert st.session_state.expanded_records == set()


def test_active_locator_helpers_unchanged_by_the_redesign():
    _reset()
    state.init_state()
    assert state.get_active_locator() is None
    state.set_active_locators([{"page": 2, "polygon": [[0, 0]]}, {"page": 5, "polygon": None}])
    assert state.get_active_locator() == {"page": 2, "polygon": [[0, 0]]}
    state.set_active_locator_index(1)
    assert state.get_active_locator() == {"page": 5, "polygon": None}
