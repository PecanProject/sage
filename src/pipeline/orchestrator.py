"""`pipeline/orchestrator.py` -- the deterministic control loop for:

    PDF (already processed) -> Extraction AI -> sealed raw evidence
        -> Conversion/Reasoning AI -> Sage IR
        -> deterministic validation (hard gate)
        -> bounded correction loop
        -> AI Validator (observe-only)
        -> final persistence (ir-store)

Architecture decision this sprint: the outer workflow is fully deterministic
and owned by THIS module, not by any agent's own multi-turn judgment.
Concretely:

  * Every model invocation is a single, short, single-purpose headless
    `opencode run` call (`invoke_agent`) -- never a long conversational
    session spanning multiple stages. The model never decides what happens
    after it answers; this module reads a structured result and decides.
  * `propose_record` / `commit_record` / `flag_unresolved` are called
    directly against `ir_service` over HTTP from THIS module
    (`IRServiceClient`), never via an agent's own tool call. No agent in
    this pipeline is granted those tools (see `opencode.json`) -- the model
    can never itself decide "I'm done, let me commit" or keep going after a
    validation result the way earlier ad hoc `opencode run` sessions did.
  * Every attempt at every stage is persisted via `pipeline.run_store`
    before this module decides what to do with it -- a failure never
    silently disappears; every record ends as "ready", "unresolved", or
    "error", each with its full artifact trail on disk.
  * The Conversion AI is sealed: it is only ever shown the Extraction AI's
    `RawExtraction` package (`pipeline.raw_schema`), never `content.md`
    itself, so it cannot fabricate a new anchor to rationalize a value --
    it can only choose among anchors an earlier, tool-verified stage
    already read.
  * The AI Validator stage runs in OBSERVE-ONLY mode: its critique is
    always recorded, and never changes whether a record is committed, in
    this foundation. Promoting it to a live correction trigger is a later,
    separate decision (see the module docstring's own TODO note below).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from pydantic import ValidationError

from pipeline import results_store, run_store, store
from pipeline.fingerprint import config_fingerprint, schema_fingerprint
from pipeline.ir_schema import IRDataset
from pipeline.raw_schema import EnumerationResult, RawExtraction, all_anchors
from pipeline.validators import _load_rendered_blocks, validate_dataset  # reuse, don't re-implement anchor parsing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENCODE_CONFIG_PATH = PROJECT_ROOT / "opencode.json"

DEFAULT_MODEL = "jetstream-scout/llama-4-scout"
DEFAULT_IR_SERVICE_URL = os.environ.get("IR_SERVICE_URL", "http://127.0.0.1:8420")

# 3, not 2: a real run (20260914T204018_d8c6ddb6, Citation/Oceologia-1998)
# showed attempt 2 can be lost entirely to a model/provider-level formatting
# crash (malformed GPT-OSS "harmony" tool-call tokens rejected by the
# inference backend with an HTTP 400, before any assistant text was
# produced) rather than a genuine shape-validation failure -- that attempt
# never got a real chance to apply the previous attempt's feedback. One
# extra attempt gives a transient provider-level glitch a chance to be
# absorbed without weakening RawExtraction's shape validation itself.
#
# Confirmed again, more precisely, on a real run (20260916T050510_41f112a0,
# Winter cover): invoke_agent's own internal empty-response retry (below)
# now absorbs most of this before it ever reaches these numbered attempts,
# but MAX_EXTRACTION_ATTEMPTS itself is left unchanged -- this budget is
# for genuine content/shape problems the model needs feedback to fix, not
# for infrastructure noise.
MAX_EXTRACTION_ATTEMPTS = 3
# Phase A (multi-record Variable): bounded retry for the enumeration pass,
# same rationale/shape as MAX_EXTRACTION_ATTEMPTS above.
MAX_ENUMERATION_ATTEMPTS = 3
# Deterministic-retry safety ceiling. ir_service.MAX_PROPOSE_ATTEMPTS (4) is
# the real, authoritative cap enforced server-side by attempt counters keyed
# on (paper_id, entity_type, record_id); this is a defensive backstop so a
# server-side bug can't turn into a local infinite loop.
MAX_CONVERSION_LOOP_SAFETY = 6
# Phase 1D: wired. A "suspicious" verdict now triggers exactly ONE bounded
# Conversion correction pass before commit (see _attempt_ai_validation_correction).
# The corrected payload still has to pass the SAME deterministic
# propose_record validation as everything else -- the AI Validator's opinion
# never substitutes for or bypasses it, and a record still "suspicious"
# after this one attempt falls back to the existing flag_unresolved off-ramp
# rather than being force-committed. Not a general N-attempt loop: raising
# this above 1 is not supported by the code below without further work.
MAX_AI_VALIDATION_CORRECTIONS = 1


# --------------------------------------------------------------------------- #
# Agent invocation -- the only place a model is ever called
# --------------------------------------------------------------------------- #


@dataclass
class AgentInvocation:
    agent: str
    model: str
    prompt: str
    returncode: int
    stdout: str
    stderr: str
    final_text: Optional[str]
    parsed_json: Optional[dict]
    parse_error: Optional[str] = None

    def as_artifact(self) -> dict:
        d = asdict(self)
        # Keep raw stdout/stderr in the artifact for debugging, but don't
        # duplicate huge text twice if it parsed cleanly.
        return d


# Real runs (20260914T204018_d8c6ddb6 Citation/Oceologia-1998;
# 20260916T050510_41f112a0 Winter cover -- Variable/below_ground_c_input and
# Treatment/rye_annual both lost 2-3 of their 3 real MAX_EXTRACTION_ATTEMPTS
# slots to this) confirm a real, recurring provider/decode-level failure
# class distinct from a genuine shape/content problem: the gpt-oss-120b
# backend (via vLLM) occasionally fails to produce any usable final-answer
# text at all after tool use already completed successfully -- sometimes
# surfacing as an explicit `litellm.BadRequestError: ... could not decode
# header` (HTTP 400) event in the stream, sometimes silently ending the
# turn with reason="stop" and a handful of output tokens that never appear
# as a text part. Directly confirmed by inspecting the raw JSON-lines
# stream for every failing case: zero `part.type=="text"` events exist in
# ANY of them -- this is not `_extract_final_text` failing to recognize
# real output, there is no text to find. A retry here has no model-authored
# content to give feedback about (unlike a genuine shape-validation retry),
# so retrying the IDENTICAL prompt is the correct recovery, not wasted
# effort -- and doing it here, inside the one place a model is ever called,
# means it transparently benefits extraction/conversion/enumeration/
# AI-validation alike without changing any of their own attempt-counting.
MAX_EMPTY_RESPONSE_RETRIES = 2
EMPTY_RESPONSE_RETRY_BACKOFF_SECONDS = 1.5


def _invoke_agent_once(agent: str, model: str, prompt: str, timeout: int) -> AgentInvocation:
    """Exactly one real `opencode run` subprocess call and parse attempt --
    factored out of invoke_agent so its internal empty-response retry loop
    (below) can call this repeatedly without duplicating the subprocess/
    parse logic itself."""
    cmd = [
        "opencode", "run",
        "--agent", agent,
        "--model", model,
        "--format", "json",
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout,
        )
        returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = -1
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\n[orchestrator] timed out after {timeout}s"
    except FileNotFoundError as exc:
        return AgentInvocation(
            agent=agent, model=model, prompt=prompt, returncode=-1, stdout="", stderr=str(exc),
            final_text=None, parsed_json=None, parse_error=f"opencode executable not found: {exc}",
        )

    final_text = _extract_final_text(stdout)
    if final_text:
        parsed_json, parse_error = _parse_json_block(final_text)
    else:
        parsed_json, parse_error = None, "no final assistant text found in agent output"
    return AgentInvocation(
        agent=agent, model=model, prompt=prompt,
        returncode=returncode, stdout=stdout, stderr=stderr,
        final_text=final_text, parsed_json=parsed_json, parse_error=parse_error,
    )


def invoke_agent(agent: str, model: str, prompt: str, timeout: int = 300) -> AgentInvocation:
    """Run one short, single-purpose headless OpenCode call and extract its
    final JSON answer. `--format json` gives a raw JSON-events stream on
    stdout (one event per line) instead of human-formatted text, so the
    final assistant message can be recovered reliably instead of scraped
    from rendered TUI-style output.

    Internally retries, up to MAX_EMPTY_RESPONSE_RETRIES extra times with a
    short backoff, ONLY when the provider returns literally no usable text
    at all (see MAX_EMPTY_RESPONSE_RETRIES's own docstring) -- never for a
    real-but-wrong response (a shape/content problem still returns
    immediately, unchanged, exactly as before this retry existed, so a
    genuine validation failure still costs exactly one call, and the
    caller's own attempt-counting/feedback loop is completely unaffected).
    A `FileNotFoundError` (missing opencode executable) also returns
    immediately -- retrying a missing binary can never succeed."""
    last_invocation: Optional[AgentInvocation] = None
    for retry in range(MAX_EMPTY_RESPONSE_RETRIES + 1):
        invocation = _invoke_agent_once(agent, model, prompt, timeout)
        if invocation.final_text is not None:
            return invocation
        if invocation.parse_error and "opencode executable not found" in invocation.parse_error:
            return invocation  # never retry a missing binary -- it will never succeed
        last_invocation = invocation
        if retry < MAX_EMPTY_RESPONSE_RETRIES:
            time.sleep(EMPTY_RESPONSE_RETRY_BACKOFF_SECONDS)
    return last_invocation


def _extract_final_text(stdout: str) -> Optional[str]:
    """`opencode run --format json` emits one JSON object per line. Text
    parts carry `part.type == "text"` and are grouped by messageID; the last
    message's concatenated text parts are the agent's final answer. Anything
    that isn't a recognizable text-part line is ignored here (tool events,
    step markers, etc.) -- the full stdout is still captured verbatim in the
    artifact regardless of whether this extraction succeeds."""
    texts_by_message: dict[str, list[str]] = {}
    order: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        part = obj.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str):
            continue
        message_id = part.get("messageID") or obj.get("messageID") or "unknown"
        if message_id not in texts_by_message:
            texts_by_message[message_id] = []
            order.append(message_id)
        texts_by_message[message_id].append(text)
    if not order:
        return None
    return "".join(texts_by_message[order[-1]])


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_json_block(text: str) -> tuple[Optional[dict], Optional[str]]:
    """Every agent in this pipeline is instructed to answer with exactly one
    fenced ```json block and nothing else. Try that first; fall back to
    treating the whole answer as bare JSON in case the model dropped the
    fence but still emitted a clean object -- never accept partial/garbled
    JSON silently."""
    candidates = _JSON_BLOCK_RE.findall(text)
    candidates.append(text)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed, None
    return None, "no valid JSON object found in agent output"


# --------------------------------------------------------------------------- #
# ir_service client -- the only place propose_record/commit_record/
# flag_unresolved are ever called from
# --------------------------------------------------------------------------- #


class IRServiceClient:
    """Wraps whatever HTTP-shaped client is handed in (`httpx.Client` in
    production, `fastapi.testclient.TestClient` in tests -- both expose the
    same `.get`/`.post` interface) so the control-flow logic below is
    identical in both cases and testable without a live subprocess."""

    def __init__(self, http_client: Any):
        self._client = http_client

    def health(self) -> dict:
        resp = self._client.get("/health")
        resp.raise_for_status()
        return resp.json()

    def propose_record(self, **kwargs: Any) -> dict:
        resp = self._client.post("/propose_record", json=kwargs)
        resp.raise_for_status()
        return resp.json()

    def commit_record(self, **kwargs: Any) -> dict:
        resp = self._client.post("/commit_record", json=kwargs)
        if resp.status_code == 200:
            return {"committed": True, **resp.json()}
        return {"committed": False, "status_code": resp.status_code, "detail": _safe_detail(resp)}

    def flag_unresolved(self, **kwargs: Any) -> dict:
        resp = self._client.post("/flag_unresolved", json=kwargs)
        if resp.status_code == 200:
            return {"recorded": True, **resp.json()}
        return {"recorded": False, "status_code": resp.status_code, "detail": _safe_detail(resp)}


def _safe_detail(resp: Any) -> Any:
    try:
        body = resp.json()
    except (ValueError, TypeError):
        return {"message": resp.text}
    return body.get("detail", body) if isinstance(body, dict) else body


# --------------------------------------------------------------------------- #
# Health / staleness guard
# --------------------------------------------------------------------------- #


def check_health(ir_service_url: str) -> tuple[bool, str]:
    """Refuses to run against an unreachable or stale ir_service, per the
    concrete failure this is designed to catch: see `fingerprint.py`'s
    docstring. Never silently proceeds on a mismatch."""
    try:
        with httpx.Client(base_url=ir_service_url, timeout=10.0) as client:
            resp = client.get("/health")
    except httpx.HTTPError as exc:
        return False, (
            f"ir_service unreachable at {ir_service_url}: {exc}\n"
            "Start it with:\n"
            "  uvicorn pipeline.ir_service:app --host 127.0.0.1 --port 8420"
        )
    if resp.status_code != 200:
        return False, f"ir_service /health returned HTTP {resp.status_code}"
    body = resp.json()
    local_fp = schema_fingerprint()
    remote_fp = body.get("schema_fingerprint")
    if remote_fp != local_fp:
        return False, (
            f"ir_service is running STALE schema/validator code "
            f"(service reports {remote_fp}, disk is {local_fp}). "
            "Restart it: stop the running `uvicorn pipeline.ir_service:app` "
            "process and relaunch it so it picks up the current code."
        )
    return True, f"ir_service healthy (schema_fingerprint={local_fp}, uptime={body.get('uptime_seconds', 0):.0f}s)"


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def _extraction_prompt(
    paper_id: str, entity_type: str, record_id: str, prior_errors: Optional[list[dict]] = None,
    extraction_context: Optional[str] = None,
) -> str:
    # The reminder of the exact top-level shape is repeated here, not just in
    # extractor.md's system prompt: both models tested during this sprint
    # (jetstream-scout/llama-4-scout, jetstream-gpt/gpt-oss-120b) drifted
    # toward a plausible-looking but wrong shape -- a hallucinated
    # propose_record-style tool call, and a per-field {value, source,
    # provenance_label} IR-shaped object -- despite the system prompt
    # already forbidding both. Repeating the contract in the immediate user
    # turn is cheap defense in depth against that drift; it does not loosen
    # what counts as valid (run_record still checks for a literal `facts`
    # array and drops anything else).
    return (
        f"Extract raw evidence for a Sage IR `{entity_type}` record "
        f"(record_id=`{record_id}`) from paper_id=`{paper_id}`. Use "
        f"read_document_start/read_section/read_table/list_sections/"
        f"read_section_by_path/read_table_row/read_table_cell/read_nearby to "
        f"find the relevant passages -- prefer read_table_row/read_table_cell "
        f"over read_table when citing one specific value from a table, so the "
        f"anchor you cite is the one the tool hands you, not one you have to "
        f"work out yourself.\n\n"
        f"Output ONLY a RawExtraction JSON object with exactly these top-level "
        f"keys: paper_id, entity_type, record_id, facts, extraction_notes. "
        f"`facts` is a flat array of {{field_name, raw_value, raw_text_excerpt, "
        f"anchors, notes}} entries -- every fact you report (title, author, "
        f"year, or anything else) is one entry in that array, never a "
        f"top-level key of its own and never wrapped in {{value, "
        f"provenance_label, source}}. Do not call, or write JSON that "
        f"imitates, any tool (you have none besides the read tools listed "
        f"above) -- your answer is the RawExtraction object itself, nothing "
        f"else."
    ) + (
        # Phase A (multi-record Variable): a prior, orchestrator-run
        # enumeration pass already identified this specific candidate and
        # the anchor(s) it was found at -- passed through unmodified as
        # targeted context, never as a value to copy verbatim without
        # re-reading the source yourself. Every other (single-record)
        # caller of this function omits extraction_context entirely, so
        # this branch never fires for them -- byte-identical prompt to
        # before this parameter existed.
        f"\n\nThis record is specifically for the following already-identified "
        f"candidate (found by an earlier enumeration pass, not yet fully "
        f"extracted): {extraction_context}\n"
        f"Focus your reading on confirming and extracting full details for "
        f"THIS SPECIFIC one. Do not report facts that actually belong to a "
        f"different {entity_type} this paper may also mention."
        if extraction_context
        else ""
    ) + (
        "\n\nThe previous attempt failed shape validation with these errors "
        "-- fix exactly these, the rest of your approach was fine:\n"
        + json.dumps(prior_errors, indent=2)
        if prior_errors
        else ""
    )


# Universal Multi-Record pass: per-entity-type "what counts as a distinct
# record" reinforcement, grounded directly in the Calibration/Validation
# Data Collection Protocol -- one short sentence each, not a rewrite of the
# generic merge-don't-split rule already in `_enumeration_prompt`. Originally
# omitted for Variable, Treatment, and Observation on the assumption that the
# generic merge-don't-split rule already sufficed for them -- disproven by
# real evidence (design-review session following the Daren-1997-Canopy /
# Oceologia-1998 / Kathryn-2020-Winter paper audits): Treatment merged two
# distinctly-named CO2 levels described in one sentence into one candidate,
# and Observation repeatedly collapsed an entire population/system/time grid
# into one candidate per variable name (a real run capturing exactly ONE of
# 36 real Table-2 cells for Daren's total_dry_matter_yield; a live-caught
# Kathryn candidate bundling 5 systems' distinct carbon-input values into a
# single candidate). Treatment and Observation now have their own entries
# below. Variable's omission remains correct and unchanged: it is a registry
# entity (one record per named quantity, reused by many Observations, per
# its own docstring's "primary key `name`") -- no under-merging evidence was
# found for it in any of the three audits, and applying Treatment/
# Observation-style per-combination splitting to it would incorrectly
# fragment one real named quantity into duplicate Variable records for each
# context it happens to be measured in.
_ENTITY_IDENTITY_GUIDANCE: dict[str, str] = {
    "Treatment": (
        "A Treatment is one distinct applied condition or level of an experimental factor -- when "
        "a paper names two or more levels of the SAME factor, even in one sentence (e.g. 'ambient "
        "(375 ppm) and elevated (700 ppm) atmospheric CO2', or 'quadrennial vs. annual cover "
        "cropping'), EACH NAMED LEVEL is a separate Treatment candidate, never one candidate "
        "describing both. Different numeric values, different explicit level names (ambient/"
        "elevated, control/treated, low/high), or different named systems/plots are each enough "
        "on their own to make two mentions distinct -- do not require them to appear in separate "
        "sentences or a table to count as 'clear, specific evidence they are different'; a real "
        "run merged 'ambient' and 'elevated' CO2 into one candidate because they were named in one "
        "sentence, while a standalone test on the same paper correctly split them -- the level of "
        "the writing (prose vs. table) must not change how many distinct Treatments exist."
    ),
    "Observation": (
        "An Observation is ONE reported value for ONE combination of (variable, treatment/group, "
        "site, time-point) -- when a table or passage reports a value broken out by more than one "
        "named group, system, population, maturity stage, site, or time-point, EACH broken-out "
        "value is its own Observation candidate; never average, pick one representative cell, or "
        "report only the first row when the source reports several. A candidate whose own "
        "description says 'for each population and maturity' or 'for each system' is a signal you "
        "have NOT finished splitting -- keep splitting until each candidate corresponds to exactly "
        "one cell/row the paper actually reports, not a whole row/column/table of them. A "
        "cross-group summary value (a 'Mean' or 'overall' row/figure spanning multiple treatments) "
        "is itself a separate, distinctly-anchored candidate -- it must never be substituted for, "
        "or merged with, any one specific treatment's own value; if you cannot tell which single "
        "treatment a value belongs to, do not force it onto one -- that is a signal this is a "
        "cross-group summary, report it as its own candidate or omit it, never guess."
    ),
    "Site": (
        "A Site is the experimental LOCATION only (Protocol Section 6.2) -- never create a "
        "separate Site for each plot, block, or treatment at the same physical location; only "
        "genuinely different physical locations (e.g. two field stations, or a greenhouse study "
        "plus a field study) are distinct Sites."
    ),
    "Species": (
        "A Species is a taxonomic identity (genus + species epithet), reusable across the whole "
        "paper -- if the paper studies more than one species (e.g. comparing crop rotations of "
        "corn and soybean), each is a distinct Species; different cultivars/varieties of the SAME "
        "species are NOT distinct Species (that is Crop, a separate entity type)."
    ),
    "Method": (
        "A Method is one distinct measurement/analytical procedure (Protocol Section 6.4) -- a "
        "paper using several different methods for different variables (e.g. dry combustion for "
        "soil carbon, a gas chromatograph for N2O flux, an isotope tracer for 15N recovery) has "
        "that many distinct Methods, not one; a real, material distinction such as dry vs wet "
        "basis, concentration vs stock, or depth interval is enough to make two methods distinct "
        "even if the paper never gives them separate names."
    ),
    "Crop": (
        "A Crop is the specific cultivar/variety actually used in the experiment (Protocol Section "
        "3/16.1), distinct from Species (taxonomy) -- if the paper compares multiple named "
        "cultivars of the same species, each named cultivar is a distinct Crop."
    ),
    "Management": (
        "A Management record is ONE EVENT (Protocol Section 6.6: 'keep one row per event') -- two "
        "occurrences of the SAME event type (e.g. two separate fertilization applications, or "
        "planting followed later by harvest) are TWO distinct Management records even when they "
        "share an event_type, because they happened at different times and/or with different "
        "reported amounts; do not collapse a sequence of events into one generic record."
    ),
    "Study": (
        "A Study is a real-world experiment, distinct from Citation (the paper reporting it) -- "
        "most papers describe exactly one Study; only report more than one when the paper clearly "
        "describes two genuinely separate experiments (different designs, sites, or objectives), "
        "never merely because results are broken out by year or by factor."
    ),
    "TreatmentPair": (
        "A TreatmentPair is one EXPLICIT, NAMED comparison the paper itself draws between two of "
        "its own Treatments (Protocol Section 7.3, e.g. 'compost vs. no compost', 'tilled vs. "
        "zero-till') -- only report a pair when the paper actually frames that comparison, never "
        "invent a pairing between two treatments merely because both exist; if the paper draws no "
        "such explicit comparison, an empty candidates array is the correct, normal outcome."
    ),
    "Coverage": (
        "A Coverage record is a data-availability rollup (how many rows of some kind exist for "
        "one site/variable combination), not something a paper's prose typically states directly -- "
        "only report a candidate when the paper contains an explicit statement about how much data "
        "exists (e.g. 'daily measurements were taken over 3 years'); do not infer a Coverage record "
        "merely because Observations for that site/variable exist elsewhere in this run."
    ),
}


def _enumeration_prompt(
    paper_id: str, entity_type: str, prior_errors: Optional[list[dict]] = None,
    link_pools: Optional[dict[str, list[dict]]] = None,
) -> str:
    """Phase A (multi-record Variable), Phase B (+ Treatment), Phase C (+
    Observation): asks the SAME extractor agent (same tools, same sealed
    no-write access) a narrower question than full extraction -- "how many
    distinct real ones are there, and where", not "extract every field for
    one of them". Entirely orchestrator-authored, exactly like
    `_extraction_prompt` above; no new agent, no change to extractor.md.

    Phase C: `link_pools` (field_name -> [{slug, name}, ...]) is populated
    by `_multi_record_link_pools` only when this entity_type has its own
    dependency on ANOTHER multi-record type that already has ready records
    in this run (e.g. Observation depending on Treatment/Variable) -- for
    Treatment/Variable's OWN enumeration (dependencies are single-record
    Citation/Site) this is always empty, so their prompt is byte-identical
    to before Phase C.

    Universal Multi-Record pass: `_ENTITY_IDENTITY_GUIDANCE` below adds ONE
    short, protocol-grounded sentence for entity types whose "what counts as
    a distinct record" rule isn't obvious from the generic merge-don't-split
    guidance alone (e.g. Management: the Calibration/Validation Protocol
    Section 6.6 requires "one row per event" -- two fertilization events at
    different dates are two records even though they'd otherwise look like
    duplicates by name/type alone; Treatment and Observation, added in the
    enumeration-granularity design-review session, for the same reason --
    see `_ENTITY_IDENTITY_GUIDANCE`'s own comment for the real evidence).
    Absent only for Variable, whose one-record-per-named-quantity identity
    genuinely is unambiguous from the generic guidance alone (see the same
    comment for why Variable is different in kind from Treatment/Observation)."""
    base = (
        f"Identify every DISTINCT real-world {entity_type} that paper_id=`{paper_id}` actually "
        f"reports, measures, or observes -- however many genuinely exist, not a fixed number. "
        f"Use read_document_start/read_section/read_table/list_sections/read_section_by_path/"
        f"read_table_row/read_table_cell/read_nearby -- passing paper_id=`{paper_id}` to EVERY "
        f"one of them -- to find them.\n\n"
        f"Output ONLY an EnumerationResult JSON object with exactly these top-level keys: "
        f"entity_type, candidates. `candidates` is a flat array of "
        f"{{candidate_id, description, anchors, linked_candidates}} entries -- one entry per "
        f"DISTINCT {entity_type} you find, never a top-level key of its own.\n\n"
        f"- candidate_id: a short, stable, lowercase slug you choose (e.g. 'leaf_area_index'), "
        f"unique among the candidates you report in this same answer.\n"
        f"- description: one concise sentence identifying this specific {entity_type} and "
        f"distinguishing it from any others you report.\n"
        f"- anchors: at least one content.md block anchor you ACTUALLY READ that supports this "
        f"being a real, distinct {entity_type} -- never an anchor you did not look at, never "
        f"invented just to satisfy this field.\n"
    )
    if link_pools:
        pool_lines = []
        for field, pool in link_pools.items():
            pool_lines.append(f'For "{field}", the already-extracted candidates for this paper are:')
            for item in pool:
                label = item.get("name") or "(name not yet resolved)"
                pool_lines.append(f'  - slug "{item["slug"]}": {label!r}')
        base += (
            f"- linked_candidates: a JSON object naming, for each field below, the SPECIFIC "
            f"already-extracted record this {entity_type} is actually about -- ONLY when the "
            f"evidence clearly ties it to exactly one. Use the EXACT slug string shown (e.g. "
            f'{{"treatment_id": "ambient_co2"}}), never a slug you invent or one not listed below, '
            f"and never guess when the paper does not make the link explicit -- omit that key "
            f"entirely rather than guessing.\n\n" + "\n".join(pool_lines) + "\n\n"
        )
    else:
        base += "- linked_candidates: leave as an empty object {} -- not used for this entity type.\n\n"
    base += (
        f"Do not invent {entity_type}s that are not actually reported in this paper. If you are "
        f"uncertain whether two mentions refer to the same {entity_type} or two different ones, "
        f"MERGE them into a single candidate unless the text gives clear, specific evidence they "
        f"are different (different units, different definitions, explicitly distinguished names). "
        f"If the paper genuinely reports none, output an empty candidates array -- do not "
        f"fabricate one just to avoid an empty result."
    )
    identity_guidance = _ENTITY_IDENTITY_GUIDANCE.get(entity_type)
    if identity_guidance:
        base += f"\n\n{identity_guidance}"
    if prior_errors:
        base += (
            "\n\nThe previous attempt failed validation with these errors -- fix exactly these, "
            "the rest of your approach was fine:\n" + json.dumps(prior_errors, indent=2)
        )
    return base


def run_enumeration(
    *, run_id: str, paper_id: str, entity_type: str, model: str,
    invoke: Callable[..., AgentInvocation] = invoke_agent,
    link_pools: Optional[dict[str, list[dict]]] = None,
) -> tuple[list, Optional[str]]:
    """Bounded, deterministically-validated enumeration pass for a
    multi-record entity type. Returns (candidates, None) on success --
    `candidates` may legitimately be an empty list, that is not an error --
    or ([], error_message) if no valid, anchor-grounded EnumerationResult
    was produced within the attempt budget. Never trusts the model's own
    claim that an anchor is real: every cited anchor is checked against the
    paper's actual rendered content.md, reusing `_load_rendered_blocks`
    (the same anchor-resolution `_anchor_texts` elsewhere in this module
    already relies on), not a new anchor-checking mechanism.

    Phase C: when `link_pools` is given (Observation depending on
    Treatment/Variable, both multi-record), every candidate's
    `linked_candidates` values are likewise never trusted at face value --
    each one must name a slug actually present in the corresponding pool,
    checked deterministically here, same bounded-retry treatment as an
    invalid anchor. `_run_multi_record_entity`/`_apply_candidate_links`
    perform this exact same check again before actually using a link (the
    real safety boundary); this is purely for a useful retry signal."""
    record_key = f"{entity_type}__enumeration"
    errors: list[dict] = []
    last_message = "enumeration never produced a valid EnumerationResult"

    for attempt in range(1, MAX_ENUMERATION_ATTEMPTS + 1):
        result = invoke("extractor", model, _enumeration_prompt(paper_id, entity_type, errors, link_pools))
        artifact = result.as_artifact()

        if result.parsed_json is None:
            errors = [{"field": None, "message": result.parse_error}]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", attempt, artifact)
            last_message = f"attempt {attempt}: {result.parse_error}"
            continue

        try:
            validated = EnumerationResult.model_validate(result.parsed_json)
        except ValidationError as exc:
            errors = [
                {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()
            ]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", attempt, artifact)
            last_message = f"attempt {attempt}: EnumerationResult shape validation failed: {errors}"
            continue

        try:
            blocks = _load_rendered_blocks(paper_id)
        except FileNotFoundError as exc:
            errors = [{"field": None, "message": f"cannot validate candidate anchors: {exc}"}]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", attempt, artifact)
            last_message = f"attempt {attempt}: {errors[0]['message']}"
            continue

        invalid_anchor_errors = [
            {
                "field": "anchors",
                "message": f"candidate '{candidate.candidate_id}' cites anchor '{anchor}' which does not "
                           f"exist in content.md for paper '{paper_id}' -- never cite an anchor you did not "
                           f"actually read.",
            }
            for candidate in validated.candidates
            for anchor in candidate.anchors
            if anchor.strip("[]") not in blocks
        ]
        if invalid_anchor_errors:
            errors = invalid_anchor_errors
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", attempt, artifact)
            last_message = f"attempt {attempt}: invalid anchors: {errors}"
            continue

        invalid_link_errors = [
            {
                "field": "linked_candidates",
                "message": f"candidate '{candidate.candidate_id}' links {field!r} to slug "
                           f"'{slug}', which is not one of the known candidates for {field!r} "
                           f"listed in the prompt -- never invent a slug or link to one not offered.",
            }
            for candidate in validated.candidates
            for field, slug in (candidate.linked_candidates or {}).items()
            if link_pools and field in link_pools and slug not in {item["slug"] for item in link_pools[field]}
        ]
        if invalid_link_errors:
            errors = invalid_link_errors
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", attempt, artifact)
            last_message = f"attempt {attempt}: invalid linked_candidates: {errors}"
            continue

        artifact["validation_errors"] = []
        run_store.save_stage_attempt(run_id, record_key, "enumeration", attempt, artifact)
        return validated.candidates, None

    run_store.save_final(run_id, record_key, {"status": "error", "message": last_message})
    return [], last_message


_SANITIZE_CANDIDATE_ID_RE = re.compile(r"[^a-z0-9]+")


def _sanitize_candidate_id(candidate_id: str) -> str:
    """Deterministic, orchestrator-side normalization of a model-chosen
    candidate_id into something safe to embed in a record_id/filename --
    the model's raw string is never trusted directly as an id component."""
    slug = _SANITIZE_CANDIDATE_ID_RE.sub("_", candidate_id.strip().lower()).strip("_")
    return slug or "candidate"


def _run_multi_record_entity(
    *, run_id: str, paper_id: str, entity_type: str, model: str, client: IRServiceClient,
    invoke: Callable[..., AgentInvocation] = invoke_agent, enable_ai_validation: bool,
    this_run_records: dict,
) -> list[dict]:
    """Phase A (Variable), Phase B (+ Treatment): enumeration + one
    `run_record()` call per enumerated candidate. `run_record()` itself is
    completely unmodified in its control flow -- every existing
    deterministic-validation, provenance, AI-Validator, retry, and
    unresolved/turn-cap behavior applies per-candidate exactly as it does
    for any single-record entity type today. One candidate's failure never
    affects another: each gets its own independent `run_record()` call in
    a plain loop, no shared early-exit state.

    Phase C (+ Observation): unlike Treatment/Variable's own dependencies
    (Citation, Site -- both single-record, so every candidate shares the
    exact same `known_refs`), Observation depends on Treatment AND
    Variable, both themselves multi-record -- so WHICH specific Treatment/
    Variable applies can differ per Observation candidate.
    `_multi_record_link_pools` exposes this run's other already-ready
    multi-record candidates to the enumeration prompt, and
    `_apply_candidate_links` turns each candidate's own (deterministically
    verified) `linked_candidates` into a per-candidate refinement of the
    entity-wide `known_refs` -- never a guess made by this function itself
    (a field that isn't linked and remains ambiguous is either left
    omitted (optional) or constrained to the real, already-extracted
    allowed set (required) -- see `_apply_candidate_links`'s own
    docstring). For Treatment/Variable, whose own dependencies are all
    single-record, `_multi_record_link_pools` always returns {} and
    `_apply_candidate_links` is a no-op -- byte-identical to before Phase C.

    Universal Multi-Record pass: `_resolve_known_refs` is checked BEFORE
    enumeration, not after. Before this, a structurally blocked multi-record
    entity (a required prerequisite not ready, e.g. TreatmentPair with fewer
    than 2 ready Treatments) still spent one full enumeration LLM call
    before marking every candidate it found "blocked" -- correct but
    wasteful, exactly the kind of unnecessary-retry cost the Observation
    efficiency work (Phase C conversion loop) was built to eliminate
    elsewhere. A structurally blocked entity can never produce a valid
    record regardless of what enumeration finds, so there is nothing to
    enumerate for yet -- this now short-circuits with a single synthetic
    "blocked" entry (using the same `_entity_record_id` convention the
    single-record path already uses for its own blocked entries), matching
    single-record `run_paper()` behavior byte-for-byte."""
    known_refs, blocked_reason = _resolve_known_refs(entity_type, this_run_records)
    if blocked_reason is not None:
        return [{
            "entity_type": entity_type, "record_id": _entity_record_id(paper_id, entity_type),
            "status": "blocked", "reason": blocked_reason,
        }]

    link_pools = _multi_record_link_pools(paper_id, entity_type, this_run_records)
    candidates, enum_error = run_enumeration(
        run_id=run_id, paper_id=paper_id, entity_type=entity_type, model=model, invoke=invoke,
        link_pools=link_pools or None,
    )
    if enum_error is not None or not candidates:
        return []

    record_infos = []
    for candidate in candidates:
        record_id = f"{paper_id}_{entity_type.lower()}_{_sanitize_candidate_id(candidate.candidate_id)}"
        candidate_known_refs = _apply_candidate_links(
            paper_id, entity_type, this_run_records, known_refs, candidate,
        )
        context = (
            f"{candidate.description} (identified by an earlier enumeration pass from "
            f"anchor(s): {', '.join(candidate.anchors)})"
        )
        result = run_record(
            run_id=run_id, paper_id=paper_id, entity_type=entity_type, record_id=record_id,
            model=model, client=client, invoke=invoke, enable_ai_validation=enable_ai_validation,
            known_refs=candidate_known_refs or None, extraction_context=context,
        )
        record_info = {
            "entity_type": entity_type, "record_id": record_id,
            "status": result.status, "detail": result.detail,
        }
        record_infos.append(record_info)
    return record_infos


def _conversion_prompt(
    paper_id: str, entity_type: str, record_id: str, raw_extraction: dict, prior_errors: Optional[list[dict]],
    known_refs: Optional[dict[str, str]] = None,
) -> str:
    parts = [
        f"Map the following sealed raw evidence into a Sage IR `{entity_type}` "
        f"record (record_id=`{record_id}`). Call get_schema first.",
        "",
        "RAW_EVIDENCE:",
        "```json",
        json.dumps(raw_extraction, indent=2),
        "```",
    ]
    if known_refs:
        # ir_schema.py's bare-reference fields (site_id, citation_id,
        # treatment_id, method_id, species_id -- ExtractedReference = str,
        # IR spec Section 10) are deliberately NOT UNRESOLVED-able, unlike
        # Treatment.study_id (the one field the Option B amendment upgraded
        # to ExtractedField, specifically for cross-paper reconciliation).
        # The architecture's assumption is these always point at an entity
        # already extracted in the same session -- so the fix for "the
        # model invented a placeholder id" is to actually give it the real
        # one, not to make the field UNRESOLVED-able.
        parts += [
            "",
            "KNOWN REFERENCE IDS -- these entities already exist in this "
            "paper's dataset. For a field given as a single string, use "
            "that EXACT id string verbatim for the corresponding "
            "bare-reference field (e.g. site_id, citation_id, "
            "treatment_id, method_id, species_id) -- never invent, guess, "
            "or placeholder a different value. For a field given as a "
            "LIST of strings instead (this paper has more than one "
            "extracted candidate of that type, e.g. more than one "
            "Treatment): read the raw evidence below and choose the ONE "
            "id from that list that this specific record is actually "
            "about -- never output the list itself as the value, and "
            "never use an id that is not one of the ones listed:",
            "```json",
            json.dumps(known_refs, indent=2),
            "```",
        ]
    if prior_errors:
        parts += [
            "",
            "The previous attempt failed deterministic validation with these "
            "errors -- fix exactly these fields, do not restructure fields "
            "that were not flagged:",
            "```json",
            json.dumps(prior_errors, indent=2),
            "```",
        ]
        if any(err.get("code") == "provenance_value_mismatch" for err in prior_errors):
            # Phase 1C: don't just TELL the model to reconsider the anchor --
            # deterministically fetch the real, verbatim text of every anchor
            # already present in RAW_EVIDENCE (the only anchors Conversion is
            # ever allowed to cite -- see the "never invent a new anchor"
            # hard rule in converter.md) and hand it back, so there is
            # nothing left to recall from memory. This never expands what
            # Conversion may cite; it only resolves anchor->text for anchors
            # already in scope.
            candidate_anchors = all_anchors(raw_extraction)
            candidate_anchor_texts = _anchor_texts(paper_id, candidate_anchors)
            parts += [
                "",
                "For every `provenance_value_mismatch` above: the most likely "
                "problem is the CITED ANCHOR, not the value's wording. Below is "
                "the exact, verbatim source text for every candidate anchor "
                "already present in RAW_EVIDENCE -- deterministically fetched "
                "from content.md, not reconstructed from memory. Find a "
                "DIFFERENT anchor among these whose text actually contains the "
                "mismatched value, and cite that anchor instead. Only reword "
                "the value itself if one of these texts genuinely supports "
                "different wording. Never cite an anchor that is not listed in "
                "CANDIDATE_ANCHOR_TEXTS below -- you cannot verify a new one "
                "yourself. Do not resubmit the same value with only cosmetic "
                "rewording while keeping an anchor that has already been "
                "rejected -- that will fail the same way again.",
                "",
                "CANDIDATE_ANCHOR_TEXTS:",
                "```json",
                json.dumps(candidate_anchor_texts, indent=2),
                "```",
            ]
        if any("requires unresolved_reason populated as an inference-basis note" in (err.get("message") or "") for err in prior_errors):
            # Real Oceologia-1998 Observation runs (Phase C investigation):
            # this exact error was the single most common failure (111
            # occurrences across 59/61 candidates), almost always on
            # is_raw_replicate_level marked INFERRED with no reason at all --
            # a converter-contract gap, not evidence the value itself is
            # unextractable. Spell out the fix directly rather than relying
            # on the model to infer it from the bare pydantic message alone.
            parts += [
                "",
                "For every field above requiring an inference-basis note: either write a "
                "real, specific `unresolved_reason` sentence citing what in the cited "
                "evidence supports the INFERRED value, or -- if you cannot articulate a "
                "genuine basis -- change that field's `provenance_label` to `UNRESOLVED` "
                "instead. Never resubmit the same INFERRED value with the reason still "
                "missing or empty.",
            ]
        if any("requires aggregated_over_factors" in (err.get("message") or "") for err in prior_errors):
            # Real Oceologia-1998 Observation runs (Phase C investigation):
            # third most common failure (25 occurrences, terminal in 7/61
            # candidates) -- converter.md previously said nothing about this
            # coupling at all. See converter.md's own "reported_effect_scope
            # / aggregated_over_factors coupling" section for the full rule.
            parts += [
                "",
                "For the `reported_effect_scope` / `aggregated_over_factors` error above: if "
                "`reported_effect_scope.value == \"treatment_mean\"`, `aggregated_over_factors` "
                "must be `{\"value\": [], \"provenance_label\": \"EXTRACTED\", ...}` -- an EXTRACTED "
                "empty list, never omitted/null and never a non-empty list. If "
                "`reported_effect_scope.value == \"aggregated_mean\"`, `aggregated_over_factors` "
                "must be `EXTRACTED` with a non-empty factor-name list, or `UNRESOLVED` with a "
                "real reason -- never `EXTRACTED` with an empty list in that case.",
            ]
    parts += ["", "Output only the candidate record JSON as instructed in your system prompt."]
    return "\n".join(parts)


