from __future__ import annotations

import html

import streamlit as st

import state
import styles
from components import field_row


def _safe_key(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text)


def _val(record: dict, field_name: str):
    field = record.get("fields", {}).get(field_name)
    return field.get("effective_value") if field else None


def _text(record: dict, field_name: str):
    value = _val(record, field_name)
    return value if isinstance(value, str) and value.strip() else None


def _citation_summary(r: dict, all_data: dict) -> str | None:
    author = _text(r, "author") or "Unknown author"
    year = _val(r, "year")
    title = _text(r, "title") or ""
    title_short = title if len(title) <= 80 else title[:79] + "…"
    year_part = f"({year}) " if year else ""
    return f"{author} {year_part}— {title_short}".strip() if title_short else f"{author} {year_part}".strip()


def _study_summary(r: dict, all_data: dict) -> str | None:
    return _text(r, "id")


def _site_summary(r: dict, all_data: dict) -> str | None:
    name = _text(r, "name")
    city = _text(r, "nearest_city")
    country = _text(r, "country")
    loc = ", ".join(x for x in (city, country) if x)
    if name and loc:
        return f"{name} · {loc}"
    return name or loc or None


def _species_summary(r: dict, all_data: dict) -> str | None:
    sci = _text(r, "scientific_name")
    common = _text(r, "common_name")
    if sci and common:
        return f"{sci} · {common}"
    return sci or common


def _method_summary(r: dict, all_data: dict) -> str | None:
    return _text(r, "name")


def _treatment_summary(r: dict, all_data: dict) -> str | None:
    return _text(r, "name") or _text(r, "definition")


def _management_summary(r: dict, all_data: dict) -> str | None:
    event = _text(r, "event_type")
    date_val = _val(r, "date")
    date_text = date_val.get("reported_text") if isinstance(date_val, dict) else None
    if event and date_text:
        return f"{event} · {date_text}"
    return event or date_text


def _observation_summary(r: dict, all_data: dict) -> str | None:
    var = _text(r, "variable_name")
    value_field = r.get("fields", {}).get("value") or {}
    value_val = value_field.get("effective_value")
    if isinstance(value_val, dict) and value_val.get("reported_text"):
        return f"{var} · {value_val['reported_text']}" if var else value_val["reported_text"]
    unresolved = value_field.get("provenance_label") == "UNRESOLVED" or value_val is None
    if unresolved:
        return f"{var} · value unresolved" if var else "value unresolved"
    return var


def _variable_summary(r: dict, all_data: dict) -> str | None:
    name = _text(r, "name")
    units = _text(r, "units")
    if name and units:
        return f"{name} · {units}"
    return name


def _treatmentpair_summary(r: dict, all_data: dict) -> str | None:
    label = _text(r, "comparison_label")
    if label:
        return label
    fields = r.get("fields", {})
    t1 = (fields.get("treatment_id_1") or {}).get("effective_value")
    t2 = (fields.get("treatment_id_2") or {}).get("effective_value")
    names = {
        tr["record_id"]: (_text(tr, "name") or tr["record_id"])
        for tr in (all_data or {}).get("Treatment", [])
    }
    n1, n2 = names.get(t1, t1), names.get(t2, t2)
    if n1 and n2:
        return f"{n1} ↔ {n2}"
    return None


def _crop_summary(r: dict, all_data: dict) -> str | None:
    return _text(r, "cultivar") or _text(r, "common_name")


def _coverage_summary(r: dict, all_data: dict) -> str | None:
    system = _text(r, "system")
    for rows_field, label in (
        ("daily_rows", "daily"), ("annual_rows", "annual"),
        ("seasonal_rows", "seasonal"), ("static_observation_rows", "static obs."),
        ("weather_rows", "weather"),
    ):
        rows = _val(r, rows_field)
        if rows:
            return f"{system + ' · ' if system else ''}{rows} {label} rows"
    return system


_SUMMARY_FUNCS = {
    "Citation": _citation_summary,
    "Study": _study_summary,
    "Site": _site_summary,
    "Species": _species_summary,
    "Crop": _crop_summary,
    "Method": _method_summary,
    "Treatment": _treatment_summary,
    "TreatmentPair": _treatmentpair_summary,
    "Variable": _variable_summary,
    "Management": _management_summary,
    "Observation": _observation_summary,
    "Coverage": _coverage_summary,
}


def summarize(entity_type: str, record: dict, all_data: dict) -> str:
    func = _SUMMARY_FUNCS.get(entity_type)
    text = None
    if func:
        try:
            text = func(record, all_data)
        except Exception:
            text = None
    return text or record.get("record_id") or "—"



def render_record(paper_id: str, entity_type: str, record: dict, all_data: dict):
    record_id = record["record_id"]
    key = (entity_type, record_id)
    is_expanded = state.is_record_expanded(key)
    widget_id = f"{entity_type}__{_safe_key(record_id)}"

    if record["status"] in ("blocked", "error"):
        summary = record.get("reason") or record["status"]
    else:
        summary = summarize(entity_type, record, all_data)
    summary_html = html.escape(summary)
    status_html = styles.compact_badge(record["status"])

    row_cols = st.columns([0.9, 0.1])
    with row_cols[0]:
        st.markdown(
            f'<div class="record-row-grid">'
            f'<div class="record-row-type">{html.escape(entity_type)}</div>'
            f'<div class="record-row-summary{" expanded" if is_expanded else ""}">{summary_html}</div>'
            f'<div>{status_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with row_cols[1]:
        with st.container(key=f"rectoggle_{widget_id}"):
            if st.button("▾" if not is_expanded else "▴", key=f"toggle_{widget_id}"):
                state.toggle_expanded_record(key)
                st.rerun()

    if not is_expanded:
        return

    with st.container(border=True):
        if record["status"] in ("blocked", "error"):
            st.caption(f"`{record_id}` — {record['status']}: {record.get('reason') or 'no reason recorded'}")
            return
        if not record["fields"]:
            st.caption(f"`{record_id}` — no fields recorded.")
            return
        for field_name, field in record["fields"].items():
            field_row.render_field_detail(paper_id, entity_type, record_id, field_name, field)
            st.markdown('<hr style="margin:0.35rem 0;border-color:#f0f1f3;">', unsafe_allow_html=True)
