"""Phase 1 deterministic provenance validator tests."""
from pathlib import Path
import pipeline.validators as validators


def _payload(anchor: str):
    return {
        "year": {
            "value": 1997,
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "test-paper",
                "page_number": 1,
                "locators": [{"kind": "text", "block_anchor": anchor}],
            },
        }
    }


def test_year_1997_supported_by_b0010(tmp_path: Path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Published in 1997. ⟦b:0010⟧\nThis block has other metadata. ⟦b:0011⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    assert validators.validate_provenance("test-paper", _payload("b:0010")) == []


def test_year_1997_rejected_for_b0011(tmp_path: Path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Published in 1997.\n⟦b:0010⟧\nThis block has other metadata.\n⟦b:0011⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    issues = validators.validate_provenance("test-paper", _payload("b:0011"))
    assert len(issues) == 1
    assert issues[0].code == "provenance_value_mismatch"
    assert "year" in issues[0].message
    assert "b:0011" in issues[0].message


def test_year_1997_rejected_for_missing_anchor(tmp_path: Path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Published in 1997.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    issues = validators.validate_provenance("test-paper", _payload("b:9999"))
    assert len(issues) == 1
    assert issues[0].code == "provenance_anchor_not_found"
    assert "b:9999" in issues[0].message


# --------------------------------------------------------------------- #
# Regression: normal string/numeric provenance validation is unaffected
# by the boolean redesign below.
# --------------------------------------------------------------------- #

def _string_payload(anchor: str, value: str = "Yolo County"):
    return {
        "name": {
            "value": value,
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "test-paper", "page_number": 1,
                "locators": [{"kind": "text", "block_anchor": anchor}],
            },
        }
    }


def _float_payload(anchor: str, value: float = 3.2):
    return {
        "amount": {
            "value": value,
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "test-paper", "page_number": 1,
                "locators": [{"kind": "text", "block_anchor": anchor}],
            },
        }
    }


def test_string_value_still_requires_literal_text_match(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "The site is in Yolo County.\n⟦b:0010⟧\nSome unrelated text.\n⟦b:0011⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    assert validators.validate_provenance("test-paper", _string_payload("b:0010")) == []
    issues = validators.validate_provenance("test-paper", _string_payload("b:0011"))
    assert len(issues) == 1
    assert issues[0].code == "provenance_value_mismatch"


def test_float_value_still_requires_literal_numeric_match(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Yield was 3.2 t/ha.\n⟦b:0010⟧\nSome unrelated text.\n⟦b:0011⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    assert validators.validate_provenance("test-paper", _float_payload("b:0010")) == []
    issues = validators.validate_provenance("test-paper", _float_payload("b:0011"))
    assert len(issues) == 1
    assert issues[0].code == "provenance_value_mismatch"


# --------------------------------------------------------------------- #
# Boolean ExtractedField provenance -- Phase 2 redesign.
#
# Real ExtractedField[bool] uses in the schema: Treatment.control_status,
# TreatmentPair.use_for_validation, Observation.is_raw_replicate_level.
# None of these are ever stated in source text as the literal word
# "true"/"false" -- the old literal-word regex made all three structurally
# unable to pass EXTRACTED/INFERRED validation (confirmed against real
# Oceologia-1998/pecan runs). The redesign must never require that literal
# word, must still require a real, existing, non-empty anchor, and must
# still require a genuine inference-basis note for INFERRED booleans.
# --------------------------------------------------------------------- #

def _bool_payload(anchor: str, value: bool, label: str = "EXTRACTED", unresolved_reason=None):
    field = {
        "value": value,
        "provenance_label": label,
        "source": {
            "source_document_id": "test-paper", "page_number": 1,
            "locators": [{"kind": "text", "block_anchor": anchor}],
        },
    }
    if unresolved_reason is not None:
        field["unresolved_reason"] = unresolved_reason
    return {"is_raw_replicate_level": field}


def test_extracted_true_passes_without_literal_true_in_text(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Individual plot values are reported for each replicate.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    assert validators.validate_provenance("test-paper", _bool_payload("b:0010", True)) == []


def test_extracted_false_passes_without_literal_false_in_text(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Values shown are means across all replicate plots.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    assert validators.validate_provenance("test-paper", _bool_payload("b:0010", False)) == []


def test_boolean_anchor_must_still_exist(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text("Some text.\n⟦b:0010⟧\n", encoding="utf-8")
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    issues = validators.validate_provenance("test-paper", _bool_payload("b:9999", True))
    assert len(issues) == 1
    assert issues[0].code == "provenance_anchor_not_found"


def test_boolean_anchor_resolving_to_empty_text_is_rejected(tmp_path, monkeypatch):
    # An anchor that exists but marks an effectively-empty block still
    # carries no real evidence a reviewer could judge the claim against.
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text("⟦b:0010⟧\nReal text.\n⟦b:0011⟧\n", encoding="utf-8")
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    issues = validators.validate_provenance("test-paper", _bool_payload("b:0010", True))
    assert len(issues) == 1
    assert issues[0].code == "provenance_value_mismatch"


def test_inferred_boolean_with_real_reason_passes(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Data were aggregated across all plots before reporting.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    payload = _bool_payload(
        "b:0010", False, label="INFERRED",
        unresolved_reason="Text states values were aggregated across plots, implying this is not raw replicate-level data.",
    )
    assert validators.validate_provenance("test-paper", payload) == []


def test_inferred_boolean_without_reason_is_rejected(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Data were aggregated across all plots before reporting.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    payload = _bool_payload("b:0010", False, label="INFERRED", unresolved_reason="")
    issues = validators.validate_provenance("test-paper", payload)
    assert len(issues) == 1
    assert issues[0].code == "boolean_inference_basis_missing"


def test_inferred_boolean_with_whitespace_only_reason_is_rejected(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Data were aggregated across all plots before reporting.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    payload = _bool_payload("b:0010", False, label="INFERRED", unresolved_reason="   ")
    issues = validators.validate_provenance("test-paper", payload)
    assert len(issues) == 1
    assert issues[0].code == "boolean_inference_basis_missing"


# --------------------------------------------------------------------- #
# reported_effect_scope -- Phase C fix: same structural problem as the
# boolean redesign above, generalized by leaf field name instead of by
# Python type. Real Oceologia-1998 Observation run 20260915T135025_d291c220
# showed 0/61 candidates ever got this field past UNRESOLVED: no paper
# states the literal string "treatment_mean"/"aggregated_mean", so the old
# literal-substring check made it structurally unable to pass
# EXTRACTED/INFERRED validation, exactly like is_raw_replicate_level before
# its own fix.
# --------------------------------------------------------------------- #

def _effect_scope_payload(anchor: str, value: str = "treatment_mean", label: str = "EXTRACTED", unresolved_reason=None):
    field = {
        "value": value,
        "provenance_label": label,
        "source": {
            "source_document_id": "test-paper", "page_number": 1,
            "locators": [{"kind": "text", "block_anchor": anchor}],
        },
    }
    if unresolved_reason is not None:
        field["unresolved_reason"] = unresolved_reason
    return {"reported_effect_scope": field}


def test_extracted_effect_scope_passes_without_literal_value_in_text(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Mean extractable NH4-N under ambient CO2, first numeric column.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    assert validators.validate_provenance("test-paper", _effect_scope_payload("b:0010")) == []


def test_effect_scope_anchor_resolving_to_empty_text_is_rejected(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text("⟦b:0010⟧\nReal text.\n⟦b:0011⟧\n", encoding="utf-8")
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    issues = validators.validate_provenance("test-paper", _effect_scope_payload("b:0010"))
    assert len(issues) == 1
    assert issues[0].code == "provenance_value_mismatch"


def test_inferred_effect_scope_without_reason_is_rejected(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Table reports one value per treatment across replicate plots.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    payload = _effect_scope_payload("b:0010", label="INFERRED", unresolved_reason="")
    issues = validators.validate_provenance("test-paper", payload)
    assert len(issues) == 1
    assert issues[0].code == "boolean_inference_basis_missing"


def test_inferred_effect_scope_with_real_reason_passes(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Table reports one value per treatment across replicate plots.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    payload = _effect_scope_payload(
        "b:0010", label="INFERRED",
        unresolved_reason="Table reports one value per treatment, not per individual replicate, "
                           "so this is a treatment-level mean.",
    )
    assert validators.validate_provenance("test-paper", payload) == []


# --------------------------------------------------------------------- #
# Whitespace-robust string matching -- Phase C fix: content.md rendering of
# chemical/scientific notation (subscripts, superscripts) can insert spurious
# single spaces inside what is really one token. Confirmed in real
# Oceologia-1998 content.md, block b:0053: "NH4+-N" renders as "NH 4 + -N".
# --------------------------------------------------------------------- #

def test_string_value_matches_despite_intratoken_whitespace_in_source(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "Extractable NH 4 + -N g m -2 0.218 0.025\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    assert validators.validate_provenance("test-paper", _string_payload("b:0010", "Extractable NH4+-N")) == []


def test_string_value_whitespace_fallback_does_not_match_unrelated_text(tmp_path, monkeypatch):
    paper_dir = tmp_path / "test-paper"
    paper_dir.mkdir()
    (paper_dir / "content.md").write_text(
        "The site is in Yolo County.\n⟦b:0010⟧\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validators, "PAPERS_ROOT", tmp_path)
    issues = validators.validate_provenance("test-paper", _string_payload("b:0010", "Extractable NH4+-N"))
    assert len(issues) == 1
    assert issues[0].code == "provenance_value_mismatch"
