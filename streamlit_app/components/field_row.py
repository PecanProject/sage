"""One field of an expanded record, as a row of the review table:  result | source | actions.

    result   `key: value` in one font (styles.result-*), plus plain-language state notes (never EXT/UNR codes)
    source   the quoted evidence text and where it is (page, section); an eye icon shows it in the PDF pane
    actions  a checkbox icon approves; a pencil opens the edit pop-up (correct value, add note, relocate evidence,
             tell agent what to do)

Approving a field the extractor could not find records the existing `confirm_unresolved` action -- for the reviewer
it is the same click ("yes, the paper does not say"), and the corrections log keeps the distinction.
Fields with no provenance (pipeline-generated values such as Coverage row counts) are shown read-only.
Link fields (`id`, `*_id`, `*_ids`) are not rows at all: see api_client.is_link_field.
"""

from __future__ import annotations

import html

import streamlit as st

import api_client
import provenance_adapter
import state
import styles

# result | source | eye | approve | edit
COL_WIDTHS = [0.36, 0.38, 0.06, 0.10, 0.10]
NO_SOURCE_TEXT = "Pipeline-generated: no source text"


def _short(value, length: int = 220) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1] + "…"


def result_html(field_name: str, field: dict) -> str:
    """`key: value` as one line of one font, then the plain-language state notes (if any)."""
    value = field["effective_value"]
    empty = value in (None, "")
    shown = html.escape(_short(value)) if not empty else "—"
    parts = styles.field_state_parts(field)
    state_html = f'<div class="result-state">{html.escape(" · ".join(parts))}</div>' if parts else ""
    return (
        f'<div class="result-cell"><span class="result-key">{html.escape(field_name)}:</span> '
        f'<span class="result-value{" result-empty" if empty else ""}">{shown}</span></div>{state_html}'
    )


def where_text(source: dict | None) -> str:
    """'p.7 · Materials and methods' -- the page and the innermost section of the field's evidence (whatever is known)."""
    source = source or {}
    bits = []
    if source.get("page_number"):
        bits.append(f"p.{source['page_number']}")
    section_path = source.get("section_path") or []
    if section_path:
        bits.append(str(section_path[-1]))
    return " · ".join(bits)


def source_html(excerpt: str | None, source: dict | None) -> str:
    anchors = [l.get("block_anchor") for l in ((source or {}).get("locators") or []) if l.get("block_anchor")]
    if not anchors:
        return '<div class="source-where">No source recorded for this field</div>'
    quote = f'<div class="source-quote">“{html.escape(_short(excerpt, 150))}”</div>' if excerpt else (
        '<div class="source-where">Evidence text could not be resolved from content.md</div>')
    where = where_text(source)
    where_html = f'<div class="source-where" title="{html.escape(", ".join(anchors))}">{html.escape(where)}</div>' if where else ""
    return quote + where_html


def approve_action(field: dict) -> str:
    """The correction the approve icon records: for a field the extractor could not find, approving means "yes, the
    paper does not state this" -- the existing `confirm_unresolved` action -- otherwise a plain `approve`."""
    return "confirm_unresolved" if field.get("provenance_label") == "UNRESOLVED" else "approve"


def render_table_header():
    cols = st.columns(COL_WIDTHS)
    for col, label in zip(cols[:3], ("result", "source", "actions")):
        col.markdown(f'<div class="table-head">{label}</div>', unsafe_allow_html=True)
    for col in cols[3:]:
        col.markdown('<div class="table-head">&nbsp;</div>', unsafe_allow_html=True)


