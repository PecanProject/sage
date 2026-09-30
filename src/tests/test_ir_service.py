import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate ir-store and papers root per test so tests never collide or
    # leak into the real project directories.
    store_root = tmp_path / "ir-store"
    papers_root = tmp_path / "papers"
    papers_root.mkdir()
    monkeypatch.setenv("IR_STORE_ROOT", str(store_root))
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers_root))

    # Reload the service module fresh so it picks up the env vars (module-
    # level DEFAULT_STORE_ROOT/DEFAULT_PAPERS_ROOT are read at import time).
    for mod in ("ir_service", "store", "content_reader"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(PIPELINE_DIR))
    import ir_service as svc

    svc._attempt_counts.clear()
    write_sample_paper(papers_root)
    return TestClient(svc.app), papers_root


def write_sample_paper(papers_root: Path, paper_id="paperA"):
    pdir = papers_root / paper_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "content.md").write_text(
        "# Materials and Methods\n\n"
        "This study was conducted in Yolo County.\n"
        "\n"
        "⟦b:0001⟧\n\n"
        "## Site description\n\n"
        "The site sits at 38.5N.\n"
        "\n"
        "⟦b:0002⟧\n\n"
        "| Treatment | Yield |\n"
        "| --- | --- |\n"
        "| control | 3.2 |\n"
        "| n_fert | 4.1 |\n"
        "⟦b:0003⟧\n\n"
        "# Results\n\n"
        "Yields increased under fertilization.\n"
        "\n"
        "⟦b:0004⟧\n",
        encoding="utf-8",
    )
    return pdir


def valid_source(anchor="b:0001"):
    return {
        "source_document_id": "paperA",
        "page_number": 1,
        "section_path": ["Methods"],
        "locators": [{"kind": "text", "block_anchor": anchor}],
    }


def valid_site_payload(site_id="site_1"):
    return {
        "id": site_id,
        "name": {
            "value": "Yolo County",
            "provenance_label": "EXTRACTED",
            "confidence": 90,
            "inference_source": "curator",
            "source": valid_source(),
        },
    }


def valid_treatment_payload(tid="control", study_id_state="unresolved", study_id_value=None):
    study_field = (
        {
            "value": study_id_value,
            "provenance_label": "EXTRACTED",
            "source": valid_source(),
        }
        if study_id_state == "extracted"
        else {
            "value": None,
            "provenance_label": "UNRESOLVED",
            "unresolved_reason": "not yet assigned to a Study",
            "source": valid_source(),
        }
    )
    return {
        "id": tid,
        "citation_id": "paperA",
        "site_id": "site_1",
        "study_id": study_field,
        "name": {"value": tid, "provenance_label": "EXTRACTED", "source": valid_source()},
        "definition": {"value": f"{tid} definition", "provenance_label": "EXTRACTED", "source": valid_source()},
    }


CITATION_PAPER_ID = "citation_paper"


def write_citation_paper(papers_root: Path, paper_id: str = CITATION_PAPER_ID) -> Path:
    # A dedicated small paper for Citation tests -- the shared "paperA"
    # fixture's text has no author/year/title-shaped content for
    # validate_provenance to match against.
    pdir = papers_root / paper_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "content.md").write_text(
        "A. Author\n⟦b:0001⟧\n\nPublished in 2012.\n⟦b:0002⟧\n\nA Title\n⟦b:0003⟧\n",
        encoding="utf-8",
    )
    return pdir