def _ai_validation_prompt(
    entity_type: str, candidate_payload: dict, raw_extraction: dict, anchor_texts: dict[str, str]
) -> str:
    return "\n".join(
        [
            f"Review this already deterministically-valid Sage IR `{entity_type}` candidate record.",
            "",
            "CANDIDATE_RECORD:",
            "```json",
            json.dumps(candidate_payload, indent=2),
            "```",
            "",
            "RAW_EVIDENCE_IT_WAS_BUILT_FROM:",
            "```json",
            json.dumps(raw_extraction, indent=2),
            "```",
            "",
            "CITED_ANCHOR_TEXT (verbatim from content.md):",
            "```json",
            json.dumps(anchor_texts, indent=2),
            "```",
            "",
            "Output only the critique JSON as instructed in your system prompt.",
        ]
    )


def _anchor_texts(paper_id: str, anchors: list[str]) -> dict[str, str]:
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError as exc:
        return {a: f"<error loading content.md: {exc}>" for a in anchors}
    return {a: blocks.get(a, "<anchor not found in content.md>") for a in anchors}


def _ref_mismatch(candidate_value: Any, expected: Any) -> bool:
    """Same known_refs check used by every stage -- generalized to also
    handle a list-shaped reference FIELD (Study.citation_ids is a
    list[ExtractedReference], not a bare string like every other reference
    field): a list field matches if the expected id is a member; a scalar
    field matches by equality.

    Phase C: `expected` itself may ALSO be a list -- an ALLOWED SET rather
    than one single required id (`_apply_candidate_links`'s fallback for a
    required field with more than one ready candidate and no specific
    link: Conversion, which actually reads the raw evidence, still has to
    pick exactly one, but is deterministically forbidden from fabricating
    a value outside this real, already-extracted set)."""
    if isinstance(expected, (list, set, tuple)):
        allowed = set(expected)
        if isinstance(candidate_value, list):
            return not any(v in allowed for v in candidate_value)
        return candidate_value not in allowed
    if isinstance(candidate_value, list):
        return expected not in candidate_value
    return candidate_value != expected


