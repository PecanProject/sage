from __future__ import annotations

import streamlit as st

import api_client
import state
from components import pdf_viewer, record_row

PANE_HEIGHT = 760  # px -- generous enough to actually read the paper, not a thumbnail
PANE_RATIO = 0.42  # PDF 42% / Review 58%


def render(paper_id: str):
    _render_overview_line(paper_id)

    left, right = st.columns([PANE_RATIO, 1 - PANE_RATIO])
    with left:
        _render_pdf_pane(paper_id)
    with right:
        _render_data_pane(paper_id)


def _render_overview_line(paper_id: str):
    top = st.columns([0.15, 0.85])
    with top[0]:
        if st.button("← Library", key="back_to_library"):
            state.go_to_library()
            st.rerun()
    summary = api_client.review_summary(paper_id)
    with top[1]:
        st.markdown(
            f'<div class="overview-line"><b>{paper_id}</b> &nbsp;·&nbsp; '
            f'{summary["total_fields"]} fields | {summary["reviewed"]} reviewed | '
            f'{summary["unresolved"]} unresolved | {summary["blocked"]} blocked | '
            f'{summary["remaining"]} remaining</div>',
            unsafe_allow_html=True,
        )
    if st.session_state.get("last_message"):
        st.info(st.session_state.last_message)
        st.session_state.last_message = None


def _render_pdf_pane(paper_id: str):
    with st.container(key="pdf_pane"):
        pdf_bytes = api_client.get_pdf_bytes(paper_id)
        locators = st.session_state.active_locators

        if len(locators) > 1:
            idx = st.radio(
                "Source", options=list(range(len(locators))),
                format_func=lambda i: f"Source {i + 1}/{len(locators)} (p.{locators[i]['page']})",
                index=st.session_state.active_locator_index, horizontal=True, key="locator_picker",
            )
            if idx != st.session_state.active_locator_index:
                state.set_active_locator_index(idx)
                st.rerun()

        locator = state.get_active_locator()
        pdf_viewer.render_pdf_page(
            pdf_bytes=pdf_bytes,
            page_number=(locator or {}).get("page", 1),
            polygon=(locator or {}).get("polygon"),
            block_id=(locator or {}).get("block_anchor"),
            height=PANE_HEIGHT,
            key="main_pdf_viewer",
        )


def _render_data_pane(paper_id: str):
    data = api_client.get_review_data(paper_id)
    with st.container(height=PANE_HEIGHT, key="data_pane", border=True):
        for entity_type in api_client.ENTITY_TYPES:
            records = data.get(entity_type, [])
            st.markdown(f'<div class="entity-heading">{entity_type.upper()}</div>', unsafe_allow_html=True)

            if not records:
                st.caption("Not extracted yet.")
                continue

            for record in records:
                record_row.render_record(paper_id, entity_type, record, data)