def valid_citation_payload(cid=CITATION_PAPER_ID, persistent_identifier="omit"):
    def src(anchor):
        return {
            "source_document_id": CITATION_PAPER_ID,
            "page_number": 1,
            "section_path": [],
            "locators": [{"kind": "text", "block_anchor": anchor}],
        }

    payload = {
        "id": cid,
        "author": {"value": "A. Author", "provenance_label": "EXTRACTED", "source": src("b:0001")},
        "year": {"value": 2012, "provenance_label": "EXTRACTED", "source": src("b:0002")},
        "title": {"value": "A Title", "provenance_label": "EXTRACTED", "source": src("b:0003")},
    }
    if persistent_identifier == "unresolved":
        payload["persistent_identifier"] = {
            "value": None,
            "provenance_label": "UNRESOLVED",
            "unresolved_reason": "No DOI appears in the visible content.",
            "source": src("b:0003"),
        }
    elif persistent_identifier == "extracted":
        payload["persistent_identifier"] = {
            "value": "10.1234/example",
            "provenance_label": "EXTRACTED",
            "source": src("b:0003"),
        }
    return payload


# --------------------------------------------------------------------- #
# /health -- staleness fingerprint
# --------------------------------------------------------------------- #


def test_health_reports_fingerprint_captured_at_startup_not_recomputed_live(client, monkeypatch):
    # Phase C fix: /health previously called schema_fingerprint() fresh
    # INSIDE the request handler, reading validators.py/ir_schema.py off
    # disk at request time -- not what this process actually loaded at
    # startup. Confirmed directly during Phase C benchmarking: a
    # long-running process kept serving pre-fix propose_record behavior
    # while /health simultaneously reported the CURRENT on-disk fingerprint
    # as a match, defeating the exact staleness check
    # `orchestrator.check_health` depends on. /health must report the value
    # captured ONCE at import time, never a live re-hash.
    import pipeline.ir_service as svc

    monkeypatch.setattr(svc, "schema_fingerprint", lambda: "deadbeefdeadbeef")
    c, _ = client
    body = c.get("/health").json()
    assert body["schema_fingerprint"] == svc._SERVICE_SCHEMA_FINGERPRINT
    assert body["schema_fingerprint"] != "deadbeefdeadbeef"


# --------------------------------------------------------------------- #
# get_schema
# --------------------------------------------------------------------- #


def test_get_schema_known_entity(client):
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Treatment"})
    assert r.status_code == 200
    body = r.json()
    assert body["entity_type"] == "Treatment"
    assert "json_schema" in body and "properties" in body["json_schema"]
    assert "filled_example" in body


def test_get_schema_unknown_entity_404(client):
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "NotAnEntity"})
    assert r.status_code == 404


def test_get_schema_citation_persistent_identifier_required(client):
    # Citation contract (finalized): persistent_identifier is required, same
    # as author/year/title -- get_schema must not strip it from "required"
    # or hand back a placeholder filled_example.
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Citation"})
    body = r.json()
    assert "persistent_identifier" in body["json_schema"]["required"]
    assert "persistent_identifier" in body["filled_example"]
    assert body["filled_example"]["persistent_identifier"] is not None


# --------------------------------------------------------------------- #
# Phase 2 item 1: worked examples for the four entities that previously had
# none (Observation, Species, Method, Management) -- real Oceologia-1998
# extraction runs whack-a-moled on Observation's interacting invariants
# with no example to imitate. Every one of these must actually construct
# against the real Pydantic model, not just look plausible as a dict.
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("entity_type", ["Species", "Method", "Management", "Observation"])
def test_get_schema_previously_missing_examples_now_present_and_valid(client, entity_type):
    from pipeline.ir_schema import ENTITY_MODELS

    c, _ = client
    r = c.get("/get_schema", params={"entity_type": entity_type})
    body = r.json()
    example = body["filled_example"]
    assert "note" not in example  # not the "no worked example authored yet" placeholder
    ENTITY_MODELS[entity_type].model_validate(example)  # raises if invalid


def test_get_schema_observation_example_demonstrates_effect_scope_coupling(client):
    # The exact invariant real Oceologia-1998 runs hit repeatedly (Phase 2
    # investigation): reported_effect_scope=treatment_mean requires
    # aggregated_over_factors=EXTRACTED with an empty list.
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Observation"})
    example = r.json()["filled_example"]
    assert example["reported_effect_scope"]["value"] == "treatment_mean"
    assert example["aggregated_over_factors"]["provenance_label"] == "EXTRACTED"
    assert example["aggregated_over_factors"]["value"] == []


