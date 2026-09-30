
from __future__ import annotations

import html

import streamlit as st

import api_client
import provenance_adapter
import state
import styles


def _short(value, length: int = 220) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1] + "…"


def _wbr_name(field_name: str) -> str:
    return html.escape(field_name).replace("_", "_<wbr>")


def render_field_detail(paper_id: str, entity_type: str, record_id: str, field_name: str, field: dict):
    widget_id = f"{entity_type}__{record_id}__{field_name}"

    if field["kind"] == "extracted_field":
        badge_text = field["review_status"] if field["review_status"] != "pending" else (field["provenance_label"] or "?")
        status_html = styles.compact_badge(badge_text)
    else:
        status_html = styles.compact_badge("reference")

    value_text = html.escape(_short(field["effective_value"]) or "—")
    conf = field.get("confidence")
    conf_html = f'<span class="field-detail-conf">{conf}%</span>' if conf is not None else ""

    st.markdown(
        f'<div class="field-detail-row">'
        f'<div class="field-detail-name">{_wbr_name(field_name)}</div>'
        f'<div class="field-detail-value">{value_text}</div>'
        f'<div class="field-detail-status">{status_html}{conf_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if field["effective_value"] != field["value"]:
        st.caption(f"Original extraction (immutable): {field['value']!r}")

    if field["kind"] != "extracted_field":
        render_reference_field_actions(paper_id, entity_type, record_id, field_name, field)
        return

    if field.get("unresolved_reason"):
        st.caption(f"⚠ {field['unresolved_reason']}")

    ai_val = field.get("ai_validation")
    if ai_val and ai_val.get("verdict") == "suspicious":
        st.caption("⚠ AI Validator flagged this field: " + "; ".join(
            i.get("concern", "") for i in ai_val.get("issues", [])
        ))

    _render_evidence(paper_id, entity_type, record_id, field_name, field, widget_id)

    latest_correction = field.get("latest_correction")
    if latest_correction:
        issues = (latest_correction.get("payload") or {}).get("revalidation_issues")
        if issues:
            st.caption("⚠ Last correction failed re-validation: " + "; ".join(i["message"] for i in issues))

    _render_review_actions(paper_id, entity_type, record_id, field_name, field, widget_id)


def _render_evidence(paper_id, entity_type, record_id, field_name, field, widget_id):
    source = field.get("source") or {}
    locators = source.get("locators") or []
    if not locators:
        st.caption("No source locator recorded for this field.")
        return

    anchors = [l.get("block_anchor") for l in locators if l.get("block_anchor")]
    excerpt = None
    for anchor in anchors:
        excerpt = api_client.get_block_text(paper_id, anchor)
        if excerpt:
            break

    cols = st.columns([0.82, 0.18])
    with cols[0]:
        if excerpt:
            st.markdown(f'<div class="evidence-excerpt">"{html.escape(_short(excerpt, 320))}"</div>', unsafe_allow_html=True)
        else:
            st.caption("Evidence text could not be resolved from content.md.")
        section = ", ".join(source.get("section_path") or []) or None
        anchor_list = ", ".join(anchors)
        st.caption(f"{section + ' · ' if section else ''}block {anchor_list}")
    with cols[1]:
        if st.button("Show in PDF", key=f"showpdf_{widget_id}", use_container_width=True):
            resolved = provenance_adapter.resolve_locators(paper_id, source)
            if resolved:
                state.set_active_locators(resolved)
                st.rerun()
            else:
                st.error("Could not resolve this source to a PDF page.")


def _render_review_actions(paper_id, entity_type, record_id, field_name, field, widget_id):
    action_cols = st.columns(3)
    if action_cols[0].button("Approve", key=f"approve_{widget_id}"):
        api_client.submit_correction(paper_id, entity_type, record_id, "approve", field_name=field_name)
        state.set_message(f"Approved {entity_type}.{field_name}")
        st.rerun()

    if action_cols[1].button("Confirm unresolved", key=f"confirmunresolved_{widget_id}",
                              disabled=field["provenance_label"] != "UNRESOLVED"):
        api_client.submit_correction(paper_id, entity_type, record_id, "confirm_unresolved", field_name=field_name)
        state.set_message(f"Confirmed {entity_type}.{field_name} as unresolved")
        st.rerun()

    with action_cols[2].popover("More actions"):
        with st.expander("Correct value"):
            new_value = st.text_input(
                "New value", value="" if field["effective_value"] is None else str(field["effective_value"]),
                key=f"correctval_{widget_id}",
            )
            if st.button("Save correction", key=f"savecorrect_{widget_id}"):
                result = api_client.submit_correction(
                    paper_id, entity_type, record_id, "correct_value",
                    field_name=field_name, payload={"new_value": new_value},
                )
                issues = result["payload"].get("revalidation_issues") or []
                state.set_message(
                    "Correction saved but FAILED re-validation: " + "; ".join(i["message"] for i in issues)
                    if issues else "Correction saved and re-validated against its source."
                )
                st.rerun()

        with st.expander("Relocate evidence"):
            st.caption(
                "Enter the correct content.md block anchor (e.g. b:0032). "
                "v1 uses the anchor id directly rather than a click-to-select PDF region."
            )
            new_anchor = st.text_input("Block anchor", key=f"relocateanchor_{widget_id}")
            if new_anchor.strip():
                preview = provenance_adapter.resolve_anchor(paper_id, new_anchor.strip())
                st.caption(f"Resolves to PDF page {preview['page']}." if preview
                           else "This anchor does not resolve to a known page/region.")
            if st.button("Save new evidence location", key=f"saverelocate_{widget_id}", disabled=not new_anchor.strip()):
                api_client.submit_correction(
                    paper_id, entity_type, record_id, "relocate_evidence", field_name=field_name,
                    payload={"new_locators": [{"kind": "text", "block_anchor": new_anchor.strip()}]},
                )
                state.set_message(f"Recorded new evidence location for {entity_type}.{field_name}")
                st.rerun()

        with st.expander("Add note / reasoning"):
            note = st.text_area("Note", key=f"note_{widget_id}", label_visibility="collapsed",
                                 placeholder="Scientist reasoning or note...")
            if st.button("Save note", key=f"savenote_{widget_id}", disabled=not note.strip()):
                api_client.submit_correction(paper_id, entity_type, record_id, "note", field_name=field_name, payload={"note": note})
                state.set_message("Note saved.")
                st.rerun()


def render_reference_field_actions(paper_id: str, entity_type: str, record_id: str, field_name: str, field: dict):
    widget_id = f"{entity_type}__{record_id}__{field_name}"
    action_cols = st.columns(2)
    if action_cols[0].button("Approve", key=f"approve_{widget_id}"):
        api_client.submit_correction(paper_id, entity_type, record_id, "approve", field_name=field_name)
        state.set_message(f"Approved {entity_type}.{field_name}")
        st.rerun()

    with action_cols[1].popover("Relink"):
        target_type = st.selectbox("Entity type", api_client.ENTITY_TYPES, key=f"relinktype_{widget_id}")
        candidates = api_client.list_record_ids(paper_id, target_type)
        target_id = st.selectbox("Target record", candidates, key=f"relinktarget_{widget_id}") if candidates else None
        if not candidates:
            st.caption(f"No ready {target_type} records exist yet for this paper.")
        if st.button("Save relink", key=f"saverelink_{widget_id}", disabled=not target_id):
            api_client.submit_correction(
                paper_id, entity_type, record_id, "relink_entity", field_name=field_name,
                payload={"new_target_id": target_id, "target_entity_type": target_type},
            )
            state.set_message(f"Relinked {entity_type}.{field_name} -> {target_type}:{target_id}")
            st.rerun()