def _ref_expectation_message(field: str, expected: Any, actual: Any) -> str:
    """Shared error text for a `_ref_mismatch` failure -- factored out so
    both call sites (the main conversion loop and the AI-validation
    correction path) describe a Phase C allowed-SET `expected` correctly
    ("must be one of ...") instead of the single-id "must equal ..."
    wording, which would otherwise mislead the model into thinking the
    field's value should literally BE the list."""
    if isinstance(expected, (list, set, tuple)):
        return f"must be one of the known reference ids {sorted(expected)!r} (got {actual!r})"
    return f"must equal known reference id {expected!r} (got {actual!r})"


def _error_field(error: dict) -> Optional[str]:
    """Recover which field an error is actually about. Pydantic-construction
    errors (`_construction_errors`) carry a real `field` key. Provenance
    errors (`validate_provenance`/`ValidationIssue.to_dict()`) do NOT -- they
    have no `field` key at all, only `code` plus a `message` that always
    starts with `"{field_path}: ..."` (see validate_provenance's own
    `f"{field_path}: value=..."` / `f"{field_path}: locator block..."`
    formatting). Without this recovery, `_error_signature` below collapsed
    every `provenance_value_mismatch` on ANY field into one identical
    signature (same missing `field`, same `code`) -- confirmed by a real
    benchmark run: a genuinely DIFFERENT mismatch on `notes` at attempt 2
    was wrongly treated as a repeat of attempt 1's mismatch on `variable_name`,
    triggering stagnation after only 2 real attempts instead of the model
    getting its full turn-cap budget to fix each new field as it surfaced."""
    field = error.get("field")
    if field:
        return field
    message = error.get("message") or ""
    prefix, sep, _ = message.partition(":")
    return prefix.strip() if sep else None