def test_get_schema_observation_example_demonstrates_unresolved_shape(client):
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Observation"})
    example = r.json()["filled_example"]
    assert example["temporal_info"]["provenance_label"] == "UNRESOLVED"
    assert example["temporal_info"]["value"] is None
    assert example["temporal_info"]["unresolved_reason"]


def test_get_schema_observation_example_demonstrates_grounded_inferred_boolean(client):
    # Demonstrates the Phase 2 item 4 boolean-provenance redesign: an
    # INFERRED boolean grounded via a real anchor + inference-basis note,
    # never via the literal word "false" appearing anywhere.
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Observation"})
    example = r.json()["filled_example"]
    field = example["is_raw_replicate_level"]
    assert isinstance(field["value"], bool)
    assert field["provenance_label"] == "INFERRED"
    assert field["unresolved_reason"]
    assert field["source"]["locators"][0]["block_anchor"]


def test_get_schema_observation_example_table_locator_has_table_id(client):
    # Phase 2 item 2: a kind="table" locator must carry table_id, equal to
    # block_anchor -- the exact shape converter.md now documents.
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Observation"})
    example = r.json()["filled_example"]
    locator = example["value"]["source"]["locators"][0]
    assert locator["kind"] == "table"
    assert locator["table_id"] == locator["block_anchor"]


def test_get_schema_management_example_date_and_amount_never_inferred(client):
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Management"})
    example = r.json()["filled_example"]
    assert example["date"]["provenance_label"] in ("EXTRACTED", "UNRESOLVED")
    assert example["amount"]["provenance_label"] in ("EXTRACTED", "UNRESOLVED")


def test_get_schema_species_example_scientific_name_matches_genus_and_epithet(client):
    c, _ = client
    r = c.get("/get_schema", params={"entity_type": "Species"})
    example = r.json()["filled_example"]
    expected_prefix = f"{example['genus']['value']} {example['species_epithet']['value']}"
    assert example["scientific_name"]["value"].startswith(expected_prefix)


# --------------------------------------------------------------------- #
# read_section / read_table
# --------------------------------------------------------------------- #


def test_read_section_found(client):
    c, papers_root = client
    write_sample_paper(papers_root)
    r = c.get("/read_section", params={"paper_id": "paperA", "section_name": "Site description"})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert "38.5N" in body["text"]
    assert "b:0002" in body["block_anchors"]


def test_read_section_not_found(client):
    c, papers_root = client
    write_sample_paper(papers_root)
    r = c.get("/read_section", params={"paper_id": "paperA", "section_name": "Discussion"})
    assert r.json()["found"] is False


def test_read_table_found(client):
    c, papers_root = client
    write_sample_paper(papers_root)
    r = c.get("/read_table", params={"paper_id": "paperA", "table_id": "b:0003"})
    body = r.json()
    assert body["found"] is True
    assert "n_fert" in body["markdown_table"]


def test_read_table_missing_paper(client):
    c, papers_root = client
    r = c.get("/read_table", params={"paper_id": "ghost_paper", "table_id": "b:0003"})
    assert r.json()["found"] is False


# --------------------------------------------------------------------- #
# lookup_vocab (advisory only)
# --------------------------------------------------------------------- #


def test_lookup_vocab_advisory_and_never_blocking(client):
    c, _ = client
    r = c.get("/lookup_vocab", params={"term": "fertilizer application", "entity_type": "event_type"})
    body = r.json()
    assert body["advisory_only"] is True
    assert "fertilization" in body["matches"] or body["matches"] == []  # fuzzy match, non-blocking either way


# --------------------------------------------------------------------- #
# propose_record
# --------------------------------------------------------------------- #


