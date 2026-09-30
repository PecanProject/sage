# AGENTS.md

This project extracts structured IR (Intermediate Representation) records
from source papers already processed by `docproc/` (Sprint 1: Marker JSON ->
`content.md` + `provenance.json`, QC-gated). This sprint (Sprint 2) is
infrastructure only: the IR service, the tool surface, and validation. **No
full-paper extraction runs happen under this configuration yet** — that is
later agent-loop sprint work (Sprint 3+), explicitly out of scope here.

## Hard rules for every agent in this project

1. **Every agent definition must explicitly deny every generic OpenCode
   built-in tool in its own `opencode.json` block** — not just `edit` and
   `bash`. `tools:` is a denylist over a wide-open default, so anything left
   unmentioned resolves to `true`; run `opencode debug agent <name>` to see
   the actual resolved set for the installed version rather than assuming.
   The extractor agent currently denies `edit, bash, write, read, glob,
   grep, task, webfetch, todowrite, skill`. Sprint 0 also found this is NOT
   inherited by subagents from a delegating primary — a subagent with no
   explicit `tools` block resolves to everything enabled regardless of the
   parent's restrictions. Never rely on inheritance. See Playbook Section 3.
   Separately: `opencode run`/`opencode agent list` default to OpenCode's
   own built-in `build` agent (all tools enabled) whenever `--agent` is not
   passed explicitly, even when a project defines its own primary agent —
   always pass `--agent extractor` (or whichever agent is intended).
2. **Agents interact with paper content and the IR store only through the
   custom tools in `plugin/ir-tools.ts`** (`get_schema`, `read_section`,
   `read_table`, `lookup_vocab`, `propose_record`, `apply_reconstruction`,
   `flag_unresolved`, `commit_record`) — never by reading/writing files or
   running shell commands directly, even if a future OpenCode version makes
   that technically possible for a given agent. The `tools: {edit: false,
   bash: false}` block is what enforces this; treat it as load-bearing, not
   boilerplate.
3. **Deterministic Python validation is authoritative.** `propose_record`
   and `commit_record` both re-run the real Pydantic construction validators
   and provenance-anchor checks server-side every time, regardless of
   `status` — `status: "unresolved"` means specific fields may carry
   `provenance_label: "UNRESOLVED"`, not that the payload's shape goes
   unchecked; identifier fields such as `id` are still required as bare
   strings either way. Whole-graph checks (duplicate names, dangling refs
   across a supplied `dataset_context`) run only when `status: "ready"`. An
   agent's own belief that a record is valid is never sufficient — only a
   `valid: true` / `committed: true` response from the service means
   anything.
4. **Turn cap:** after 4 failed `propose_record` attempts for the same
   `(paper_id, entity_type, record_id)`, the service returns
   `forced_flag_unresolved: true` and further proposals are refused. Call
   `flag_unresolved` at that point — do not keep guessing at slightly
   different payloads.
5. **`flag_unresolved` requires real evidence**, not just a reason string:
   every content.md block anchor actually examined (`blocks_examined`) and
   an explanation of why they conflict or fall short
   (`conflict_explanation`). The service rejects calls missing either.

## Extraction / Conversion / AI-Validator pipeline (this sprint)

6. **No agent in this pipeline holds `propose_record`, `commit_record`, or
   `flag_unresolved`.** `pipeline/orchestrator.py` is the sole caller of
   those three endpoints, over HTTP, directly against `ir_service` -- never
   via an agent's own tool call. This is deliberate: the retry/commit
   decision is Python control flow reading a structured response, not a
   model's own multi-turn judgment about what to do next. (This replaced an
   earlier design where the single `extractor` agent called those tools
   itself; see `pipeline/orchestrator.py`'s module docstring.)
7. **The `converter` agent is sealed**: it holds none of
   `read_section`/`read_table`/`read_document_start` and is shown only the
   `extractor` agent's `RawExtraction` output (`pipeline/raw_schema.py`),
   passed as prompt content by the orchestrator, never fetched via a tool.
   It can therefore never invent a new anchor to rationalize a fabricated
   value -- it can only choose among anchors an earlier, tool-verified stage
   already read. Do not give `converter` a `content.md` read tool of any
   kind; doing so removes this guarantee.
8. **The `ir-validator` agent is observe-only** in this sprint
   (`MAX_AI_VALIDATION_CORRECTIONS = 0` in `pipeline/orchestrator.py`): its
   critique is always recorded in `runs/<run_id>/records/.../ai_validation/`
   and attached to the committed record's `run_metadata`, but never changes
   whether a record is committed. It holds no tools at all. Promoting it to
   a live correction trigger is a deliberate later decision, not an
   oversight -- see the TODO at that constant.
