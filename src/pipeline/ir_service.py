"""
`pipeline/ir_service.py` — FastAPI app wrapping the real Pydantic
schema/validators/reconstruction logic (`ir_schema.py`, `validators.py`,
`reconstruction.py`, `vocab.py`, `content_reader.py`, `store.py`).

Playbook Section 6: "Custom tools, exposed to the OpenCode agent via a
TypeScript plugin, each a thin call to a local Python `ir-service` (FastAPI)
wrapping the real Pydantic schema/validators/reconstruction logic -- never
reimplement that logic in TypeScript." This module IS that wrapper; the
TypeScript plugin (`opencode-config/plugin/ir-tools.ts`) only does HTTP
calls into these endpoints and returns the JSON straight through -- no
validation logic lives in TS.

Deterministic-validation-is-authoritative (this sprint's explicit
instruction): every endpoint that could commit or approve a record
(`propose_record`, `commit_record`) re-runs the real Pydantic construction
validators plus `validators.validate_dataset`'s whole-graph checks against a
single-entity-scoped dataset view. An LLM agent's own claim that a record is
valid is never trusted -- these functions either accept or reject, and the
rejection reasons are the only thing that comes back.
"""

from __future__ import annotations

import os
import ast
import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError, field_validator

from pipeline import content_reader
from pipeline import reconstruction
from pipeline import store
from pipeline import vocab
from pipeline.fingerprint import schema_fingerprint
from pipeline.ir_schema import ENTITY_MODELS, IRDataset
from pipeline.validators import validate_dataset, validate_provenance

app = FastAPI(title="ir-service", version="0.1.0")
_SERVICE_STARTED_AT = time.time()
# Captured ONCE, at import time -- see /health's own docstring below. Calling
# schema_fingerprint() fresh inside the /health handler (the previous
# behavior) reads validators.py/ir_schema.py off disk at REQUEST time, not
# what this process actually loaded at startup, so it silently reports
# "healthy" against a running process's stale in-memory code the instant the
# files on disk change underneath it -- confirmed directly during Phase C
# benchmarking: a propose_record call against a long-running process
# returned old (pre-fix) behavior while /health simultaneously reported the
# CURRENT on-disk fingerprint as a match. This defeated the exact staleness
# check `orchestrator.check_health` depends on to refuse running against a
# stale service.
_SERVICE_SCHEMA_FINGERPRINT = schema_fingerprint()

# In-memory per-record attempt counters, for the turn-cap rule (Playbook
# Section 6: "a hard turn cap per record (~4 self-correction attempts) forces
# flag_unresolved instead of an agent quietly guessing its way to a
# fabricated value under retry pressure"). Deliberately server-side state,
# not something the agent/prompt can reset by just trying again with
# slightly different wording -- the key is (paper_id, entity_type, record_id)
# so distinct records don't share a budget.
MAX_PROPOSE_ATTEMPTS = 4
_attempt_counts: dict[tuple[str, str, str], int] = {}


def _attempt_key(paper_id: str, entity_type: str, record_id: str) -> tuple[str, str, str]:
    return (paper_id, entity_type, record_id)


# ---------------------------------------------------------------------------
# get_schema
# ---------------------------------------------------------------------------

