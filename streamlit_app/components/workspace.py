from __future__ import annotations

import streamlit as st

import api_client
import state
from components import pdf_viewer, record_row

PANE_HEIGHT = 760  # px -- generous enough to actually read the paper, not a thumbnail
PANE_RATIO = 0.42  # PDF 42% / Review 58%


def render(paper_id: str):
    if st.session_state.get("review_run") is None:
        st.session_state.review_run = api_client.default_review_run(paper_id)
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
    run_id = st.session_state.review_run
    summary = api_client.review_summary(paper_id, run_id)
    with top[1]:
        st.markdown(
            f'<div class="overview-line"><b>{paper_id}</b> &nbsp;·&nbsp; '
            f'{summary["total_fields"]} fields | {summary["reviewed"]} reviewed | '
            f'{summary["unresolved"]} unresolved | {summary["blocked"]} blocked | '
            f'{summary["remaining"]} remaining</div>',
            unsafe_allow_html=True,
        )
    _render_run_picker(paper_id, run_id)
    if st.session_state.get("last_message"):
        st.info(st.session_state.last_message)
        st.session_state.last_message = None


def _run_label(run: dict) -> str:
    counts = run["counts"]
    parts = [f"{counts[k]} {k}" for k in ("ready", "unresolved", "error", "blocked") if counts.get(k)]
    return f"{run['label']} — {', '.join(parts) or 'no records'}"


def _render_run_picker(paper_id: str, run_id):
    """Which results set is under review. Every run of the paper is listed (the legacy flat layout too), so a failed
    latest run never hides earlier results; the default is `api_client.default_review_run`."""
    runs = api_client.list_result_runs(paper_id)
    if not runs:
        return
    ids = [r["run_id"] for r in runs]
    labels = {r["run_id"]: _run_label(r) for r in runs}
    chosen = st.selectbox(
        "Extraction run", options=ids, index=ids.index(run_id) if run_id in ids else 0,
        format_func=lambda rid: labels[rid], key=f"run_picker_{paper_id}",
    )
    if chosen != run_id:
        state.set_review_run(chosen)
        st.rerun()
    current = next(r for r in runs if r["run_id"] == chosen)
    if not current["reviewable"]:
        st.warning("This run has no ready or unresolved records to review -- the records below show why it stopped.")
    elif not current["is_latest"] and any(r["is_latest"] for r in runs):
        st.caption("Showing an earlier run: the latest run has nothing to review, or you selected this one.")


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


# Bibliographic metadata is not what the review is about: it is auto-filled, low priority, and shown collapsed at the
# bottom rather than as the first thing a reviewer sees. (Future: look it up from the DOI -- docs/feature-requests.)
DEEMPHASIZED_ENTITY_TYPES = ("Citation",)


def _render_entity(paper_id: str, entity_type: str, data: dict, heading: bool = True):
    records = data.get(entity_type, [])
    if heading:
        st.markdown(f'<div class="entity-heading">{entity_type.upper()}</div>', unsafe_allow_html=True)
    if not records:
        st.caption("Not extracted yet.")
        return
    for record in records:
        record_row.render_record(paper_id, entity_type, record, data)


def _render_data_pane(paper_id: str):
    data = api_client.get_review_data(paper_id, st.session_state.review_run)
    with st.container(height=PANE_HEIGHT, key="data_pane", border=True):
        for entity_type in api_client.ENTITY_TYPES:
            if entity_type not in DEEMPHASIZED_ENTITY_TYPES:
                _render_entity(paper_id, entity_type, data)
        for entity_type in DEEMPHASIZED_ENTITY_TYPES:
            with st.expander(f"Paper metadata ({entity_type.lower()}, auto-filled)", expanded=False):
                _render_entity(paper_id, entity_type, data, heading=False)