def _error_signature(errors: list[dict]) -> Optional[frozenset]:
    """A stable, order-independent fingerprint of a validation-error list,
    used only for stagnation detection in `run_record`'s conversion loop --
    never for anything that decides validity. Keyed on (field, code) rather
    than the full message text: `provenance_value_mismatch` messages embed
    the rejected value itself, which is largely stable across a stuck
    record's retries (see this function's caller), but comparing on the
    stable (field, code) pair is robust even if a message's incidental
    wording shifts slightly between attempts. Returns None for an empty
    list (attempt 1 has no prior errors to compare against, and "no
    errors" is never itself a stagnation signature)."""
    if not errors:
        return None
    return frozenset((_error_field(e), e.get("code") or e.get("message")) for e in errors)


def _run_ai_validation(
    run_id: str, record_key: str, paper_id: str, entity_type: str, candidate_payload: dict,
    raw_extraction: dict, model: str, invoke: Callable[..., AgentInvocation], attempt: int,
) -> dict:
    """One AI Validator call, recorded under `attempt` -- factored out so
    Phase 1D's one bounded correction pass can call this exact same logic a
    second time on the corrected payload, rather than duplicating it."""
    anchors = all_anchors(raw_extraction)
    anchor_texts = _anchor_texts(paper_id, anchors)
    val_result = invoke(
        "ir-validator", model,
        _ai_validation_prompt(entity_type, candidate_payload, raw_extraction, anchor_texts),
    )
    run_store.save_stage_attempt(run_id, record_key, "ai_validation", attempt, val_result.as_artifact())
    return val_result.parsed_json or {
        "verdict": "parse_error", "issues": [], "raw_parse_error": val_result.parse_error,
    }


def _attempt_ai_validation_correction(
    *, run_id: str, record_key: str, paper_id: str, entity_type: str, record_id: str,
    raw_extraction: dict, known_refs: Optional[dict[str, str]], ai_validation: dict,
    model: str, client: IRServiceClient, invoke: Callable[..., AgentInvocation], stage_counts: dict,
) -> dict:
    """Phase 1D: exactly ONE bounded Conversion correction pass, triggered
    only by the AI Validator's "suspicious" verdict -- never a second,
    unbounded retry loop of its own. The corrected payload is run through
    the SAME deterministic known_refs check and propose_record validation
    every other candidate payload goes through in the main loop above; the
    AI Validator's opinion never substitutes for or bypasses that gate.

    Returns {"ok": True, "payload", "ai_validation"} on a corrected payload
    that both passes deterministic validation and was re-checked by the AI
    Validator, or {"ok": False, "payload", "errors"} when the correction
    attempt itself fails (crash, known_refs mismatch, or deterministic
    validation) -- the caller falls back to the existing flag_unresolved
    off-ramp in either failure case, never force-committing.
    """
    correction_errors = [
        {"field": issue.get("field"), "message": f"AI Validator concern: {issue.get('concern')}"}
        for issue in (ai_validation.get("issues") or [])
    ] or [{
        "field": None,
        "message": "AI Validator flagged this record as suspicious overall; re-examine the cited "
                   "evidence and correct or better justify the flagged field(s).",
    }]

    stage_counts["conversion"] += 1
    conv_result = invoke(
        "converter", model,
        _conversion_prompt(paper_id, entity_type, record_id, raw_extraction, correction_errors, known_refs),
    )
    run_store.save_stage_attempt(run_id, record_key, "conversion", stage_counts["conversion"], conv_result.as_artifact())

    if conv_result.parsed_json is None:
        return {
            "ok": False, "payload": None,
            "errors": [{"field": None, "message": f"AI-Validator-triggered correction attempt: {conv_result.parse_error}"}],
        }

    corrected_payload = conv_result.parsed_json

    ref_errors = [
        {"field": field, "message": _ref_expectation_message(field, expected, corrected_payload.get(field))}
        for field, expected in (known_refs or {}).items()
        if field in corrected_payload and _ref_mismatch(corrected_payload.get(field), expected)
    ]
    if ref_errors:
        run_store.save_stage_attempt(
            run_id, record_key, "conversion_validation", stage_counts["conversion"],
            {"valid": False, "errors": ref_errors, "source": "orchestrator_known_refs_check"},
        )
        return {"ok": False, "payload": corrected_payload, "errors": ref_errors}

    propose_result = client.propose_record(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=corrected_payload,
    )
    run_store.save_stage_attempt(run_id, record_key, "conversion_validation", stage_counts["conversion"], propose_result)
    if not propose_result.get("valid"):
        return {"ok": False, "payload": corrected_payload, "errors": propose_result.get("errors", [])}

    stage_counts["ai_validation"] = 2
    new_ai_validation = _run_ai_validation(
        run_id, record_key, paper_id, entity_type, corrected_payload, raw_extraction, model, invoke, attempt=2,
    )
    return {"ok": True, "payload": corrected_payload, "ai_validation": new_ai_validation}


