"""The review table: result | source | actions.

- `key: value` is one result in one font; source is the quoted evidence with an eye icon; actions are icons.
- EXT/UNR/REF codes are gone: state is said in words, and only when it is not the ordinary case.
- `confirm unresolved` is not a separate button: approving a not-found field records it.
- id and link fields (`id`, `*_id`, `*_ids`) are not shown, not approvable, and not counted toward progress.
- Citation is de-emphasised: collapsed, at the bottom.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import api_client
import provenance_adapter
import styles
from components import field_row, pdf_viewer

PAPER = "paper_x"


def _source(anchor="b:0001", page=3, section=("Materials and methods",)):
    return {"source_document_id": PAPER, "page_number": page, "section_path": list(section),
            "locators": [{"kind": "text", "block_anchor": anchor}]}


def _extracted(value, label="EXTRACTED", review="pending", reason=None, source=True, confidence=None):
    return {"kind": "extracted_field", "value": value, "effective_value": value, "provenance_label": label,
            "source": _source() if source else None, "confidence": confidence, "unresolved_reason": reason,
            "review_status": review, "latest_correction": None}


def _bare(value):
    return {"kind": "reference", "value": value, "effective_value": value, "provenance_label": None, "source": None,
            "confidence": None, "unresolved_reason": None, "review_status": "pending", "latest_correction": None}


# --------------------------------------------------------------------- #
# which fields a reviewer sees
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("name, expected", [
    ("id", True), ("citation_id", True), ("site_id", True), ("treatment_id", True), ("method_id", True), ("dataset_id", True),
    ("treatment_ids", True), ("citation_ids", True),
    ("name", False), ("identifier", False), ("persistent_identifier", False), ("variable_name", False), ("valid", False),
])
def test_link_fields_are_exactly_id_and_the_id_suffixes(name, expected):
    assert api_client.is_link_field(name) is expected


def test_reviewable_fields_drop_links_and_empty_plain_fields_but_keep_everything_reviewable():
    record = {"fields": {
        "id": _bare("p_treatment_a"), "citation_id": _bare("p"), "site_id": _extracted("s"),          # links: never shown
        "name": _extracted("Fallow"),
        "definition": _extracted(None, label="UNRESOLVED", reason="not stated"),                     # not found: still reviewable
        "latitude": _bare(None), "notes": _bare(""),                                                  # empty plain fields
        "weather_rows": _bare(365),                                                                   # a plain value: shown read-only
    }}
    assert list(api_client.reviewable_fields(record)) == ["name", "definition", "weather_rows"]


def test_review_progress_does_not_count_link_fields(monkeypatch):
    data = {t: [] for t in api_client.ENTITY_TYPES}
    data["Treatment"] = [{"record_id": "t1", "status": "ready", "run_id": "r", "ai_validation": None, "reason": None,
                          "fields": {"id": _bare("t1"), "site_id": _extracted("s", review="pending"),
                                     "name": _extracted("Fallow", review="approved"), "definition": _extracted("x")}}]
    monkeypatch.setattr(api_client, "get_review_data", lambda paper_id, run_id=None: data)
    summary = api_client.review_summary(PAPER)
    assert (summary["total_fields"], summary["reviewed"], summary["remaining"]) == (2, 1, 1)   # name + definition only


# --------------------------------------------------------------------- #
# words, not codes
# --------------------------------------------------------------------- #


def test_field_state_is_said_in_words_and_only_when_not_ordinary():
    assert styles.field_state_parts(_extracted("x")) == []
    assert styles.field_state_parts(_extracted(None, label="UNRESOLVED")) == ["Not found in paper"]
    assert styles.field_state_parts(_extracted("x", label="INFERRED")) == ["Inferred"]
    assert styles.field_state_parts(_extracted("x", review="approved")) == ["Approved"]
    assert styles.field_state_parts(_extracted("x", review="corrected")) == ["Edited"]
    assert styles.field_state_parts(_extracted(None, label="UNRESOLVED", review="confirmed_unresolved")) == ["Not found in paper", "Approved"]
    assert styles.field_state_parts(_extracted("x", confidence=95)) == ["confidence 95%"]


def test_record_status_uses_words_never_the_compact_codes():
    assert [styles.plain_status(s) for s in ("ready", "unresolved", "blocked", "error", "not_extracted")] == [
        "Extracted", "Unresolved", "Blocked", "Error", "Not extracted"]
    for status in ("ready", "unresolved", "blocked", "error"):
        badge = styles.plain_badge(status)
        assert not any(f">{code}<" in badge for code in ("EXT", "UNR", "BLK", "ERR", "REF"))


def test_result_is_key_and_value_in_one_line_and_escapes_html():
    html_out = field_row.result_html("author", _extracted("Smith <b>& Jones"))
    assert html_out.count("result-cell") == 1 and 'result-key">author:</span>' in html_out
    assert "Smith &lt;b&gt;&amp; Jones" in html_out and "<b>" not in html_out
    empty = field_row.result_html("journal", _extracted(None, label="UNRESOLVED"))
    assert "result-empty" in empty and "—" in empty and "Not found in paper" in empty


def test_source_shows_the_quote_and_where_it_is_and_says_so_when_there_is_none():
    out = field_row.source_html("Fallow plots were not tilled.", _source(anchor="b:0032", page=7, section=("Methods", "Site")))
    assert "“Fallow plots were not tilled.”" in out and "p.7 · Site" in out and 'title="b:0032"' in out
    assert "No source recorded" in field_row.source_html(None, None)
    assert "could not be resolved" in field_row.source_html(None, _source())
    assert field_row.where_text({}) == "" and field_row.where_text({"page_number": 2}) == "p.2"


def test_approving_a_not_found_field_records_confirm_unresolved_and_anything_else_a_plain_approve():
    assert field_row.approve_action(_extracted(None, label="UNRESOLVED")) == "confirm_unresolved"
    assert field_row.approve_action(_extracted("x")) == "approve"
    assert field_row.approve_action(_extracted("x", label="INFERRED")) == "approve"


# --------------------------------------------------------------------- #
# the page, rendered
# --------------------------------------------------------------------- #


@pytest.fixture()
def page(monkeypatch):
    data = {t: [] for t in api_client.ENTITY_TYPES}
    data["Treatment"] = [{"record_id": "T1", "status": "ready", "run_id": "r", "ai_validation": None, "reason": None, "fields": {
        "id": _bare("T1"), "citation_id": _bare("paper_x"), "site_id": _bare("S1"),
        "name": _extracted("Fallow"),
        "definition": _extracted(None, label="UNRESOLVED", reason="The paper gives no definition."),
    }}]
    data["Citation"] = [{"record_id": "C1", "status": "ready", "run_id": "r", "ai_validation": None, "reason": None, "fields": {
        "id": _bare("C1"), "title": _extracted("A Paper About Tomatoes"),
    }}]
    data["Coverage"] = [{"record_id": "V1", "status": "ready", "run_id": "r", "ai_validation": None, "reason": None, "fields": {
        "id": _bare("V1"), "notes": _bare("generated"), "weather_rows": _bare(None),
    }}]
    calls: list[tuple] = []
    monkeypatch.setattr(api_client, "get_review_data", lambda paper_id, run_id=None: data)
    monkeypatch.setattr(api_client, "default_review_run", lambda paper_id: "r")
    monkeypatch.setattr(api_client, "list_result_runs", lambda paper_id: [
        {"run_id": "r", "label": "r (latest)", "counts": {"ready": 3}, "reviewable": True, "is_latest": True},
    ])
    monkeypatch.setattr(api_client, "get_pdf_bytes", lambda paper_id: None)
    monkeypatch.setattr(api_client, "get_block_text", lambda paper_id, anchor: "Fallow plots were left untilled.")
    monkeypatch.setattr(api_client, "submit_correction", lambda *a, **k: calls.append((a, k)) or {"payload": {}})
    monkeypatch.setattr(provenance_adapter, "resolve_locators", lambda paper_id, source: [{"page": 3, "polygon": None, "block_anchor": "b:0001"}])
    monkeypatch.setattr(pdf_viewer, "render_pdf_page", lambda **kwargs: None)

    def script():
        import state
        import styles
        from components import workspace
        styles.inject_base_css()
        state.init_state()
        workspace.render("paper_x")

    at = AppTest.from_function(script, default_timeout=60)
    at.session_state["nav"] = "review"
    at.session_state["paper_id"] = PAPER
    at.session_state["expanded_records"] = {("Treatment", "T1"), ("Citation", "C1"), ("Coverage", "V1")}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at, calls


def _all_markdown(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_the_expanded_record_is_a_result_source_actions_table_without_id_rows(page):
    at, _ = page
    text = _all_markdown(at)
    assert 'table-head">result<' in text and 'table-head">source<' in text and 'table-head">actions<' in text
    keys = set(__import__("re").findall(r'result-key">([^:<]+):', text))
    assert {"name", "definition", "title", "notes"} <= keys
    assert not {"id", "citation_id", "site_id"} & keys and "weather_rows" not in keys       # links and empty plain fields are gone
    assert "“Fallow plots were left untilled.”" in text and "p.3 · Materials and methods" in text
    assert "The paper gives no definition." in " ".join(c.value for c in at.caption)
    assert not any(f">{code}<" in text for code in ("EXT", "UNR", "REF", "BLK", "ERR"))


def test_every_reviewable_field_has_visible_icon_actions_and_no_relink_or_confirm_button(page):
    at, _ = page
    keys = [b.key for b in at.button if b.key]
    for prefix in ("showpdf_", "approve_"):
        assert sum(k.startswith(prefix) for k in keys) == 3                              # name, definition, title (`notes` is read-only)
    assert not any("confirmunresolved" in k or "relink" in k for k in keys)
    assert len(at.get("popover")) == 3                                                    # one edit pop-up per editable field
    assert [t.label for t in at.tabs[:4]] == ["Correct value", "Add note", "Relocate evidence", "Tell agent"]


def test_approve_records_confirm_unresolved_for_a_not_found_field_and_approve_otherwise(page):
    at, calls = page
    at.button(key="approve_Treatment__T1__definition").click().run()
    assert calls[-1][0][3] == "confirm_unresolved" and calls[-1][1]["field_name"] == "definition"
    at.button(key="approve_Treatment__T1__name").click().run()
    assert calls[-1][0][3] == "approve" and calls[-1][1]["field_name"] == "name"
    assert all(k["run_id"] == "r" for _, k in calls)   # every review action belongs to the run under review


def test_the_eye_icon_sends_the_field_source_to_the_pdf_pane(page):
    at, _ = page
    assert not at.session_state["active_locators"]
    at.button(key="showpdf_Treatment__T1__name").click().run()
    assert at.session_state["active_locators"][0]["page"] == 3


def test_citation_is_collapsed_at_the_bottom_not_a_main_section(page):
    at, _ = page
    assert [e.label for e in at.expander] == ["Paper metadata (citation, auto-filled)"]
    assert "A Paper About Tomatoes" in "\n".join(m.value for m in at.expander[0].markdown)
    assert 'entity-heading">CITATION<' not in _all_markdown(at)
    assert _all_markdown(at).index("TREATMENT") < _all_markdown(at).index("A Paper About Tomatoes")   # main flow first, metadata after