def render_field_detail(paper_id: str, entity_type: str, record_id: str, field_name: str, field: dict):
    widget_id = f"{entity_type}__{record_id}__{field_name}"
    editable = field["kind"] == "extracted_field"
    source = (field.get("source") or {}) if editable else {}
    locators = source.get("locators") or []

    excerpt = None
    for anchor in [l.get("block_anchor") for l in locators if l.get("block_anchor")]:
        excerpt = api_client.get_block_text(paper_id, anchor)
        if excerpt:
            break

    result_col, source_col, eye_col, approve_col, edit_col = st.columns(COL_WIDTHS)
    with result_col:
        st.markdown(result_html(field_name, field), unsafe_allow_html=True)
        _render_notes(field, editable)
    with source_col:
        if editable:
            st.markdown(source_html(excerpt, source), unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="source-where">{NO_SOURCE_TEXT}</div>', unsafe_allow_html=True)

    if not editable:
        return

    with eye_col, st.container(key=f"fld_eye_{widget_id}"):
        if st.button("", key=f"showpdf_{widget_id}", icon=":material/visibility:", help="View source in the PDF",
                     disabled=not locators):
            resolved = provenance_adapter.resolve_locators(paper_id, source)
            if resolved:
                state.set_active_locators(resolved)
                st.rerun()
            else:
                st.error("Could not resolve this source to a PDF page.")

    unresolved = field["provenance_label"] == "UNRESOLVED"
    approved = field["review_status"] in ("approved", "confirmed_unresolved")
    with approve_col, st.container(key=f"fld_ok_{widget_id}"):
        if st.button(
            "", key=f"approve_{widget_id}", icon=":material/check_box:", type="primary" if approved else "secondary",
            help="Approve — confirms the paper does not state this" if unresolved else "Approve this value",
        ):
            api_client.submit_correction(paper_id, entity_type, record_id, approve_action(field), field_name=field_name)
            state.set_message(f"Approved {entity_type}.{field_name}")
            st.rerun()

    with edit_col, st.container(key=f"fld_edit_{widget_id}"):
        with st.popover("", icon=":material/edit:", help="Edit: correct value, add note, relocate evidence, tell agent"):
            _render_edit_tabs(paper_id, entity_type, record_id, field_name, field, widget_id)


def _render_notes(field: dict, editable: bool):
    if field["effective_value"] != field["value"]:
        st.caption(f"Original extraction (immutable): {field['value']!r}")
    if not editable:
        return
    if field.get("unresolved_reason"):
        st.caption(f"⚠ {field['unresolved_reason']}")
    ai_val = field.get("ai_validation")
    if ai_val and ai_val.get("verdict") == "suspicious":
        st.caption("⚠ AI Validator flagged this field: " + "; ".join(i.get("concern", "") for i in ai_val.get("issues", [])))
    latest = field.get("latest_correction")
    if latest:
        issues = (latest.get("payload") or {}).get("revalidation_issues")
        if issues:
            st.caption("⚠ Last correction failed re-validation: " + "; ".join(i["message"] for i in issues))


def _render_edit_tabs(paper_id, entity_type, record_id, field_name, field, widget_id):
    correct_tab, note_tab, relocate_tab, agent_tab = st.tabs(["Correct value", "Add note", "Relocate evidence", "Tell agent"])

    with correct_tab:
        new_value = st.text_input(
            "New value", value="" if field["effective_value"] is None else str(field["effective_value"]),
            key=f"correctval_{widget_id}",
        )
        if st.button("Save correction", key=f"savecorrect_{widget_id}"):
            result = api_client.submit_correction(
                paper_id, entity_type, record_id, "correct_value", field_name=field_name, payload={"new_value": new_value},
            )
            issues = result["payload"].get("revalidation_issues") or []
            state.set_message(
                "Correction saved but FAILED re-validation: " + "; ".join(i["message"] for i in issues)
                if issues else "Correction saved and re-validated against its source."
            )
            st.rerun()

    with note_tab:
        note = st.text_area("Note", key=f"note_{widget_id}", label_visibility="collapsed", placeholder="Your reasoning or a note…")
        if st.button("Save note", key=f"savenote_{widget_id}", disabled=not note.strip()):
            api_client.submit_correction(paper_id, entity_type, record_id, "note", field_name=field_name, payload={"note": note})
            state.set_message("Note saved.")
            st.rerun()

    with relocate_tab:
        st.caption("Enter the correct content.md block anchor (e.g. b:0032); it is resolved to its PDF page.")
        new_anchor = st.text_input("Block anchor", key=f"relocateanchor_{widget_id}")
        if new_anchor.strip():
            preview = provenance_adapter.resolve_anchor(paper_id, new_anchor.strip())
            st.caption(f"Resolves to PDF page {preview['page']}." if preview else "This anchor does not resolve to a known page/region.")
        if st.button("Save new evidence location", key=f"saverelocate_{widget_id}", disabled=not new_anchor.strip()):
            api_client.submit_correction(
                paper_id, entity_type, record_id, "relocate_evidence", field_name=field_name,
                payload={"new_locators": [{"kind": "text", "block_anchor": new_anchor.strip()}]},
            )
            state.set_message(f"Recorded new evidence location for {entity_type}.{field_name}")
            st.rerun()

    with agent_tab:
        st.caption("Say what the agent should do differently for this field. It is saved with the field for the next "
                   "extraction run; nothing is applied automatically yet.")
        instruction = st.text_area("Instruction", key=f"agentnote_{widget_id}", label_visibility="collapsed",
                                    placeholder="e.g. Take the year from the copyright line, not the submission date")
        if st.button("Save instruction", key=f"saveagent_{widget_id}", disabled=not instruction.strip()):
            api_client.submit_correction(
                paper_id, entity_type, record_id, "note", field_name=field_name,
                payload={"note": instruction, "audience": "agent"},
            )
            state.set_message("Instruction saved for the agent.")
            st.rerun()