# --------------------------------------------------------------------------- #
# Per-record control loop
# --------------------------------------------------------------------------- #


@dataclass
class RecordResult:
    status: str  # "ready" | "unresolved" | "error"
    entity_type: str
    record_id: str
    detail: dict


def run_record(
    *,
    run_id: str,
    paper_id: str,
    entity_type: str,
    record_id: str,
    model: str,
    client: IRServiceClient,
    invoke: Callable[..., AgentInvocation] = invoke_agent,
    enable_ai_validation: bool = True,
    known_refs: Optional[dict[str, str]] = None,
    extraction_context: Optional[str] = None,
) -> RecordResult:
    record_key = f"{entity_type}__{record_id}"
    stage_counts = {"extraction": 0, "conversion": 0, "ai_validation": 0}

    # --- Stage 1: Extraction (sealed evidence gathering) ---
    raw_extraction: Optional[dict] = None
    extraction_errors: list[dict] = []
    last_extraction_message = "extraction stage never produced a valid RawExtraction"
    # Tracks whether EVERY failed attempt was specifically the provider-
    # empty-response class (invoke_agent's own internal retry already
    # absorbed most transient occurrences; this catches the case where it
    # was exhausted on every single numbered attempt too) -- set False the
    # moment any attempt fails for a REAL content/shape reason instead, so
    # a candidate that failed for a genuine reason is never mislabeled.
    all_extraction_failures_were_empty_response = True
    for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        stage_counts["extraction"] = attempt
        result = invoke(
            "extractor", model,
            _extraction_prompt(paper_id, entity_type, record_id, extraction_errors, extraction_context),
        )
        artifact = result.as_artifact()

        if result.parsed_json is None:
            if result.final_text is not None:
                all_extraction_failures_were_empty_response = False
            extraction_errors = [{"field": None, "message": result.parse_error}]
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: {result.parse_error}"
            continue

        # Deterministic shape check -- a loose "did it have a facts key"
        # check is not enough: RawFact's own invariants (anchors required,
        # etc.) must actually hold before this evidence is handed to the
        # sealed Conversion stage.
        try:
            validated = RawExtraction.model_validate(result.parsed_json)
        except ValidationError as exc:
            all_extraction_failures_were_empty_response = False
            extraction_errors = [
                {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()
            ]
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: RawExtraction shape validation failed: {extraction_errors}"
            continue

        if not validated.facts:
            all_extraction_failures_were_empty_response = False
            extraction_errors = [{"field": "facts", "message": "facts array was empty -- report at least one fact or explain in extraction_notes"}]
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: empty facts array"
            continue

        artifact["validation_errors"] = []
        run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
        raw_extraction = validated.model_dump()
        break

    if raw_extraction is None:
        failure_class = "provider_empty_response" if all_extraction_failures_were_empty_response else None
        return _finalize_error(run_id, record_key, entity_type, record_id, last_extraction_message, stage_counts, failure_class=failure_class)

    # Refuse-to-guess gate (Kathryn-2020-Winter conversion-misattribution
    # finding, enumeration-granularity design-review session): a `list`
    # value in `known_refs` means ONLY one thing, by construction of
    # `_apply_candidate_links` -- a REQUIRED reference field this
    # candidate's own `linked_candidates` did NOT resolve to one specific
    # already-extracted record, left as the full allowed-set of this run's
    # ready record_ids as a membership safety net (an OPTIONAL field left
    # ambiguous the same way is always omitted, never a list -- see that
    # function's own docstring, case 3). Real Kathryn-2020-Winter evidence
    # (run 20260916T100946_333f3166): every one of that run's 8 Observation
    # records ended up attached to the SAME single treatment_id regardless
    # of which system's value it actually reported -- e.g.
    # cover_crop_shoot_carbon_inputs=6.3 (System 1's real value, per Fig 2's
    # caption) was committed under System 3's treatment_id, and
    # soil_c_n_ratio used the cross-system Mean row attributed to one
    # specific system -- both `ready`, both silently wrong, because the
    # existing safety net (`_ref_mismatch`'s allowed-set membership check)
    # only verifies the chosen id is A valid choice, never that it is THE
    # correct one for the cited evidence -- membership passes for any of
    # the 4-5 candidates equally. There is no cheap, general, deterministic
    # way to verify "is this specific value really about this specific
    # treatment" without re-deriving the same judgment a human review is
    # for -- so, per the explicit "never guess merely because multiple
    # records exist" requirement already stated in
    # `_apply_candidate_links`'s own docstring, this refuses outright
    # rather than let Conversion attempt a choice the rest of the pipeline
    # cannot verify. This should be rare after the enumeration-granularity
    # fix (a correctly per-treatment-split candidate's own evidence usually
    # resolves via `linked_candidates` instead, case 1) -- but a paper can
    # still legitimately present a cross-group "Mean"/"overall" value
    # alongside per-group ones even after correct splitting, so this stays
    # as a permanent safeguard, not a temporary patch.
    ambiguous_fields = {
        field: values for field, values in (known_refs or {}).items() if isinstance(values, list)
    }
    if ambiguous_fields:
        last_errors = [
            {
                "field": field,
                "message": (
                    f"{field} is ambiguous among {len(values)} equally-valid candidates {values} -- "
                    f"this record's own evidence did not tie it to exactly one at enumeration time, "
                    f"and no deterministic check can verify a Conversion guess against the cited "
                    f"evidence. Refusing to guess rather than risk attaching this value to the wrong "
                    f"{field.removesuffix('_id')}."
                ),
            }
            for field, values in sorted(ambiguous_fields.items())
        ]
        return _finalize_unresolved(
            run_id, client, paper_id, entity_type, record_id, record_key,
            raw_extraction, None, last_errors, stage_counts,
        )

    # --- Stage 2: Conversion, gated by deterministic validation (Stage 3) ---
    last_errors: list[dict] = []
    candidate_payload: Optional[dict] = None
    propose_result: Optional[dict] = None
    previous_error_signature: Optional[frozenset] = None
    for attempt in range(1, MAX_CONVERSION_LOOP_SAFETY + 1):
        if propose_result is not None:
            # Real Oceologia-1998 Observation runs (Phase C investigation,
            # run 20260915T135025_d291c220) showed 55/61 candidates always
            # spent one FULLY WASTED conversion LLM call (~15-20s) right
            # before giving up: ir_service's own MAX_PROPOSE_ATTEMPTS (4) had
            # already been exhausted by the previous real attempt, so the
            # next propose_record call was guaranteed to return
            # forced_flag_unresolved without even looking at the new
            # payload. Checking the server's own reported attempts_remaining
            # BEFORE paying for another conversion call catches this
            # deterministically -- it never changes which records end up
            # ready/unresolved, it only stops calling the model once calling
            # it again cannot possibly help.
            if propose_result.get("forced_flag_unresolved") or propose_result.get("attempts_remaining") == 0:
                break
            # Stagnation detection: the exact same deterministic-validation
            # error signature recurring unchanged across two consecutive
            # real attempts is real evidence the model is not converging
            # (confirmed against real runs: once an error signature repeated
            # verbatim -- e.g. the same wrong anchor re-cited for the same
            # field -- it kept recurring unchanged until the turn cap forced
            # a stop, never actually resolving). Stopping here preserves the
            # real attempts/errors already captured for flag_unresolved and
            # frees the remaining turn-cap budget rather than spending it on
            # a call very unlikely to produce a different outcome.
            current_signature = _error_signature(last_errors)
            if current_signature is not None and current_signature == previous_error_signature:
                break
            previous_error_signature = current_signature

        stage_counts["conversion"] = attempt
        conv_result = invoke(
            "converter", model,
            _conversion_prompt(paper_id, entity_type, record_id, raw_extraction, last_errors, known_refs),
        )
        run_store.save_stage_attempt(run_id, record_key, "conversion", attempt, conv_result.as_artifact())

        if conv_result.parsed_json is None:
            last_errors = [{"field": None, "message": f"conversion attempt {attempt}: {conv_result.parse_error}"}]
            continue

        candidate_payload = conv_result.parsed_json

        # Deterministic cross-check against known_refs, before spending a
        # propose_record attempt: a bare ExtractedReference field
        # (site_id/citation_id/etc.) that doesn't match an id we already
        # know is correct is never a legitimate value -- ir_service's own
        # validators can't catch this (it has no concept of "known refs"),
        # so this check belongs here, not as a schema change.
        ref_errors = [
            {"field": field, "message": _ref_expectation_message(field, expected, candidate_payload.get(field))}
            for field, expected in (known_refs or {}).items()
            if field in candidate_payload and _ref_mismatch(candidate_payload.get(field), expected)
        ]
        if ref_errors:
            run_store.save_stage_attempt(
                run_id, record_key, "conversion_validation", attempt,
                {"valid": False, "errors": ref_errors, "source": "orchestrator_known_refs_check"},
            )
            last_errors = ref_errors
            continue

        propose_result = client.propose_record(
            paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=candidate_payload,
        )
        run_store.save_stage_attempt(run_id, record_key, "conversion_validation", attempt, propose_result)

        if propose_result.get("valid"):
            break
        last_errors = propose_result.get("errors", [])
        if propose_result.get("forced_flag_unresolved"):
            break

    if not propose_result or not propose_result.get("valid"):
        return _finalize_unresolved(
            run_id, client, paper_id, entity_type, record_id, record_key,
            raw_extraction, candidate_payload, last_errors, stage_counts,
        )

    # --- Stage 4: AI Validator (Phase 1D: wired -- one bounded correction) ---
    ai_validation: Optional[dict] = None
    if enable_ai_validation:
        stage_counts["ai_validation"] = 1
        ai_validation = _run_ai_validation(
            run_id, record_key, paper_id, entity_type, candidate_payload, raw_extraction, model, invoke, attempt=1,
        )

        if ai_validation.get("verdict") == "suspicious" and MAX_AI_VALIDATION_CORRECTIONS > 0:
            # Real observed failure (pecan, Citation): the correction attempt
            # can itself produce a payload that fails deterministic
            # provenance validation (e.g. "cleaning up" a value in a way
            # that no longer matches the source text verbatim) even though
            # the ORIGINAL payload -- the one that triggered this correction
            # in the first place -- had already passed that same
            # deterministic check. The AI Validator's opinion must never be
            # allowed to override deterministic provenance truth: once a
            # payload has passed deterministic validation, it is preserved
            # here as last_known_good and is the fallback if the correction
            # attempt fails validation, rather than being discarded in favor
            # of an unresolved outcome.
            last_known_good_payload = candidate_payload
            last_known_good_ai_validation = ai_validation

            correction = _attempt_ai_validation_correction(
                run_id=run_id, record_key=record_key, paper_id=paper_id, entity_type=entity_type,
                record_id=record_id, raw_extraction=raw_extraction, known_refs=known_refs,
                ai_validation=ai_validation, model=model, client=client, invoke=invoke,
                stage_counts=stage_counts,
            )
            if not correction["ok"]:
                # The correction itself failed deterministic validation (or
                # crashed) -- never discard the previously deterministic-
                # valid payload for that. Fall back to it and commit it,
                # still carrying its own (suspicious) verdict for
                # transparency -- this is not silently accepting the failed
                # correction, it is refusing it and keeping the evidence-
                # grounded value the deterministic validator already
                # confirmed. The rejected correction attempt and exactly why
                # it failed remain on disk under this run_id regardless
                # (run_store captured it above, unconditionally).
                candidate_payload = last_known_good_payload
                ai_validation = last_known_good_ai_validation
            else:
                candidate_payload = correction["payload"]
                ai_validation = correction["ai_validation"]
                if ai_validation.get("verdict") == "suspicious":
                    # Different from the branch above: HERE the correction
                    # DID pass deterministic validation, so there is no
                    # provenance-truth conflict to refuse -- it's simply
                    # still flagged after the one bounded attempt is
                    # exhausted (MAX_AI_VALIDATION_CORRECTIONS == 1). Do not
                    # force a value; preserve unresolved semantics exactly
                    # as before.
                    return _finalize_unresolved(
                        run_id, client, paper_id, entity_type, record_id, record_key,
                        raw_extraction, candidate_payload,
                        [{
                            "field": None,
                            "message": "AI Validator still flagged this record as suspicious after "
                                       "one bounded correction attempt.",
                        }],
                        stage_counts,
                    )

    # --- Commit ---
    commit_result = client.commit_record(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id,
        payload=candidate_payload, status="ready",
        run_metadata={
            "run_id": run_id,
            "schema_version": schema_fingerprint(),
            "ai_validation": ai_validation,
        },
    )
    outcome = "ready" if commit_result.get("committed") else "error"
    final_record = {
        "status": outcome,
        "paper_id": paper_id, "entity_type": entity_type, "record_id": record_id,
        "payload": candidate_payload, "ai_validation": ai_validation, "commit_result": commit_result,
    }
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": outcome,
        "attempts": stage_counts,
    })
    return RecordResult(status=outcome, entity_type=entity_type, record_id=record_id, detail=final_record)