def test_propose_record_valid_site(client):
    c, _ = client
    r = c.post(
        "/propose_record",
        json={"paper_id": "paperA", "entity_type": "Site", "record_id": "site_1", "payload": valid_site_payload()},
    )
    body = r.json()
    assert body["valid"] is True
    assert body["errors"] == []


def test_propose_record_citation_rejects_omitted_persistent_identifier(client):
    c, papers_root = client
    write_citation_paper(papers_root)
    r = c.post(
        "/propose_record",
        json={
            "paper_id": CITATION_PAPER_ID,
            "entity_type": "Citation",
            "record_id": CITATION_PAPER_ID,
            "payload": valid_citation_payload(persistent_identifier="omit"),
        },
    )
    body = r.json()
    assert body["valid"] is False
    assert any(e.get("field") == "persistent_identifier" for e in body["errors"])


def test_propose_record_citation_accepts_unresolved_persistent_identifier(client):
    c, papers_root = client
    write_citation_paper(papers_root)
    r = c.post(
        "/propose_record",
        json={
            "paper_id": CITATION_PAPER_ID,
            "entity_type": "Citation",
            "record_id": CITATION_PAPER_ID,
            "payload": valid_citation_payload(persistent_identifier="unresolved"),
        },
    )
    body = r.json()
    assert body["valid"] is True
    assert body["errors"] == []


def test_propose_record_rejects_empty_locator_for_extracted_field(client):
    c, _ = client
    payload = valid_site_payload()
    payload["name"]["source"]["locators"] = []
    r = c.post(
        "/propose_record",
        json={"paper_id": "paperA", "entity_type": "Site", "record_id": "site_1", "payload": payload},
    )
    body = r.json()
    assert body["valid"] is False
    assert body["errors"]  # locators min_length=1 fails construction


def test_propose_record_rejects_extracted_with_null_value(client):
    c, _ = client
    payload = valid_site_payload()
    payload["name"]["value"] = None  # EXTRACTED but no value -- must fail
    r = c.post(
        "/propose_record",
        json={"paper_id": "paperA", "entity_type": "Site", "record_id": "site_1", "payload": payload},
    )
    assert r.json()["valid"] is False


def test_propose_record_turn_cap_forces_flag_unresolved(client):
    c, _ = client
    bad_payload = valid_site_payload()
    bad_payload["name"]["value"] = None  # always invalid
    last_body = None
    for _ in range(6):
        r = c.post(
            "/propose_record",
            json={"paper_id": "paperA", "entity_type": "Site", "record_id": "site_cap_test", "payload": bad_payload},
        )
        last_body = r.json()
    assert last_body["valid"] is False
    assert last_body.get("forced_flag_unresolved") is True


def test_propose_record_surfaces_construction_and_provenance_errors_in_one_response(client):
    # Phase C fix: previously validate_provenance only ran `if not errors`,
    # so a payload with BOTH a construction-time problem (here: missing
    # required `id`) and an unrelated provenance mismatch (here: `country`
    # cites real text that doesn't contain "France") only ever surfaced the
    # construction error on this attempt, discovering the provenance problem
    # only on a LATER attempt -- burning an extra attempt against the
    # 4-attempt turn cap that combined feedback would not have needed.
    c, _ = client
    payload = valid_site_payload()
    del payload["id"]  # construction error: `id` is required
    payload["country"] = {
        "value": "France",  # provenance error: content.md never says "France"
        "provenance_label": "EXTRACTED",
        "source": valid_source(),
    }
    r = c.post(
        "/propose_record",
        json={"paper_id": "paperA", "entity_type": "Site", "record_id": "site_1", "payload": payload},
    )
    body = r.json()
    assert body["valid"] is False
    assert any(e.get("field") == "id" for e in body["errors"])
    assert any(e.get("code") == "provenance_value_mismatch" and "country" in e.get("message", "") for e in body["errors"])