_FILLED_EXAMPLES: dict[str, dict[str, Any]] = {
    "Site": {
        "id": "yolo_county_ca",
        "name": {
            "value": "Yolo County",
            "provenance_label": "EXTRACTED",
            "confidence": 90,
            "inference_source": "curator",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 2,
                "section_path": ["Materials and Methods", "Site description"],
                "locators": [{"kind": "text", "block_anchor": "b:0042"}],
            },
        },
        "latitude": None,
        "longitude": None,
    },
    "Treatment": {
        "id": "control_site_a",
        "citation_id": "smukler2012a",
        "site_id": "yolo_county_ca",
        "study_id": {
            "value": None,
            "provenance_label": "UNRESOLVED",
            "unresolved_reason": "Study assignment deferred to Scientist Review reconciliation (Section 5.1).",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 1,
                "section_path": [],
                "locators": [{"kind": "text", "block_anchor": "b:0001"}],
            },
        },
        "name": {
            "value": "control",
            "provenance_label": "EXTRACTED",
            "confidence": 85,
            "inference_source": "llm",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0088"}],
            },
        },
        "definition": {
            "value": "No fertilizer applied.",
            "provenance_label": "EXTRACTED",
            "confidence": 80,
            "inference_source": "llm",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0089"}],
            },
        },
        "control_status": {
            "value": True,
            "provenance_label": "EXTRACTED",
            "confidence": 95,
            "inference_source": "curator",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0088"}],
            },
        },
    },
    "Study": {
        "id": "smukler_yolo_nutrient_cycling",
        "citation_ids": ["smukler2012a"],
    },
    "Citation": {
        "id": "smukler2012a",
        "author": {
            "value": "Smukler, S.M.",
            "provenance_label": "EXTRACTED",
            "confidence": 90,
            "inference_source": "llm",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 1,
                "section_path": [],
                "locators": [{"kind": "text", "block_anchor": "b:0007"}],
            },
        },
        "year": {
            "value": 2012,
            "provenance_label": "EXTRACTED",
            "confidence": 95,
            "inference_source": "llm",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 1,
                "section_path": [],
                "locators": [{"kind": "text", "block_anchor": "b:0011"}],
            },
        },
        "title": {
            "value": "Nutrient cycling and soil carbon in Yolo County agroecosystems",
            "provenance_label": "EXTRACTED",
            "confidence": 95,
            "inference_source": "llm",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 1,
                "section_path": [],
                "locators": [{"kind": "text", "block_anchor": "b:0006"}],
            },
        },
        "persistent_identifier": {
            "value": None,
            "provenance_label": "UNRESOLVED",
            "unresolved_reason": "No DOI or other persistent identifier appears anywhere in the visible content.md text for this paper.",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 1,
                "section_path": [],
                "locators": [{"kind": "text", "block_anchor": "b:0001"}],
            },
        },
    },
    "Variable": {
        "id": "soil_organic_carbon_concentration",
        "name": {
            "value": "soil organic carbon concentration",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 3,
                "section_path": ["Materials and Methods"],
                "locators": [{"kind": "text", "block_anchor": "b:0090"}],
            },
        },
        "units": {
            "value": "g C kg-1 soil",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 3,
                "section_path": ["Materials and Methods"],
                "locators": [{"kind": "text", "block_anchor": "b:0090"}],
            },
        },
    },
    "Crop": {
        "id": "smukler2012a_switchgrass_trailblazer",
        "citation_id": "smukler2012a",
        "species_id": "panicum_virgatum",
        "cultivar": {
            "value": "Trailblazer",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 2,
                "section_path": ["Materials and Methods"],
                "locators": [{"kind": "text", "block_anchor": "b:0026"}],
            },
        },
    },
    "TreatmentPair": {
        "id": "smukler2012a_compost_vs_control",
        "citation_id": "smukler2012a",
        "treatment_id_1": "control",
        "treatment_id_2": "compost",
        "comparison_factor": {
            "value": "compost",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0088"}],
            },
        },
        "comparison_label": {
            "value": "compost amendment vs. no amendment",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a",
                "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0088"}],
            },
        },
    },
    "Coverage": {
        "id": "smukler2012a_yolo_soc",
        "citation_id": "smukler2012a",
        "site_id": "yolo_county_ca",
        "variable_id": "soil_organic_carbon_concentration",
        "annual_rows": 4,
        "notes": "Curator rollup: not itself a value reported by the source -- see ir_schema.Coverage's docstring.",
    },
    "Species": {
        "id": "panicum_virgatum",
        "genus": {
            "value": "Panicum",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 2,
                "section_path": ["Materials and Methods"],
                "locators": [{"kind": "text", "block_anchor": "b:0026"}],
            },
        },
        "species_epithet": {
            "value": "virgatum",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 2,
                "section_path": ["Materials and Methods"],
                "locators": [{"kind": "text", "block_anchor": "b:0026"}],
            },
        },
        "scientific_name": {
            # Must equal genus + " " + species_epithet -- see
            # ir_schema.Species._scientific_name_consistency.
            "value": "Panicum virgatum",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 2,
                "section_path": ["Materials and Methods"],
                "locators": [{"kind": "text", "block_anchor": "b:0026"}],
            },
        },
        "common_name": {
            "value": "switchgrass",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 2,
                "section_path": ["Materials and Methods"],
                "locators": [{"kind": "text", "block_anchor": "b:0026"}],
            },
        },
    },
    "Method": {
        "id": "smukler2012a_soc_method",
        "citation_id": "smukler2012a",
        "name": {
            "value": "Loss-on-ignition soil organic carbon analysis",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 3,
                "section_path": ["Materials and Methods", "Soil analysis"],
                "locators": [{"kind": "text", "block_anchor": "b:0090"}],
            },
        },
        "description": {
            "value": "Soil organic carbon was determined by loss-on-ignition at 375C for 16 hours.",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 3,
                "section_path": ["Materials and Methods", "Soil analysis"],
                "locators": [{"kind": "text", "block_anchor": "b:0090"}],
            },
        },
    },
    "Management": {
        "id": "smukler2012a_compost_application",
        "citation_id": "smukler2012a",
        "treatment_ids": {
            "value": ["compost"],
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0089"}],
            },
        },
        "event_type": {
            # event_type occurrence MAY be INFERRED (unlike date/amount below)
            # -- see ir_schema.Management's docstring.
            "value": "compost application",
            "provenance_label": "INFERRED",
            "unresolved_reason": "Text describes adding composted material to plots; 'compost application' is the "
                                  "natural event-type label for that description, not a term used verbatim.",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0089"}],
            },
        },
        "date": {
            # Management.date must NEVER be INFERRED -- EXTRACTED or
            # UNRESOLVED only (ir_schema.Management._date_amount_never_inferred).
            "value": {
                "reported_text": "March 2009", "earliest": "2009-03-01", "latest": "2009-03-31",
            },
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0089"}],
            },
        },
        "amount": {
            # Management.amount must also never be INFERRED -- same rule as date.
            "value": {
                "reported_text": "10 Mg ha-1", "reported_numeric_value": 10, "reported_units": "Mg ha-1",
            },
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 3,
                "section_path": ["Experimental design"],
                "locators": [{"kind": "text", "block_anchor": "b:0089"}],
            },
        },
    },
    "Observation": {
        # Demonstrates every interacting invariant that real Oceologia-1998
        # extraction runs hit and failed on repeatedly (Phase 2 investigation):
        # reported_effect_scope=treatment_mean <-> aggregated_over_factors
        # shape, INFERRED <-> unresolved_reason, UNRESOLVED <-> value=None,
        # and a boolean ExtractedField (is_raw_replicate_level) that is
        # EXTRACTED/INFERRED-grounded WITHOUT the source literally containing
        # the word "true"/"false".
        "id": "smukler2012a_yolo_soc_control_obs1",
        "dataset_id": "smukler2012a_dataset",
        "citation_id": "smukler2012a",
        "site_id": "yolo_county_ca",
        "treatment_id": "control_site_a",
        # species_id, crop_id, variable_id, replicate_id, statistical_encoding,
        # notes are all optional -- omitted here exactly as they should be
        # omitted (never set to a placeholder) when no matching record/value
        # is known for this observation. See converter.md's "OPTIONAL bare
        # references" rule.
        "method_id": "smukler2012a_soc_method",
        "variable_name": {
            "value": "soil organic carbon concentration",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 4,
                "section_path": ["Results"],
                "locators": [{"kind": "table", "table_id": "b:0102", "block_anchor": "b:0102"}],
            },
        },
        "value": {
            "value": {
                "reported_text": "12.4 g C kg-1", "reported_numeric_value": 12.4, "reported_units": "g C kg-1",
            },
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 4,
                "section_path": ["Results"],
                "locators": [{"kind": "table", "table_id": "b:0102", "block_anchor": "b:0102"}],
            },
        },
        "reported_effect_scope": {
            # "treatment_mean" REQUIRES aggregated_over_factors to be present
            # as EXTRACTED with an empty list -- not applicable != absent.
            # See ir_schema.Observation._effect_scope_aggregation_rule.
            "value": "treatment_mean",
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 4,
                "section_path": ["Results"],
                "locators": [{"kind": "table", "table_id": "b:0102", "block_anchor": "b:0102"}],
            },
        },
        "aggregated_over_factors": {
            "value": [],
            "provenance_label": "EXTRACTED",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 4,
                "section_path": ["Results"],
                "locators": [{"kind": "table", "table_id": "b:0102", "block_anchor": "b:0102"}],
            },
        },
        "temporal_info": {
            # UNRESOLVED REQUIRES value=None plus a real unresolved_reason --
            # never a bare null. See ir_schema.ExtractedField._provenance_invariants.
            "value": None,
            "provenance_label": "UNRESOLVED",
            "unresolved_reason": "No explicit sampling date is stated anywhere near this reported value.",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 4,
                "section_path": ["Results"],
                "locators": [{"kind": "table", "table_id": "b:0102", "block_anchor": "b:0102"}],
            },
        },
        "is_raw_replicate_level": {
            # Boolean ExtractedField: grounded via a real anchor plus (for
            # INFERRED) a genuine inference-basis note -- NEVER via the
            # source literally containing the word "false". See
            # validators._value_supported_by_text's boolean branch.
            "value": False,
            "provenance_label": "INFERRED",
            "unresolved_reason": "Table reports this value as a treatment mean across replicate plots (see "
                                  "reported_effect_scope=treatment_mean above), not an individual replicate "
                                  "measurement.",
            "source": {
                "source_document_id": "smukler2012a", "page_number": 4,
                "section_path": ["Results"],
                "locators": [{"kind": "table", "table_id": "b:0102", "block_anchor": "b:0102"}],
            },
        },
    },
}