def _finalize_unresolved(
    run_id: str, client: IRServiceClient, paper_id: str, entity_type: str, record_id: str, record_key: str,
    raw_extraction: dict, candidate_payload: Optional[dict], last_errors: list[dict], stage_counts: dict,
) -> RecordResult:
    """Deterministic validation never passed within the loop's safety
    ceiling (or the server's own turn cap fired). The orchestrator, not the
    model, authors the flag_unresolved call -- it has the full attempt
    history to cite, and this never depends on the model correctly deciding
    to call flag_unresolved itself."""
    blocks_examined = all_anchors(raw_extraction) or ["<none-recorded>"]
    conflict_explanation = (
        f"Deterministic validation did not pass after {stage_counts['conversion']} conversion attempt(s). "
        f"Last errors: {json.dumps(last_errors)[:2000]}"
    )
    flag_result = client.flag_unresolved(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id,
        field="<record>", reason="conversion could not produce a deterministically valid record",
        blocks_examined=blocks_examined, conflict_explanation=conflict_explanation,
        run_metadata={"run_id": run_id, "schema_version": schema_fingerprint()},
    )
    final_record = {
        "status": "unresolved",
        "paper_id": paper_id, "entity_type": entity_type, "record_id": record_id,
        "last_candidate_payload": candidate_payload, "last_errors": last_errors,
        "flag_result": flag_result,
    }
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": "unresolved", "attempts": stage_counts,
    })
    return RecordResult(status="unresolved", entity_type=entity_type, record_id=record_id, detail=final_record)


def _finalize_error(
    run_id: str, record_key: str, entity_type: str, record_id: str, message: str, stage_counts: dict,
    failure_class: Optional[str] = None,
) -> RecordResult:
    """A pipeline-level failure with no ir_service call to make (e.g. the
    Extraction stage never returned parseable evidence at all). Still writes
    a final artifact -- 'failures must never silently disappear' applies
    here too, not just to model/validation failures.

    `failure_class` is an explicit, additive classification (currently only
    "provider_empty_response" -- see MAX_EMPTY_RESPONSE_RETRIES's own
    docstring) for when EVERY failed attempt was infrastructure noise
    rather than a genuine content/shape problem -- never changes the
    terminal status (still "error": the pipeline genuinely produced no
    usable candidate payload either way, capture-first requires that stay
    disclosed as a real failure, not silently become "ready" or bypass
    deterministic validation), only makes WHY it failed easier to find
    without reading the full attempt history."""
    final_record = {"status": "error", "entity_type": entity_type, "record_id": record_id, "message": message}
    if failure_class:
        final_record["failure_class"] = failure_class
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": "error", "attempts": stage_counts,
        **({"failure_class": failure_class} if failure_class else {}),
    })
    return RecordResult(status="error", entity_type=entity_type, record_id=record_id, detail=final_record)


# --------------------------------------------------------------------------- #
# Whole-paper graph validation
# --------------------------------------------------------------------------- #

ENTITY_TYPE_TO_PLURAL = {
    "Citation": "citations", "Study": "studies", "Site": "sites", "Species": "species", "Crop": "crops",
    "Method": "methods", "Treatment": "treatments", "TreatmentPair": "treatment_pairs", "Variable": "variables",
    "Management": "managements", "Observation": "observations", "Coverage": "coverages",
}


def build_dataset_from_store(paper_id: str, dataset_id: Optional[str] = None) -> tuple[dict, list[str]]:
    """Assemble an IRDataset-shaped dict from ir-store's LATEST entry per
    (entity_type, record_id) for one paper -- store.py's own definition of
    "current state" ("a record's current status is whatever the last line
    for that record_key says"). Only status=="ready" entries join the
    graph; "unresolved" entries are real, disclosed evidence of an
    incomplete record and are reported separately rather than silently
    folded in as if they validated. Returns (dataset_dict, skipped_keys).
    """
    entries = store.read_all(paper_id)
    latest: dict[tuple[str, str], dict] = {}
    for entry in entries:
        latest[(entry["entity_type"], entry["record_id"])] = entry  # later lines win

    dataset: dict[str, Any] = {"dataset_id": dataset_id or paper_id}
    for plural in ENTITY_TYPE_TO_PLURAL.values():
        dataset[plural] = []

    skipped: list[str] = []
    for (entity_type, record_id), entry in latest.items():
        plural = ENTITY_TYPE_TO_PLURAL.get(entity_type)
        if plural is None:
            continue
        if entry.get("status") != "ready":
            skipped.append(f"{entity_type}__{record_id} ({entry.get('status')})")
            continue
        dataset[plural].append(entry["payload"])
    return dataset, skipped


def _citation_record_id_conflicts(paper_id: str) -> list[str]:
    """Citation is documented as 1:1 with 'the paper currently being
    curated' (extractor.md/AGENTS.md) -- unlike Treatment/Observation/etc,
    which legitimately have many record_ids per paper. More than one
    distinct Citation record_id for the same paper_id is a naming/hygiene
    problem (typically: earlier ad hoc testing used an inconsistent
    record_id before `record_id == paper_id` became the convention), not a
    normal outcome. A bare Pydantic "citations.0.persistent_identifier:
    Input should be a valid dictionary" is technically correct but
    unhelpful on its own -- this turns it into an actionable diagnostic."""
    record_ids = sorted({e["record_id"] for e in store.read_all(paper_id) if e["entity_type"] == "Citation"})
    return record_ids if len(record_ids) > 1 else []


def graph_check(paper_id: str) -> dict:
    """Deterministic whole-paper audit: does everything currently marked
    'ready' in ir-store for this paper actually form a consistent
    IRDataset per validators.validate_dataset (Table 19)? Read-only --
    never commits, flags, or mutates anything."""
    dataset_dict, skipped = build_dataset_from_store(paper_id)
    citation_conflicts = _citation_record_id_conflicts(paper_id)
    try:
        ds = IRDataset.model_validate(dataset_dict)
    except ValidationError as exc:
        return {
            "paper_id": paper_id, "constructed": False,
            "construction_errors": [
                {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
            ],
            "skipped_not_ready": skipped,
            "citation_record_id_conflicts": citation_conflicts,
        }
    issues = validate_dataset(ds)
    return {
        "paper_id": paper_id, "constructed": True,
        "counts": {plural: len(getattr(ds, plural)) for plural in ENTITY_TYPE_TO_PLURAL.values()},
        "issues": [issue.to_dict() for issue in issues],
        "skipped_not_ready": skipped,
        "citation_record_id_conflicts": citation_conflicts,
    }


# --------------------------------------------------------------------------- #
# Full-paper pipeline (run-paper): dependency-ordered execution across all
# 12 entity types in ONE run, reusing run_record + the known_refs/--ref
# mechanism internally instead of inventing a second dependency system.
# --------------------------------------------------------------------------- #

# Each entry: entity_type -> [(field_name_on_this_entity, prerequisite_entity_type, required), ...]
# `field_name` is the ACTUAL schema field (ir_schema.py) -- "citation_ids"
# for Study (a list), everything else is the real bare ExtractedReference
# field name. `required=True` means: if this run didn't successfully
# produce a `ready` record of the prerequisite type, this entity cannot be
# attempted at all (blocked, not attempted with a fabricated id).
# `required=False` (Observation's species_id/crop_id/variable_id) means:
# pass it along as an optional enrichment when available, proceed without
# it otherwise -- these are all `Optional[ExtractedReference]` fields.
ENTITY_DEPENDENCIES: dict[str, list[tuple[str, str, bool]]] = {
    "Citation": [],
    "Site": [],
    "Species": [],
    "Variable": [],
    "Study": [("citation_ids", "Citation", True)],
    "Crop": [("citation_id", "Citation", True), ("species_id", "Species", True)],
    "Method": [("citation_id", "Citation", True)],
    "Treatment": [("citation_id", "Citation", True), ("site_id", "Site", True)],
    "Management": [("citation_id", "Citation", True)],
    "Coverage": [("citation_id", "Citation", True), ("site_id", "Site", True), ("variable_id", "Variable", False)],
    "Observation": [
        ("citation_id", "Citation", True), ("site_id", "Site", True),
        ("treatment_id", "Treatment", True), ("method_id", "Method", True),
        ("species_id", "Species", False), ("crop_id", "Crop", False), ("variable_id", "Variable", False),
    ],
    # TreatmentPair needs TWO DISTINCT Treatment records (treatment_id_1 !=
    # treatment_id_2, enforced at construction time in ir_schema.py).
    # Treatment is a multi-record type (Phase B: `results_store.
    # MULTI_RECORD_ENTITY_TYPES`), so this run-paper is reported "blocked"
    # by _resolve_known_refs below only until at least 2 'ready' Treatment
    # records actually exist in this run -- two Treatment prerequisites of
    # the same type is exactly the ">1 instance of the same prerequisite
    # type" case that check generically detects, without TreatmentPair
    # needing special-case code either way.
    "TreatmentPair": [
        ("citation_id", "Citation", True),
        ("treatment_id_1", "Treatment", True),
        ("treatment_id_2", "Treatment", True),
    ],
}

assert set(ENTITY_DEPENDENCIES.keys()) == set(ENTITY_TYPE_TO_PLURAL.keys())


def _topological_entity_order() -> list[str]:
    """Real topological sort over ENTITY_DEPENDENCIES's prerequisite edges
    -- not a hand-maintained list that could silently drift from the
    actual schema relationships. Layered (Kahn's algorithm): at each step,
    every entity whose prerequisites are already fully placed is added,
    sorted alphabetically within its layer so the result is deterministic
    and reproducible across runs/processes."""
    prereqs: dict[str, set[str]] = {
        et: {prereq_type for _, prereq_type, _ in deps} for et, deps in ENTITY_DEPENDENCIES.items()
    }
    order: list[str] = []
    placed: set[str] = set()
    remaining = set(prereqs.keys())
    while remaining:
        ready = sorted(et for et in remaining if prereqs[et] <= placed)
        if not ready:
            raise RuntimeError(f"circular or unresolvable entity dependency among: {sorted(remaining)}")
        order.extend(ready)
        placed |= set(ready)
        remaining -= set(ready)
    return order


def _resolve_known_refs(
    entity_type: str, this_run_records: dict[str, dict]
) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Resolve known_refs for `entity_type` from records already produced
    EARLIER IN THIS SAME RUN ONLY -- never from ir-store history, which is
    the whole point (a full-paper run must not silently inherit stale
    records just because they happen to exist from unrelated earlier
    testing). Returns (known_refs, None) when every required prerequisite
    is satisfied, or (None, reason) when it is not -- the caller must skip
    extraction entirely in that case rather than guess.

    Phase B: a prerequisite type needed MORE THAN ONCE by the same entity
    (e.g. `TreatmentPair.treatment_id_1`/`treatment_id_2`, both `Treatment`)
    is now resolvable -- but only when that prerequisite is itself a
    multi-record type (`results_store.MULTI_RECORD_ENTITY_TYPES`) and this
    run actually produced at least that many distinct 'ready' records of
    it. Each such field is then bound to a DIFFERENT ready record (stable,
    enumeration order), never the same one twice -- exactly what
    `TreatmentPair._distinct_treatments` requires at construction time.
    Before Phase B, Treatment was single-record, so this could never be
    satisfied and TreatmentPair was unconditionally reported blocked by
    this same generic ">1 instance of the same prerequisite type" rule --
    that rule itself hasn't changed, only the underlying fact (how many
    Treatment records one run can produce) has."""
    deps = ENTITY_DEPENDENCIES.get(entity_type, [])
    if not deps:
        return {}, None

    required_counts: dict[str, int] = {}
    for _, prereq_type, required in deps:
        if required:
            required_counts[prereq_type] = required_counts.get(prereq_type, 0) + 1

    for prereq_type, needed in required_counts.items():
        ready = _ready_records(this_run_records, prereq_type)
        if needed > 1:
            if len(ready) < needed:
                return None, (
                    f"requires {needed} distinct {prereq_type} records; only {len(ready)} "
                    f"'ready' {prereq_type} record(s) exist in this run"
                )
        elif not ready:
            available = this_run_records.get(prereq_type)
            if available is None:
                got = "not attempted"
            elif isinstance(available, list):
                got = f"{len(available)} record(s), none ready" if available else "not attempted"
            else:
                got = available["status"]
            return None, f"required prerequisite {prereq_type} is not 'ready' in this run (status={got})"

    known_refs: dict[str, str] = {}
    consumed: dict[str, int] = {}
    for field, prereq_type, required in deps:
        ready = _ready_records(this_run_records, prereq_type)
        if required_counts.get(prereq_type, 0) > 1:
            # Multiple DISTINCT slots on THIS entity for the same
            # prerequisite type (e.g. TreatmentPair.treatment_id_1/2, both
            # Treatment) -- already confirmed above that at least `needed`
            # ready records exist.
            #
            # If `entity_type` ITSELF is multi-record (Universal Multi-
            # Record: TreatmentPair is, now that treatment_pairs.csv's
            # "multiple named comparisons" cardinality is honored), do NOT
            # eagerly bind these slots here -- a real bug this generalization
            # would otherwise hit: `_apply_candidate_links` below skips any
            # field already present in `known_refs`, so eagerly assigning
            # "first two ready records in stable order" here would silently
            # force EVERY enumerated pair candidate onto the same two
            # Treatments regardless of which pair its own evidence/
            # linked_candidates actually names -- exactly the "guess instead
            # of using evidence" failure this architecture forbids. Leave
            # both slots unresolved here; `_apply_candidate_links` resolves
            # each one PER CANDIDATE instead (real link if named, or a
            # deterministic allowed-set of all ready ids otherwise -- still
            # never a guess, since construction-time validation then
            # requires the two chosen ids to be distinct).
            #
            # For a (currently hypothetical) SINGLE-RECORD entity_type with
            # this same shape, there is no per-candidate mechanism to defer
            # to, so the pre-Phase-C "consume one per slot, in stable order"
            # behavior is preserved unchanged.
            if entity_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
                idx = consumed.get(prereq_type, 0)
                known_refs[field] = ready[idx]["record_id"]
                consumed[prereq_type] = idx + 1
        elif len(ready) == 1:
            # Exactly one ready record of this type -- byte-identical
            # resolution to before multi-record types existed.
            known_refs[field] = ready[0]["record_id"]
        # 0 ready -> omit, unchanged from before ("not attempted"/not ready).
        # >1 ready with only a single slot for this type on this entity (a
        # soft dependency, e.g. Coverage.variable_id -- or a `required` one
        # like Observation.treatment_id once Treatment has multiple ready
        # records) -> also deliberately omit here rather than guess which
        # one is relevant to THIS entity type as a whole. This function
        # only ever resolves ONE known_refs dict shared by every candidate
        # of a multi-record entity type -- it has no per-candidate
        # evidence to disambiguate with. Phase C's `_apply_candidate_links`
        # is the (separate, later) layer that refines this per-candidate,
        # using each candidate's own verified `linked_candidates` or, for a
        # required field, an allowed-set fallback -- never by changing
        # what this function itself returns.
    return known_refs, None


def _ready_records(this_run_records: dict, prereq_type: str) -> list[dict]:
    """Normalize `this_run_records[prereq_type]` -- a single record_info
    dict for a single-record entity type, or a list of them for a
    multi-record type (Phase A: Variable) -- into a list of just the
    'ready' ones. Pure shape normalization: a single-record type still
    ever returns at most one entry, exactly as before this helper existed."""
    available = this_run_records.get(prereq_type)
    if available is None:
        return []
    records = available if isinstance(available, list) else [available]
    return [r for r in records if r.get("status") == "ready"]


def _candidate_slug_from_record_id(paper_id: str, prereq_type: str, record_id: str) -> str:
    """Inverse of `_run_multi_record_entity`'s own record_id construction
    (`f"{paper_id}_{entity_type.lower()}_{slug}"`) -- deterministic, no
    guessing: if the prefix doesn't match (shouldn't happen for a record_id
    this same run produced), falls back to the full record_id so a lookup
    against it simply won't match anything, rather than raising."""
    prefix = f"{paper_id}_{prereq_type.lower()}_"
    return record_id[len(prefix):] if record_id.startswith(prefix) else record_id


