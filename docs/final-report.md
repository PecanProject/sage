---
title: "SAGE — GSoC 2026 Final Report"
---

# SAGE: LLM extraction of agronomic papers into a structured, reviewable IR

**Google Summer of Code 2026 · PEcAn Project · Contributor: Abhishek Kumar Rai**
**Mentors:** [mentor names] · **Code:** <https://github.com/Abhishek-Kumar-Rai5/sage>

## 1. What SAGE does

SAGE reads a published crop-science paper and produces a **structured intermediate representation (IR)** of what the
paper reports — sites, species, crops, treatments, methods, variables, management events, coverage and observations — in
the shape required by the *Calibration and Validation Data Collection Protocol*. Every value is tied to the source text
that supports it, values the paper does not state stay explicitly **UNRESOLVED**, and a person reviews the result in a
web UI before anything is accepted.

```
PDF ──Marker──▶ docproc adapter ──▶ content.md + provenance.json   (text blocks with ⟦b:NNNN⟧ anchors)
                                          │
                     ┌────────────────────┴─────────────────────┐
                     ▼                                          ▼
             ir_service (FastAPI, 15 endpoints)       opencode agents
             schema · validation · storage            extractor  (sees the paper, never the schema)
                     ▲                                converter  (sees the schema, never the paper)
                     │                                ir_validator (observe-only second opinion)
                     └──────────── orchestrator ◀─────────────────┘
                     enumerate → extract → grounding gate → convert → validate → commit
                                          │
                                          ▼
                       results store ──▶ Streamlit review UI ──▶ corrections log
```

The extractor and the converter are kept apart on purpose: the model that reads the paper never sees the schema, and the
model that fills the schema never sees the paper, so a value has to be quoted from the source before it can enter a
record.

## 2. Repository layout

```
src/
  docproc/            Marker output → content.md + provenance.json adapter, QC gate, batch runner
  pipeline/           orchestrator, ir_service (FastAPI), IR + raw schemas, validators, content reader,
                      results / run / corrections stores, table pooling evidence, run configuration & locks
  opencode-config/    agent definitions (extractor, converter, ir_validator), skills, tool plugin
  tests/              42 test modules + fixtures (trimmed paper text only)
streamlit_app/        review UI (paper library, PDF pane, record review table) + UI tests
docs/                 this report, feature requests
.session_scratch/     the Calibration and Validation protocol and its datapackage schema (reference)
```

## 3. Main components

- **Document processing (`src/docproc`).** Turns Marker's JSON into `content.md` with a stable anchor after every block
  and a `provenance.json` mapping each anchor to page, section and polygon, so any extracted value can be shown on the
  PDF page. A QC gate flags conversions that lose text before extraction starts.
- **Extraction pipeline (`src/pipeline/orchestrator.py`).** Per entity type it enumerates candidates from prose and
  reconstructs tables deterministically (table role → factors and dimensions → variables → row groups → pooled
  factors). Each candidate then goes through extraction, a **grounding gate** (every quoted excerpt must appear in its
  cited blocks), conversion, deterministic validation, readiness classification (`ready` / `unresolved` / `error`), one
  AI-validator pass with a single bounded correction, and commit.
- **IR service (`ir_service.py`).** The single writer: `propose_record`, `commit_record`, `flag_unresolved` plus read
  tools for sections, tables, rows and cells. It enforces the schema, provenance labels (EXTRACTED / INFERRED /
  UNRESOLVED) and reference integrity.
- **Provider resilience.** Empty, timed-out or malformed model responses are classified separately from content
  failures. A record waits out a provider burst with a growing cool-down and does not spend a numbered attempt on it; if
  a run sees repeated exhausted budgets it drops to one round per record so an outage costs minutes, not hours.
- **Review UI (`streamlit_app`).** A record is a `result | source | actions` table: the value, the quoted evidence with
  page and section, and icon actions — approve, edit (correct value, add note, relocate evidence, tell the agent) and
  view in PDF. Link fields (`*_id`) are hidden, citation metadata is collapsed, and every correction is appended to an
  immutable log; the original extraction is never overwritten.

## 4. Prompt changes

Agent definitions in `src/opencode-config/` are unchanged. The prompts the orchestrator builds changed as follows
(`src/pipeline/orchestrator.py`):

| Prompt | Change |
|---|---|
| Table classification | (1) Declares every table variable, including variables encoded as **row labels**; a table whose row levels are not declared is retried once with feedback and otherwise accepted and flagged. (2) The paper's Methods blocks are supplied verbatim so a method hint can be tied to real prose; hints the prose does not support are withheld. (3) Page-split table continuations are marked as one logical table. |
| Enumeration | Names the exact list-field spelling for links (`treatment_ids`, plural), links an event to a condition only when the cited text names it, and can ask the model to declare its dimensions. Fixes a bug where the singular key in the example made every proposed link vanish. |
| Conversion | Tells the converter not to mark a field UNRESOLVED for a missing page number (the pipeline fills it), and passes a sealed, orchestrator-authored `CANDIDATE_CONTEXT` for treatment links instead of letting the model guess them. |
| Extraction, AI validation | Unchanged. |