def _llm_json_schema(entity_type: str) -> dict[str, Any]:
    """Return the schema exposed to the LLM, unmodified from the real Pydantic
    model's own model_json_schema(). Citation.persistent_identifier is a
    required field (Citation contract, finalized): always present, EXTRACTED
    with a real DOI/PID or UNRESOLVED with a reason -- never omitted. The
    schema's own "required" list already reflects that; nothing needs
    weakening here.
    """
    model = ENTITY_MODELS[entity_type]
    return model.model_json_schema()


@app.get("/get_schema")
def get_schema(entity_type: str) -> dict:
    model = ENTITY_MODELS.get(entity_type)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown entity_type '{entity_type}'. Known types: {sorted(ENTITY_MODELS.keys())}",
        )
    return {
        "entity_type": entity_type,
        "json_schema": _llm_json_schema(entity_type),
        "filled_example": _FILLED_EXAMPLES.get(entity_type, {"note": "no worked example authored yet for this entity_type"}),
    }


# ---------------------------------------------------------------------------
# read_section / read_table
# ---------------------------------------------------------------------------


def _papers_root() -> Path:
    """Resolve the papers root from the current environment at request time."""
    return Path(os.environ.get("IR_PAPERS_ROOT", "paper"))


@app.get("/read_section")
def read_section(paper_id: str, section_name: str) -> dict:
    return content_reader.read_section(
        paper_id, section_name, papers_root=_papers_root()
    )