def _multi_record_link_pools(
    paper_id: str, entity_type: str, this_run_records: dict
) -> dict[str, list[dict]]:
    """Phase C: for each of `entity_type`'s OWN dependency fields whose
    prerequisite is ITSELF a multi-record type with MORE THAN ONE ready
    record already produced earlier in this run (i.e. genuinely
    ambiguous -- `_resolve_known_refs` already resolves the 0-or-1-ready
    case deterministically on its own, no linking needed), build a small,
    human-readable pool of {slug, record_id, name} -- shown in the
    enumeration prompt so the model can link a specific candidate (e.g.
    one Observation) to a SPECIFIC already-known record (e.g. one
    Treatment) using a real, later-verifiable slug, instead of the
    orchestrator ever guessing which one is relevant. Returns {} for
    Treatment/Variable themselves (their own dependencies -- Citation,
    Site -- are single-record) and for any entity run before its
    multi-record dependency has more than one ready record -- both cases
    leave the enumeration prompt completely unaffected by this mechanism."""
    pools: dict[str, list[dict]] = {}
    for field, prereq_type, _required in ENTITY_DEPENDENCIES.get(entity_type, []):
        if prereq_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
            continue
        ready = _ready_records(this_run_records, prereq_type)
        if len(ready) <= 1:
            continue
        items = []
        for r in ready:
            record_id = r["record_id"]
            payload = ((r.get("detail") or {}).get("payload")) or {}
            name_field = payload.get("name")
            name = name_field.get("value") if isinstance(name_field, dict) else None
            items.append({
                "slug": _candidate_slug_from_record_id(paper_id, prereq_type, record_id),
                "record_id": record_id, "name": name,
            })
        pools[field] = items
    return pools


def _apply_candidate_links(
    paper_id: str, entity_type: str, this_run_records: dict, known_refs: dict, candidate: Any,
) -> dict:
    """Phase C: refine `entity_type`'s entity-wide `known_refs` (identical
    for every candidate, resolved once by `_resolve_known_refs`) into a
    PER-CANDIDATE known_refs -- needed because a multi-record dependency
    (Treatment, Variable) can have more than one ready record, and WHICH
    one is relevant can differ per candidate (one Observation is about
    ambient CO2 x leaf area index, another about elevated CO2 x NH4
    uptake). For each dependency field `_resolve_known_refs` left
    unresolved (ambiguous: >1 ready record, no way to guess generically):

      1. If THIS candidate's own `linked_candidates` names a slug that
         deterministically matches one of this run's actual READY records
         of that type, bind the field to that exact record_id. The model
         must point at a real, already-extracted record it cannot invent
         one; `run_enumeration` already rejected any candidate citing a
         slug outside the pool it was shown, but that check is re-applied
         here too -- this function is the actual safety boundary, never
         trusting a prior check alone.
      2. Otherwise, for a REQUIRED field (`ir_schema.py` has no Optional
         escape hatch for these bare-reference fields, e.g.
         `Observation.treatment_id`): fall back to the full set of this
         run's ready record_ids for that type as an ALLOWED-SET
         constraint. `_ref_mismatch` treats a list `expected` as
         membership, not equality -- Conversion, which actually reads the
         raw evidence, still must pick exactly one, but is deterministically
         forbidden from fabricating a value outside this real, verified
         set. This is never the orchestrator itself guessing which
         Treatment/Variable applies (requirement: never guess merely
         because multiple records exist) -- it is a safety NET that only
         narrows what Conversion is allowed to output; if the raw evidence
         gives no basis to choose, that candidate fails deterministic
         validation/is flagged unresolved via the existing bounded retry,
         exactly like any other unsupported field.
      3. An OPTIONAL field (e.g. `Observation.variable_id`) that is still
         ambiguous is left omitted -- unchanged from Phase A/B behavior:
         it can legitimately be absent rather than forced.

    For Treatment/Variable themselves (dependencies are single-record), no
    field is ever left unresolved by `_resolve_known_refs` in the first
    place, so this function is a no-op and returns `known_refs` unchanged."""
    resolved = dict(known_refs)
    linked = getattr(candidate, "linked_candidates", None) or {}
    for field, prereq_type, required in ENTITY_DEPENDENCIES.get(entity_type, []):
        if field in resolved:
            continue
        if prereq_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
            continue
        ready = _ready_records(this_run_records, prereq_type)
        if not ready:
            continue
        ready_ids = {r["record_id"] for r in ready}

        linked_slug = linked.get(field)
        if linked_slug:
            candidate_record_id = f"{paper_id}_{prereq_type.lower()}_{_sanitize_candidate_id(linked_slug)}"
            if candidate_record_id in ready_ids:
                resolved[field] = candidate_record_id
                continue
            # Named a slug that isn't a real ready record of this type --
            # never trust it; fall through to the generic ambiguous
            # handling below exactly as if no link had been given.

        if required:
            resolved[field] = sorted(ready_ids)
        # optional and still ambiguous -> leave omitted, unchanged from Phase A/B.
    return resolved


def _entity_record_id(paper_id: str, entity_type: str) -> str:
    # Citation is documented (AGENTS.md) as 1:1 with "the paper currently
    # being curated" -- use the paper_id itself, matching the convention
    # already established in prior single-entity testing, rather than
    # inventing yet another Citation record_id naming scheme for this
    # paper. Every other entity type gets a plain, deterministic,
    # reproducible <paper_id>_<entity_type> id.
    if entity_type == "Citation":
        return paper_id
    return f"{paper_id}_{entity_type.lower()}"


def _entity_result_file(paper_id: str, entity_type: str, run_id: str, record_id: str, record_info: dict) -> dict:
    """Normalize one entity's outcome (whichever of run_record's several
    result shapes it is) into one stable shape for results/<paper_id>/
    <Entity>.json, regardless of status -- so a downstream reader never
    has to branch on which kind of failure happened to know what keys
    exist."""
    status = record_info["status"]
    base = {
        "paper_id": paper_id, "entity_type": entity_type, "record_id": record_id,
        "run_id": run_id, "status": status, "payload": None, "ai_validation": None,
    }
    detail = record_info.get("detail") or {}
    if status == "ready":
        base["payload"] = detail.get("payload")
        base["ai_validation"] = detail.get("ai_validation")
    elif status == "unresolved":
        base["payload"] = detail.get("last_candidate_payload")
        base["reason"] = detail.get("last_errors")
        base["flag_result"] = detail.get("flag_result")
    elif status == "blocked":
        base["reason"] = record_info.get("reason")
    else:  # "error"
        base["reason"] = detail.get("message")
    return base