def test_propose_record_study_scoped_duplicate_treatment_caught_via_dataset_context(client):
    c, _ = client
    dataset_context = {
        "dataset_id": "ds1",
        "citations": [
            {
                "id": "paperA",
                "author": {"value": "A", "provenance_label": "EXTRACTED", "source": valid_source()},
                "year": {"value": 2012, "provenance_label": "EXTRACTED", "source": valid_source()},
                "title": {"value": "T", "provenance_label": "EXTRACTED", "source": valid_source()},
                "persistent_identifier": {"value": "10.1/x", "provenance_label": "EXTRACTED", "source": valid_source()},
            }
        ],
        "studies": [{"id": "study_1", "citation_ids": ["paperA"]}],
        "sites": [valid_site_payload()],
        "treatments": [valid_treatment_payload("control_existing", study_id_state="extracted", study_id_value="study_1")],
    }
    duplicate_payload = valid_treatment_payload("control_new", study_id_state="extracted", study_id_value="study_1")
    duplicate_payload["name"]["value"] = "control_existing"  # same name, same study -> conflict

    r = c.post(
        "/propose_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Treatment",
            "record_id": "control_new",
            "payload": duplicate_payload,
            "dataset_context": dataset_context,
        },
    )
    body = r.json()
    assert body["valid"] is False
    codes = [e.get("code") for e in body["errors"]]
    assert "duplicate_treatment_name" in codes


# --------------------------------------------------------------------- #
# apply_reconstruction
# --------------------------------------------------------------------- #


def test_apply_reconstruction_date_mapping(client):
    c, _ = client
    r = c.post("/apply_reconstruction", json={"kind": "date_mapping", "payload": {"reported_text": "2007-06 to 2007-08"}})
    body = r.json()["result"]
    assert body["earliest"] == "2007-06-01"
    assert body["latest"] == "2007-08-28"


def test_apply_reconstruction_relative_timing(client):
    c, _ = client
    r = c.post("/apply_reconstruction", json={"kind": "date_mapping", "payload": {"reported_text": "applied before planting"}})
    body = r.json()["result"]
    assert body["relative_timing"] == "before_planting"


def test_apply_reconstruction_stat_encoding(client):
    c, _ = client
    r = c.post(
        "/apply_reconstruction",
        json={"kind": "stat_encoding", "payload": {"raw_stats": {"SE": 0.4, "n": 3}}},
    )
    body = r.json()["result"]
    names = [e["statistic_name"] for e in body["entries"]]
    assert "SE" in names and "n" in names


def test_apply_reconstruction_factorial_expansion_treatment_mean(client):
    c, _ = client
    r = c.post(
        "/apply_reconstruction",
        json={
            "kind": "factorial_expansion",
            "payload": {"factor_levels": {"nitrogen": ["0", "100"]}, "aggregated": False},
        },
    )
    body = r.json()["result"]
    assert body["reported_effect_scope"] == "treatment_mean"
    assert body["aggregated_over_factors"]["value"] == []


def test_apply_reconstruction_unknown_kind_400(client):
    c, _ = client
    r = c.post("/apply_reconstruction", json={"kind": "not_a_kind", "payload": {}})
    assert r.status_code == 400


# --------------------------------------------------------------------- #
# flag_unresolved
# --------------------------------------------------------------------- #


def test_flag_unresolved_requires_blocks_and_explanation(client):
    c, _ = client
    r = c.post(
        "/flag_unresolved",
        json={
            "paper_id": "paperA",
            "entity_type": "Treatment",
            "record_id": "control",
            "field": "study_id",
            "reason": "unclear",
            "blocks_examined": [],
            "conflict_explanation": "",
        },
    )
    assert r.status_code == 422


