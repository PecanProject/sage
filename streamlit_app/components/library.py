"""
Paper library / landing page. Two sections, deliberately different in feel
(see styles.py's own comments for the CSS backing each):
"""

from __future__ import annotations

import html
import re
from typing import Optional

import streamlit as st
from streamlit import runtime

import api_client
import state

_SAFE_KEY_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _safe_key(raw: str) -> str:
    return _SAFE_KEY_RE.sub("_", raw).strip("_") or "paper"


def _reorder_by_match(rows: list[dict], query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return rows
    matches = [r for r in rows if q in r["paper_id"].lower()]
    non_matches = [r for r in rows if q not in r["paper_id"].lower()]
    return matches + non_matches


def _stored_status_label(row: dict) -> str:
    if row["extracted"]:
        return "Extracted — with errors" if row.get("has_errors") else "Extracted"
    if row["processed"]:
        return "Processed — not yet extracted"
    return "Not processed"


def _extracted_status_label(row: dict) -> str:
    return "Extracted — with errors" if row.get("has_errors") else "Extracted"


def render():
    _render_stored_papers(api_client.list_papers())

    if st.session_state.get("pipeline_run_result"):
        _render_run_result(st.session_state.pipeline_run_result)
    if st.session_state.get("last_message"):
        st.info(st.session_state.last_message)
        st.session_state.last_message = None

    st.markdown("---")
    _render_extracted_papers(api_client.list_extracted_papers())


# --------------------------------------------------------------------- #
# Stored Papers -- compact, hover-actionable file list
# --------------------------------------------------------------------- #


def _render_stored_papers(papers: list[dict]):
    col_head, col_search, col_upload = st.columns([0.32, 0.42, 0.26])
    with col_head:
        st.markdown("#### Stored Papers")
    with col_search:
        query = st.text_input(
            "Search stored papers", key="stored_search", placeholder="Search stored papers…",
            label_visibility="collapsed",
        )
    with col_upload:
        with st.popover("Upload a PDF", use_container_width=True):
            uploaded = st.file_uploader(
                "Upload PDF(s)", type=["pdf"], accept_multiple_files=True, key="stored_uploader",
            )
            if uploaded and st.button("Save uploaded PDF(s)", key="save_uploaded_pdfs"):
                saved = api_client.upload_pdfs([(f.name, f.getvalue()) for f in uploaded])
                state.set_message(f"Saved {len(saved)} PDF(s): {', '.join(saved)}")
                st.rerun()

    if not papers:
        st.caption("No PDFs uploaded yet. Upload one above to get started.")
        return

    ordered = _reorder_by_match(papers, query)
    with st.container(height=300, border=True):
        for row in ordered:
            _render_stored_row(row)


def _render_stored_row(row: dict):
    paper_id = row["paper_id"]
    safe_key = _safe_key(paper_id)
    with st.container(key=f"stored_row_{safe_key}"):
        cols = st.columns([0.50, 0.16, 0.11, 0.11, 0.12], vertical_alignment="center")
        with cols[0]:
            st.markdown(
                f'<div class="stored-row-name">{html.escape(paper_id)}.pdf'
                f'<span class="stored-row-status">{_stored_status_label(row)}</span></div>',
                unsafe_allow_html=True,
            )
        with cols[1]:
            if st.button("Extract", key=f"extract_{safe_key}", use_container_width=True):
                _run_pipeline_for(paper_id)
        with cols[2]:
            data_url = _pdf_data_url(paper_id)
            if data_url:
                st.link_button("📄", data_url, key=f"openrow_{safe_key}", use_container_width=True,
                                help="Open the uploaded PDF in a new browser tab")
            else:
                st.button("📄", key=f"openrow_{safe_key}", use_container_width=True, disabled=True,
                           help="No PDF found on disk for this paper.")
        with cols[3]:
            if st.button("✏️", key=f"renamerow_{safe_key}", use_container_width=True, help="Rename this stored paper"):
                _open_rename_stored_dialog(paper_id)
        with cols[4]:
            if st.button("🗑", key=f"deleterow_{safe_key}", use_container_width=True, help="Delete this stored PDF"):
                _open_delete_dialog(paper_id)


# --------------------------------------------------------------------- #
# Extracted Papers -- detailed processing/review table
# --------------------------------------------------------------------- #


def _render_extracted_papers(extracted_rows: list[dict]):
    col_head, col_search = st.columns([0.35, 0.65])
    with col_head:
        st.markdown("#### Extracted Papers")
    with col_search:
        query = st.text_input(
            "Search extracted papers", key="extracted_search", placeholder="Search extracted papers…",
            label_visibility="collapsed",
        )

    if not extracted_rows:
        st.caption("No papers extracted yet. Extract a stored paper above.")
        return

    ordered = _reorder_by_match(extracted_rows, query)
    with st.container(height=360, border=True):
        header = st.columns([0.22, 0.16, 0.38, 0.24])
        header[0].markdown("**Paper**")
        header[1].markdown("**Status**")
        header[2].markdown("**Review progress**")
        header[3].markdown("**Action**")
        for row in ordered:
            _render_extracted_row(row)


def _render_extracted_row(row: dict):
    paper_id = row["paper_id"]
    safe_key = _safe_key(paper_id)
    cols = st.columns([0.22, 0.16, 0.38, 0.24], vertical_alignment="center")
    cols[0].write(paper_id)
    cols[1].write(_extracted_status_label(row))
    if row["summary"]:
        s = row["summary"]
        cols[2].caption(
            f"{s['total_fields']} fields | {s['reviewed']} reviewed | "
            f"{s['unresolved']} unresolved | {s['blocked']} blocked | {s['remaining']} remaining"
        )
    else:
        cols[2].caption("—")
    with cols[3]:
        action_cols = st.columns([0.5, 0.25, 0.25])
        with action_cols[0]:
            if st.button("Review", key=f"review_{safe_key}", use_container_width=True):
                state.open_paper(paper_id)
                st.rerun()
        with action_cols[1]:
            if st.button(
                "✏️", key=f"rename_extracted_{safe_key}", use_container_width=True,
                help="Rename this paper's extracted records",
            ):
                _open_rename_extracted_dialog(paper_id)
        with action_cols[2]:
            if st.button(
                "🗑", key=f"delete_extracted_{safe_key}", use_container_width=True,
                help="Delete extracted records for this paper",
            ):
                _open_delete_records_dialog(paper_id)


# --------------------------------------------------------------------- #
# Shared: PDF preview, rename dialogs, delete confirmation, pipeline run
# --------------------------------------------------------------------- #


def _pdf_data_url(paper_id: str) -> Optional[str]:
    pdf_bytes = api_client.get_pdf_bytes(paper_id)
    if not pdf_bytes or not runtime.exists():
        return None
    return runtime.get_instance().media_file_mgr.add(
        pdf_bytes, "application/pdf", f"stored-pdf-open-{paper_id}", file_name=f"{paper_id}.pdf",
    )


@st.dialog("Rename stored paper")
def _open_rename_stored_dialog(paper_id: str):
    st.write(f"Rename the stored paper **{paper_id}**")
    st.caption(
        "Renames the PDF and, if present, its derived Marker output and rendered document, "
        "so already-done processing isn't orphaned. This is independent of the paper's "
        "extracted records (if any) — rename those separately from the Extracted Papers row."
    )
    new_name = st.text_input("New name", value=paper_id, key=f"rename_stored_input_{_safe_key(paper_id)}")
    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("Rename", type="primary", use_container_width=True):
            ok, message = api_client.rename_stored_paper(paper_id, new_name)
            state.set_message(message)
            if ok:
                st.rerun()
            else:
                st.error(message)
    with col_cancel:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


@st.dialog("Rename extracted records")
def _open_rename_extracted_dialog(paper_id: str):
    st.write(f"Rename the extracted records for **{paper_id}**")
    st.caption(
        "Renames the results/ snapshot and the ir-store commit history to the new name. "
        "This is independent of the stored PDF (if any) — rename that separately from the "
        "Stored Papers row."
    )
    new_name = st.text_input("New name", value=paper_id, key=f"rename_extracted_input_{_safe_key(paper_id)}")
    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("Rename", type="primary", use_container_width=True):
            ok, message = api_client.rename_extracted_paper(paper_id, new_name)
            state.set_message(message)
            if ok:
                st.rerun()
            else:
                st.error(message)
    with col_cancel:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


@st.dialog("Delete paper")
def _open_delete_dialog(paper_id: str):
    st.write(f"Remove the uploaded PDF for **{paper_id}** from the library?")
    st.caption(
        "Only the PDF is removed. Marker output, the rendered document, and any extraction "
        "already committed for this paper are kept — if this paper_id is uploaded again later, "
        "Extract always reprocesses it fresh from the new PDF rather than reusing old output."
    )
    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("Delete", type="primary", use_container_width=True):
            removed = api_client.delete_paper(paper_id)
            state.set_message(
                f"Removed {len(removed)} item(s) for '{paper_id}'." if removed
                else f"Nothing on disk for '{paper_id}'."
            )
            st.rerun()
    with col_cancel:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


@st.dialog("Delete extracted records")
def _open_delete_records_dialog(paper_id: str):
    st.write(f"Delete the extracted records for **{paper_id}**?")
    st.caption(
        "This clears the reviewable results for this paper, so it stops showing as Extracted "
        "and can be re-run from scratch. The uploaded PDF, Marker output, and rendered document "
        "are kept, and the paper's commit history in ir-store is preserved — this only clears "
        "the current derived snapshot, never the permanent extraction ledger."
    )
    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("Delete records", type="primary", use_container_width=True):
            removed = api_client.delete_extracted_records(paper_id)
            state.set_message(
                f"Removed extracted records for '{paper_id}'." if removed
                else f"No extracted records found for '{paper_id}'."
            )
            st.rerun()
    with col_cancel:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


def _run_pipeline_for(paper_id: str):
    stages: list[dict] = []
    had_error = False
    had_warnings = False
    with st.status(f"Running pipeline for {paper_id}...", expanded=True) as status_box:
        # A real paper can produce 50-90+ progress events -- writing each
        # one straight into the status box (no fixed height) made it grow
        # without bound, pushing further down the page every few seconds
        # and never staying in one place to scroll back to. A fixed-height,
        # internally-scrollable container keeps this box's position and
        # size on the page completely stable for the whole run; only the
        # log inside it scrolls, same as the review-progress panels
        # elsewhere on this page (see the height=300/360 containers above).
        log = st.container(height=280, border=False)
        for event in api_client.run_pipeline_for_paper(paper_id):
            stages.append(event)
            with log:
                st.write(event["message"])
                if event["status"] == "error":
                    st.error(f"Failed at stage `{event['stage']}`: {event['message']}")
                if event["status"] == "done_with_errors":
                    st.warning(event["message"])
            if event["status"] == "error":
                had_error = True
                status_box.update(label=f"Failed at stage: {event['stage']}", state="error")
                break
            if event["status"] == "done_with_errors":
                had_warnings = True
        if not had_error:
            label = "Complete." if not had_warnings else "Complete, with some errors — see below."
            status_box.update(label=label, state="complete")

    st.session_state.pipeline_run_result = {
        "ok": not had_error, "had_warnings": had_warnings, "paper_id": paper_id, "stages": stages,
    }
    if had_error:
        message = f"Pipeline failed for {paper_id} — see details below."
    elif had_warnings:
        message = f"Pipeline finished for {paper_id}, with some errors — see details below."
    else:
        message = f"Pipeline finished for {paper_id}."
    state.set_message(message)
    st.rerun()


def _render_run_result(result: dict):
    with st.expander(
        f"Last pipeline run details ({result['paper_id']})",
        expanded=not result["ok"] or result.get("had_warnings", False),
    ):
        for event in result["stages"]:
            marker = (
                "✅" if event["status"] == "done"
                else "⚠️" if event["status"] == "done_with_errors"
                else "❌" if event["status"] == "error"
                else "…"
            )
            st.caption(f"{marker} [{event['stage']}] {event['message']}")
            if event["status"] in ("error", "done_with_errors") and event.get("detail"):
                with st.expander("Raw details (advanced)", expanded=False):
                    st.json(event["detail"])