@app.get("/read_document_start")
def read_document_start(paper_id: str, max_lines: int = 80) -> dict:
    return content_reader.read_document_start(
        paper_id, max_lines, papers_root=_papers_root()
    )


@app.get("/read_table")
def read_table(paper_id: str, table_id: str) -> dict:
    return content_reader.read_table(
        paper_id, table_id, papers_root=_papers_root()
    )


# ---------------------------------------------------------------------------
# Phase 1 deterministic evidence tools -- list_sections / read_section_by_path
# / read_table_row / read_table_cell / read_nearby. All read provenance.json
# metadata that docproc/marker_adapter.py already computed during document
# preparation (section_path, per-cell row_index/col_index) -- nothing here
# re-derives it, and nothing here calls a model.
# ---------------------------------------------------------------------------


@app.get("/list_sections")
def list_sections(paper_id: str) -> dict:
    return content_reader.list_sections(paper_id, papers_root=_papers_root())


@app.get("/read_section_by_path")
def read_section_by_path(paper_id: str, section_path: str) -> dict:
    # section_path arrives as a single ">"-delimited string (matching
    # list_sections' own section_path segments joined the same way) --
    # simpler for a GET-query-param tool call than a JSON array, same
    # convention as every other read tool in this module.
    segments = [s.strip() for s in section_path.split(">") if s.strip()]
    return content_reader.read_section_by_path(paper_id, segments, papers_root=_papers_root())


@app.get("/read_table_row")
def read_table_row(paper_id: str, table_anchor: str, row_index: int) -> dict:
    return content_reader.read_table_row(paper_id, table_anchor, row_index, papers_root=_papers_root())