9a. **Bare-reference fields (`site_id`, `citation_id`, `treatment_id`,
   `method_id`, `species_id` — `ExtractedReference = str`, IR spec Section
   10) are deliberately not `UNRESOLVED`-able**, unlike `Treatment.study_id`
   (the one field the Option B amendment upgraded to `ExtractedField`,
   specifically because cross-paper Study identity is a genuinely separate
   reconciliation step). The architecture's assumption is that these always
   point at an entity already extracted in the same session. Testing a
   dependent entity type (Treatment, Method, Management, Observation) in
   isolation, before its prerequisite (Site, Citation, ...) has been
   extracted and committed, gives the Conversion AI no real id to use —
   confirmed in practice: it fabricated the placeholder `"UNKNOWN"` for
   `site_id` rather than leaving it genuinely unresolved (there is no
   schema-level way for it to). **The fix is not a schema change**: extract
   prerequisites first, then pass their real committed ids forward with
   `orchestrator.py run --ref site_id=<id> --ref citation_id=<id> ...`,
   which both puts the real id in front of the Conversion AI and
   deterministically rejects (with a retry, not a silent accept) any
   candidate payload that contradicts a supplied `--ref` value. Always
   supply every applicable `--ref` once its prerequisite is committed;
   never treat a run without them as authoritative for a dependent entity.
9b. **Four entities added this sprint**: `Variable`, `Crop`, `TreatmentPair`,
    `Coverage` (`pipeline/ir_schema.py`), bringing `ENTITY_MODELS` to 12.
    `TreatmentPair` and `Variable` were reinstated from
    `src2/betydb_extraction/ir/entities/` (a separate, never-wired-in
    implementation elsewhere in this repo) after an audit found they had
    been confirmed requirements there ("Confirmed gap to fix" per a
    `PROJECT_STATE_HANDOFF.md` that no longer exists in the repository) and
    silently dropped from this codebase. `Crop` and `Coverage` are new
    designs with no prior code, derived from the calibration/validation
    datapackage's `crops`/`coverage` tables.
    - `Crop` references `Species` (`species_id`) rather than duplicating
      its taxonomic fields — `Species` stays the reusable taxonomic record,
      `Crop` is the paper-specific cultivar/variety actually used.
    - `Observation.variable_name` (free text) is unchanged; `variable_id`
      (optional bare reference to `Variable`, same pattern as `species_id`)
      and `crop_id` (optional bare reference to `Crop`) were added
      alongside it, additive and non-breaking.
    - `Coverage`'s fields are plain types, not `ExtractedField`-wrapped —
      row counts and planning labels aren't claims a paper's text states
      with a citable anchor; see `ir_schema.Coverage`'s docstring. `Study`
      is the existing precedent for a zero-`ExtractedField` entity.
    - New whole-graph checks in `pipeline/validators.py`:
      `check_variable_name_uniqueness`, `check_crop_species_ref`,
      `check_treatment_pair_references_resolve`,
      `check_variable_ref_integrity`, `check_crop_ref_integrity`,
      `check_coverage_site_ref`. `check_global_id_uniqueness` and
      `check_source_of_record_referential_integrity` were extended to
      cover all four.
9c. **`orchestrator.py run-paper --paper-id <id> --model <model>`** runs the
    complete pipeline for all 12 entity types for one paper, in dependency
    order, under a single shared `run_id`, and writes
    `results/<paper_id>/<EntityType>.json` (one file per entity type,
    always all 12). It is a genuine execution -- every entity type goes
    through `run_record` (Extraction -> Conversion -> deterministic
    validation -> bounded correction -> AI Validator -> commit) exactly
    like the single-entity `run` command, never a re-aggregation of old
    `ir-store` history. Dependency order and the field-level reference map
    are declared explicitly in `orchestrator.ENTITY_DEPENDENCIES` and
    computed via a real topological sort (`_topological_entity_order`) --
    not a hand-maintained list -- so it can never silently drift from the
    actual `ir_schema.py` reference fields. Reference propagation between
    entities reuses the existing `--ref`/`known_refs` mechanism internally
    (`_resolve_known_refs`); no second dependency system was introduced.
    - **Scope decision**: exactly one record per entity type per
      `run-paper` call (id = `paper_id` for Citation, `<paper_id>_<type>`
      for everything else). This means `TreatmentPair` -- which needs two
      *distinct* Treatment records by construction
      (`ir_schema.TreatmentPair._distinct_treatments`) -- is always
      reported `"blocked"` by a single full-paper run, generically (the
      same ">1 instance of the same prerequisite type" rule that would
      catch any future entity in the same situation), not a special case.
      This is correct, disclosed behavior for this milestone, not a bug.
      Extracting multiple records per entity type is future work.
    - A blocked entity never invokes an agent or touches `ir-store` at
      all -- it's a pure, cheap, pre-flight dependency check.
    - `results/<paper_id>/` from `run-paper` (per-entity files) coexists
      with `finalize`'s `results/<paper_id>/result.json` (one combined
      file, aggregated from `ir-store` history) -- different filenames,
      same directory, two different and still both-valid operations.
9. **Always invoke agents through `pipeline/orchestrator.py`**
   (`python -m pipeline.orchestrator run ...` / `scripts/sage_run run ...`),
   not a hand-typed `opencode run` command. A manually-typed command that
   omits `--agent` silently resolves to OpenCode's wide-open `build` agent
   (see rule 1) -- this has actually happened during ad hoc testing and
   produced a run with unrestricted filesystem tool access. The orchestrator
   always passes `--agent` explicitly for every stage and records the
   resolved model/config fingerprints in the run manifest.