def test_flag_unresolved_success(client):
    c, _ = client
    r = c.post(
        "/flag_unresolved",
        json={
            "paper_id": "paperA",
            "entity_type": "Treatment",
            "record_id": "control",
            "field": "study_id",
            "reason": "paper never mentions a broader study",
            "blocks_examined": ["b:0001", "b:0002"],
            "conflict_explanation": "b:0001 introduces the site, b:0002 describes it, neither mentions a companion citation.",
        },
    )
    assert r.status_code == 200
    assert r.json()["recorded"] is True


# --------------------------------------------------------------------- #
# commit_record
# --------------------------------------------------------------------- #


def test_commit_record_invalid_status_rejected(client):
    c, _ = client
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Site",
            "record_id": "site_1",
            "payload": valid_site_payload(),
            "status": "draft",  # not allowed
        },
    )
    assert r.status_code == 422


def test_commit_record_ready_requires_valid_payload(client):
    c, _ = client
    bad_payload = valid_site_payload()
    bad_payload["name"]["value"] = None
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Site",
            "record_id": "site_1",
            "payload": bad_payload,
            "status": "ready",
        },
    )
    assert r.status_code == 422


def test_commit_record_ready_valid_payload_succeeds(client):
    c, _ = client
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Site",
            "record_id": "site_1",
            "payload": valid_site_payload(),
            "status": "ready",
        },
    )
    assert r.status_code == 200
    assert r.json()["committed"] is True


def test_commit_record_unresolved_still_rejects_malformed_shape(client):
    # Regression test: commit_record previously skipped all Pydantic/
    # provenance validation for status="unresolved", so a malformed payload
    # (e.g. `id` wrapped as an ExtractedField object instead of a bare
    # string) would be silently persisted. Reproduces the exact shape a
    # real Llama 4 Scout run sent.
    c, _ = client
    bad_payload = valid_site_payload()
    bad_payload["id"] = {
        "value": "site_1",
        "provenance_label": "UNRESOLVED",
        "source": valid_source(),
    }
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Site",
            "record_id": "site_1",
            "payload": bad_payload,
            "status": "unresolved",
        },
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert any(e.get("field") == "id" for e in errors)


def test_commit_record_unresolved_valid_shape_succeeds(client):
    # The legitimate use case status="unresolved" exists for: a
    # structurally valid record with one field genuinely UNRESOLVED.
    c, _ = client
    payload = valid_site_payload()
    payload["name"] = {
        "value": None,
        "provenance_label": "UNRESOLVED",
        "unresolved_reason": "name not stated in the visible text",
        "source": valid_source(),
    }
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Site",
            "record_id": "site_1",
            "payload": payload,
            "status": "unresolved",
        },
    )
    assert r.status_code == 200
    assert r.json()["committed"] is True


def test_commit_record_appends_to_store_jsonl(client):
    c, papers_root = client
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Site",
            "record_id": "site_1",
            "payload": valid_site_payload(),
            "status": "ready",
        },
    )
    assert r.status_code == 200
    # Verify the store file actually exists and has the entry.
    import os

    store_root = Path(os.environ["IR_STORE_ROOT"])
    store_file = store_root / "paperA.jsonl"
    assert store_file.exists()
    lines = store_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["status"] == "ready"
    assert entry["record_id"] == "site_1"


def test_commit_record_ready_rejects_study_scoped_duplicate(client):
    c, _ = client
    dataset_context = {
        "dataset_id": "ds1",
        "citations": [
            {
                "id": "paperA",
                "author": {"value": "A", "provenance_label": "EXTRACTED", "source": valid_source()},
                "year": {"value": 2012, "provenance_label": "EXTRACTED", "source": valid_source()},
                "title": {"value": "T", "provenance_label": "EXTRACTED", "source": valid_source()},
                "persistent_identifier": {"value": "10.1/x", "provenance_label": "EXTRACTED", "source": valid_source()},
            }
        ],
        "studies": [{"id": "study_1", "citation_ids": ["paperA"]}],
        "sites": [valid_site_payload()],
        "treatments": [valid_treatment_payload("control_existing", study_id_state="extracted", study_id_value="study_1")],
    }
    dup = valid_treatment_payload("control_new", study_id_state="extracted", study_id_value="study_1")
    dup["name"]["value"] = "control_existing"

    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA",
            "entity_type": "Treatment",
            "record_id": "control_new",
            "payload": dup,
            "status": "ready",
            "dataset_context": dataset_context,
        },
    )
    assert r.status_code == 422