@app.get("/read_table_cell")
def read_table_cell(paper_id: str, table_anchor: str, row_index: int, col_index: int) -> dict:
    return content_reader.read_table_cell(
        paper_id, table_anchor, row_index, col_index, papers_root=_papers_root()
    )


@app.get("/read_nearby")
def read_nearby(paper_id: str, anchor: str, before: int = 1, after: int = 1) -> dict:
    return content_reader.read_nearby(paper_id, anchor, before, after, papers_root=_papers_root())


# ---------------------------------------------------------------------------
# lookup_vocab
# ---------------------------------------------------------------------------


@app.get("/lookup_vocab")
def lookup_vocab_endpoint(term: str, entity_type: str) -> dict:
    return vocab.lookup_vocab(term, entity_type)

def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        # JSON first -- a caller sending valid JSON (e.g. bare `null`, which
        # is not a Python literal) must not be rejected just because
        # ast.literal_eval can't parse it. ast.literal_eval stays as a
        # fallback for a Python-dict-repr string, which some caller may
        # still legitimately send; this only re-orders which parser gets
        # first refusal; it doesn't remove either.
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError) as e:
                raise ValueError(f"Invalid dictionary string: {e}") from e

        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Expected a JSON object or Python dict string")


# ---------------------------------------------------------------------------
# propose_record
# ---------------------------------------------------------------------------


class ProposeRecordRequest(BaseModel):
    paper_id: str
    entity_type: str
    record_id: str
    payload: dict[str, Any]
    dataset_context: Optional[dict[str, Any]] = None

    @field_validator("payload", mode="before")
    @classmethod
    def coerce_payload(cls, value: Any) -> dict[str, Any]:
        return _coerce_dict(value)

    @field_validator("dataset_context", mode="before")
    @classmethod
    def coerce_dataset_context(cls, value: Any) -> Optional[dict[str, Any]]:
        if value is None:
            return None
        return _coerce_dict(value)