Related rule changes that are not prompts: method matching now uses only the variable and method hint (a Treatment level
such as "Fallow" can no longer leak into Method matching), and a *null* fact with no source text is dropped as ungrounded
instead of failing the whole Citation.

## 5. Tests

| Suite | Result | Scope |
|---|---|---|
| `src/tests` (42 modules) | **1046 passed** | orchestrator flow and attempt budgets, grounding gate, validators, IR schema and service, table reconstruction (roles, factors, continuation, pooling, method hints, variable declarations), temporal context, treatment identity, provider failures and resilience, run isolation and locks, results / corrections stores |
| `streamlit_app/tests` (8 modules) | **87 passed, 13 skipped** | review table rendering (headless, Streamlit AppTest), field state wording, approve / edit actions, link-field hiding, PDF-pane wiring; skipped tests need a browser (Playwright) |
| `src/docproc/test_marker_adapter.py` | **5 passed** | anchor and provenance generation |

Tests run without a network or a model: agents are replaced by scripted responses, and regression fixtures are trimmed
paper text plus recorded model answers; these branches add no research-paper PDFs.

```bash
cd src && ../.venv/bin/python -m pytest tests
cd ../streamlit_app && ../.venv/bin/python -m pytest tests
cd ../src && ../.venv/bin/python -m pytest docproc/test_marker_adapter.py
```

## 6. Validation on a real paper

One end-to-end run on *Barrios-Masias et al. 2010, "Cultivar mixtures of processing tomato in an organic
agroecosystem"* (model `gpt-oss-120b`, Jetstream endpoint), no code changed during the run:

| | |
|---|---|
| Runtime / model calls | 3 h 24 min · 481 (extractor 201, converter 174, validator 106) |
| Records | 133 — **96 ready**, 35 unresolved, 2 error |
| Provider failures | 47 failed rounds; 12 of the 13 affected records recovered |
| Citation | ready (no longer blocks the paper) |
| Observations | 13 ready, 28 unresolved, 1 error |

Checked against the paper text, the 13 ready observation values are all correct. The remaining problems are:

- **Method pool incomplete in this run** (no soil-sampling, chamber-gas or plant-sampling method), so 8 soil
  observations and the 16 non-PAR Table 1 cells are refused rather than linked to a wrong method.
- **Two wrong method links** (fruit phosphorus → pH meter; harvestable fruit → colour reflectance).
- **Date confusions in Management** (cover-crop incorporation date, harvest date attached to weeding and sulfur).
- Observation timing (DAP) is not carried into `temporal_info`, and Treatments are not linked to the Study.

Figures are images in the source, so nothing can be extracted from them; the DOI is not printed in the PDF (see the
feature request below).

## 7. Reference documents

- **Calibration and Validation Data Collection Protocol** (D. LeBauer, working draft) — the target the IR follows:
  [`.session_scratch/protocol.txt`](../.session_scratch/protocol.txt), with its schema in
  [`.session_scratch/datapackage.json`](../.session_scratch/datapackage.json).
- **Feature request — fill Citation metadata from a DOI (habanero / Crossref) instead of the PDF text:**
  [`docs/feature-requests/citation-metadata-from-doi.md`](feature-requests/citation-metadata-from-doi.md).

## 8. Code delivered

All pushes are to the contributor fork; each branch is stacked on the previous one.

| Branch | Commit | Adds |
|---|---|---|
| [`gsoc/sage-docproc`](https://github.com/Abhishek-Kumar-Rai5/sage/tree/gsoc/sage-docproc) | `4fb9f62` | `src/docproc/` adapter, QC gate, batch runner + tests |
| [`gsoc/sage-pipeline-hardening`](https://github.com/Abhishek-Kumar-Rai5/sage/tree/gsoc/sage-pipeline-hardening) | `9d3416f` | grounding and null-fact rules, table reconstruction and pooling evidence, method matching, Step-B variable declarations, provider resilience, run isolation/config/locks, backend test suite with fixtures |
| [`gsoc/sage-review-table`](https://github.com/Abhishek-Kumar-Rai5/sage/tree/gsoc/sage-review-table) | `d9bed49`, `3d442ab` | redesigned review table (result / source / actions), link-field hiding, de-emphasised Citation, UI tests, DOI feature request |
| [`gsoc/sage-final-report`](https://github.com/Abhishek-Kumar-Rai5/sage/tree/gsoc/sage-final-report) | this file | the report |

Excluded on purpose: the source PDFs, Marker JSON, generated results and logs.

## 9. Known limits and next steps

1. Stabilise Method enumeration (it drives most unresolved observations) and re-check method matching for short names.
2. Fix the Management date parsing and carry DAP into observation timing.
3. Link Treatments to the Study and decide how factorial designs (cover crop × cultivar mixture) are represented.
4. Citation metadata from Crossref (feature request above).
5. Shorten the provider cool-down schedule: at the point it was measured (2 h into the run) about half of the elapsed time was spent waiting on provider cool-downs.