# --------------------------------------------------------------------- #
# Variable, Crop, TreatmentPair, Coverage (added this sprint)
# --------------------------------------------------------------------- #

NEW_ENTITY_TYPES = ("Variable", "Crop", "TreatmentPair", "Coverage")


def valid_variable_payload(vid="soc"):
    return {"id": vid, "name": {"value": "Yolo County", "provenance_label": "EXTRACTED", "source": valid_source()}}


def valid_crop_payload(cid="crop1"):
    return {"id": cid, "citation_id": "paperA", "species_id": "panicum_virgatum"}


def valid_treatment_pair_payload(pid="pair1"):
    return {
        "id": pid,
        "citation_id": "paperA",
        "treatment_id_1": "control",
        "treatment_id_2": "n_fert",
        "comparison_factor": {"value": "Yolo County", "provenance_label": "EXTRACTED", "source": valid_source()},
        "comparison_label": {"value": "Yolo County", "provenance_label": "EXTRACTED", "source": valid_source()},
    }


def valid_coverage_payload(cid="cov1"):
    return {"id": cid, "citation_id": "paperA", "site_id": "site_1", "annual_rows": 3}


def test_get_schema_new_entities(client):
    c, _ = client
    for entity_type in NEW_ENTITY_TYPES:
        r = c.get("/get_schema", params={"entity_type": entity_type})
        assert r.status_code == 200
        body = r.json()
        assert body["entity_type"] == entity_type
        assert "properties" in body["json_schema"]
        assert "filled_example" in body and body["filled_example"].get("note") is None


def test_entity_models_has_all_twelve():
    from ir_schema import ENTITY_MODELS

    expected = {
        "Citation", "Study", "Site", "Species", "Method", "Treatment", "Management",
        "Observation", "TreatmentPair", "Variable", "Coverage", "Crop",
    }
    assert set(ENTITY_MODELS.keys()) == expected
    assert len(ENTITY_MODELS) == 12


def test_propose_record_valid_variable(client):
    c, _ = client
    r = c.post(
        "/propose_record",
        json={"paper_id": "paperA", "entity_type": "Variable", "record_id": "soc", "payload": valid_variable_payload()},
    )
    assert r.json()["valid"] is True


def test_propose_record_valid_crop(client):
    c, _ = client
    r = c.post(
        "/propose_record",
        json={"paper_id": "paperA", "entity_type": "Crop", "record_id": "crop1", "payload": valid_crop_payload()},
    )
    assert r.json()["valid"] is True


def test_propose_record_treatment_pair_rejects_identical_treatments(client):
    c, _ = client
    bad = valid_treatment_pair_payload()
    bad["treatment_id_2"] = bad["treatment_id_1"]
    r = c.post(
        "/propose_record",
        json={"paper_id": "paperA", "entity_type": "TreatmentPair", "record_id": "pair1", "payload": bad},
    )
    assert r.json()["valid"] is False


def test_commit_record_treatment_pair_appends_to_store(client):
    c, papers_root = client
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA", "entity_type": "TreatmentPair", "record_id": "pair1",
            "payload": valid_treatment_pair_payload(), "status": "ready",
        },
    )
    assert r.status_code == 200
    assert r.json()["committed"] is True


def test_commit_record_coverage_appends_to_store(client):
    c, _ = client
    r = c.post(
        "/commit_record",
        json={
            "paper_id": "paperA", "entity_type": "Coverage", "record_id": "cov1",
            "payload": valid_coverage_payload(), "status": "ready",
        },
    )
    assert r.status_code == 200
    assert r.json()["committed"] is True