def _construction_errors(entity_type: str, payload: dict[str, Any]) -> list[dict]:
    model = ENTITY_MODELS.get(entity_type)
    if model is None:
        return [{"field": None, "message": f"Unknown entity_type '{entity_type}'"}]
    try:
        model.model_validate(payload)
        return []
    except ValidationError as e:
        return [
            {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
            for err in e.errors()
        ]


@app.post("/propose_record")
def propose_record(req: ProposeRecordRequest) -> dict:
    key = _attempt_key(req.paper_id, req.entity_type, req.record_id)
    attempts_so_far = _attempt_counts.get(key, 0)

    if attempts_so_far >= MAX_PROPOSE_ATTEMPTS:
        return {
            "valid": False,
            "forced_flag_unresolved": True,
            "attempts_used": attempts_so_far,
            "errors": [
                {
                    "field": None,
                    "message": (
                        f"Turn cap reached ({MAX_PROPOSE_ATTEMPTS} attempts) for this record. "
                        "Call flag_unresolved instead of proposing again."
                    ),
                }
            ],
        }

    errors = _construction_errors(req.entity_type, req.payload)

    # Provenance is deterministic and independent of the LLM's own claim that
    # an anchor supports a value. Run it EVERY time, even when construction
    # already failed, not just when construction passed -- validate_provenance
    # walks the raw payload dict looking for {value, provenance_label, source}
    # shapes wherever they occur, so it doesn't need the payload to fully
    # construct first, and a malformed subtree elsewhere simply yields no
    # extra findings for that subtree rather than crashing.
    #
    # Real Oceologia-1998 Observation runs (Phase C investigation) showed the
    # previous "only after construction succeeds" gating forces failures to
    # be discovered ONE CATEGORY PER ATTEMPT: a payload with both a
    # construction-time problem (e.g. an INFERRED boolean missing its
    # inference-basis note) and an unrelated provenance mismatch (e.g. a
    # wrong anchor for a different field) only ever saw the construction
    # error on one attempt, fixed it, and only THEN learned about the
    # provenance problem on the next attempt -- burning an extra attempt
    # against the 4-attempt turn cap that better-combined feedback would not
    # have needed. Surfacing both categories together whenever both are
    # findable never loosens what counts as valid; it only gives the
    # Conversion stage complete feedback per round instead of partial.
    errors.extend(issue.to_dict() for issue in validate_provenance(req.paper_id, req.payload))

    warnings: list[dict] = []
    if req.dataset_context is not None:
        try:
            merged = _merge_into_dataset_context(req.entity_type, req.payload, req.dataset_context)
            ds = IRDataset.model_validate(merged)
            issues = validate_dataset(ds)
            for issue in issues:
                target = (issue.entity_type, issue.entity_id)
                relevant = target == (req.entity_type, req.record_id) or issue.entity_id is None
                bucket = errors if issue.severity == "error" else warnings
                if relevant or issue.severity == "error":
                    bucket.append(issue.to_dict())
        except ValidationError as e:
            errors.extend(
                {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()
            )

    is_valid = not errors
    if not is_valid:
        _attempt_counts[key] = attempts_so_far + 1

    return {
        "valid": is_valid,
        "attempts_used": _attempt_counts.get(key, attempts_so_far),
        "attempts_remaining": max(0, MAX_PROPOSE_ATTEMPTS - _attempt_counts.get(key, attempts_so_far)),
        "errors": errors,
        "warnings": warnings,
    }


def _merge_into_dataset_context(entity_type: str, payload: dict, dataset_context: dict) -> dict:
    """Builds a minimal IRDataset-shaped dict for whole-graph validation:
    dataset_context supplies everything else already known (other
    Citations, Treatments, Studies, ...); the candidate payload is spliced
    into (or replaces its slot in) the corresponding list."""
    plural = {
        "Citation": "citations",
        "Study": "studies",
        "Site": "sites",
        "Species": "species",
        "Crop": "crops",
        "Method": "methods",
        "Treatment": "treatments",
        "TreatmentPair": "treatment_pairs",
        "Variable": "variables",
        "Management": "managements",
        "Observation": "observations",
        "Coverage": "coverages",
    }[entity_type]
    merged = {k: list(v) if isinstance(v, list) else v for k, v in dataset_context.items()}
    merged.setdefault("dataset_id", dataset_context.get("dataset_id", "unknown_dataset"))
    for key in plural_values():
        merged.setdefault(key, [])
    existing = [item for item in merged.get(plural, []) if item.get("id") != payload.get("id")]
    existing.append(payload)
    merged[plural] = existing
    return merged


def plural_values():
    return [
        "citations", "studies", "sites", "species", "crops", "methods", "treatments",
        "treatment_pairs", "variables", "managements", "observations", "coverages",
    ]


# ---------------------------------------------------------------------------
# apply_reconstruction
# ---------------------------------------------------------------------------


class ApplyReconstructionRequest(BaseModel):
    kind: str
    payload: dict[str, Any]


@app.post("/apply_reconstruction")
def apply_reconstruction(req: ApplyReconstructionRequest) -> dict:
    fn = reconstruction.RECONSTRUCTION_KINDS.get(req.kind)
    if fn is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown reconstruction kind '{req.kind}'. Known kinds: {sorted(reconstruction.RECONSTRUCTION_KINDS.keys())}",
        )
    try:
        result = fn(**req.payload)
    except TypeError as e:
        raise HTTPException(status_code=422, detail=f"Bad payload for kind '{req.kind}': {e}")
    return {"kind": req.kind, "result": result}


# ---------------------------------------------------------------------------
# flag_unresolved
# ---------------------------------------------------------------------------


class FlagUnresolvedRequest(BaseModel):
    paper_id: str
    entity_type: str
    record_id: str
    field: str
    reason: str
    blocks_examined: list[str]  # content.md block anchors the agent actually looked at
    conflict_explanation: str  # why those blocks conflict or fall short -- required, not optional
    run_metadata: Optional[dict[str, Any]] = None  # orchestrator lineage: run_id, schema fingerprint, etc.


@app.post("/flag_unresolved")
def flag_unresolved(req: FlagUnresolvedRequest) -> dict:
    # Playbook Section 6: "require the agent to cite every block it examined
    # and explain why they conflict or fall short -- a bare reason string
    # isn't enough". Enforced server-side, not left to prompt discipline.
    if not req.blocks_examined:
        raise HTTPException(
            status_code=422,
            detail="flag_unresolved requires at least one block_anchor in blocks_examined -- a bare reason is not sufficient.",
        )
    if not req.conflict_explanation or not req.conflict_explanation.strip():
        raise HTTPException(
            status_code=422,
            detail="flag_unresolved requires conflict_explanation describing why the examined blocks conflict or fall short.",
        )

    entry = store.append_record(
        paper_id=req.paper_id,
        entity_type=req.entity_type,
        record_id=req.record_id,
        status="unresolved",
        payload={
            "field": req.field,
            "reason": req.reason,
            "blocks_examined": req.blocks_examined,
            "conflict_explanation": req.conflict_explanation,
        },
        extra=req.run_metadata,
    )
    # Flagging unresolved resets the attempt counter for this record -- it's
    # the designed off-ramp from the turn cap, not another attempt.
    _attempt_counts.pop(_attempt_key(req.paper_id, req.entity_type, req.record_id), None)
    return {"recorded": True, "entry": entry}


# ---------------------------------------------------------------------------
# commit_record
# ---------------------------------------------------------------------------


class CommitRecordRequest(BaseModel):
    paper_id: str
    entity_type: str
    record_id: str
    payload: dict[str, Any]
    status: str
    dataset_context: Optional[dict[str, Any]] = None
    run_metadata: Optional[dict[str, Any]] = None  # orchestrator lineage: run_id, schema fingerprint, ai_validation, etc.

    @field_validator("payload", mode="before")
    @classmethod
    def coerce_payload(cls, value: Any) -> dict[str, Any]:
        return _coerce_dict(value)

    @field_validator("dataset_context", mode="before")
    @classmethod
    def coerce_dataset_context(cls, value: Any) -> Optional[dict[str, Any]]:
        if value is None:
            return None
        return _coerce_dict(value)


@app.post("/commit_record")
def commit_record(req: CommitRecordRequest) -> dict:
    # Server-side enum enforcement, never prompt-side only (Playbook Section 6).
    if req.status not in ("ready", "unresolved"):
        raise HTTPException(
            status_code=422,
            detail=f"status must be 'ready' or 'unresolved', got '{req.status}'.",
        )

    # Construction (Pydantic shape) and provenance validation are
    # unconditional, regardless of status: AGENTS.md and extractor.md both
    # promise commit_record "requires a syntactically valid payload shape
    # either way" -- status="unresolved" means specific fields may carry
    # provenance_label=UNRESOLVED (already require value=None + a real
    # unresolved_reason, enforced by ir_schema.py's own construction
    # invariants), not that the payload's shape is unchecked. Whole-graph
    # dataset_context checks remain "ready"-only below: that check is about
    # cross-record graph consistency (duplicate names, dangling refs), a
    # judgment call about readiness to join the graph, not shape validity.
    errors = _construction_errors(req.entity_type, req.payload)
    if not errors:
        errors.extend(issue.to_dict() for issue in validate_provenance(req.paper_id, req.payload))

    warnings: list[dict] = []
    if req.status == "ready" and not errors and req.dataset_context is not None:
        merged = _merge_into_dataset_context(req.entity_type, req.payload, req.dataset_context)
        try:
            ds = IRDataset.model_validate(merged)
            for issue in validate_dataset(ds):
                (errors if issue.severity == "error" else warnings).append(issue.to_dict())
        except ValidationError as e:
            errors.extend(
                {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()
            )
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "commit_record rejected: record does not pass validation.", "errors": errors},
        )

    entry = store.append_record(
        paper_id=req.paper_id,
        entity_type=req.entity_type,
        record_id=req.record_id,
        status=req.status,
        payload=req.payload,
        extra=req.run_metadata,
    )
    _attempt_counts.pop(_attempt_key(req.paper_id, req.entity_type, req.record_id), None)
    return {"committed": True, "entry": entry}


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Reports the schema/validator fingerprint actually loaded in THIS
    process. A long-running uvicorn process serving a code version older
    than what's on disk is a real, previously-observed failure mode (an
    invalid Citation with a bare-null persistent_identifier reached ir-store
    because the running process still had the pre-fix schema in memory) --
    `orchestrator.check_health` compares this against a fresh hash of the
    files on disk and refuses to run against a stale service."""
    return {
        "status": "ok",
        "schema_fingerprint": _SERVICE_SCHEMA_FINGERPRINT,
        "started_at": _SERVICE_STARTED_AT,
        "uptime_seconds": time.time() - _SERVICE_STARTED_AT,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("IR_SERVICE_PORT", "8420")))