def run_paper(
    *,
    paper_id: str,
    model: str,
    client: IRServiceClient,
    invoke: Callable[..., AgentInvocation] = invoke_agent,
    enable_ai_validation: bool = True,
    run_id: Optional[str] = None,
) -> dict:
    """Run the complete Extraction -> Conversion -> deterministic validation
    -> AI Validator -> commit pipeline for ALL 12 Sage IR entity types for
    one paper, in dependency order, under a single shared run_id -- this is
    a genuine multi-entity pipeline execution, not an aggregation over
    ir-store history (see `finalize_paper` above for that, a different,
    still-supported operation).

    Scope decision (see ENTITY_DEPENDENCIES's docstring): exactly one
    record per entity type per call, EXCEPT entity types listed in
    `results_store.MULTI_RECORD_ENTITY_TYPES` (Phase A: Variable; Phase B:
    + Treatment), which instead run a bounded enumeration pass and one full
    record per real, evidence-grounded candidate found (see
    `_run_multi_record_entity`). TreatmentPair still structurally needs two
    distinct Treatment records, so it is reported "blocked" by any run that
    doesn't itself produce at least 2 'ready' Treatment records -- an
    honest, disclosed, per-run outcome (not a bug), now genuinely reachable
    once a paper's Treatment enumeration finds >=2 real candidates.

    Writes results/<paper_id>/<EntityType>.json (single-record types) or
    results/<paper_id>/<EntityType>/<record_id>.json (multi-record types)
    for all 12 entity types, built ONLY from what this call itself produced
    (`this_run_records`) -- never from ir-store's historical records under
    other record_ids, even when they exist for the same paper_id.
    """
    run_id = run_id or _new_run_id()
    order = _topological_entity_order()
    this_run_records: dict[str, Any] = {}  # dict per entity_type, or list[dict] for a multi-record type

    for entity_type in order:
        if entity_type in results_store.MULTI_RECORD_ENTITY_TYPES:
            record_infos = _run_multi_record_entity(
                run_id=run_id, paper_id=paper_id, entity_type=entity_type, model=model,
                client=client, invoke=invoke, enable_ai_validation=enable_ai_validation,
                this_run_records=this_run_records,
            )
            this_run_records[entity_type] = record_infos
            results_store.save_multi_entity_results(
                paper_id, entity_type,
                [
                    _entity_result_file(paper_id, entity_type, run_id, r["record_id"], r)
                    for r in record_infos
                ],
            )
            continue

        record_id = _entity_record_id(paper_id, entity_type)
        record_key = f"{entity_type}__{record_id}"
        known_refs, blocked_reason = _resolve_known_refs(entity_type, this_run_records)

        if blocked_reason is not None:
            record_info = {
                "entity_type": entity_type, "record_id": record_id,
                "status": "blocked", "reason": blocked_reason,
            }
            run_store.save_stage_attempt(run_id, record_key, "blocked", 1, record_info)
            run_store.save_final(run_id, record_key, record_info)
        else:
            result = run_record(
                run_id=run_id, paper_id=paper_id, entity_type=entity_type, record_id=record_id,
                model=model, client=client, invoke=invoke, enable_ai_validation=enable_ai_validation,
                known_refs=known_refs or None,
            )
            record_info = {
                "entity_type": entity_type, "record_id": record_id,
                "status": result.status, "detail": result.detail,
            }

        this_run_records[entity_type] = record_info
        results_store.save_entity_result(
            paper_id, entity_type,
            _entity_result_file(paper_id, entity_type, run_id, record_id, record_info),
        )

    run_store.save_run_manifest(run_id, {
        "run_id": run_id, "paper_id": paper_id, "model": model,
        "schema_fingerprint": schema_fingerprint(),
        "opencode_config_fingerprint": config_fingerprint(OPENCODE_CONFIG_PATH),
        "ai_validation_enabled": enable_ai_validation,
        "kind": "run-paper", "entity_order": order,
        "started_at": time.time(), "finished_at": time.time(),
        "status": {
            et: (r["status"] if not isinstance(r, list) else [x["status"] for x in r])
            for et, r in this_run_records.items()
        },
    })

    return {"run_id": run_id, "paper_id": paper_id, "entity_order": order, "records": this_run_records}


# --------------------------------------------------------------------------- #
# Unified full-paper result (finalize)
# --------------------------------------------------------------------------- #


def finalize_paper(paper_id: str) -> dict:
    """Aggregate the latest ir-store record per (entity_type, record_id) for
    ONE paper into a single, unified, deterministic result covering all 12
    Sage IR entity types -- the "current best known state" of everything
    extracted for this paper so far.

    Deliberately built on top of `build_dataset_from_store` and
    `graph_check` (above) rather than re-deriving "what does ir-store's
    latest line say" a second time -- this is a pure aggregation/reporting
    step, not a new source of truth. Read-only against ir-store; the only
    write this function makes is the one results/<paper_id>/result.json
    file via `pipeline.results_store`.

    Every one of the 12 entity types always appears in the result, with
    `ready`/`unresolved` lists that may both be empty -- that emptiness IS
    the "no record of this type exists yet" signal, kept structurally
    distinct from an entity type that has real but UNRESOLVED records
    (non-empty `unresolved`, empty `ready`). Nothing is ever fabricated to
    fill a gap.
    """
    entries = store.read_all(paper_id)
    latest: dict[tuple[str, str], dict] = {}
    for entry in entries:
        latest[(entry["entity_type"], entry["record_id"])] = entry  # later lines win, same as build_dataset_from_store

    entities: dict[str, dict[str, list]] = {et: {"ready": [], "unresolved": []} for et in ENTITY_TYPE_TO_PLURAL}
    for (entity_type, record_id), entry in latest.items():
        if entity_type not in entities:
            continue  # defensive: an entity_type ir-store somehow has that ENTITY_MODELS doesn't recognize
        record_summary = {
            "record_id": record_id,
            "status": entry.get("status"),
            "payload": entry.get("payload"),
            "run_id": entry.get("run_id"),
            "schema_version": entry.get("schema_version"),
            "ai_validation": entry.get("ai_validation"),
            "ts": entry.get("ts"),
        }
        bucket = "ready" if entry.get("status") == "ready" else "unresolved"
        entities[entity_type][bucket].append(record_summary)

    graph = graph_check(paper_id)
    dataset_dict, _skipped = build_dataset_from_store(paper_id)

    result = {
        "paper_id": paper_id,
        "finalized_at": time.time(),
        "schema_fingerprint": schema_fingerprint(),
        "entity_types": sorted(ENTITY_TYPE_TO_PLURAL.keys()),
        "entities": entities,
        "graph_constructed": graph.get("constructed"),
        "graph_issues": graph.get("issues", []),
        "graph_construction_errors": graph.get("construction_errors", []),
        "citation_record_id_conflicts": graph.get("citation_record_id_conflicts", []),
        # The clean, validated IRDataset -- only meaningful (and only
        # included) when the graph actually constructed; never a
        # best-effort/partial dataset standing in for a failed one.
        "dataset": dataset_dict if graph.get("constructed") else None,
    }
    results_store.save_result(paper_id, result)
    return result


def _finalize_summary_line(result: dict) -> str:
    counts = {et: (len(b["ready"]), len(b["unresolved"])) for et, b in result["entities"].items()}
    lines = [f"paper_id: {result['paper_id']}"]
    for et in result["entity_types"]:
        ready, unresolved = counts[et]
        if ready == 0 and unresolved == 0:
            state = "absent"
        else:
            state = f"ready={ready} unresolved={unresolved}"
        lines.append(f"  {et:15s} {state}")
    lines.append(f"graph_constructed: {result['graph_constructed']}")
    error_count = len([i for i in result["graph_issues"] if i.get("severity") == "error"])
    warning_count = len([i for i in result["graph_issues"] if i.get("severity") == "warning"])
    lines.append(f"graph_issues: {error_count} error(s), {warning_count} warning(s)")
    if result["citation_record_id_conflicts"]:
        lines.append(f"citation_record_id_conflicts: {result['citation_record_id_conflicts']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _new_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.orchestrator",
        description="Run one Sage IR record through Extraction -> Conversion -> validation -> persistence.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="Check ir_service reachability and schema freshness.")
    health.add_argument("--ir-service-url", default=DEFAULT_IR_SERVICE_URL)

    run = sub.add_parser("run", help="Run one paper/entity/record through the full pipeline.")
    run.add_argument("--paper-id", required=True)
    run.add_argument("--entity-type", required=True)
    run.add_argument("--record-id", required=True)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--ir-service-url", default=DEFAULT_IR_SERVICE_URL)
    run.add_argument("--no-ai-validation", action="store_true")
    run.add_argument("--run-id", default=None, help="Reuse an existing run id instead of generating a new one.")
    run.add_argument(
        "--ref", action="append", default=[], metavar="FIELD=VALUE",
        help="Known, already-committed reference id for a bare-reference field "
             "(e.g. --ref site_id=pecan_site --ref citation_id=pecan). Repeatable. "
             "Required for any entity type with a site_id/citation_id/treatment_id/"
             "method_id/species_id field once its prerequisite entity has been "
             "committed -- see AGENTS.md.",
    )

    graph = sub.add_parser(
        "graph-check",
        help="Assemble the latest 'ready' records for one paper from ir-store and run whole-graph (Table 19) validation.",
    )
    graph.add_argument("--paper-id", required=True)

    finalize = sub.add_parser(
        "finalize",
        help="Aggregate the latest ir-store records for one paper into a unified results/<paper_id>/result.json covering all 12 entity types.",
    )
    finalize.add_argument("--paper-id", required=True)

    run_paper_cmd = sub.add_parser(
        "run-paper",
        help="Run the complete Extraction->Conversion->validation->AI-Validator->commit pipeline "
             "for all 12 entity types for one paper, in dependency order, and write results/<paper_id>/<Entity>.json.",
    )
    run_paper_cmd.add_argument("--paper-id", required=True)
    run_paper_cmd.add_argument("--model", default=DEFAULT_MODEL)
    run_paper_cmd.add_argument("--ir-service-url", default=DEFAULT_IR_SERVICE_URL)
    run_paper_cmd.add_argument("--no-ai-validation", action="store_true")
    run_paper_cmd.add_argument("--run-id", default=None)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.command == "health":
        ok, msg = check_health(args.ir_service_url)
        print(msg)
        return 0 if ok else 1

    if args.command == "graph-check":
        result = graph_check(args.paper_id)
        print(json.dumps(result, indent=2))
        has_errors = not result.get("constructed") or any(i["severity"] == "error" for i in result.get("issues", []))
        return 1 if has_errors else 0

    if args.command == "finalize":
        result = finalize_paper(args.paper_id)
        print(_finalize_summary_line(result))
        print(f"\nwritten to: {results_store.result_path(args.paper_id)}")
        has_errors = not result["graph_constructed"] or any(i["severity"] == "error" for i in result["graph_issues"])
        return 1 if has_errors else 0

    if args.command == "run-paper":
        ok, msg = check_health(args.ir_service_url)
        print(msg)
        if not ok:
            return 1

        http_client = httpx.Client(base_url=args.ir_service_url, timeout=120.0)
        client = IRServiceClient(http_client)
        outcome = run_paper(
            paper_id=args.paper_id, model=args.model, client=client,
            enable_ai_validation=not args.no_ai_validation, run_id=args.run_id,
        )
        print(f"\nrun_id: {outcome['run_id']}")
        print(f"entity_order: {outcome['entity_order']}")
        any_error = False
        for entity_type in outcome["entity_order"]:
            record = outcome["records"][entity_type]
            if isinstance(record, list):
                # Multi-record entity type (Phase A: Variable; Phase B: +
                # Treatment) -- summarize every record produced this run
                # instead of indexing a list with a string key.
                summary = ", ".join(f"{r['record_id']}={r['status']}" for r in record) or "(no candidates)"
                print(f"  {entity_type:15s} {summary}")
                if any(r["status"] == "error" for r in record):
                    any_error = True
            else:
                status = record["status"]
                print(f"  {entity_type:15s} {status}")
                if status == "error":
                    any_error = True
        print(f"\nresults written to: {results_store.paper_dir(args.paper_id)}")
        return 1 if any_error else 0

    ok, msg = check_health(args.ir_service_url)
    print(msg)
    if not ok:
        return 1

    try:
        known_refs = dict(item.split("=", 1) for item in args.ref)
    except ValueError:
        print(f"invalid --ref value (expected FIELD=VALUE): {args.ref}")
        return 1

    run_id = args.run_id or _new_run_id()
    http_client = httpx.Client(base_url=args.ir_service_url, timeout=120.0)
    client = IRServiceClient(http_client)

    run_store.save_run_manifest(run_id, {
        "run_id": run_id,
        "paper_id": args.paper_id, "entity_type": args.entity_type, "record_id": args.record_id,
        "model": args.model,
        "schema_fingerprint": schema_fingerprint(),
        "opencode_config_fingerprint": config_fingerprint(OPENCODE_CONFIG_PATH),
        "ai_validation_enabled": not args.no_ai_validation,
        "known_refs": known_refs,
        "started_at": time.time(),
        "status": "running",
    })

    result = run_record(
        run_id=run_id, paper_id=args.paper_id, entity_type=args.entity_type, record_id=args.record_id,
        model=args.model, client=client, enable_ai_validation=not args.no_ai_validation,
        known_refs=known_refs or None,
    )

    manifest = run_store.load_run_manifest(run_id)
    manifest["status"] = result.status
    manifest["finished_at"] = time.time()
    run_store.save_run_manifest(run_id, manifest)

    print(f"\nrun_id:    {run_id}")
    print(f"status:    {result.status}")
    print(f"artifacts: {run_store.run_dir(run_id)}")
    return 0 if result.status in ("ready", "unresolved") else 1


if __name__ == "__main__":
    raise SystemExit(main())