def test_commit_record_coverage_negative_row_count_rejected(client):
    c, _ = client
    bad = valid_coverage_payload()
    bad["annual_rows"] = -5
    r = c.post(
        "/commit_record",
        json={"paper_id": "paperA", "entity_type": "Coverage", "record_id": "cov1", "payload": bad, "status": "ready"},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------- #
# Phase 1B: new deterministic evidence endpoints -- thin wiring checks
# (the underlying lookup logic itself is fully unit-tested against
# synthetic fixtures in tests/test_content_reader.py; these just confirm
# ir_service.py's routes actually call through to it).
# --------------------------------------------------------------------- #

def _write_provenance(papers_root: Path, paper_id: str = "paperA"):
    provenance = {
        "b:0001": {"block_type": "SectionHeader", "page_id": "page_0",
                   "section_path": ["Materials and Methods"], "rendered_in_content_md": True},
        "b:0002": {"block_type": "SectionHeader", "page_id": "page_0",
                   "section_path": ["Materials and Methods", "Site description"], "rendered_in_content_md": True},
        "b:0003": {"block_type": "Table", "page_id": "page_0",
                   "section_path": ["Materials and Methods", "Site description"], "rendered_in_content_md": True},
        "b:0004": {"block_type": "SectionHeader", "page_id": "page_1",
                   "section_path": ["Results"], "rendered_in_content_md": True},
        "b:9001": {"block_type": "TableCell", "page_id": "page_0",
                   "section_path": ["Materials and Methods", "Site description"], "rendered_in_content_md": False,
                   "parent_table_anchor": "b:0003", "row_index": 0, "col_index": 0, "cell_text": "Treatment"},
        "b:9002": {"block_type": "TableCell", "page_id": "page_0",
                   "section_path": ["Materials and Methods", "Site description"], "rendered_in_content_md": False,
                   "parent_table_anchor": "b:0003", "row_index": 1, "col_index": 0, "cell_text": "control"},
        "b:9003": {"block_type": "TableCell", "page_id": "page_0",
                   "section_path": ["Materials and Methods", "Site description"], "rendered_in_content_md": False,
                   "parent_table_anchor": "b:0003", "row_index": 1, "col_index": 1, "cell_text": "3.2"},
    }
    (papers_root / paper_id / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")


def test_list_sections_endpoint(client):
    c, papers_root = client
    _write_provenance(papers_root)
    r = c.get("/list_sections", params={"paper_id": "paperA"})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["sections"][0]["section_path"] == ["Materials and Methods"]


def test_read_section_by_path_endpoint(client):
    c, papers_root = client
    _write_provenance(papers_root)
    r = c.get("/read_section_by_path", params={"paper_id": "paperA", "section_path": "Results"})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["blocks"][0]["block_anchor"] == "b:0004"


def test_read_table_row_endpoint(client):
    c, papers_root = client
    _write_provenance(papers_root)
    r = c.get("/read_table_row", params={"paper_id": "paperA", "table_anchor": "b:0003", "row_index": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["cells"] == ["control", "3.2"]
    assert body["table_anchor"] == "b:0003"


def test_read_table_cell_endpoint(client):
    c, papers_root = client
    _write_provenance(papers_root)
    r = c.get("/read_table_cell", params={
        "paper_id": "paperA", "table_anchor": "b:0003", "row_index": 1, "col_index": 1,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["cell_text"] == "3.2"


def test_read_nearby_endpoint(client):
    c, papers_root = client
    _write_provenance(papers_root)
    r = c.get("/read_nearby", params={"paper_id": "paperA", "anchor": "b:0002", "before": 1, "after": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert [b["block_anchor"] for b in body["blocks"]] == ["b:0001", "b:0002", "b:0003"]
