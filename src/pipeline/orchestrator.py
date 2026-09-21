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
import copy
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

from pipeline import content_reader, pooling_evidence, results_store, run_config, run_lock, run_store, store, vocab
from pipeline.fingerprint import config_fingerprint, schema_fingerprint
from pipeline.ir_schema import IRDataset
from pipeline.raw_schema import (
    MIXTURE_LEVEL_RULE, CandidateDimension, EnumerationCandidate, EnumerationResult, MethodHintFlag, RawExtraction,
    RawFact, TableClassification, TableVariable, TimeLevel, UnitHintFlag, _variable_key, all_anchors,
)
from pipeline.validators import (  # reuse, don't re-implement anchor parsing/grounding
    _load_rendered_blocks, _normalize_typography, _papers_root, validate_dataset, _value_supported_by_text,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENCODE_CONFIG_PATH = PROJECT_ROOT / "opencode.json"

# There is deliberately NO default model here (this used to be
# "jetstream-scout/llama-4-scout", which silently disagreed with the Streamlit
# launcher's own default). A run's model/provider comes from the explicit,
# checked-in run config (`pipeline.run_config`, `src/eval_config.json`) and is
# recorded in the run manifest.
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
# Table-enumeration design review (this sprint): bounded retry for the
# per-table classification/reconstruction pass (Step B), same rationale/
# shape as MAX_ENUMERATION_ATTEMPTS above -- a classification that fails
# TableClassification's own shape validation, cites an anchor never
# actually read, or fails the deterministic reconstruction sanity check
# (see _table_classification_sanity_check) gets fed back and retried
# within this same budget before that one table is given up on (never
# blocking the rest of the entity type's enumeration).
MAX_TABLE_CLASSIFICATION_ATTEMPTS = 3
# Item 9 (Step B provider failures): a provider failure (empty final text, timeout,
# malformed tool-call stream) says nothing about the table -- the model produced no
# answer to correct -- so it never consumes one of the numbered attempts above. It has
# its own budget instead, per loop per run, with a growing cooldown between rounds.
# `invoke_agent` has already retried the identical call internally by then
# (MAX_EMPTY_RESPONSE_RETRIES).
#
# Sizing (provider-resilience pass). The old budget was 2 rounds with one 20 s cooldown -- about two minutes of
# patience. The stored call timestamps of six live Felipe runs (2026-09-20/21) show the provider fails in short
# bursts that recur through a run, each ending within 0.5-5.7 minutes; records that met a burst died in the middle of
# it, and one dead Citation blocks every later entity. 5 rounds with cooldowns of 20, 60, 180 and 300 s (capped) wait
# out a burst of about ten minutes.
MAX_PROVIDER_FAILURE_ROUNDS = 5
TABLE_PROVIDER_COOLDOWN_SECONDS = 20      # the first cooldown; each later one is PROVIDER_COOLDOWN_GROWTH times longer
PROVIDER_COOLDOWN_GROWTH = 3
PROVIDER_COOLDOWN_CAP_SECONDS = 300
# A real outage must not multiply that patience across every record of the run: after this many loops in a row
# ended on a spent provider budget with no successful model call in between, each further loop gets ONE round
# (`invoke_agent`'s internal retries still apply) until any call succeeds again.
MAX_CONSECUTIVE_PROVIDER_TERMINALS = 3
# Real evidence (Daren-1997-Canopy, run 20260916T143651_0987d819 and the
# earlier 20260916T073404_967ec136): the free-form enumeration pass
# collapsed a table with 6 populations x 3 maturities x 2 sites x 4
# measures into ONE candidate per measure, in both runs, despite the
# entity-identity guidance explicitly warning against exactly that
# pattern -- confirmed NOT a prompt/config regression (opencode_config_
# fingerprint was byte-identical between the two runs). Deterministic
# per-table classification + cross-product (Steps A-D, see orchestrator
# table-enumeration design review) directly attacks this by moving the
# combinatorial "how many distinct ones exist" arithmetic from the model's
# own free-form judgment into code, which cannot lose count or summarize.
# Treatment generalization (real evidence, same run): free-form Treatment
# enumeration split "Population" and "Maturity" into two flatly separate
# sets of Treatments (site-corrected: 18 total), leaving no single
# Treatment able to represent a value genuinely about a combination of
# both -- a real Observation candidate scored an exact tie between its
# matching population-Treatment and maturity-Treatment, correctly
# refusing to guess which one to use. The general principle (a Treatment
# is the FULL combination of factor levels applied to one unit, not any
# one factor alone -- true for any paper crossing more than one factor,
# not specific to this one) is exactly what the SAME table reconstruction
# already used for Observation also expresses: row_group.factor_values +
# value_column.site_hint IS that combination. Reusing it for Treatment
# (see _table_classifications_to_treatment_candidates) removes a second,
# independent free-form guess at the same structure entirely, rather than
# trying to prompt-engineer free-form Treatment enumeration into reliably
# discovering the same combinations on its own -- the exact failure mode
# already confirmed for Observation before Steps A-D existed.
# Still scoped to Treatment/Observation only -- Variable/Coverage/etc.
# stay on free-form enumeration until real evidence (the same audit
# discipline used to find this problem) shows they need it too.
TABLE_ENUMERATION_ENTITY_TYPES = {"Treatment", "Observation"}


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
    # Set by _invoke_agent_once when _has_malformed_harmony_tool_call finds
    # the known gpt-oss-120b/vLLM harmony-format channel-token leak in this
    # attempt's tool-call stream -- see that function's own docstring for
    # the confirmed real signature. True here means parsed_json/final_text
    # above must never be trusted even if they look superficially valid;
    # see _invoke_agent_once's own handling.
    had_malformed_tool_call: bool = False

    def as_artifact(self) -> dict:
        d = asdict(self)
        # Keep raw stdout/stderr in the artifact for debugging, but don't
        # duplicate huge text twice if it parsed cleanly.
        return d


# Failure classes recorded on every failed Step B attempt. The first four are
# provider/infrastructure classes (nothing usable came back), the rest are about a
# model answer that DID come back.
PROVIDER_FAILURE_CLASSES = frozenset({"provider_empty", "provider_timeout", "provider_malformed", "provider_unavailable"})
FAILURE_CLASSES = PROVIDER_FAILURE_CLASSES | {"invalid_json", "schema_invalid", "validation_failure"}


def classify_invocation_failure(result: AgentInvocation) -> Optional[str]:
    """Why an invocation yielded no parsed JSON, or None when it did. Provider
    classes are kept apart from extraction classes so an outage is never
    reported as an extraction failure:
      provider_unavailable -- the agent executable is missing (never retried);
      provider_malformed   -- the harmony-format tool-call leak (see
                              `_has_malformed_harmony_tool_call`);
      provider_timeout     -- the call hit its time limit;
      provider_empty       -- the call ended with no final assistant text;
      invalid_json         -- text came back but is not a JSON object (the
                              model's own output, so an extraction class)."""
    if result.parsed_json is not None:
        return None
    if result.parse_error and "opencode executable not found" in result.parse_error:
        return "provider_unavailable"
    if result.had_malformed_tool_call:
        return "provider_malformed"
    if result.final_text is None:
        if result.returncode == -1 and "timed out after" in (result.stderr or ""):
            return "provider_timeout"
        return "provider_empty"
    return "invalid_json"


def _provider_failure(result: AgentInvocation) -> Optional[str]:
    """The provider failure class of an invocation (`PROVIDER_FAILURE_CLASSES`), or None when the model answered --
    including a genuinely wrong answer (`invalid_json`), which is the model's own and stays a numbered attempt."""
    failure = classify_invocation_failure(result)
    return failure if failure in PROVIDER_FAILURE_CLASSES else None


# Per-run log of provider-failed rounds, filled by `_ProviderBudget` and disclosed in the run manifest
# (`provider_failures`): {run_id: [{stage, record_key, failure_class, terminal}, ...]}.
_PROVIDER_FAILURE_LOG: dict[str, list[dict]] = {}
_PROVIDER_OUTAGE: dict[str, int] = {}   # run_id -> consecutive provider-terminal loops with no successful call since


def provider_cooldown_seconds(failed_rounds: int) -> float:
    """The wait before the next round, given how many rounds this loop has already failed (1 -> the base cooldown):
    it grows by PROVIDER_COOLDOWN_GROWTH each time, up to PROVIDER_COOLDOWN_CAP_SECONDS."""
    return min(TABLE_PROVIDER_COOLDOWN_SECONDS * PROVIDER_COOLDOWN_GROWTH ** max(failed_rounds - 1, 0), PROVIDER_COOLDOWN_CAP_SECONDS)


class _ProviderBudget:
    """Item 9's separate provider-failure budget, ONE mechanism for every loop that calls a model and counts attempts
    (Step B table classification, record-level Extraction, enumeration). A round with no usable answer (empty final
    text, timeout, harmony leak, missing binary) says nothing about the source, so it never consumes one of the loop's
    numbered attempts; it spends this small budget instead: `MAX_PROVIDER_FAILURE_ROUNDS` rounds per loop, with a
    cooldown between them. The model gets no feedback about a failure that was not its answer, and the same prompt
    is repeated. Real evidence (Felipe-2010-Cultivar, run felipe_smoke_20260920T132343): before this, only Step B had
    the budget, and 5 of the 6 records that ended in error had lost a numbered attempt to a provider failure (Crop
    enumeration lost 2 of its 3)."""

    def __init__(self, run_id: str, record_key: str, stage: str):
        self.run_id, self.record_key, self.stage = run_id, record_key, stage
        self.rounds = 0
        self.classes: list[str] = []

    def round_limit(self) -> int:
        """The rounds this loop may spend: the full budget, or a single round while the provider is evidently down
        (`MAX_CONSECUTIVE_PROVIDER_TERMINALS` loops in a row ended on a spent budget with no success between)."""
        down = _PROVIDER_OUTAGE.get(self.run_id, 0) >= MAX_CONSECUTIVE_PROVIDER_TERMINALS
        return 1 if down else MAX_PROVIDER_FAILURE_ROUNDS

    def failed(self, failure_class: str) -> bool:
        """Record one provider-failed round. True when the loop must stop: the executable is missing (never
        retried) or the budget is spent."""
        self.rounds += 1
        self.classes.append(failure_class)
        outage_mode = self.round_limit() < MAX_PROVIDER_FAILURE_ROUNDS
        terminal = failure_class == "provider_unavailable" or self.rounds >= self.round_limit()
        if terminal and failure_class != "provider_unavailable":
            _PROVIDER_OUTAGE[self.run_id] = _PROVIDER_OUTAGE.get(self.run_id, 0) + 1
        _PROVIDER_FAILURE_LOG.setdefault(self.run_id, []).append({
            "stage": self.stage, "record_key": self.record_key, "failure_class": failure_class, "terminal": terminal,
            "outage_mode": outage_mode,
        })
        return terminal

    def cooldown(self) -> None:
        time.sleep(provider_cooldown_seconds(self.rounds))

    def disclosure(self, numbered: int, terminal: bool) -> dict[str, Any]:
        """The fields a terminal record/enumeration/table failure carries: whose failure it was."""
        return {
            "failure_kind": "provider" if terminal else "extraction", "failure_classes": list(self.classes),
            "numbered_attempts": numbered, "provider_failure_rounds": self.rounds,
        }


def summarize_provider_failures(run_id: str, *, discard: bool = True) -> dict[str, Any]:
    """Every provider-failed round of this run for the manifest: totals by stage and class, and the loops that ended
    because the provider budget was spent (`terminal`). Provider failures are never counted as extraction failures."""
    log = _PROVIDER_FAILURE_LOG.pop(run_id, []) if discard else list(_PROVIDER_FAILURE_LOG.get(run_id, []))
    if discard:
        _PROVIDER_OUTAGE.pop(run_id, None)
    by_stage: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for entry in log:
        by_stage[entry["stage"]] = by_stage.get(entry["stage"], 0) + 1
        by_class[entry["failure_class"]] = by_class.get(entry["failure_class"], 0) + 1
    return {
        "total_rounds": len(log), "by_stage": by_stage, "by_class": by_class,
        "terminal": [{k: e[k] for k in ("stage", "record_key", "failure_class")} for e in log if e["terminal"]],
        # Loops that got only one round because the provider was evidently down (see MAX_CONSECUTIVE_PROVIDER_TERMINALS).
        "outage_mode": [e["record_key"] for e in log if e["terminal"] and e.get("outage_mode")],
    }


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


def _has_malformed_harmony_tool_call(stdout: str) -> bool:
    """True when the model's own harmony-format channel-separator token
    ("<|channel|>") leaked into a tool-call NAME and the tool-runner
    rejected it as an unavailable tool -- a confirmed real gpt-oss-120b/
    vLLM provider-format defect (run final_check_observation_daren,
    Observation/Daren-1997-Canopy: `read_section<|channel|>commentary`,
    with stdout containing `'error': "Model tried to call unavailable
    tool 'read_section<|channel|>commentary'. Available tools: ..."`).
    This is never a real, legitimate tool call the model intended -- it is
    always retryable provider-format noise, not evidence about the content
    being extracted. Deliberately a plain substring check on the raw
    stdout stream (not a structured JSON-lines parse like
    `_extract_final_text`): the malformed tool name can appear inside a
    nested `error`/`output` string within a `tool_use` event, not as its
    own top-level recognizable part type, so a substring check across the
    whole stream is the reliable way to catch it regardless of exactly
    which JSON shape it's embedded in."""
    return "<|channel|>" in stdout and "unavailable tool" in stdout


def _decode_if_bytes(value: Optional[Any]) -> Optional[str]:
    """`subprocess.TimeoutExpired.stdout`/`.stderr` can be raw bytes even
    when the original `subprocess.run()` call used `text=True` -- see the
    real confirmed case in `_invoke_agent_once`'s own TimeoutExpired
    handler. Normalizes back to str (or leaves None/str untouched) so
    every downstream string operation is safe regardless of which path
    produced the value."""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


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
        # Confirmed real CPython behavior (reproduced directly): when the
        # subprocess produced SOME output before timing out,
        # TimeoutExpired.stdout/.stderr come back as raw BYTES even though
        # text=True was passed to subprocess.run() -- the internal
        # post-kill partial-output capture bypasses the normal text-
        # decoding step. This was latent until table-enumeration's much
        # higher per-run call volume (hundreds of Observation candidates
        # instead of a dozen) made an actual 300s timeout statistically
        # likely for the first time in a live run -- the real crash it
        # caused: _has_malformed_harmony_tool_call's own substring check
        # ("<|channel|>" in stdout) raises `TypeError: a bytes-like object
        # is required, not 'str'` outright if stdout is bytes, not str.
        stdout = _decode_if_bytes(exc.stdout) or ""
        stderr = (_decode_if_bytes(exc.stderr) or "") + f"\n[orchestrator] timed out after {timeout}s"
    except FileNotFoundError as exc:
        return AgentInvocation(
            agent=agent, model=model, prompt=prompt, returncode=-1, stdout="", stderr=str(exc),
            final_text=None, parsed_json=None, parse_error=f"opencode executable not found: {exc}",
        )

    had_malformed_tool_call = _has_malformed_harmony_tool_call(stdout)
    final_text = _extract_final_text(stdout)
    if had_malformed_tool_call:
        # Never trust final_text/parsed_json even if they look valid --
        # this stream contains the known harmony-format defect, so any
        # "answer" that came alongside it is not a real, intended response.
        parsed_json, parse_error = None, "provider_malformed_response: harmony-format tool-call name leak detected"
    elif final_text:
        parsed_json, parse_error = _parse_json_block(final_text)
    else:
        parsed_json, parse_error = None, "no final assistant text found in agent output"
    return AgentInvocation(
        agent=agent, model=model, prompt=prompt,
        returncode=returncode, stdout=stdout, stderr=stderr,
        final_text=final_text, parsed_json=parsed_json, parse_error=parse_error,
        had_malformed_tool_call=had_malformed_tool_call,
    )


def invoke_agent(agent: str, model: str, prompt: str, timeout: int = 300) -> AgentInvocation:
    """Run one short, single-purpose headless OpenCode call and extract its
    final JSON answer. `--format json` gives a raw JSON-events stream on
    stdout (one event per line) instead of human-formatted text, so the
    final assistant message can be recovered reliably instead of scraped
    from rendered TUI-style output.

    Internally retries, up to MAX_EMPTY_RESPONSE_RETRIES extra times with a
    short backoff, for TWO provider-level infrastructure-noise classes,
    never for a real-but-wrong response (a shape/content problem still
    returns immediately, unchanged, so a genuine validation failure still
    costs exactly one call, and the caller's own attempt-counting/feedback
    loop is completely unaffected):
      1. The provider returns literally no usable text at all (see
         MAX_EMPTY_RESPONSE_RETRIES's own docstring).
      2. The known harmony-format malformed-tool-call signature (see
         _has_malformed_harmony_tool_call's own docstring) -- treated as a
         retryable provider-format failure, never as a valid tool call,
         even when the stream also contains superficially valid-looking
         output alongside it.
    A `FileNotFoundError` (missing opencode executable) also returns
    immediately -- retrying a missing binary can never succeed."""
    last_invocation: Optional[AgentInvocation] = None
    for retry in range(MAX_EMPTY_RESPONSE_RETRIES + 1):
        invocation = _invoke_agent_once(agent, model, prompt, timeout)
        if invocation.final_text is not None and not invocation.had_malformed_tool_call:
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
        "the writing (prose vs. table) must not change how many distinct Treatments exist. "
        "`site_id` is REQUIRED and a single value -- if the paper's experiment is conducted at MORE "
        "THAN ONE site/location and the same experimental treatment level is applied at each one "
        "(e.g. 'no-till' applied at both Site A and Site B), the SAME treatment level at each site is "
        "a SEPARATE Treatment candidate -- 'no-till at Site A' and 'no-till at Site B' are two "
        "candidates, not one, each linked via linked_candidates['site_id'] to its own site. "
        "Reporting one candidate per treatment level regardless of site, leaving site_id unlinked "
        "because 'the paper doesn't distinguish', is only correct when the paper genuinely never "
        "applies that level at more than one site -- check this explicitly whenever more than one "
        "site exists, don't default to skipping it. This applies ONLY to experimental treatment "
        "levels. A cultivar, variety, population or genotype is a CROP, a growth stage, maturity, "
        "date or year is TIME, and a location is a SITE: none of them is a Treatment, and naming "
        "them together in one phrase never makes them one. 'Population P at Site A' and 'Population "
        "P at Site B' are NOT two Treatments -- it is the same crop (one Crop record) grown in two "
        "Site contexts, and 'Population P at maturity M' is that same crop at a point in time. If "
        "the only things that distinguish the candidates you would report are populations or "
        "cultivars, maturities or dates, and sites -- with no experimental treatment level applied "
        "-- report no Treatment candidates for them. "
        "A designed mixture or composition level of several cultivars grown together (e.g. a one-, "
        "three- and five-cultivar mixture) is a level of an experimental factor when the paper "
        "defines it as an experimental treatment, so each such level is a separate Treatment "
        "candidate -- but only at the granularity the paper actually reports values or comparisons "
        "for: never multiply it with another factor's levels into combinations the paper does not "
        "report."
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
        "reported amounts; do not collapse a sequence of events into one generic record. "
        "An event is something that HAPPENED to the field (Protocol Section 6.6: events are distinct "
        "from treatments); an experimental condition -- a winter fallow, a cover-crop treatment -- is a "
        "Treatment, not a Management record, and it is the planting, mowing or incorporation events that "
        "define it. Capture events as far as the source states them (Protocol Section 9.1), keeping the "
        "top-level event type PEcAn-aligned where practical: "
        + ", ".join(vocab.SEED_EVENT_TYPES) + ". "
        "Do infer that an event occurred when it is certain (crops were planted; there was a harvest event "
        "if yields or harvested biomass are reported), even when the paper states nothing more about it; "
        "do NOT infer or complete event dates -- never supply a year or any part of a date the source does "
        "not give for that event -- nor unreported rates or management histories implied only by local "
        "practice (Protocol Section 9.2). A relative statement such as 'before planting' or 'after harvest' "
        "is kept as that statement, without inventing a date."
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
        "merely because Observations for that site/variable exist elsewhere in this run. `site_id` is "
        "REQUIRED and a single value -- ONE SITE per Coverage candidate: if the paper conducts the "
        "same data collection at more than one site (e.g. two field stations), that is ONE Coverage "
        "candidate PER SITE, each linked via linked_candidates['site_id'] to its own site, never one "
        "candidate covering multiple sites at once."
    ),
}


def _enumeration_prompt(
    paper_id: str, entity_type: str, prior_errors: Optional[list[dict]] = None,
    link_pools: Optional[dict[str, list[dict]]] = None,
    excluded_table_anchors: Optional[set[str]] = None,
    covered_conditions: Optional[list[str]] = None,
    declare_dimensions: bool = False,
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
        # The example uses a field this entity type is actually offered. Real bug (Felipe-2010-Cultivar, Management): the
        # example was hard-coded as {"treatment_id": ...}, so for the offered list field `treatment_ids` the model copied
        # the singular key and every link it proposed was silently ignored (Item 14's verification never ran).
        example_field = "treatment_id" if "treatment_id" in link_pools else next(iter(link_pools))
        example_value = '["ambient_co2"]' if example_field.endswith("_ids") else '"ambient_co2"'
        base += (
            f"- linked_candidates: a JSON object naming, for each field below, the SPECIFIC "
            f"already-extracted record this {entity_type} is actually about -- ONLY when the "
            f"evidence clearly ties it to exactly one. Use the EXACT slug string shown (e.g. "
            f'{{"{example_field}": {example_value}}}), never a slug you invent or one not listed below, '
            f"and never guess when the paper does not make the link explicit -- omit that key "
            f"entirely rather than guessing.\n\n" + "\n".join(pool_lines) + "\n\n"
        )
        list_fields = sorted(field for field in link_pools if field.endswith("_ids"))
        if list_fields:
            base += (
                f"The key must be spelled exactly {', '.join(repr(f) for f in list_fields)} (plural, a list) -- never the "
                f"singular form. "
                f"For {', '.join(repr(f) for f in list_fields)}, give a JSON LIST of the slug(s) of the record(s) this "
                f"{entity_type} is stated to apply to -- and ONLY when the source text you cite in `anchors` itself NAMES "
                f"that condition. Most events (site-wide land preparation, an operation done on every plot) apply to every "
                f"condition and name none: for those omit the key entirely. Never link an event to a condition merely "
                f"because that is plausible; an unnamed condition is left unlinked.\n\n"
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
    if excluded_table_anchors:
        base += (
            f"\n\nThe following content.md table anchors have ALREADY been deterministically "
            f"enumerated for {entity_type} by a separate pass and are fully covered -- do NOT "
            f"report a new candidate whose evidence comes primarily from one of these anchors: "
            f"{sorted(excluded_table_anchors)}. Only report candidates from narrative prose or "
            f"from tables NOT in this list."
        )
    if declare_dimensions:
        base += _declare_dimensions_block(entity_type, covered_conditions)
    if prior_errors:
        base += (
            "\n\nThe previous attempt failed validation with these errors -- fix exactly these, "
            "the rest of your approach was fine:\n" + json.dumps(prior_errors, indent=2)
        )
    return base


_MAX_LISTED_COVERED_CONDITIONS = 40


def _declare_dimensions_block(entity_type: str, covered_conditions: Optional[list[str]]) -> str:
    """Item 8: added to the free-form prompt ONLY when deterministic table
    enumeration already covers this entity type. The model is told what is
    already covered and must declare, for every candidate it still reports, the
    factor levels that distinguish it and what each one IS -- so the
    orchestrator can compare it, by the same canonical identity table
    candidates use, instead of guessing from names."""
    text = ""
    if covered_conditions:
        listed = covered_conditions[:_MAX_LISTED_COVERED_CONDITIONS]
        more = len(covered_conditions) - len(listed)
        text += (
            f"\n\nThe tables of this paper have ALREADY been deterministically enumerated for {entity_type}: "
            f"these conditions are covered, do not report them again:\n"
            + "\n".join(f"  - {c}" for c in listed)
            + (f"\n  - ... and {more} more" if more > 0 else "")
        )
    text += (
        f"\n\nFor EVERY candidate you still report, also give `dimensions`: a list of "
        f"{{name, dimension, level}} entries, one per factor level that distinguishes it. `name` is the "
        f"factor as the paper calls it; `level` is its literal level; `dimension` is exactly one of "
        f"'treatment' (an experimental management or system condition the study APPLIES -- e.g. a "
        f"cover-crop, tillage, fertilizer or irrigation level), 'crop' (an individual cultivar, variety, "
        f"population or genotype), 'time' (a date, growth stage, year or season), 'site' (a location), or "
        f"'other'. A single cultivar/population is 'crop', a date or growth stage is 'time', a location is "
        f"'site' -- none of these is a 'treatment'. {MIXTURE_LEVEL_RULE} "
        f"Every level must appear in your description or in a block you cite. "
        f"Something with no treatment-dimension level is not a {entity_type}: do not report it."
    )
    return text


def _unsplit_required_dimensions(
    entity_type: str, candidates: list, link_pools: Optional[dict[str, list[dict]]],
) -> list[tuple[str, str]]:
    """Detects a real, confirmed failure pattern (Daren-1997-Canopy,
    Treatment enumeration, run 20260916T200235_5fae474d): a paper with
    more than one ready Site applies the SAME factor levels at every site
    (e.g. all 6 switchgrass populations grown at both Ames AND Mead), but
    enumeration reported each level as ONE candidate rather than splitting
    per site -- none of the candidates could then link `site_id` to a
    specific one (correctly, per the "never guess when the paper does not
    make the link explicit" rule), leaving `site_id` genuinely ambiguous
    for every candidate. The refuse-to-guess gate downstream then
    correctly refuses ALL of them, which can cascade into blocking every
    entity type that depends on this one (the real run: zero ready
    Treatments blocked both Observation and TreatmentPair entirely, even
    though nothing else was wrong).

    Returns [(field, prereq_type), ...] for every REQUIRED dependency
    field whose link_pool is genuinely ambiguous (>1 ready candidate --
    `_multi_record_link_pools` only ever builds a pool in that case to
    begin with) but which NOT ONE candidate linked to. This is a RETRY
    SIGNAL, not a hard failure: a paper can legitimately not distinguish
    by this dimension for every candidate of this entity type, so the
    caller only asks the model to double-check, never blocks acceptance
    of the result once attempts are exhausted."""
    if not link_pools:
        return []
    required_fields = {
        field: prereq_type
        for field, prereq_type, required in ENTITY_DEPENDENCIES.get(entity_type, [])
        if required
    }
    return [
        (field, required_fields[field])
        for field, pool in link_pools.items()
        if field in required_fields
        and not any(field in (c.linked_candidates or {}) for c in candidates)
    ]


def _misnamed_link_keys(candidates: list, link_pools: Optional[dict[str, list[dict]]]) -> list[tuple[str, str, str]]:
    """[(candidate_id, key given, canonical key offered)] for every `linked_candidates` key that is not an offered field
    but is a singular/plural near-miss of one (`treatment_id` for `treatment_ids`). There is ONE canonical spelling per
    link field; a near-miss is never silently accepted (it would bypass the verification that runs on the canonical
    field) and never silently ignored either -- it is reported back to the model, and dropped, logged, at the last
    attempt."""
    if not link_pools:
        return []

    def stem(name: str) -> str:
        return name.removesuffix("_ids").removesuffix("_id")

    by_stem = {stem(field): field for field in link_pools}
    return [
        (c.candidate_id, key, by_stem[stem(key)])
        for c in candidates for key in (c.linked_candidates or {})
        if key not in link_pools and stem(key) in by_stem
    ]


def run_enumeration(
    *, run_id: str, paper_id: str, entity_type: str, model: str,
    invoke: Callable[..., AgentInvocation] = invoke_agent,
    link_pools: Optional[dict[str, list[dict]]] = None,
    excluded_table_anchors: Optional[set[str]] = None,
    covered_conditions: Optional[list[str]] = None,
    declare_dimensions: bool = False,
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
    # Provider failures spend the separate provider budget, not a numbered attempt (correction pass, Fix 2).
    provider = _ProviderBudget(run_id, record_key, "enumeration")
    attempt = 0   # numbered attempts: the model's own answers
    rounds = 0    # every invocation round: the artifact index
    provider_terminal = False

    while attempt < MAX_ENUMERATION_ATTEMPTS:
        rounds += 1
        result = invoke(
            "extractor", model,
            _enumeration_prompt(
                paper_id, entity_type, errors, link_pools, excluded_table_anchors,
                covered_conditions=covered_conditions, declare_dimensions=declare_dimensions,
            ),
        )
        artifact = result.as_artifact()

        failure = _provider_failure(result)
        if failure:
            artifact["validation_errors"] = [{"field": None, "message": result.parse_error}]
            artifact.update(failure_class=failure, failure_kind="provider", numbered_attempt=None)
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            last_message = f"round {rounds}: provider failure ({failure}): {result.parse_error}"
            if provider.failed(failure):
                provider_terminal = True
                break
            provider.cooldown()
            continue
        attempt += 1

        if result.parsed_json is None:
            errors = [{"field": None, "message": result.parse_error}]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            last_message = f"attempt {attempt}: {result.parse_error}"
            continue

        try:
            validated = EnumerationResult.model_validate(result.parsed_json)
        except ValidationError as exc:
            errors = [
                {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()
            ]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            last_message = f"attempt {attempt}: EnumerationResult shape validation failed: {errors}"
            continue

        try:
            blocks = _load_rendered_blocks(paper_id)
        except FileNotFoundError as exc:
            errors = [{"field": None, "message": f"cannot validate candidate anchors: {exc}"}]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
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
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
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
            for field, value in (candidate.linked_candidates or {}).items()
            for slug in ([value] if isinstance(value, str) else value)
            if link_pools and field in link_pools and slug not in {item["slug"] for item in link_pools[field]}
        ]
        if invalid_link_errors:
            errors = invalid_link_errors
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            last_message = f"attempt {attempt}: invalid linked_candidates: {errors}"
            continue

        misnamed = _misnamed_link_keys(validated.candidates, link_pools)
        if misnamed and attempt < MAX_ENUMERATION_ATTEMPTS:
            errors = [
                {
                    "field": "linked_candidates",
                    "message": f"candidate '{cid}' uses the link key {key!r}, but the field offered for this entity type is "
                               f"{canonical!r}{' (plural: a JSON LIST of slugs)' if canonical.endswith('_ids') else ''} -- use exactly "
                               f"that key; the other spelling is not accepted.",
                }
                for cid, key, canonical in misnamed
            ]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            last_message = f"attempt {attempt}: misnamed linked_candidates key(s): {errors}"
            continue
        if misnamed:
            # Last attempt: the link is optional evidence, so the mis-keyed link is DROPPED (logged), never accepted.
            wrong = {(cid, key) for cid, key, _ in misnamed}
            validated = validated.model_copy(update={"candidates": [
                c.model_copy(update={"linked_candidates": {
                    k: v for k, v in (c.linked_candidates or {}).items() if (c.candidate_id, k) not in wrong}})
                for c in validated.candidates
            ]})
            artifact["dropped_link_keys"] = [{"candidate_id": cid, "key": key, "canonical": canonical} for cid, key, canonical in misnamed]

        # Real confirmed cascade (Daren-1997-Canopy, Treatment enumeration,
        # run 20260916T200235_5fae474d): give the model ONE chance to
        # double-check before accepting candidates that leave a genuinely
        # ambiguous REQUIRED dimension unsplit -- see
        # _unsplit_required_dimensions's own docstring. Only while attempts
        # remain: on the final attempt, accept whatever was produced rather
        # than erroring out entirely (the downstream refuse-to-guess gate
        # remains the real safety net either way; this is purely a chance
        # to avoid the cascade, never a hard requirement).
        unsplit_dims = _unsplit_required_dimensions(entity_type, validated.candidates, link_pools)
        if unsplit_dims and attempt < MAX_ENUMERATION_ATTEMPTS:
            errors = [
                {
                    "field": "linked_candidates",
                    "message": (
                        f"This paper has more than one real, ready {prereq_type} "
                        f"({sorted(item['slug'] for item in link_pools[field])}), but NONE of your "
                        f"candidates linked {field!r} to a specific one. If the paper reports or "
                        f"applies the same {entity_type} at more than one {prereq_type.lower()} "
                        f"separately (a common pattern -- e.g. a multi-{prereq_type.lower()} "
                        f"experiment applying the same factor levels at each one), each "
                        f"{prereq_type.lower()}'s occurrence is a SEPARATE candidate: split further "
                        f"and link each one via linked_candidates[{field!r}]. If, having checked, the "
                        f"paper genuinely does not distinguish by {prereq_type.lower()} here, leave "
                        f"it unlinked as before -- this is a prompt to double-check, not a requirement "
                        f"to force a link that doesn't exist."
                    ),
                }
                for field, prereq_type in unsplit_dims
            ]
            artifact["validation_errors"] = errors
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            last_message = f"attempt {attempt}: candidates not split by required dimension(s): {[f for f, _ in unsplit_dims]}"
            continue

        candidates = validated.candidates
        if declare_dimensions:
            dimension_errors, bad_ids = _candidate_dimension_errors(candidates, blocks)
            if dimension_errors and attempt < MAX_ENUMERATION_ATTEMPTS:
                errors = dimension_errors
                artifact["validation_errors"] = errors
                run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
                last_message = f"attempt {attempt}: candidate dimensions invalid: {errors}"
                continue
            # Final attempt: never lose a candidate over its dimensions. One whose
            # dimensions are missing or inconsistent keeps NO dimensions, so its
            # identity is unresolved and it is kept and flagged, never compared.
            candidates = [c.model_copy(update={"dimensions": []}) if c.candidate_id in bad_ids else c for c in candidates]

        artifact["validation_errors"] = []
        run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
        return candidates, None

    run_store.save_final(run_id, record_key, {
        "status": "error", "message": last_message,
        **(provider.disclosure(attempt, provider_terminal) if provider.rounds else {}),
    })
    return [], last_message


# --------------------------------------------------------------------------- #
# Table enumeration (Steps A-D) -- deterministic candidate generation for
# dense data tables, to fix a confirmed collapse in free-form enumeration.
#
# Real evidence (Daren-1997-Canopy): a table with 6 populations x 3
# maturities x 2 sites x 4 measures (content.md anchor b:0119, continued at
# b:0178) produced only ONE free-form enumeration candidate per measure --
# in BOTH a run before and a run after the entity-identity-guidance fix,
# with the model's own candidate descriptions literally reading "for each
# population... across maturities", exactly the anti-pattern that guidance
# warns against. Confirmed NOT a prompt/config regression
# (opencode_config_fingerprint was byte-identical between the two runs) --
# free-form enumeration simply cannot reliably hold ~140 things in one
# output. This moves the "how many distinct ones exist" arithmetic from
# the model's own judgment (where it keeps losing count) into deterministic
# code (which cannot), while keeping the model responsible for the one
# thing it's actually needed for: interpreting one table's real structure,
# a MUCH narrower and more tractable ask than enumerating the whole paper.
#
# Step A (table discovery, pure code) -> Step B (per-table classification/
# reconstruction, one narrow LLM call per table, bounded retry + a
# deterministic reconstruction sanity check) -> Step C (pure deterministic
# cross-product into EnumerationCandidates) -> Step D (merge with the
# existing free-form pass, told which anchors are already covered).
# Everything downstream of candidate generation -- _apply_candidate_links,
# run_record, extraction, conversion, provenance validation, the
# refuse-to-guess gate, AI validation -- is completely unchanged: this
# mechanism only changes what populates the candidates list.
# --------------------------------------------------------------------------- #


_NUMERIC_TOKEN_RE = re.compile(r"-?\d+\.?\d*")


def _numeric_tokens(text: str) -> list[str]:
    return _NUMERIC_TOKEN_RE.findall(text or "")


def _table_classification_sanity_check(classification: TableClassification, paper_id: str) -> Optional[str]:
    """Deterministic reconstruction check (added per the table-enumeration
    design review's own identified main risk): TableClassification's own
    schema validation only confirms the SHAPE is well-formed, never that
    the reconstruction is actually faithful to the source -- a well-formed
    row_groups list could still have silently dropped or fabricated real
    numeric values. Deliberately count-based on NUMERIC TOKENS, not raw
    cell/row counts or positions: raw geometric table cells are confirmed
    NOT reliably 1:1 with logical rows in real data (Daren-1997-Canopy
    content.md anchor b:0119's row 3 packs three maturity-stage values --
    the single raw cell text '0.19 0.90 1.16' -- into ONE cell; anchor
    b:0178's row 3 merges cells from THREE unrelated population rows into
    one row_index, because an LSD footnote row breaks up that page's
    visual layout and throws off Marker's geometric clustering there), so
    comparing raw structure position-by-position would itself be
    unreliable. Every real reported number must appear as a numeric token
    somewhere in the raw cell text regardless of how cells happened to get
    geometrically clustered, and (if genuinely reported, not blank) as a
    numeric token somewhere in the reconstruction -- comparing TOTAL counts
    is robust to how cells were grouped.

    Returns None when the counts are within a generous tolerance of each
    other, or a diagnostic message (used as a retryable validation error,
    same as a schema-validation failure) when they are not. The tolerance
    is a deliberately loose heuristic, not a precise scientific threshold:
    real numeric content correctly EXCLUDED from row_groups (LSD footnote
    values, unit-row noise) means some drop is normal and expected, not
    itself evidence of a bad reconstruction -- this catches GROSS
    under- or over-accounting, not small legitimate differences."""
    raw_tokens_count = sum(
        len(_numeric_tokens(t))
        for t in content_reader.raw_table_cells(paper_id, classification.table_anchors, papers_root=_papers_root())
    )
    if raw_tokens_count == 0:
        return None  # nothing numeric in the raw source to check the reconstruction against

    reconstructed_count = sum(
        len(_numeric_tokens(v))
        for row in classification.row_groups
        for v in (row.cells or {}).values()
        if v
    )

    if reconstructed_count > raw_tokens_count:
        return (
            f"reconstruction has MORE numeric values ({reconstructed_count}) than the raw table "
            f"cells actually contain ({raw_tokens_count}) across anchors {classification.table_anchors} "
            f"-- every cell value must come from real, actually-read source text, never fabricated "
            f"or duplicated."
        )
    if reconstructed_count < raw_tokens_count * 0.5:
        return (
            f"reconstruction accounts for only {reconstructed_count} of {raw_tokens_count} raw "
            f"numeric values across anchors {classification.table_anchors} -- likely real reported "
            f"data was dropped; re-check every row, including any where a single cell appeared to "
            f"pack more than one value together (e.g. several maturity stages in one cell)."
        )
    return None


def _table_classification_prompt(
    paper_id: str, seed_table_anchor: str, other_tables: list[dict],
    prior_errors: Optional[list[dict]] = None,
    confirmed_chain: Optional[list[str]] = None,
    methods_context: str = "",
) -> str:
    """Step B's prompt: a MUCH narrower ask than free-form enumeration --
    "reconstruct the real row-by-row structure of THIS one table", not
    "enumerate every {entity_type} in the whole paper". Reuses the same
    extractor agent and content.md tools already available (read_table,
    read_table_row, read_table_cell) -- no new agent, no new tool.

    Deliberately entity-agnostic (no entity_type parameter): this
    reconstruction is a paper-level, shared structural fact -- which
    experimental conditions this table reports, and what values it gives
    for each -- not something that differs depending on which entity type
    (Treatment, Observation, ...) will eventually consume it. Computed
    ONCE per table per run (see run_table_classification's own on-disk
    cache) and projected into whatever entity-specific candidates a caller
    needs (see run_table_enumeration's Treatment vs Observation
    projections) -- this keeps that projection provably consistent instead
    of two separate passes independently guessing at the same table."""
    # Continuation chain (deterministic, see content_reader.table_continuation_map):
    # blocks confirmed to be the page-split continuation of the seed. Present
    # ONLY when the chain has more than one block, so the prompt for every
    # ordinary single-block table is byte-identical to before this existed.
    chain_note = ""
    if confirmed_chain and len(confirmed_chain) > 1:
        chain_note = (
            f"CONFIRMED CONTINUATION: the block(s) {confirmed_chain[1:]} have been deterministically "
            f"verified as the page-split continuation of '{seed_table_anchor}' (they directly follow it "
            f"in reading order with no new caption, have the same number of columns, and are on the "
            f"same or the next page). They are the SAME logical table, not a separate table: read "
            f"them with read_table/read_table_row/read_table_cell too, include EVERY one of "
            f"{confirmed_chain} in table_anchors, and reconstruct ONE row-by-row table across all of "
            f"them (rows such as an 'LSD' row are statistics, not data rows, wherever they appear).\n\n"
        )
        other_tables = [t for t in other_tables if t["table_anchor"] not in confirmed_chain]
    other_lines = "\n".join(
        f"  - {t['table_anchor']} (page {t.get('page')}, section {t.get('section_path')})"
        for t in other_tables
    ) or "  (none)"
    # Real evidence (Felipe-2010-Cultivar Table 1, run felipe_smoke_20260920T132343): the model made two tool calls
    # (read_table, read_nearby), never read Methods, and gave no method_hint for any of the 6 variables -- so none of
    # the 22 table Observations could resolve a Method. The paper's Methods blocks are therefore supplied here,
    # verbatim, by the orchestrator; a hint is still only kept if the paper's prose supports it (see
    # `_withhold_ungrounded_method_hints`). Absent (no Methods section found), the prompt is unchanged.
    methods_note = (
        f"METHODS TEXT -- the verbatim blocks of this paper's Methods section, each labelled with its anchor, supplied "
        f"so you do not have to go and find them. Use it ONLY for `method_hint`: for each variable this table reports, "
        f"when this text says how THAT variable was measured, give `method_hint` as a short phrase in the text's own "
        f"words for the instrument, technique or procedure (e.g. 'portable-tube solarimeter', 'Nitrogen Gas "
        f"Analyzer'). Omit `method_hint` when the text does not say how it was measured: a hint no block of this text "
        f"supports is discarded, and a guess would link the wrong Method.\n{methods_context}\n\n"
        if methods_context else ""
    )
    base = (
        f"Reconstruct the FULL row-by-row structure of the table at content.md anchor "
        f"'{seed_table_anchor}' in paper_id=`{paper_id}` -- which experimental condition(s) each row "
        f"represents, and what value(s) it reports for each -- for use identifying both the "
        f"distinct experimental conditions/treatments this table covers AND the distinct "
        f"individual values it reports.\n\n"
        f"Use read_table/read_table_row/read_table_cell (passing paper_id=`{paper_id}` and this "
        f"table_anchor) to read the table's REAL rendered markdown AND its raw per-cell "
        f"row_index/col_index data. Read BOTH -- they can disagree (a raw geometric cell can "
        f"contain more than one logical value packed together, e.g. several space-separated "
        f"numbers for several growth stages in one cell; a raw row_index can also merge unrelated "
        f"rows together when a footnote breaks up a table's visual layout on the page). When they "
        f"disagree, reconstruct the CORRECT row-by-row breakdown yourself using both as evidence -- "
        f"never trust raw row_index groupings uncritically.\n\n"
        f"ALSO call read_nearby(paper_id=`{paper_id}`, anchor='{seed_table_anchor}', before=2) to read "
        f"this table's own caption (the block immediately preceding it) -- captions spell out "
        f"abbreviations in full (e.g. 'mean stage by count (MSC)') and this is often the ONLY place "
        f"they are, which matters because a table's own column headers can themselves be rendered "
        f"split/garbled by the source PDF extraction (a real confirmed case: a 'MSC' header rendered "
        f"as two separate raw cells reading 'M.' and 'SC' -- use the caption to recognize this and "
        f"label the resulting value_column with the REAL name, not the garbled fragment).\n\n"
        f"A factor/label cell (e.g. a Location or Population column) that is BLANK, or contains text "
        f"unrelated to any real label the table could possibly mean (a rendering artifact -- a real "
        f"confirmed case: a Location cell rendered as the literal garbage text 'CONTRACTOR "
        f"DESCRIPTION' where a location name belonged), is very likely a VERTICALLY-MERGED cell: "
        f"many real tables state a label ONCE at the top of a group of rows it applies to, leaving "
        f"every row below it blank in the source layout until the next real label appears. In that "
        f"case, use the most recent REAL, legible label above it in that same column, never the "
        f"blank/garbled text itself and never a guess unrelated to the table's actual structure.\n\n"
        f"Other table blocks in this paper (for reference only -- read one of these with "
        f"read_table/read_table_row ONLY if you determine, from its own content, that it is a "
        f"page-split CONTINUATION of the SAME table as '{seed_table_anchor}': same columns, "
        f"immediately following page, no new caption of its own):\n{other_lines}\n\n"
        f"{chain_note}"
        f"{methods_note}"
        f"Output ONLY a TableClassification JSON object with exactly these top-level keys: "
        f"table_role, reason, table_anchors, factors, context_levels, pooled_factors, time_levels, "
        f"variables, value_columns, row_groups.\n\n"
        f"- table_role: what KIND of table this is -- exactly one of:\n"
        f"    'treatment_response': reports measured response values (yields, biomass, concentrations, "
        f"fluxes, soil or plant measurements, ...) broken out by the conditions of the study "
        f"(treatment levels, cultivars/populations, sites, dates, depths, ...), one value per "
        f"combination -- the normal data table.\n"
        f"    'aggregated_summary': reports POOLED or AVERAGED main-effect summaries rather than real "
        f"per-combination cells -- telltale row/column labels include phrases like 'across populations', "
        f"'across locations and maturities', 'averaged across', 'pooled', or a factor label that only "
        f"ever appears ALONE (e.g. a 'Location' section with no other factor, immediately followed by "
        f"a separate 'Population' section -- two different rollups sharing one table). Never invent a "
        f"'Treatment' out of a rollup label like a bare site name or a bare population name averaged "
        f"over everything else; if unsure between this and 'treatment_response', choose "
        f"'aggregated_summary' and explain why in reason.\n"
        f"    'weather_context': meteorological or climate data (temperature, precipitation, "
        f"radiation, humidity, wind, ...) describing the conditions during the study, reported per "
        f"location and/or period. Soil, plant, animal or gas measurements are NOT weather_context, "
        f"even when reported per site or date.\n"
        f"    'non_enumerable': anything else that does not report per-condition measured values -- "
        f"statistical model or test tables (regression equations, ANOVA, LSD/HSD or other comparison "
        f"statistics), lists of abbreviations, or a table too garbled to reconstruct. A table whose "
        f"cells hold equations, regression coefficients, R-squared values or test statistics rather than "
        f"measured quantities is 'non_enumerable', even when it is organized by population and site.\n"
        f"- reason: REQUIRED (a real, specific sentence) for every table_role except "
        f"'treatment_response' (for 'aggregated_summary', state which factor(s) are pooled or "
        f"averaged), and also for a 'treatment_response' table whose row_groups you cannot "
        f"confidently reconstruct (a genuinely garbled or ambiguous table) -- otherwise may be "
        f"omitted/null.\n"
        f"- factors: one entry per experimental dimension of THIS table, {{name, dimension, encoding}}. name is "
        f"the dimension as the table calls it (e.g. 'Population', 'Maturity', 'Location', 'Variable'). "
        f"dimension is what it IS: 'treatment' (an experimental management or system condition the "
        f"study applies -- e.g. a cover-crop, tillage, fertilizer or irrigation level), 'crop' (an individual "
        f"cultivar, variety, population or genotype), 'time' (a sampling or harvest date, growth stage, year, "
        f"season or day after planting), 'site' (a location), 'variable' (WHICH measured quantity a row or "
        f"column reports), 'replicate' (block or plot), or 'other'. encoding is where its levels live: "
        f"'rows' (a label column -- the level goes in each row group's factor_values), 'columns' (column "
        f"headers -- the level goes in that value column's factor_levels), or 'context' (one level for "
        f"the whole table, taken from its caption or a footnote -- goes in context_levels). "
        f"context_levels and each value column's factor_levels are JSON OBJECTS mapping a factor name to "
        f"its level: write {{}} when empty, never []. Every key "
        f"you use in factor_values, factor_levels or context_levels MUST be declared here. A "
        f"single cultivar/population is 'crop', a date or growth stage is 'time', a location is 'site' -- none "
        f"of these is a 'treatment'. {MIXTURE_LEVEL_RULE}\n"
        f"- pooled_factors: ONLY when the table's values are means pooled over some factor that does NOT "
        f"appear in its rows or columns (a table note such as 'means across all X'): one entry per such "
        f"factor, {{name, dimension, evidence_anchor, evidence_excerpt}}, where evidence_excerpt is the "
        f"LITERAL source text (caption, footnote or body) stating the pooling. Omit when nothing is pooled.\n"
        f"- time_levels: ONLY for a table with a 'time'-dimension factor whose levels (a growth stage, a "
        f"sampling occasion, ...) the paper DATES somewhere else, usually in Methods (use read_section): one "
        f"entry per level -- and per site when the paper dates it differently at each site -- "
        f"{{factor, level, site, date_text, year_text, anchors}}. factor is the declared time factor, level "
        f"the level exactly as this table has it, site (only when it differs per site) the site as the paper "
        f"calls it, date_text the LITERAL source text giving that level's day and month (e.g. '9 June'), "
        f"year_text the LITERAL source text giving the year (it may be in another block), and anchors every "
        f"content.md block you read them from. Copy the text verbatim -- never compute, convert or guess a "
        f"date; omit this key when the table has no time factor or the paper states no date.\n"
        f"- variables: one entry per measured variable the table reports, {{label, variable_name, units, "
        f"method_hint}} -- label is the variable exactly as the table calls it (a row label or a column "
        f"header), variable_name its normalized name (e.g. 'shoot biomass'), units as the table gives them, "
        f"and method_hint ONLY when the paper's Methods actually says how THIS variable was measured "
        f"(otherwise omit it -- never invent a method). When a variable is given by ROW labels (a "
        f"'variable'-dimension factor with encoding 'rows'), declare it here and leave variable_name_hint "
        f"out of the value columns; when it is given by a column header, set that column's `variable` to "
        f"the entry's label. Omit `variables` for a table whose columns each carry their own "
        f"variable_name_hint.\n"
        f"- table_anchors: every content.md block anchor that is part of THIS one logical table -- "
        f"just ['{seed_table_anchor}'] unless you confirmed a genuine continuation as described "
        f"above.\n"
        f"- value_columns: one entry per column that reports an actual measured value (never a "
        f"factor/label column like Population or Maturity), each "
        f"{{value_column_id, variable_name_hint, variable, units_hint, site_hint, method_hint, "
        f"treatment_level_hint, factor_levels}} -- "
        f"value_column_id is a short unique slug you choose; variable_name_hint combines "
        f"multi-level headers if the table has them (e.g. a 'Total yield' header spanning "
        f"'Ames'/'Mead' sub-columns becomes TWO value_columns, one per site, each with site_hint "
        f"set). method_hint is REQUIRED whenever the paper's Methods section (use read_section to "
        f"check it) names how this specific column's values were actually measured -- e.g. 'hand-"
        f"clipping harvest', 'LI-COR LAI-2000 leaf area analyzer', 'forced-draft oven at 55C' -- "
        f"omit only when the paper genuinely never describes a method for this measurement. "
        f"treatment_level_hint: set ONLY when the column header ITSELF names a distinct EXPERIMENTAL "
        f"TREATMENT LEVEL (e.g. a table with separate 'Fallow'/'Mustard' sub-columns -- each becomes "
        f"its own value_column with treatment_level_hint='Fallow' / 'Mustard'), as opposed to the "
        f"far more common case where treatment is named in a row/factor column instead (leave "
        f"treatment_level_hint unset in that case -- it must never be set 'just in case', only when "
        f"the treatment genuinely lives in the column header itself).\n"
        f"- row_groups: one entry per LOGICAL data row (after splitting any packed cells -- see "
        f"above), each {{row_group_id, factor_values, source_table_anchor, cells}} -- factor_values "
        f'is e.g. {{"Population": "Trailblazer", "Maturity": "Vegetative"}}; source_table_anchor is '
        f"whichever ONE of table_anchors this row's real data came from; cells is "
        f"{{value_column_id: the exact cell text for this row, or null if genuinely not reported}}. "
        f"Never fabricate a cell value that is not really in the source -- use null.\n\n"
        f"Do not skip real rows to save space -- if the table has 18 population x maturity "
        f"combinations, report all 18 you can find, not a representative sample."
    )
    if prior_errors:
        base += (
            "\n\nThe previous attempt failed validation with these errors -- fix exactly these, "
            "the rest of your approach was fine:\n" + json.dumps(prior_errors, indent=2)
        )
    return base


def _load_cached_table_classification(run_id: str, record_key: str) -> Optional[TableClassification]:
    """Table classification (Step B) is entity-agnostic and paper-level --
    computed once per table per run_id, reused across every entity type
    that needs it (Treatment's turn, then Observation's turn later), never
    spending a second real LLM call on the exact same table. Checked via
    the same run_store.save_final convention every other terminal stage
    result already uses (see run_table_classification's own success path)
    -- just read back before deciding whether any new work is needed."""
    path = run_store.record_dir(run_id, record_key) / "final.json"
    if not path.is_file():
        return None
    try:
        data = run_store.load_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    try:
        return TableClassification.model_validate(data["classification"])
    except (KeyError, ValidationError):
        return None


def run_table_classification(
    *, run_id: str, paper_id: str, seed_table_anchor: str,
    other_tables: list[dict], model: str, invoke: Callable[..., AgentInvocation] = invoke_agent,
    chain_anchors: Optional[list[str]] = None,
) -> tuple[Optional[TableClassification], Optional[str]]:
    """Step B: bounded, deterministically-validated classification/
    reconstruction pass for ONE table -- mirrors run_enumeration's own
    shape (same MAX_*_ATTEMPTS-bounded retry-with-feedback loop, same
    "never trust the model's own anchor claim" re-check against real
    content.md), plus one extra check neither shape validation nor the
    anchor check alone can catch: _table_classification_sanity_check.

    Entity-agnostic and cached on disk per (run_id, table): a second call
    for the same table within the same run (e.g. Observation's turn,
    after Treatment's turn already classified it) returns the cached
    result with no new LLM call at all -- see
    _load_cached_table_classification."""
    record_key = f"table_classification__{seed_table_anchor.replace(':', '_')}"

    cached = _load_cached_table_classification(run_id, record_key)
    if cached is not None:
        return cached, None
    # A terminal failure is cached for the run too, so a second entity type's pass (Observation
    # after Treatment) does not spend the same provider budget on the same table again.
    cached_failure = _load_cached_table_failure(run_id, record_key)
    if cached_failure is not None:
        return None, cached_failure["message"]

    errors: list[dict] = []
    methods_context = _methods_context(paper_id)
    last_message = "table classification never produced a valid TableClassification"
    numbered = 0          # model answers that came back (a genuine attempt): bounded by MAX_TABLE_CLASSIFICATION_ATTEMPTS
    rounds = 0            # every invocation round, provider-failed or not: the artifact index
    provider = _ProviderBudget(run_id, record_key, "table_classification")  # rounds with no usable answer
    failure_classes: list[str] = []
    terminal_kind = "extraction"

    def _save(failure_class: Optional[str]) -> None:
        artifact["failure_class"] = failure_class
        artifact["failure_kind"] = None if failure_class is None else ("provider" if failure_class in PROVIDER_FAILURE_CLASSES else "extraction")
        artifact["numbered_attempt"] = None if failure_class in PROVIDER_FAILURE_CLASSES else numbered
        if failure_class is not None:
            failure_classes.append(failure_class)
        run_store.save_stage_attempt(run_id, record_key, "table_classification", rounds, artifact)

    while numbered < MAX_TABLE_CLASSIFICATION_ATTEMPTS:
        rounds += 1
        result = invoke(
            "extractor", model,
            _table_classification_prompt(
                paper_id, seed_table_anchor, other_tables, errors, chain_anchors, methods_context=methods_context,
            ),
        )
        artifact = result.as_artifact()

        failure = _provider_failure(result)
        if failure:
            # Nothing usable came back: not a numbered attempt, and the model gets no "feedback" about
            # a failure that was not its answer -- the next round repeats the same prompt.
            artifact["validation_errors"] = [{"field": None, "message": result.parse_error}]
            _save(failure)
            last_message = f"round {rounds}: provider failure ({failure}): {result.parse_error}"
            if provider.failed(failure):
                terminal_kind = "provider"
                break
            provider.cooldown()
            continue

        numbered += 1
        attempt = numbered

        if result.parsed_json is None:
            errors = [{"field": None, "message": result.parse_error}]
            artifact["validation_errors"] = errors
            _save("invalid_json")
            last_message = f"attempt {attempt}: {result.parse_error}"
            continue

        try:
            validated = TableClassification.model_validate(result.parsed_json)
        except ValidationError as exc:
            errors = [
                {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()
            ]
            artifact["validation_errors"] = errors
            _save('schema_invalid')
            last_message = f"attempt {attempt}: TableClassification shape validation failed: {errors}"
            continue

        try:
            blocks = _load_rendered_blocks(paper_id)
        except FileNotFoundError as exc:
            errors = [{"field": None, "message": f"cannot validate table anchors: {exc}"}]
            artifact["validation_errors"] = errors
            _save('validation_failure')
            last_message = f"attempt {attempt}: {errors[0]['message']}"
            continue

        invalid_anchor_errors = [
            {
                "field": "table_anchors",
                "message": f"cites anchor '{a}' which does not exist in content.md for paper '{paper_id}'.",
            }
            for a in validated.table_anchors if a.strip("[]") not in blocks
        ] + [
            {
                "field": "row_groups",
                "message": f"row_group '{r.row_group_id}' cites source_table_anchor "
                           f"'{r.source_table_anchor}' which does not exist in content.md for paper "
                           f"'{paper_id}'.",
            }
            for r in validated.row_groups if r.source_table_anchor.strip("[]") not in blocks
        ]
        if invalid_anchor_errors:
            errors = invalid_anchor_errors
            artifact["validation_errors"] = errors
            _save('validation_failure')
            last_message = f"attempt {attempt}: invalid anchors: {errors}"
            continue

        # A deterministically confirmed continuation chain is part of THIS
        # logical table: a classification that leaves a member out cannot be
        # trusted (its sanity check would compare against only part of the
        # raw table), so it is rejected with feedback and retried.
        missing_chain = [a for a in (chain_anchors or []) if a not in validated.table_anchors]
        if missing_chain:
            errors = [{
                "field": "table_anchors",
                "message": (
                    f"table_anchors {validated.table_anchors} omits {missing_chain}, which "
                    f"{seed_table_anchor!r} was deterministically confirmed to continue across (same table, "
                    f"page-split). table_anchors must include every block of the chain {chain_anchors}, and "
                    f"row_groups must cover the rows of all of them."
                ),
            }]
            artifact["validation_errors"] = errors
            _save('validation_failure')
            last_message = f"attempt {attempt}: classification omitted confirmed continuation block(s) {missing_chain}"
            continue

        # A pooled factor's evidence must be LITERAL source text actually
        # present in its cited block -- the same grounding rule every other
        # excerpt in this pipeline obeys (never accept an invented pooling
        # statement; it decides aggregated_mean vs treatment_mean).
        pooled_errors = []
        for pooled in validated.pooled_factors:
            block_text = blocks.get(pooled.evidence_anchor.strip("[]"))
            if block_text is None:
                pooled_errors.append({
                    "field": "pooled_factors",
                    "message": f"pooled factor '{pooled.name}' cites evidence_anchor '{pooled.evidence_anchor}' which does not exist in content.md.",
                })
            elif not _value_supported_by_text(pooled.evidence_excerpt, block_text):
                pooled_errors.append({
                    "field": "pooled_factors",
                    "message": (
                        f"pooled factor '{pooled.name}': evidence_excerpt {pooled.evidence_excerpt!r} is not found in "
                        f"the text of {pooled.evidence_anchor} -- it must be the literal source text stating the "
                        f"pooling, never a paraphrase."
                    ),
                })
        if pooled_errors:
            errors = pooled_errors
            artifact["validation_errors"] = errors
            _save('validation_failure')
            last_message = f"attempt {attempt}: ungrounded pooled_factors: {errors}"
            continue

        time_errors = _time_level_grounding_errors(validated, blocks)
        if time_errors:
            errors = time_errors
            artifact["validation_errors"] = errors
            _save("validation_failure")
            last_message = f"attempt {attempt}: ungrounded time_levels: {errors}"
            continue

        if validated.applicable and validated.row_groups:
            sanity_error = _table_classification_sanity_check(validated, paper_id)
            if sanity_error:
                errors = [{"field": "row_groups", "message": sanity_error}]
                artifact["validation_errors"] = errors
                _save('validation_failure')
                last_message = f"attempt {attempt}: {sanity_error}"
                continue

        # A variable named by ROW labels can carry its units, canonical name and method hint only in `variables`
        # (a value column has no such field), so a level left undeclared loses them for good. Every other check
        # above has passed here, so this is the answer's only defect: retried with feedback, then -- on the last
        # attempt -- accepted and flagged, because the reconstruction itself is sound and losing the whole table
        # would cost more than its variables' metadata (candidates without a hint still refuse safely).
        variable_flags: list[dict] = []
        undeclared = _undeclared_variable_levels(validated)
        if undeclared:
            if numbered < MAX_TABLE_CLASSIFICATION_ATTEMPTS:
                errors = [{
                    "field": "variables",
                    "message": (
                        f"the table's row factor(s) of dimension 'variable' have level(s) {undeclared} that no `variables` "
                        f"entry declares. Declare one `variables` entry per such level, its `label` exactly as the row "
                        f"shows it; `variable_name`, `units` and `method_hint` are optional -- give a `method_hint` only "
                        f"when the paper's Methods says how that variable was measured, otherwise omit it (never invent one)."
                    ),
                }]
                artifact["validation_errors"] = errors
                _save("validation_failure")
                last_message = f"attempt {attempt}: undeclared variable level(s) {undeclared}"
                continue
            variable_flags = [{
                "undeclared_levels": undeclared,
                "reason": f"still undeclared after {numbered} attempts; the classification was accepted without their "
                          f"units, canonical names and method hints",
            }]
            artifact["variable_declaration_flags"] = variable_flags

        # Pooling supplied by the model is always recorded as the model's (the
        # `origin` field is ours to set, never the model's), then completed
        # deterministically from the table's own caption/note when it gave none.
        validated = validated.model_copy(update={
            "pooled_factors": [pf.model_copy(update={"origin": "model", "pattern": None}) for pf in validated.pooled_factors],
        })
        validated, pooling_records = _apply_pooling_evidence(validated, paper_id, blocks)
        # Units hints the source does not contain are flagged deterministically (anything the model put in
        # `unit_hint_flags` is discarded) and withheld from candidates.
        validated = validated.model_copy(update={"unit_hint_flags": _unit_hint_flags(validated, blocks, paper_id)})
        # Method hints the paper's prose does not support are withheld the same way (Felipe Table 1 / Item 10).
        validated = _withhold_ungrounded_method_hints(validated, paper_id)

        artifact["validation_errors"] = []
        _save(None)
        run_store.save_final(run_id, record_key, {
            "status": "success", "classification": validated.model_dump(), "pooling_evidence": pooling_records,
            **({"variable_declaration_flags": variable_flags} if variable_flags else {}),
        })
        return validated, None

    run_store.save_final(run_id, record_key, {
        "status": "error", "message": last_message,
        # Item 9: why the table was given up on, and whether that is the provider's failure or the
        # extraction's -- disclosed in the run manifest (`table_pass_failures`).
        "failure_class": failure_classes[-1] if failure_classes else "validation_failure",
        "failure_kind": terminal_kind, "failure_classes": failure_classes,
        "numbered_attempts": numbered, "provider_failure_rounds": provider.rounds,
    })
    return None, last_message


def _cached_table_failures(run_id: str) -> dict[str, dict]:
    """{record key: terminal failure record} for every Step B table this run gave up on."""
    out: dict[str, dict] = {}
    for record_key in run_store.list_records(run_id):
        if record_key.startswith("table_classification__"):
            failure = _load_cached_table_failure(run_id, record_key)
            if failure is not None:
                out[record_key] = failure
    return out


def _load_cached_table_failure(run_id: str, record_key: str) -> Optional[dict]:
    """The terminal failure of an earlier Step B call for this table in this run, or None. Only
    failures written with a `failure_class` (Item 9 onward) are treated as final; an older bare
    error record is retried as before."""
    path = run_store.record_dir(run_id, record_key) / "final.json"
    if not path.is_file():
        return None
    try:
        data = run_store.load_json(path)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("status") == "error" and data.get("failure_class") and data.get("message"):
        return data
    return None


def _undeclared_variable_levels(classification: TableClassification) -> list[str]:
    """The levels of a `variable`-dimension ROWS factor that no `variables` entry declares (compared as
    `_variable_key`, so case and punctuation never matter), in row order. Only a label is needed to declare one;
    everything else on a TableVariable stays optional. A table whose variables are named by column headers
    (`variables` legitimately empty, each column carrying its own `variable_name_hint`) has no such factor, so it is
    never affected, and neither is a table that feeds no candidates."""
    if classification.table_role != "treatment_response" or not classification.applicable:
        return []
    factors = [f.name for f in classification.factors if f.dimension == "variable" and f.encoding == "rows"]
    if not factors:
        return []
    declared = {_variable_key(v.label) for v in classification.variables}
    missing: list[str] = []
    seen: set[str] = set()
    for row in classification.row_groups:
        for name in factors:
            level = (row.factor_values or {}).get(name) or ""
            key = _variable_key(level)
            if key and key not in declared and key not in seen:
                seen.add(key)
                missing.append(level.strip())
    return missing


def _time_level_grounding_errors(classification: TableClassification, blocks: dict[str, str]) -> list[dict]:
    """A time level's dates are sealed evidence, so they get the same grounding rule as every excerpt
    in this pipeline: each cited anchor must exist, and the literal `date_text` / `year_text` (and the
    site, when given) must be found in at least one cited block's text. An invented or paraphrased date
    is rejected with feedback -- it decides what a later Observation's temporal_info says."""
    errors: list[dict] = []
    for tl in classification.time_levels:
        label = f"time_level {tl.factor}={tl.level!r}"
        cited = {}
        for anchor in tl.anchors:
            text = blocks.get(anchor.strip("[]"))
            if text is None:
                errors.append({"field": "time_levels", "message": f"{label} cites anchor '{anchor}' which does not exist in content.md."})
            else:
                cited[anchor] = text
        if not cited:
            continue
        for field, value in (("date_text", tl.date_text), ("year_text", tl.year_text)):
            if value and not any(_value_supported_by_text(value, text) for text in cited.values()):
                errors.append({
                    "field": "time_levels",
                    "message": f"{label}: {field} {value!r} is not found in the text of {sorted(cited)} -- it must be the "
                               f"literal source text, never a paraphrase or a computed date.",
                })
        if tl.site and not any(_normalize_for_matching(tl.site) in _normalize_for_matching(text) for text in cited.values()):
            errors.append({"field": "time_levels", "message": f"{label}: site {tl.site!r} is not named in {sorted(cited)}."})
    return errors


def _time_levels_for_cell(classification: TableClassification, row: Any, column: Any) -> list[TimeLevel]:
    """The dated time levels that apply to ONE cell: for each `time`-dimension level the cell carries
    (row, column or table context), the single matching `time_levels` entry -- matched by declared
    factor and level, and by site when the entry is dated per site. Anything ambiguous (several entries,
    or a per-site entry when the cell names no site) attaches nothing: refuse to guess. A table with no
    time factor, or no `time_levels`, yields nothing."""
    if not classification.time_levels:
        return []
    levels = _cell_dimension_levels(classification, row, column)
    cell_sites = [_normalize_for_matching(level) for _, (dimension, level) in levels.items() if dimension == "site"]
    matched: list[TimeLevel] = []
    for name, (dimension, level) in levels.items():
        if dimension != "time":
            continue
        entries = [
            tl for tl in classification.time_levels
            if _variable_key(tl.factor) == _variable_key(name) and _variable_key(tl.level) == _variable_key(level)
        ]
        applicable = []
        for tl in entries:
            if tl.site is None:
                applicable.append(tl)
            elif any(
                (len(cs) >= 3 and cs in _normalize_for_matching(tl.site)) or (len(_normalize_for_matching(tl.site)) >= 3 and _normalize_for_matching(tl.site) in cs)
                for cs in cell_sites
            ):
                applicable.append(tl)
        if len(applicable) == 1:
            matched.append(applicable[0])
    return matched


def _temporal_context(time_levels: list[TimeLevel]) -> dict[str, Any]:
    """Sealed candidate context for a cell whose time level(s) the paper dates."""
    if not time_levels:
        return {}
    return {"temporal_context": [
        {"factor": tl.factor, "level": tl.level, "site": tl.site, "date_text": tl.date_text, "year_text": tl.year_text, "anchors": list(tl.anchors)}
        for tl in time_levels
    ]}


def _table_level_texts(classification: TableClassification) -> set[str]:
    """Normalized level texts this table reports in its rows, columns or context."""
    texts: set[str] = set()
    for row in classification.row_groups:
        texts |= {pooling_evidence.normalize_name(v) for v in (row.factor_values or {}).values()}
    for column in classification.value_columns:
        texts |= {pooling_evidence.normalize_name(v) for v in (column.factor_levels or {}).values()}
        for hint in (column.treatment_level_hint, column.site_hint):
            if hint:
                texts.add(pooling_evidence.normalize_name(hint))
    texts |= {pooling_evidence.normalize_name(v) for v in (classification.context_levels or {}).values()}
    return texts


def _pooling_conflict(classification: TableClassification, name: str) -> Optional[str]:
    """Why a detected pooled factor cannot be one: pooling means the factor is
    ABSENT from the table, so a name that is a declared factor or a reported
    level of this very table contradicts it."""
    key = pooling_evidence.normalize_name(name)
    for factor in classification.factors:
        if pooling_evidence.normalize_name(factor.name) == key:
            return f"'{name}' is the declared factor {factor.name!r} of this table, so its levels are not pooled away"
    if key and key in _table_level_texts(classification):
        return f"'{name}' is a level this table itself reports, so it is not pooled away"
    return None


def _apply_pooling_evidence(
    classification: TableClassification, paper_id: str, blocks: dict[str, str],
) -> tuple[TableClassification, list[dict]]:
    """Deterministic pooling evidence (pipeline/pooling_evidence.py) for one
    validated classification. Returns (classification, records) where every
    detected statement is a record with `status` accepted/rejected, its
    anchor, literal excerpt, factor, pattern and -- when rejected -- why.

    Only an EMPTY `pooled_factors` of a `treatment_response` table is ever
    filled in; a model-supplied result is preserved untouched and any
    disagreement is recorded, and for any other table role the evidence is
    informational. Every accepted excerpt is re-validated with the pipeline's
    grounding check and the merged classification re-runs the schema
    validation, so nothing ungrounded or self-contradictory gets through."""
    found = pooling_evidence.detect_pooling_evidence(paper_id, classification.table_anchors, _papers_root())
    if not found:
        return classification, []

    records: list[dict] = []
    usable: list[dict] = []
    for raw in found:
        record = {**raw, "applied": False}
        if record["status"] == "accepted":
            reason = _pooling_conflict(classification, record["factor"])
            if reason is None and not _value_supported_by_text(record["excerpt"], blocks.get(record["anchor"], "")):
                reason = "the excerpt is not found in the cited block's text (ungrounded)"
            if reason is not None:
                record.update(status="rejected", rejection_reason=reason)
            else:
                usable.append(record)
        records.append(record)
    if not usable:
        return classification, records

    if classification.table_role != "treatment_response":
        for record in usable:
            record["note"] = f"informational only: the table role is {classification.table_role!r}, which has no pooled_factors use"
        return classification, records

    if classification.pooled_factors:
        model_keys = {pooling_evidence.normalize_name(pf.name) for pf in classification.pooled_factors}
        for record in usable:
            agrees = pooling_evidence.normalize_name(record["factor"]) in model_keys
            record["agreement"] = "agrees" if agrees else "disagrees"
            record["model_pooled_factors"] = [pf.name for pf in classification.pooled_factors]
            record["note"] = "the model's own pooled_factors were preserved; the deterministic result was not applied"
        return classification, records

    detected, seen = [], set()
    for record in usable:
        key = pooling_evidence.normalize_name(record["factor"])
        if key in seen:
            continue
        seen.add(key)
        detected.append({
            "name": record["factor"], "dimension": record["dimension"], "evidence_anchor": record["anchor"],
            "evidence_excerpt": record["excerpt"], "origin": "deterministic", "pattern": record["pattern"],
        })
    try:
        merged = TableClassification.model_validate({**classification.model_dump(), "pooled_factors": detected})
    except ValidationError as exc:
        why = "; ".join(err["msg"] for err in exc.errors())
        for record in usable:
            record.update(status="rejected", rejection_reason=f"the merged classification failed schema validation: {why}")
        return classification, records
    for record in usable:
        record["applied"] = True
    return merged, records


def _cached_pooling_evidence(run_id: str) -> dict[str, list[dict]]:
    """{record key: pooling-evidence records} saved next to each cached Step B classification."""
    out: dict[str, list[dict]] = {}
    for record_key in run_store.list_records(run_id):
        if not record_key.startswith("table_classification__"):
            continue
        path = run_store.record_dir(run_id, record_key) / "final.json"
        try:
            data = run_store.load_json(path) if path.is_file() else None
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("status") == "success" and data.get("pooling_evidence"):
            out[record_key] = list(data["pooling_evidence"])
    return out


def _cached_variable_declaration_flags(run_id: str) -> dict[str, list[dict]]:
    """{record key: variable-declaration flags} saved next to each Step B classification that was accepted with
    undeclared row-encoded variable levels."""
    out: dict[str, list[dict]] = {}
    for record_key in run_store.list_records(run_id):
        if not record_key.startswith("table_classification__"):
            continue
        path = run_store.record_dir(run_id, record_key) / "final.json"
        try:
            data = run_store.load_json(path) if path.is_file() else None
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("status") == "success" and data.get("variable_declaration_flags"):
            out[record_key] = list(data["variable_declaration_flags"])
    return out


def _normalize_for_matching(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# Fix 1 (table-enumeration Method-linking design review): words that must
# never, on their own, justify a token-containment match -- both generic
# English stopwords and a handful of domain-generic words that show up in
# almost every method_hint sentence regardless of which real method is
# meant ("hand measurement of X as described in the Methods section" is
# the SAME boilerplate for three completely different real measurements
# in real Daren-1997-Canopy hints). Deliberately conservative and short:
# this is a denylist against false positives, not an attempt at general
# stopword removal.
_GENERIC_MATCH_TOKENS = frozenset({
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for", "by", "with",
    "as", "is", "was", "were", "using", "used", "based", "method", "methods",
    "measurement", "measurements", "measured", "measure", "analysis", "described",
    "section", "hand", "value", "values", "see", "materials",
})


def _significant_tokens(text: str) -> set[str]:
    """Tokens long enough (>=4 chars, after normalization) and specific
    enough (not in _GENERIC_MATCH_TOKENS) to mean something on their own --
    the set a token-containment match is allowed to be judged against."""
    return {t for t in text.split() if len(t) >= 4 and t not in _GENERIC_MATCH_TOKENS}


def _match_row_group_to_pool(factor_values: dict[str, str], pool: list[dict]) -> Optional[str]:
    """Deterministically match a table row's own known values (factor
    values plus any site_hint/method_hint merged in by the caller) against
    a link_pool entry's `name` or `slug` (the SAME pools
    `_multi_record_link_pools` already builds for the free-form pass).

    SCORES each pool entry by how many of the row's normalized values it
    accounts for, then returns the slug of the entry with the STRICTLY
    HIGHEST score, or None if the best score is 0 or tied. Three tiers,
    highest always wins outright over any lower tier:

      1. EXACT match (2 points) -- value equals the pool entry's name/slug
         verbatim after normalization.
      2. BIDIRECTIONAL SUBSTRING (1 point) -- either the value is found
         inside the pool text, or the pool text is found inside the value,
         whichever side is being searched must be >=3 characters. The
         short-in-long direction is the original Site case (a table's
         short site_hint like 'Ames' inside a Site's long institution
         name). The long-in-short direction is the same pattern for
         Method: `method_hint` is deliberately a full descriptive clause
         ("General Linear Model (GLM) analysis (SAS) - LSD (0.05) values"),
         while a real Method's own `name` is a short label ("General
         Linear Model (GLM)") that appears verbatim, contiguous, inside
         the hint -- a value longer than the text it should match is not
         evidence of anything BUT a short, literal pool name appearing
         inside it.
      3. TOKEN CONTAINMENT (1 point, same tier as substring, never higher)
         -- only when a pool entry's SIGNIFICANT tokens (see
         `_significant_tokens`: >=4 chars, non-generic, per
         `_GENERIC_MATCH_TOKENS`) number at least TWO and are ALL present
         as whole words in the value. Deliberately strict: a match must
         never rest on a single shared word, generic or not (two
         completely unrelated hints sharing one incidental word like
         'standard' or 'area' must never score), and a pool name whose
         significant vocabulary isn't fully echoed in the hint (e.g. a
         hint about leaf blade WIDTH alone against a Method named 'leaf
         blade length and width' -- 'length' is missing) correctly stays
         unresolved rather than guessing the two are close enough.

    This combined scoring (not a flat "any value matches" check) is what
    correctly resolves a real, confirmed ambiguity: Daren-1997-Canopy's
    Treatment pool has BOTH 'trailblazer_ames' and 'trailblazer_mead'
    sharing the identical name 'Trailblazer' (site lives only in the
    slug) -- a row with {'Population': 'Trailblazer', 'Site': 'Ames'}
    scores 'trailblazer_ames' at 3 (exact match on 'trailblazer' + a
    substring match on 'ames') and 'trailblazer_mead' at 2 (only the exact
    population match), so the higher-scoring entry wins outright, without
    requiring every value to match (a row
    that ALSO carries an unrelated value, e.g. Maturity while matching
    against the Site pool, simply never scores from that irrelevant value
    on any entry -- it does not dilute a real match on Site elsewhere).
    A genuine tie is preserved as unresolved rather than picked arbitrarily
    -- e.g. the SAME row's Population+Maturity+Site values against a
    Treatment pool that separately has 'trailblazer_ames' AND
    'vegetative_ames' (population-level and maturity-level Treatments,
    both real, both score 3) correctly returns None: a single treatment_id
    genuinely cannot represent both dimensions at once, and this function
    must not silently pick one and discard the other.

    This combined scoring (not a flat "any value matches" check) is what
    correctly resolves a real, confirmed ambiguity: Daren-1997-Canopy's
    Treatment pool has BOTH 'trailblazer_ames' and 'trailblazer_mead'
    sharing the identical name 'Trailblazer' (site lives only in the
    slug) -- a row with {'Population': 'Trailblazer', 'Site': 'Ames'}
    scores 'trailblazer_ames' at 3 (exact match on 'trailblazer' + a
    substring match on 'ames') and 'trailblazer_mead' at 2 (only the exact
    population match), so the higher-scoring entry wins outright, without
    requiring every value to match (a row
    that ALSO carries an unrelated value, e.g. Maturity while matching
    against the Site pool, simply never scores from that irrelevant value
    on any entry -- it does not dilute a real match on Site elsewhere).
    A genuine tie is preserved as unresolved rather than picked arbitrarily
    -- e.g. the SAME row's Population+Maturity+Site values against a
    Treatment pool that separately has 'trailblazer_ames' AND
    'vegetative_ames' (population-level and maturity-level Treatments,
    both real, both score 3) correctly returns None: a single treatment_id
    genuinely cannot represent both dimensions at once, and this function
    must not silently pick one and discard the other.

    A wrong link here is far more dangerous than an unlinked candidate: an
    unmatched candidate still goes through the existing
    _apply_candidate_links/refuse-to-guess-gate machinery downstream
    unchanged (a required field falls back to the allowed-set safety net,
    an optional one is simply omitted), so failing to match is always
    safe -- a WRONG match would silently attach a value to the wrong
    record, exactly the real failure class the refuse-to-guess gate exists
    to prevent."""
    scored = _pool_scores(factor_values, pool)
    best_score = max((s for _, s in scored), default=0)
    if best_score == 0:
        return None
    winners = [slug for slug, s in scored if s == best_score]
    return winners[0] if len(winners) == 1 else None


def _pool_scores(factor_values: dict[str, str], pool: list[dict]) -> list[tuple[str, int]]:
    """Each pool entry's slug with the score `_match_row_group_to_pool`'s tiers give it
    (exact 2, substring / name-token containment 1). Kept apart so a caller can tell
    "nothing matched at all" from "several entries tied" -- both return None from the
    matcher, but only the first may fall through to the description tier."""
    if not factor_values:
        return []
    normalized_values = {_normalize_for_matching(v) for v in factor_values.values() if v} - {""}
    if not normalized_values:
        return []

    pool_texts = [
        (item["slug"], {_normalize_for_matching(item.get("name") or ""),
                         _normalize_for_matching(item["slug"].replace("_", " "))} - {""})
        for item in pool
    ]

    def _score(texts: set[str]) -> int:
        # An exact match (2 points) outweighs a substring or token-
        # containment match (1 point each, never stacked for the same
        # value) -- e.g. a row's site_hint 'Ames' exactly matching a real
        # Site named 'Ames' must win outright over it merely being a
        # substring of an unrelated 'Ames Annex' entry.
        total = 0
        for value in normalized_values:
            if value in texts:
                total += 2
                continue
            if any(
                (len(value) >= 3 and value in text) or (len(text) >= 3 and text in value)
                for text in texts
            ):
                total += 1
                continue
            for text in texts:
                sig = _significant_tokens(text)
                if len(sig) >= 2 and sig <= set(value.split()):
                    total += 1
                    break
        return total

    return [(slug, _score(texts)) for slug, texts in pool_texts]


# Item 10 (Method matching, tier 3): a Method's own `name` is a short label, but its
# `description` is the Methods-section sentence the hint was paraphrased from. A hint
# with no exact/substring/name-token hit on ANY pool entry may still be resolved by
# that description -- only when it is distinctive and unambiguous.
_MIN_DESCRIPTION_TOKENS = 3


def _description_tokens(item: dict) -> set[str]:
    return _significant_tokens(_normalize_for_matching(item.get("name") or "")) | _significant_tokens(
        _normalize_for_matching(item.get("description") or ""))


def _description_candidates(hint: str, pool: list[dict]) -> list[str]:
    """Slugs of pool entries whose name+description contain EVERY significant token of
    `hint`, provided the hint has at least `_MIN_DESCRIPTION_TOKENS` of them (the
    >=4-character minimum and the generic-token denylist still apply, so one generic
    word can never match)."""
    hint_tokens = _significant_tokens(_normalize_for_matching(hint))
    if len(hint_tokens) < _MIN_DESCRIPTION_TOKENS:
        return []
    return [item["slug"] for item in pool if hint_tokens <= _description_tokens(item)]


def _method_hint_seeds(
    *, run_id: str, paper_id: str, model: str, invoke: Callable[..., AgentInvocation],
    freeform_candidates: list[EnumerationCandidate],
) -> tuple[list[EnumerationCandidate], list[dict]]:
    """Seed Method candidates from the method hints of this paper's data tables (Step B,
    cached per run) -- decision Q6, deliberately conservative. A hint seeds a candidate
    ONLY when:
      - it has enough distinctive tokens (`_MIN_DESCRIPTION_TOKENS`) -- an arbitrary
        method-sounding phrase never qualifies;
      - it is GROUNDED: some prose block (Text/ListItem, never a table) contains every one
        of its significant tokens, and those blocks become the candidate's anchors, so a
        method the paper never describes is never fabricated (Felipe's variables with no
        method paragraph stay unresolved);
      - the free-form pass has not already produced it: no free-form candidate scores on it
        under the same matcher tiers (`_pool_scores`) or contains it by description, and no
        earlier seed does -- a covered or ambiguous hint seeds nothing.
    Returns (seeds, one decision per distinct hint)."""
    classifications = run_table_classification_pass(run_id=run_id, paper_id=paper_id, model=model, invoke=invoke)
    hints: dict[str, str] = {}
    for classification in classifications.values():
        if classification.table_role != "treatment_response":
            continue
        for hint in [v.method_hint for v in classification.variables] + [c.method_hint for c in classification.value_columns]:
            if hint and hint.strip():
                hints.setdefault(_normalize_for_matching(hint), hint.strip())
    if not hints:
        return [], []
    prose = _prose_token_sets(paper_id)
    if prose is None:
        return [], []
    pool = [{"slug": c.candidate_id, "name": c.description, "description": c.description} for c in freeform_candidates]

    seeds: list[EnumerationCandidate] = []
    decisions: list[dict] = []
    for hint in hints.values():
        tokens = _significant_tokens(_normalize_for_matching(hint))
        record = {"hint": hint}
        if len(tokens) < _MIN_DESCRIPTION_TOKENS:
            decisions.append({**record, "decision": "skipped", "reason": "too few distinctive tokens to be a method description"})
            continue
        covered = [slug for slug, score in _pool_scores({"Method": hint}, pool) if score] or _description_candidates(hint, pool)
        if covered:
            decisions.append({**record, "decision": "skipped", "reason": f"already covered by Method candidate(s) {covered}"})
            continue
        anchors = [a for a in sorted(prose, key=content_reader._anchor_sort_key) if tokens <= prose[a]][:3]
        if not anchors:
            decisions.append({**record, "decision": "skipped", "reason": "no prose block states this method (not grounded), so no Method is created"})
            continue
        candidate = EnumerationCandidate(
            candidate_id=_sanitize_candidate_id("method_hint_" + "_".join(_normalize_for_matching(hint).split()[:5])),
            description=f"Measurement method described as: {hint}", anchors=anchors,
        )
        seeds.append(candidate)
        pool.append({"slug": candidate.candidate_id, "name": candidate.description, "description": candidate.description})
        decisions.append({**record, "decision": "seeded", "candidate_id": candidate.candidate_id, "anchors": anchors})
    return seeds, decisions


def _match_method_hint(match_values: dict[str, str], hint: Optional[str], pool: list[dict]) -> Optional[str]:
    """Resolve a table row's Method link. Strict tier order: the existing exact /
    substring / name-token scoring first; the description tier only when that scoring
    hit NOTHING on any entry (a tie is a tie, never a reason to look further), and then
    only for a unique winner -- several candidates, or none, stay unresolved."""
    match = _match_row_group_to_pool(match_values, pool)
    if match is not None or not hint:
        return match
    if any(score for _, score in _pool_scores(match_values, pool)):
        return None  # something matched but tied: ambiguous, refuse to guess
    winners = _description_candidates(hint, pool)
    return winners[0] if len(winners) == 1 else None


# Current-IR representability limits, surfaced explicitly (never as extraction
# failures) in the run manifest and in `blocked` reasons:
#   L1: Observation.treatment_id is REQUIRED, so an observational design with no
#       treatment-dimension factor (population -> Crop, maturity -> time,
#       site -> Site) cannot yield Observations without inventing a Treatment.
#   L2: Observation.site_id is REQUIRED and single-valued, so values pooled over
#       sites cannot be represented.
LIMITATION_L1 = (
    "L1 (current-IR limitation, not an extraction failure): Observation.treatment_id is required, but this "
    "paper's data tables declare no treatment-dimension factor (their dimensions are crop/time/site only), so "
    "there is no Treatment to reference; a Treatment is deliberately not invented to satisfy the schema"
)
LIMITATION_L2 = (
    "L2 (current-IR limitation, not an extraction failure): the values are pooled over the site dimension but "
    "Observation.site_id is required and single-valued"
)


#   L3 (recorded, not yet detected per run): the IR has no home for the structured
#       experimental design. Protocol Section 16.2 asks for design metadata (design_type,
#       experimental_unit, replicate_unit, control_definition, "factor names and levels");
#       `Study` holds only identity ("design_type / experimental_unit / replicate_unit are
#       future work"). A factorial design such as Felipe's 2 (winter fallow, mustard cover
#       crop) x 3 (1-cv, 3-cv, 5-cv mixtures) can therefore only be described in free-text
#       `Treatment.definition`; its unreported combinations are NEVER manufactured as
#       Treatments or Observations to compensate (Q2). Class D (schema/IR representability).
LIMITATION_L3 = (
    "L3 (current-IR limitation, not an extraction failure): the structured experimental design (factor names and "
    "levels, design type, experimental/replicate unit; protocol Section 16.2) has no IR field -- Study holds only "
    "identity -- so design combinations the source does not report are not represented as Treatments or Observations"
)


def _has_treatment_dimension(classification: TableClassification) -> bool:
    """Does this table carry any treatment-dimension factor (declared, or --
    for a legacy classification with no declared factors -- implied by the
    legacy hints/row factors, which legacy code always treated as treatment
    identity)? A column's legacy `treatment_level_hint` is a treatment level
    even on a table with declared factors (`_cell_dimension_levels` reads it
    as one, and Treatment generation already uses it), so it counts here too
    -- otherwise a Fallow/Mustard column table was wrongly reported as L1."""
    if classification.factors:
        return (
            any(f.dimension == "treatment" for f in classification.factors)
            or any(vc.treatment_level_hint for vc in classification.value_columns)
        )
    return True  # legacy classification: behavior unchanged


def _pooled_representability(classification: TableClassification) -> tuple[bool, Optional[str]]:
    """Can this table's values be expressed as Observations in the current IR
    (protocol Section 7.4: aggregated_mean + aggregated_over_factors)?

    A table pooled over a NON-site factor, while still retaining at least one
    treatment-dimension factor to reference, is representable: the Observation
    is `aggregated_mean` with `aggregated_over_factors` naming what was pooled.
    Pooled over the site (L2), or with no retained treatment (L1), it is not."""
    if not classification.pooled_factors:
        return True, None
    if any(pf.dimension == "site" for pf in classification.pooled_factors):
        return False, LIMITATION_L2
    if not _has_treatment_dimension(classification):
        return False, LIMITATION_L1
    return True, None


def _pooled_context(classification: TableClassification) -> dict[str, Any]:
    """Sealed candidate context for a representable pooled table: which
    factors the values are pooled over, with the literal source text that says
    so. The candidate's Extraction cites that statement; Conversion is told to
    use aggregated_mean rather than treatment_mean."""
    if not classification.pooled_factors:
        return {}
    return {
        "reported_effect_scope": "aggregated_mean",
        "aggregated_over_factors": [pf.name for pf in classification.pooled_factors],
        "pooling_evidence": [
            {"factor": pf.name, "anchor": pf.evidence_anchor, "excerpt": pf.evidence_excerpt}
            for pf in classification.pooled_factors
        ],
    }


# --- Item 12: units and canonical variable name ------------------------------------
# Marker's rendering of units is sometimes garbled (real Daren table b:0119: "kg DI | M m -2" for "kg DM m-2"),
# and a table reconstruction may "correct" it from world knowledge. The datapackage says `reported_units` is
# "units as reported by the source", so a units hint travels only when the source text supports it, is used
# only where it agrees with that text, and a hint the source does not support is FLAGGED -- never silently
# adopted and never silently corrected to either reading.
_UNIT_CHAR_MAP = str.maketrans({
    "⁻": "-", "−": "-", "–": "-", "—": "-", "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
    "µ": "u", "μ": "u", "·": "", "×": "",
})
_PLACEHOLDER_UNITS = frozenset({"unknown", "n/a", "na", "none", "not reported", "not stated", "not given", "unspecified", "tbd", "?", "-"})


def _units_key(text: str) -> str:
    """Comparison key for a units string: lowercase, superscripts and unicode minus folded, whitespace and
    brackets dropped ('g m -2 ' == 'g m⁻²' == 'g m-2')."""
    return re.sub(r"[\s.^{}()\[\]|*,;:]+", "", (text or "").translate(_UNIT_CHAR_MAP).lower())


def _units_supported(hint: str, texts: list[str]) -> bool:
    """Is `hint` written in any of `texts`? Compared ignoring spacing and superscript spelling; a very short
    hint ('g', 'm', '%') must additionally stand as a whole token so a stray letter never counts as support."""
    key = _units_key(hint)
    if not key:
        return True
    for text in texts:
        if key not in _units_key(text):
            continue
        if len(key) >= 3 or re.search(rf"(?<![a-z0-9]){re.escape(hint.strip().lower())}(?![a-z0-9])", text.translate(_UNIT_CHAR_MAP).lower()):
            return True
    return False


def _unit_hint_flags(classification: TableClassification, blocks: dict[str, str], paper_id: str) -> list[UnitHintFlag]:
    """The units hints of this table that its own blocks, caption and notes do not contain."""
    eligible = [blocks[a] for a in classification.table_anchors if a in blocks]
    eligible += [
        blocks[a] for _, a in pooling_evidence.pooling_windows(paper_id, classification.table_anchors, _papers_root()) if a in blocks
    ]
    reason = "not found in the table, its caption or its notes"
    flags = [UnitHintFlag(scope="variable", key=v.label, units_hint=v.units, reason=reason)
             for v in classification.variables if v.units and not _units_supported(v.units, eligible)]
    flags += [UnitHintFlag(scope="column", key=c.value_column_id, units_hint=c.units_hint, reason=reason)
              for c in classification.value_columns if c.units_hint and not _units_supported(c.units_hint, eligible)]
    return flags


# --- Method hints: supplied evidence and grounding (Felipe Table 1) -----------------------------------------------
_METHODS_HEADER_RE = re.compile(r"method", re.IGNORECASE)
_METHODS_END_HEADER_RE = re.compile(r"result|discussion|conclusion|acknowledg|reference|literature cited", re.IGNORECASE)
_METHODS_CONTEXT_MAX_CHARS = 9000
_METHODS_CONTEXT_BLOCK_CHARS = 1500


def _methods_context(paper_id: str) -> str:
    """The paper's own Methods prose, verbatim and anchor-labelled, for the Step B prompt.

    The Methods span is found POSITIONALLY, in document order over the rendered blocks: it starts at the first
    SectionHeader whose text contains "method" and ends at the next SectionHeader that opens Results / Discussion /
    Conclusions / References. Deliberately NOT read from `section_path`: Marker's heading hierarchy is not reliable for
    this (in Felipe-2010-Cultivar the subsections of "Materials and methods" hang under a sibling heading, so no block's
    section_path mentions methods at all). Every Text/ListItem block inside the span is included, each cut to
    `_METHODS_CONTEXT_BLOCK_CHARS`, the whole to `_METHODS_CONTEXT_MAX_CHARS`; nothing is summarised or rewritten. ""
    when the paper has no such header (or no provenance), in which case the prompt is unchanged."""
    provenance = content_reader._load_provenance(paper_id, _papers_root())
    if not provenance:
        return ""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return ""
    lines: list[str] = []
    used = 0
    in_methods = False
    for anchor in sorted(blocks, key=content_reader._anchor_sort_key):
        block_type = (provenance.get(anchor) or {}).get("block_type")
        text = " ".join(blocks[anchor].split())
        if block_type == "SectionHeader":
            heading = text.lstrip("# ").strip()
            if _METHODS_HEADER_RE.search(heading):
                in_methods = True
            elif in_methods and _METHODS_END_HEADER_RE.search(heading):
                break
            continue
        if not in_methods or block_type not in ("Text", "ListItem") or not text:
            continue
        text = text[:_METHODS_CONTEXT_BLOCK_CHARS]
        if used + len(text) > _METHODS_CONTEXT_MAX_CHARS:
            break
        lines.append(f"[{anchor}] {text}")
        used += len(text)
    return "\n".join(lines)


def _prose_token_sets(paper_id: str) -> Optional[dict[str, set[str]]]:
    """{anchor: significant tokens} of every prose (Text/ListItem, never a table) block of the paper, or None when the
    paper has no rendered content. The evidence a method hint is grounded against."""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return None
    provenance = content_reader._load_provenance(paper_id, _papers_root()) or {}
    return {
        a: _significant_tokens(_normalize_for_matching(t)) for a, t in blocks.items()
        if (provenance.get(a) or {}).get("block_type") in ("Text", "ListItem")
    }


def _prose_normalized_texts(paper_id: str) -> list[str]:
    """Normalized text (`_normalize_for_matching`) of every prose block, for contiguous-phrase checks."""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return []
    provenance = content_reader._load_provenance(paper_id, _papers_root()) or {}
    return [
        f" {_normalize_for_matching(t)} " for a, t in blocks.items()
        if (provenance.get(a) or {}).get("block_type") in ("Text", "ListItem")
    ]


def _method_hint_grounded(hint: str, prose: dict[str, set[str]], texts: list[str]) -> bool:
    """A hint is grounded when ONE prose block contains every one of its significant words (>=4 characters, not a
    generic method word -- the matcher's own `_significant_tokens`) AND either it has at least two of them or the whole
    hint occurs contiguously in a prose block. So a hint of one distinctive word ("oven") must be found as that exact
    word, and a hint made only of generic words, or resting on one incidental word ("measured using standard
    methods"), is never grounded."""
    tokens = _significant_tokens(_normalize_for_matching(hint))
    if not tokens or not any(tokens <= block_tokens for block_tokens in prose.values()):
        return False
    if len(tokens) >= 2:
        return True
    phrase = f" {_normalize_for_matching(hint)} "
    return any(phrase in text for text in texts)


def _withhold_ungrounded_method_hints(classification: TableClassification, paper_id: str) -> TableClassification:
    """Withhold every method hint (per variable and per legacy column) that no prose block of the paper supports,
    recording each as a `method_hint_flags` entry. A withheld hint can neither link a Method (the approved matcher is
    unchanged) nor seed one; the candidate's Method stays unresolved. A grounded hint is passed through untouched.
    Anything the model put in `method_hint_flags` is discarded."""
    prose = _prose_token_sets(paper_id)
    if prose is None:
        return classification.model_copy(update={"method_hint_flags": []})
    texts = _prose_normalized_texts(paper_id)
    reason = "no prose block of the paper contains this hint's significant words (or it has none)"
    flags: list[MethodHintFlag] = []
    variables = []
    for variable in classification.variables:
        if variable.method_hint and not _method_hint_grounded(variable.method_hint, prose, texts):
            flags.append(MethodHintFlag(scope="variable", key=variable.label, method_hint=variable.method_hint, reason=reason))
            variable = variable.model_copy(update={"method_hint": None})
        variables.append(variable)
    columns = []
    for column in classification.value_columns:
        if column.method_hint and not _method_hint_grounded(column.method_hint, prose, texts):
            flags.append(MethodHintFlag(scope="column", key=column.value_column_id, method_hint=column.method_hint, reason=reason))
            column = column.model_copy(update={"method_hint": None})
        columns.append(column)
    return classification.model_copy(update={"variables": variables, "value_columns": columns, "method_hint_flags": flags})


def _effective_units(classification: TableClassification, variable: Optional[TableVariable], column: Any) -> Optional[str]:
    """The units hint for a cell -- the variable's own, else the column's legacy hint -- unless the source did not support it."""
    if variable is not None and variable.units:
        hint, key = variable.units, ("variable", variable.label)
    elif column.units_hint:
        hint, key = column.units_hint, ("column", column.value_column_id)
    else:
        return None
    return None if key in {(f.scope, f.key) for f in classification.unit_hint_flags} else hint


def _match_variable_pool(label: str, variable_name: Optional[str], pool: list[dict]) -> Optional[dict]:
    """The ready Variable record a table variable IS, or None. The table's verbatim label and its canonical name are
    each matched with the standard tiers; they must agree (or one must be silent) -- a disagreement is refused, so
    several spellings of one variable share a Variable record without any two variables ever being merged on a guess."""
    slugs = {m for m in (_match_row_group_to_pool({"Variable": text}, pool) for text in (label, variable_name) if text) if m}
    if len(slugs) != 1:
        return None
    slug = slugs.pop()
    return next((item for item in pool if item["slug"] == slug), None)


def _effective_variable(classification: TableClassification, row: Any, column: Any) -> tuple[str, Optional[TableVariable]]:
    """(label for descriptions, declared TableVariable or None) of the variable one cell
    reports. Column -> variable first (`column.variable`), then row -> variable (the
    row's level of a `variable`-dimension rows factor, matched to a declared variable's
    label; an undeclared level still names the variable, but has no method), then the
    legacy column hint. A declared variable's canonical `variable_name` wins."""
    by_label = {_variable_key(v.label): v for v in classification.variables}
    if column.variable:
        variable = by_label.get(_variable_key(column.variable))
        if variable is not None:
            return variable.variable_name or variable.label, variable
    for factor in classification.factors:
        if factor.dimension == "variable" and factor.encoding == "rows":
            level = (row.factor_values or {}).get(factor.name)
            if level:
                variable = by_label.get(_variable_key(level))
                return (variable.variable_name or variable.label) if variable else level, variable
    return column.variable_name_hint or column.variable or "value", None


def _effective_method_hint(variable: Optional[TableVariable], column: Any) -> Optional[str]:
    """The measurement-method hint for a cell: the variable's own (the stable unit),
    else the column's legacy hint. Never invented: absent means unresolved."""
    return (variable.method_hint if variable is not None and variable.method_hint else None) or column.method_hint


def _method_match_values(classification: TableClassification, row: Any, value_column: Any, method_hint: Optional[str]) -> dict[str, str]:
    """The values ONE cell may offer the Method matcher: the variable it reports (a `variable`-dimension level) and the
    method the source names for it. The cell's treatment, site, time, crop and replicate levels say WHICH
    condition/place/date was measured, never HOW, so they are not Method signals -- offering them lets a Method win
    because its description happens to mention e.g. "fallow" (real Felipe Table 1: 10 of 22 cells linked to the wrong
    Method that way). A row factor literally named "Method" is the one other explicit Method signal."""
    values = {
        name: level
        for name, (dimension, level) in _cell_dimension_levels(classification, row, value_column).items()
        if dimension == "variable" or name == "Method"
    }
    if method_hint:
        values.setdefault("Method", method_hint)
    return values


def _table_classification_to_candidates(
    classification: TableClassification, link_pools: dict[str, list[dict]],
) -> list[EnumerationCandidate]:
    """Step C: pure, deterministic cross-product -- no model call, no
    interpretation. For every (row_group x value_column) pair with a real,
    non-blank cell value, emits exactly one EnumerationCandidate. This is
    the actual fix for the confirmed collapse described in this section's
    own module comment -- code can never lose count or silently summarize
    the way free-form LLM enumeration did on the same real table."""
    candidates: list[EnumerationCandidate] = []
    # Only a treatment_response table feeds cell-level candidates. An
    # aggregated_summary (a valid source of aggregated data that the current
    # IR cannot express as a normal treatment-combination row), a
    # weather_context table, and a non_enumerable table never do.
    if classification.table_role != "treatment_response":
        return candidates
    # (Fix 2, real Daren-1997-Canopy Table 7: a successfully-reconstructed
    # aggregated/main-effects summary is the wrong KIND of table for
    # cell-level candidates -- that is now the `aggregated_summary` role, gated
    # out by the role check above.)
    # A table pooled over a factor the IR cannot represent (site: L2) or with
    # nothing left to reference (no treatment: L1) is registered as an
    # aggregated source, never expanded into misleading cell-level candidates.
    representable, _why = _pooled_representability(classification)
    if not representable:
        return candidates
    pooled_context = _pooled_context(classification)

    value_columns_by_id = {c.value_column_id: c for c in classification.value_columns}

    for row in classification.row_groups:
        for value_column_id, cell_text in (row.cells or {}).items():
            if not cell_text or not cell_text.strip():
                continue
            value_column = value_columns_by_id.get(value_column_id)
            if value_column is None:
                continue  # schema validation already guarantees this can't happen; defensive only

            candidate_id = _sanitize_candidate_id(f"{value_column_id}_{row.row_group_id}")
            # Declared column-/table-level factor levels are part of the
            # candidate's description too (legacy classifications have none, so
            # their description is unchanged).
            described_levels = dict(row.factor_values or {})
            for _k, _v in (value_column.factor_levels or {}).items():
                described_levels.setdefault(_k, _v)
            for _k, _v in (classification.context_levels or {}).items():
                described_levels.setdefault(_k, _v)
            factor_desc = ", ".join(f"{k}={v}" for k, v in described_levels.items())
            variable_label, variable = _effective_variable(classification, row, value_column)
            method_hint = _effective_method_hint(variable, value_column)
            units_hint = _effective_units(classification, variable, value_column)
            variable_name_hint = variable_label
            description = f"{variable_label} for {factor_desc}" if factor_desc else variable_label
            if value_column.site_hint:
                description += f" at {value_column.site_hint}"
            description += f" (reported value: {cell_text.strip()})"

            # Site and method information are often column-encoded, not
            # row-encoded (real Daren-1997-Canopy case: Table 2's "Ames"/
            # "Mead" sub-columns), so they live on value_column.site_hint/
            # method_hint, not row.factor_values -- merge them in under
            # distinct keys so linking works the same deterministic way as
            # any other factor, rather than only ever landing in the
            # description text.
            match_values = dict(row.factor_values or {})
            if value_column.site_hint:
                match_values.setdefault("Site", value_column.site_hint)
            if method_hint:
                match_values.setdefault("Method", method_hint)
            if value_column.treatment_level_hint:
                # Fix 5 (column-as-treatment): same pattern as site_hint/
                # method_hint above -- a column-encoded table's treatment
                # identity lives on the column, not row.factor_values, so
                # Observation.treatment_id linking needs it merged in here
                # too, not just Treatment's own candidate generation.
                match_values.setdefault("Treatment", value_column.treatment_level_hint)
            # Declared factor structure: column- and table-level levels join
            # the linking values under their own factor names (row levels are
            # already in row.factor_values).
            for _k, _v in (value_column.factor_levels or {}).items():
                match_values.setdefault(_k, _v)
            for _k, _v in (classification.context_levels or {}).items():
                match_values.setdefault(_k, _v)

            linked_candidates: dict[str, str] = {}
            for field, pool in (link_pools or {}).items():
                if field == "method_id":
                    # Method-relevant values only (never the Treatment/Site/time levels); the matcher is unchanged.
                    match = _match_method_hint(_method_match_values(classification, row, value_column, method_hint), method_hint, pool)
                elif field == "variable_id":
                    # Item 12: the variable link rests on the VARIABLE's own label/name only (never on the
                    # row's other factor values); every spelling that resolves to one Variable record shares
                    # its name.
                    record = _match_variable_pool(variable_label, variable.variable_name if variable else None, pool)
                    match = record["slug"] if record else None
                    if record and record.get("name"):
                        variable_name_hint = record["name"]
                else:
                    match = _match_row_group_to_pool(match_values, pool)
                if match:
                    linked_candidates[field] = match

            candidates.append(EnumerationCandidate(
                candidate_id=candidate_id,
                description=description,
                anchors=[row.source_table_anchor],
                linked_candidates=linked_candidates,
                known_value=cell_text.strip(),
                variable_name_hint=variable_name_hint, units_hint=units_hint,
                context={**pooled_context, **_temporal_context(_time_levels_for_cell(classification, row, value_column))},
            ))

    return candidates


def run_table_classification_pass(
    *, run_id: str, paper_id: str, model: str, invoke: Callable[..., AgentInvocation] = invoke_agent,
) -> dict[str, TableClassification]:
    """Steps A + B, entity-agnostic and computed ONCE per run: discovers
    every Table block (Step A, pure code) and classifies/reconstructs each
    not-yet-consumed one (Step B). Cached to disk per table (see
    run_table_classification's own cache), so calling this a SECOND time
    within the same run_id -- e.g. Treatment's turn, then Observation's
    turn later -- costs no new real LLM calls for tables already done.

    Returns {seed_table_anchor: TableClassification} for every table that
    successfully classified as applicable with real row_groups. Callers
    project this SAME shared structure into whatever entity-specific
    candidates they need (see run_table_enumeration): Treatment gets the
    deduplicated set of distinct factor-level combinations that actually
    co-occur; Observation gets the full row x value-column cross-product.
    Deriving both from the identical reconstruction means they are
    provably consistent with each other -- never two separate free-form
    passes independently guessing at the same table and possibly
    disagreeing.

    Never raises and never blocks the rest of the pass on one table's
    failure: a table whose classification never validates within budget
    (run_table_classification returns None) is simply skipped -- absent
    from the returned dict, exactly as if this whole mechanism didn't
    exist for that one table."""
    listing = content_reader.list_tables(paper_id, papers_root=_papers_root())
    if not listing.get("found"):
        return {}

    classifications: dict[str, TableClassification] = {}
    consumed_anchors: set[str] = set()  # anchors already absorbed as a page-split continuation of an earlier table

    # Deterministic continuation chains (content_reader.table_continuation_map):
    # a block that directly continues the table before it is never a seed of
    # its own -- it is classified together with its head, as ONE logical table.
    continuation_of = {t["table_anchor"]: t.get("continuation_of") for t in listing["tables"]}
    follower = {prev: cur for cur, prev in continuation_of.items() if prev}

    def _chain_from(head: str) -> list[str]:
        chain = [head]
        while chain[-1] in follower:
            chain.append(follower[chain[-1]])
        return chain

    for table in listing["tables"]:
        anchor = table["table_anchor"]
        if anchor in consumed_anchors or continuation_of.get(anchor):
            continue

        chain = _chain_from(anchor)
        other_tables = [t for t in listing["tables"] if t["table_anchor"] not in chain]
        classification, _error = run_table_classification(
            run_id=run_id, paper_id=paper_id,
            seed_table_anchor=anchor, other_tables=other_tables, model=model, invoke=invoke,
            chain_anchors=chain if len(chain) > 1 else None,
        )
        # The chain is one logical table whether or not its classification
        # succeeded -- its later blocks are never re-seeded on their own.
        consumed_anchors.update(chain)
        if classification is None:
            continue  # logged by run_table_classification itself; free-form pass gets a shot at this table

        consumed_anchors.update(classification.table_anchors)
        if not classification.applicable or not classification.row_groups:
            continue

        classifications[anchor] = classification

    return classifications


def _cell_dimension_levels(
    classification: TableClassification, row: Any, value_column: Any,
) -> dict[str, tuple[Optional[str], str]]:
    """{factor name: (declared dimension, level)} for ONE (row, value column)
    cell: row-encoded levels, column-encoded levels, and table-context levels,
    plus the legacy site_hint / treatment_level_hint (which always meant the
    site / a treatment level)."""
    levels: dict[str, tuple[Optional[str], str]] = {}
    for name, level in (row.factor_values or {}).items():
        levels[name] = (classification.factor_dimension(name), level)
    for name, level in (value_column.factor_levels or {}).items():
        levels.setdefault(name, (classification.factor_dimension(name), level))
    for name, level in (classification.context_levels or {}).items():
        levels.setdefault(name, (classification.factor_dimension(name), level))
    if value_column.site_hint:
        levels.setdefault("Site", ("site", value_column.site_hint))
    if value_column.treatment_level_hint:
        levels.setdefault("Treatment", ("treatment", value_column.treatment_level_hint))
    return levels


def _declared_treatment_parts(
    classification: TableClassification, row: Any, value_column: Any,
) -> Optional[tuple[dict[str, str], dict[str, str]]]:
    """(treatment-dimension levels, site-dimension levels) for one cell of a
    table with DECLARED factors, or None when the cell has no
    treatment-dimension level at all. Never includes a crop, time, replicate,
    variable or other dimension: those describe the OBSERVATION
    (Observation.crop_id / temporal_info / variable), not the experimental
    condition."""
    levels = _cell_dimension_levels(classification, row, value_column)
    treatment = {name: level for name, (dim, level) in levels.items() if dim == "treatment"}
    if not treatment:
        return None
    site = {name: level for name, (dim, level) in levels.items() if dim == "site"}
    return treatment, site


def _canonical_treatment_identity(
    treatment: dict[str, str], site: dict[str, str], site_pool: Optional[list[dict]],
    factor_names_matter: bool = False, site_matters: bool = False,
) -> tuple[tuple, str, bool]:
    """(identity key, candidate-id text, ambiguous) for a Treatment candidate
    of a declared-factor table.

    Identity is built from canonical SEMANTIC content, never from how a table
    happened to spell it:
      - the normalized treatment LEVELS (key names such as 'Location' vs 'Site'
        or 'Winter treatment' vs 'Cover crop' are ignored);
      - the site, RESOLVED to a ready Site record through the run's site pool,
        so 'Ames, IA' and 'Ames' are the same site. With no site pool the site
        is implicit -- and its text ignored -- ONLY when the table itself has a
        single site level (`site_matters` False); a table that distinguishes
        several sites it cannot resolve keeps them apart (ambiguous).
    A site that cannot be uniquely resolved against an existing pool is
    AMBIGUOUS: the candidate keeps its raw spelling in its identity, so it is
    never silently merged with anything (and is flagged by the caller).
    `factor_names_matter` (set when two different treatment factors of one table
    share a level string) keeps the factor names in the identity for the same
    fail-safe reason."""
    if factor_names_matter:
        level_part = frozenset((_normalize_for_matching(n), _normalize_for_matching(v)) for n, v in treatment.items())
        id_bits = sorted(f"{_normalize_for_matching(n)} {_normalize_for_matching(v)}" for n, v in treatment.items())
    else:
        level_part = frozenset(_normalize_for_matching(v) for v in treatment.values())
        id_bits = sorted(_normalize_for_matching(v) for v in treatment.values())

    site_key = ""
    ambiguous = False
    if site and (site_pool or site_matters):
        resolved = _match_row_group_to_pool(site, site_pool) if site_pool else None
        if resolved:
            site_key = f"site:{resolved}"
        else:
            ambiguous = True
            site_key = "raw:" + "|".join(sorted(_normalize_for_matching(v) for v in site.values()))
    if site_key:
        id_bits.append(site_key.split(":", 1)[1] if site_key.startswith("site:") else site_key[4:])
    return (site_key, level_part), "_".join(bit for bit in id_bits if bit), ambiguous


def _table_classifications_to_treatment_candidates(
    classifications: list[TableClassification], link_pools: dict[str, list[dict]],
    notes: Optional[list[dict]] = None,
) -> tuple[list[EnumerationCandidate], set[str]]:
    """Step C, Treatment projection: a Treatment is the DISTINCT
    combination of factor levels actually applied to one experimental
    unit -- not any single factor in isolation. Generalizes correctly
    regardless of how many factors a paper crosses: a single-factor
    paper's row_groups carry one key in factor_values, so this collapses
    to exactly the old one-candidate-per-level behavior; a multi-factor
    paper (2-way factorial, split-plot, ...) naturally produces the FULL
    cross-product combination, because that combination is simply
    row_group.factor_values plus (when column-encoded, e.g. a table with
    'Ames'/'Mead' sub-columns) the relevant value_column.site_hint -- the
    EXACT SAME combination Step C's Observation projection independently
    computes as its own match_values for site/method linking. Real
    evidence this is necessary (Daren-1997-Canopy): free-form enumeration
    treated 'Population' and 'Maturity' as two flatly separate sets of
    Treatments, which left no single Treatment able to represent a value
    that is genuinely about both at once.

    Deduplicates by the exact combination (order-independent) across every
    (row_group, value_column) pair with a real, non-blank cell -- multiple
    value_columns sharing the same row/site (e.g. four different measures
    all reported for 'Trailblazer at Ames') must yield ONE Treatment
    candidate, not four. Each candidate's linked_candidates is resolved
    the same deterministic way as Observation's (_match_row_group_to_pool
    against link_pools, e.g. site_id) -- never a guess."""
    seen: set[tuple] = set()
    candidates: list[EnumerationCandidate] = []
    covered_anchors: set[str] = set()

    for classification in classifications:
        # Same role gate as Observation's projection
        # (_table_classification_to_candidates). Fix 2: Table 7's "Location,
        # across populations" and "Population, across locations and
        # maturities" sections (an aggregated_summary) previously produced
        # exactly the nonsense Treatment(name="Ames, IA") /
        # Treatment(name="Trailblazer") (no site/maturity at all) records this
        # gate prevents; a weather_context table's Location x Month rows are
        # excluded the same way.
        if classification.table_role != "treatment_response":
            continue
        if not _pooled_representability(classification)[0]:
            continue  # registered as an aggregated source instead (see summarize_table_pass)
        value_columns_by_id = {c.value_column_id: c for c in classification.value_columns}
        contributed = False
        # A table whose experimental dimensions were DECLARED (`factors`) has
        # been analysed for what a Treatment is: only treatment-dimension
        # factors (plus the site) can form a Treatment's identity -- a
        # cultivar/population is `crop`, a date or growth stage is `time`
        # (protocol Section 6.3). Such a table is "covered" for Treatment even
        # when it yields none (a design with no treatment dimension), so the
        # free-form pass is not invited to re-invent Treatments from it.
        declared = bool(classification.factors)
        # Two DIFFERENT treatment factors of one table sharing a level string
        # would collapse if identity ignored factor names -- keep the names in
        # that table's identities instead (fail safe: never merge on a guess).
        factor_names_matter = False
        if declared:
            level_owners: dict[str, set[str]] = {}
            for f in classification.factors:
                if f.dimension == "treatment":
                    for level in _factor_levels_of(classification, f.name):
                        level_owners.setdefault(_normalize_for_matching(level), set()).add(f.name)
            factor_names_matter = any(len(owners) > 1 for owners in level_owners.values())
        # Does this table itself distinguish more than one site?
        site_level_texts: set[str] = set()
        if declared:
            for f in classification.factors:
                if f.dimension == "site":
                    site_level_texts |= {_normalize_for_matching(x) for x in _factor_levels_of(classification, f.name)}
        site_level_texts |= {_normalize_for_matching(vc.site_hint) for vc in classification.value_columns if vc.site_hint}
        site_matters = len(site_level_texts - {""}) > 1
        # Fix 5 (column-as-treatment), real Felipe-2010-Cultivar evidence:
        # a table where ANY value_column sets treatment_level_hint is
        # COLUMN-ENCODED for Treatment purposes -- the treatment's
        # identity comes from the column (plus site_hint, if set), and
        # row-level factor_values (e.g. a DAP time point) are CONTEXT for
        # the observation, never folded into the Treatment's own combo
        # (folding them in would incorrectly mint one "treatment" per
        # (context, column) cell instead of one per real treatment level).
        # A table with no such hint on any column is ROW-ENCODED, exactly
        # the pre-Fix-5 behavior, unchanged.
        column_encoded = any(vc.treatment_level_hint for vc in classification.value_columns)

        for row in classification.row_groups:
            for value_column_id, cell_text in (row.cells or {}).items():
                if not cell_text or not cell_text.strip():
                    continue
                value_column = value_columns_by_id.get(value_column_id)
                if value_column is None:
                    continue

                identity_key: Optional[tuple] = None
                identity_id: Optional[str] = None
                dimension_entries: list[CandidateDimension] = []
                if declared:
                    parts = _declared_treatment_parts(classification, row, value_column)
                    if parts is None:
                        continue  # no treatment-dimension factor at this cell -> no Treatment
                    treatment_levels, site_levels = parts
                    combo = {**treatment_levels, **site_levels}
                    dimension_entries = (
                        [CandidateDimension(name=n, dimension="treatment", level=v) for n, v in treatment_levels.items()]
                        + [CandidateDimension(name=n, dimension="site", level=v) for n, v in site_levels.items()]
                    )
                    identity_key, identity_id, ambiguous = _canonical_treatment_identity(
                        treatment_levels, site_levels, (link_pools or {}).get("site_id"), factor_names_matter, site_matters,
                    )
                    if ambiguous:
                        # never merged: the raw spelling is part of the identity
                        identity_key = (identity_key, tuple(sorted(combo.items())))
                        if notes is not None:
                            notes.append({
                                "kind": "identity_ambiguous_site", "combo": dict(combo),
                                "detail": "the site could not be uniquely resolved against this run's Site records; "
                                          "kept as a separate candidate, not merged",
                            })
                elif column_encoded:
                    if not value_column.treatment_level_hint:
                        continue  # this column isn't itself a treatment level in a column-encoded table
                    combo = {"Treatment": value_column.treatment_level_hint}
                    if value_column.site_hint:
                        combo["Site"] = value_column.site_hint
                else:
                    combo = dict(row.factor_values or {})
                    if value_column.site_hint:
                        combo.setdefault("Site", value_column.site_hint)
                if not combo:
                    continue
                if not declared:
                    # legacy (no declared factors): every row factor was always the
                    # treatment identity, and `Site` the site -- behavior unchanged
                    dimension_entries = [
                        CandidateDimension(name=n, dimension="site" if n == "Site" else "treatment", level=v)
                        for n, v in combo.items()
                    ]

                contributed = True
                key = identity_key if identity_key is not None else tuple(sorted(combo.items()))
                if key in seen:
                    continue
                seen.add(key)

                candidate_id = _sanitize_candidate_id(
                    identity_id if identity_id is not None else "_".join(str(v) for _, v in sorted(combo.items()))
                )
                description = "Experimental condition: " + ", ".join(f"{k}={v}" for k, v in sorted(combo.items()))
                linked_candidates: dict[str, str] = {}
                for field, pool in (link_pools or {}).items():
                    match = _match_row_group_to_pool(combo, pool)
                    if match:
                        linked_candidates[field] = match

                candidates.append(EnumerationCandidate(
                    candidate_id=candidate_id, description=description,
                    anchors=[row.source_table_anchor], linked_candidates=linked_candidates,
                    dimensions=dimension_entries,
                ))

        if contributed or declared:
            covered_anchors.update(classification.table_anchors)

    return candidates, covered_anchors


def _factor_levels_of(classification: TableClassification, name: str) -> set[str]:
    """Every level a declared factor takes anywhere in the table."""
    levels: set[str] = set()
    for row in classification.row_groups:
        if name in (row.factor_values or {}):
            levels.add(row.factor_values[name])
    for column in classification.value_columns:
        if name in (column.factor_levels or {}):
            levels.add(column.factor_levels[name])
    if name in (classification.context_levels or {}):
        levels.add(classification.context_levels[name])
    return {level for level in levels if level and level.strip()}


def _exact_pool_match(level: str, pool: list[dict]) -> Optional[str]:
    """Slug of the ONE pool entry whose normalized name (or slug phrase) equals
    the normalized level exactly, else None (no match, or ambiguous). Exact
    only: this is a semantic cross-check on a declared dimension, so it must
    never rest on a substring or token guess."""
    target = _normalize_for_matching(level)
    if not target:
        return None
    hits = [
        item["slug"] for item in pool
        if target in {_normalize_for_matching(item.get("name") or ""), _normalize_for_matching(item["slug"].replace("_", " "))} - {""}
    ]
    return hits[0] if len(hits) == 1 else None


def _reconcile_factor_dimensions(
    classification: TableClassification, dimension_pools: Optional[dict[str, list[dict]]],
) -> tuple[TableClassification, list[dict]]:
    """Deterministic cross-check of the model's declared factor dimensions
    against records this run already committed. A factor declared `treatment`
    whose EVERY level is exactly a ready Crop record (a cultivar/population) is
    a crop dimension, not a treatment; whose every level is exactly a ready
    Site is a site dimension (protocol Section 6.3). All-or-nothing and exact,
    so a factor that only partly matches is left as declared. Returns the
    (possibly corrected) classification and a diagnostic list of overrides."""
    if not dimension_pools or not (classification.factors or classification.pooled_factors):
        return classification, []
    overrides: list[dict] = []
    corrected = []
    for factor in classification.factors:
        new_dimension = factor.dimension
        if factor.dimension == "treatment":
            levels = _factor_levels_of(classification, factor.name)
            for kind in ("crop", "site"):
                pool = dimension_pools.get(kind) or []
                if levels and pool and all(_exact_pool_match(level, pool) for level in levels):
                    new_dimension = kind
                    overrides.append({
                        "factor": factor.name, "declared": factor.dimension, "corrected_to": kind,
                        "reason": f"every level {sorted(levels)} is exactly a ready {kind} record of this run",
                    })
                    break
        corrected.append(factor.model_copy(update={"dimension": new_dimension}) if new_dimension != factor.dimension else factor)
    # A deterministically detected pooled factor whose dimension the source
    # text did not reveal (`other`) is a site/crop when it is exactly a ready
    # Site/Crop record of this run (e.g. "averaged across Ames and Mead"): the
    # site case matters, since values pooled over the site are L2.
    pooled_corrected = []
    for pooled in classification.pooled_factors:
        new_dimension = pooled.dimension
        if pooled.origin == "deterministic" and pooled.dimension == "other":
            for kind in ("crop", "site"):
                pool = dimension_pools.get(kind) or []
                if pool and _exact_pool_match(pooled.name, pool):
                    new_dimension = kind
                    overrides.append({
                        "factor": pooled.name, "declared": pooled.dimension, "corrected_to": kind, "pooled": True,
                        "reason": f"the pooled name is exactly a ready {kind} record of this run",
                    })
                    break
        pooled_corrected.append(pooled.model_copy(update={"dimension": new_dimension}) if new_dimension != pooled.dimension else pooled)
    if not overrides:
        return classification, []
    return classification.model_copy(update={"factors": corrected, "pooled_factors": pooled_corrected}), overrides


def _dimension_pools(paper_id: str, this_run_records: dict) -> dict[str, list[dict]]:
    """{"crop": [...], "site": [...]} -- {slug, name} of this run's READY Crop
    and Site records, for `_reconcile_factor_dimensions`."""
    def value(payload: dict, key: str) -> Optional[str]:
        field = payload.get(key)
        return field.get("value") if isinstance(field, dict) else None

    pools: dict[str, list[dict]] = {"crop": [], "site": []}
    for kind, entity_type, keys in (("crop", "Crop", ("cultivar", "common_name")), ("site", "Site", ("name",))):
        for record in _ready_records(this_run_records, entity_type):
            payload = ((record.get("detail") or {}).get("payload")) or {}
            slug = _candidate_slug_from_record_id(paper_id, entity_type, record["record_id"])
            for key in keys:
                name = value(payload, key)
                if name:
                    pools[kind].append({"slug": slug, "name": name})
    return pools


def run_table_enumeration(
    *, run_id: str, paper_id: str, entity_type: str, model: str,
    invoke: Callable[..., AgentInvocation] = invoke_agent,
    link_pools: Optional[dict[str, list[dict]]] = None,
    dimension_pools: Optional[dict[str, list[dict]]] = None,
) -> tuple[list[EnumerationCandidate], set[str]]:
    """Steps A + B (shared, cached across entity types within one run --
    see run_table_classification_pass) + Step C (entity-specific
    projection of the SAME reconstructed tables):
      - Treatment: the distinct set of factor-level combinations that
        actually co-occur, deduplicated (see
        _table_classifications_to_treatment_candidates).
      - anything else (Observation): one candidate per (row_group,
        value_column) pair (see _table_classification_to_candidates).
    Returns (candidates, covered_table_anchors) -- covered_table_anchors
    is EXACTLY the set of anchors that actually produced >=1 real
    candidate for THIS entity_type, never every classified-applicable
    anchor: the caller (Step D, in _run_multi_record_entity) uses this to
    tell the free-form enumeration pass what NOT to re-report, and a table
    this function gave up on (or that produced nothing relevant to this
    particular entity_type) remains fully available to that fallback."""
    classifications = run_table_classification_pass(run_id=run_id, paper_id=paper_id, model=model, invoke=invoke)
    if not classifications:
        return [], set()

    overrides_log: list[dict] = []
    for seed, classification in list(classifications.items()):
        classifications[seed], overrides = _reconcile_factor_dimensions(classification, dimension_pools)
        overrides_log.extend({"table": seed, **o} for o in overrides)
    if overrides_log:
        run_store.save_stage_attempt(
            run_id, f"{entity_type}__enumeration", "dimension_check", 1, {"overrides": overrides_log},
        )

    if entity_type == "Treatment":
        identity_notes: list[dict] = []
        result = _table_classifications_to_treatment_candidates(
            list(classifications.values()), link_pools or {}, identity_notes,
        )
        if identity_notes:
            run_store.save_stage_attempt(
                run_id, f"{entity_type}__enumeration", "identity_check", 1, {"ambiguous_identities": identity_notes},
            )
        return result

    all_candidates: list[EnumerationCandidate] = []
    covered_anchors: set[str] = set()
    for classification in classifications.values():
        candidates = _table_classification_to_candidates(classification, link_pools or {})
        if candidates:
            all_candidates.extend(candidates)
            covered_anchors.update(classification.table_anchors)

    return all_candidates, covered_anchors


_SANITIZE_CANDIDATE_ID_RE = re.compile(r"[^a-z0-9]+")


def _sanitize_candidate_id(candidate_id: str) -> str:
    """Deterministic, orchestrator-side normalization of a model-chosen
    candidate_id into something safe to embed in a record_id/filename --
    the model's raw string is never trusted directly as an id component."""
    slug = _SANITIZE_CANDIDATE_ID_RE.sub("_", candidate_id.strip().lower()).strip("_")
    return slug or "candidate"


def _drop_candidates_covered_by_tables(
    candidates: list[EnumerationCandidate], covered_table_anchors: set[str],
) -> list[EnumerationCandidate]:
    """Step D's defensive dedup: drop any free-form candidate whose ENTIRE
    anchor set is already covered by the deterministic table-enumeration
    pass -- a belt-and-suspenders check in case the model didn't honor the
    excluded_table_anchors prompt text (never trust an instruction alone
    when a deterministic check can verify it instead, same discipline as
    everywhere else in this file). A candidate citing at least one anchor
    OUTSIDE the covered set is kept -- it may be genuinely new evidence
    (e.g. narrative prose) even if it also happens to cite an already-
    covered table."""
    if not covered_table_anchors:
        return candidates
    return [
        c for c in candidates
        if not set(a.strip("[]") for a in c.anchors) <= covered_table_anchors
    ]


def _drop_freeform_candidates_subsumed_by_tables(
    freeform_candidates: list[EnumerationCandidate], table_candidates: list[EnumerationCandidate],
) -> list[EnumerationCandidate]:
    """Fix 3 (table-enumeration fix-pass design review): a SEPARATE,
    additive check from `_drop_candidates_covered_by_tables` above.

    Real Daren-1997-Canopy evidence this exists for: the anchor-based
    check only catches a free-form candidate that cites a table anchor --
    it does nothing when free-form grounds its candidate in PROSE instead
    (every population in Daren is also named in the Materials and Methods
    paragraph, so free-form minted 12 population-x-site candidates
    anchored ONLY in that prose, none of them a table anchor, each a
    valid-but-coarser duplicate of maturity-specific table candidates for
    the same site).

    Deliberately NOT a text/name comparison (never runs a free-form
    candidate's description through `_match_row_group_to_pool` or any
    other fuzzy matcher against table candidates' names) -- the identity
    key here is exclusively each candidate's own ALREADY-RESOLVED,
    deterministic `linked_candidates` (e.g. site_id), computed earlier by
    the exact same mechanism table candidates themselves used. A free-form
    candidate is dropped only when EVERY field it resolved a link for is a
    value some table candidate ALSO resolved that same field to -- i.e.
    table enumeration has already independently verified coverage of
    every real entity this free-form candidate also points at. A
    candidate with NO resolved links at all is never dropped this way
    (nothing to compare -- stays conservative, kept), and a candidate
    whose link points at something no table candidate ever resolved (a
    site only ever mentioned in prose, say) is genuinely new evidence and
    is kept."""
    if not table_candidates:
        return freeform_candidates
    table_linked_values: dict[str, set[str]] = {}
    for tc in table_candidates:
        for field, value in (tc.linked_candidates or {}).items():
            if isinstance(value, str):
                table_linked_values.setdefault(field, set()).add(value)

    kept = []
    for c in freeform_candidates:
        links = c.linked_candidates or {}
        if not links or any(not isinstance(v, str) for v in links.values()):  # a list link is not comparable: kept
            kept.append(c)
            continue
        if all(table_linked_values.get(field) and value in table_linked_values[field] for field, value in links.items()):
            continue  # every real link this candidate has is already covered by table enumeration
        kept.append(c)
    return kept


# Words that name a KIND of level without distinguishing any one: a declared level made only of these ("mixture",
# "treatment") is never supported by their mere presence in the text -- the word "mixture" alone is not a Treatment.
_GENERIC_LEVEL_TOKENS = frozenset({
    "mixture", "mixtures", "treatment", "treatments", "level", "levels", "cultivar", "cultivars", "group", "groups",
    "condition", "conditions", "system", "systems", "factor", "factors",
})
_LEVEL_QUALIFIER_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_LEVEL_HEAD_SPLIT_RE = re.compile(r"\s+[-\u2010-\u2015:;,]\s+|\s*[;:,]\s*")


def _level_forms(level: str) -> list[str]:
    """The normalized forms under which a declared level may legitimately appear in the source: the level as written
    and, when the model decorated it with a qualifier, the level WITHOUT the qualifier -- the part outside brackets,
    and the part before a ':' ';' ',' or spaced dash. Real case (Felipe-2010-Cultivar): the model declared
    "1-cv (choice cultivar only)"; the paper writes "(1-cv)". Only the decoration is removed, never a word of the level
    itself, and a form made only of generic words ("mixture") is discarded, so nothing is inferred from them."""
    unbracketed = _LEVEL_QUALIFIER_RE.sub("", level).strip()
    candidates = [level, unbracketed, _LEVEL_HEAD_SPLIT_RE.split(unbracketed)[0] if unbracketed else ""]
    forms: list[str] = []
    for candidate in candidates:
        form = _normalize_for_matching(candidate)
        if form and form not in forms and any(tok not in _GENERIC_LEVEL_TOKENS for tok in form.split()):
            forms.append(form)
    return forms


def _level_supported(level: str, support: str) -> bool:
    """A declared level is supported when the level as written occurs in the support text (the original check), or a
    qualifier-free form of it occurs there as whole words. A level of only generic words is never supported."""
    forms = _level_forms(level)
    full = _normalize_for_matching(level)
    if full in forms and full in support:
        return True
    padded = f" {support} "
    return any(f" {form} " in padded for form in forms)


def _candidate_dimension_errors(
    candidates: list[EnumerationCandidate], blocks: dict[str, str],
) -> tuple[list[dict], set[str]]:
    """Validation of the dimensions a free-form candidate declared: present, and
    every declared level actually appears (normalized; see `_level_supported` for the tolerance to a decorated level)
    in the candidate's own description or in the blocks it cites -- a level the candidate itself does
    not support could otherwise make it look identical to a table candidate."""
    errors: list[dict] = []
    bad: set[str] = set()
    for candidate in candidates:
        if not candidate.dimensions:
            errors.append({
                "field": "dimensions",
                "message": f"candidate '{candidate.candidate_id}' declares no dimensions -- give one "
                           f"{{name, dimension, level}} per factor level that distinguishes it.",
            })
            bad.add(candidate.candidate_id)
            continue
        support = _normalize_for_matching(
            " ".join([candidate.description] + [blocks.get(a.strip("[]"), "") for a in candidate.anchors])
        )
        for d in candidate.dimensions:
            if not _level_supported(d.level, support):
                errors.append({
                    "field": "dimensions",
                    "message": f"candidate '{candidate.candidate_id}' declares {d.name}={d.level!r}, which "
                               f"appears neither in its description nor in the blocks it cites -- give the level as the "
                               f"source writes it (a qualifier in brackets is fine, but the level itself must be in the text).",
                })
                bad.add(candidate.candidate_id)
    return errors, bad


def _reconcile_candidate_dimensions(
    candidate: EnumerationCandidate, dimension_pools: Optional[dict[str, list[dict]]],
) -> list[CandidateDimension]:
    """A level declared `treatment` that is exactly a ready Crop/Site record of
    this run is a crop/site (protocol Section 6.3) -- the same exact-match
    correction `_reconcile_factor_dimensions` applies to a table's factors."""
    out = []
    for d in candidate.dimensions:
        new_dimension = d.dimension
        if d.dimension == "treatment":
            for kind in ("crop", "site"):
                pool = (dimension_pools or {}).get(kind) or []
                if pool and _exact_pool_match(d.level, pool):
                    new_dimension = kind
                    break
        out.append(d.model_copy(update={"dimension": new_dimension}) if new_dimension != d.dimension else d)
    return out


def _dimensioned_identity(
    dimensions: list[CandidateDimension], site_pool: Optional[list[dict]], site_matters: bool,
) -> Optional[tuple[tuple, str, bool]]:
    """Canonical identity (the SAME `_canonical_treatment_identity` table
    candidates use) from a candidate's declared dimensions, or None when it has
    no treatment-dimension level at all. Factor names are ignored (the same
    design spelled `Tillage` or `Practice` is one identity) EXCEPT when two of
    the candidate's own treatment levels are the same string, where names are
    all that tell the factors apart."""
    treatment = {d.name: d.level for d in dimensions if d.dimension == "treatment"}
    if not treatment:
        return None
    site = {d.name: d.level for d in dimensions if d.dimension == "site"}
    levels = [_normalize_for_matching(v) for v in treatment.values()]
    names_matter = len(set(levels)) < len(levels)
    return _canonical_treatment_identity(treatment, site, site_pool, factor_names_matter=names_matter, site_matters=site_matters)


def _treatment_name_map(dimensions: list[CandidateDimension]) -> dict[str, str]:
    return {_normalize_for_matching(d.name): _normalize_for_matching(d.level) for d in dimensions if d.dimension == "treatment"}


def _covered_condition_labels(table_candidates: list[EnumerationCandidate]) -> list[str]:
    return [c.description for c in table_candidates]


def _drop_freeform_treatments_covered_by_tables(
    freeform_candidates: list[EnumerationCandidate], table_candidates: list[EnumerationCandidate],
    site_pool: Optional[list[dict]], dimension_pools: Optional[dict[str, list[dict]]],
) -> tuple[list[EnumerationCandidate], list[dict]]:
    """Item 8: SEMANTIC free-form/table Treatment dedup, replacing the coarse
    anchor- and link-based drops for Treatment. Both sides are compared by the
    same canonical identity (treatment-dimension levels + the site resolved
    through the run's site pool; key names never matter).

    A free-form candidate is:
      - DROPPED as covered only when its identity equals that of a table
        candidate whose own identity is unambiguous;
      - DROPPED as not-a-Treatment when its declared dimensions contain no
        treatment-dimension level (a population, a growth stage and a site are
        crop / time / site, never a Treatment -- protocol Section 6.3);
      - KEPT (and flagged) when it declares no dimensions or its identity is
        ambiguous (an unresolved site): never merged on a guess;
      - KEPT when its identity matches no table candidate: it is new.
    Dedup only ever REMOVES; it never edits a surviving candidate, copies
    anchors or a known value across, or makes anything grounded that was not.
    Returns (kept, decisions) -- one decision per free-form candidate."""
    reconciled = {c.candidate_id: _reconcile_candidate_dimensions(c, dimension_pools) for c in freeform_candidates}
    site_texts = {
        _normalize_for_matching(d.level)
        for dims in list(reconciled.values()) + [c.dimensions for c in table_candidates]
        for d in dims if d.dimension == "site"
    } - {""}
    site_matters = len(site_texts) > 1

    table_identities: dict[tuple, list[tuple[str, dict[str, str]]]] = {}
    for tc in table_candidates:
        identity = _dimensioned_identity(tc.dimensions, site_pool, site_matters)
        if identity is not None and not identity[2]:
            table_identities.setdefault(identity[0], []).append((tc.candidate_id, _treatment_name_map(tc.dimensions)))

    kept: list[EnumerationCandidate] = []
    decisions: list[dict] = []
    for c in freeform_candidates:
        dims = reconciled[c.candidate_id]
        base = {"candidate_id": c.candidate_id, "description": c.description, "anchors": list(c.anchors)}
        if not dims:
            kept.append(c)
            decisions.append({**base, "decision": "kept_unresolved", "reason": "no valid dimensions were declared, so its identity cannot be compared"})
            continue
        identity = _dimensioned_identity(dims, site_pool, site_matters)
        if identity is None:
            decisions.append({**base, "decision": "dropped_no_treatment_dimension", "dimensions": [d.model_dump() for d in dims],
                              "reason": "its declared dimensions contain no treatment-dimension level (crop/time/site are not a Treatment)"})
            continue
        key, _id_text, ambiguous = identity
        if ambiguous:
            kept.append(c)
            decisions.append({**base, "decision": "kept_unresolved",
                              "reason": "its site cannot be uniquely resolved, so its identity is ambiguous; not merged"})
            continue
        equal_levels = table_identities.get(key, [])
        if not equal_levels:
            kept.append(c)
            decisions.append({**base, "decision": "kept_new", "reason": "its identity matches no table-derived candidate"})
            continue
        # Same levels, same site. Several treatment factors can still pair the
        # levels differently (Cover=none/Tillage=till vs Cover=till/Tillage=none):
        # when the two share any factor NAME they must also agree on which level
        # each name carries; sharing no name at all is just two spellings of one design.
        names = _treatment_name_map(dims)
        matched = next(
            (tc_id for tc_id, tmap in equal_levels
             if len(names) == 1 or not (set(names) & set(tmap)) or names == tmap),
            None,
        )
        if matched is None:
            kept.append(c)
            decisions.append({**base, "decision": "kept_unresolved",
                              "reason": "the same treatment levels are paired with different factor names than the table's; not merged"})
            continue
        decisions.append({**base, "decision": "dropped_covered", "matched_table_candidate": matched,
                          "reason": "same canonical identity as an unambiguous table-derived candidate"})
    return kept, decisions


def _dedupe_candidate_record_ids(
    candidates: list[EnumerationCandidate],
) -> tuple[list[EnumerationCandidate], list[str]]:
    """Phase 1.2 (table-enumeration design review, "candidate collision
    detection"): table-derived and free-form candidates are generated by
    two entirely independent passes and can legitimately choose the same
    sanitized candidate_id (e.g. both settle on 'ambient_co2') even though
    `EnumerationResult`'s own uniqueness check and `_sanitize_candidate_id`
    each only guarantee uniqueness WITHIN one source's own candidate list,
    never ACROSS the table + free-form merge below. Two distinct candidates
    mapping to the same `record_id` would otherwise silently overwrite one
    `run_record()` attempt's on-disk artifacts (run_store/results_store)
    with the other's, with nothing ever surfaced -- exactly the "never
    silently overwrite one candidate with another" failure this guards
    against.

    Returns (candidates_with_distinct_ids, collision_notes) in the SAME
    order as the input. Every input candidate is still present in the
    output -- none dropped, none merged -- with its original candidate_id
    unchanged EXCEPT a duplicate (second-and-later) occurrence, which gets
    a stable numeric suffix so it maps to a genuinely distinct record_id.
    `collision_notes` is empty in the normal (no-collision) case."""
    seen: dict[str, int] = {}
    deduped: list[EnumerationCandidate] = []
    notes: list[str] = []
    for candidate in candidates:
        base_id = _sanitize_candidate_id(candidate.candidate_id)
        count = seen.get(base_id, 0)
        seen[base_id] = count + 1
        if count == 0:
            deduped.append(candidate)
            continue
        disambiguated_id = f"{base_id}_dup{count + 1}"
        notes.append(
            f"candidate_id {candidate.candidate_id!r} (sanitized: {base_id!r}) collided with an "
            f"earlier candidate in this same enumeration -- record_id disambiguated to "
            f"{disambiguated_id!r} rather than silently overwriting the earlier candidate's "
            f"run_record() results."
        )
        deduped.append(candidate.model_copy(update={"candidate_id": disambiguated_id}))
    return deduped, notes


def _levenshtein_distance(a: str, b: str) -> int:
    """Minimal edit distance, pure Python (no external dependency needed)
    -- only ever called on short, already-sanitized candidate_id strings
    for a bounded, per-run candidate list (see
    _flag_near_duplicate_candidates), so O(len(a)*len(b)) is never a
    performance concern here."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


_NEAR_DUPLICATE_MAX_EDIT_DISTANCE = 1


def _flag_near_duplicate_candidates(candidates: list[EnumerationCandidate]) -> list[str]:
    """Fix 4 (table-enumeration fix-pass design review, population-name
    reconciliation): DETECTS, never merges. Real Daren-1997-Canopy
    evidence: Marker's OCR misread 'Ey' as 'Ev' specifically in Table 4's
    rendering, producing a candidate_id like 'ev_ff_ldmdc1_ames_vegetative'
    one edit away from the real 'ey_ff_ldmdc1_ames_vegetative' -- two
    candidates that are almost certainly the same real treatment reported
    twice under a corrupted spelling, sourced from two different tables
    (so neither the exact-collision dedup above nor Fix 3's link-based
    subsumption catch it -- their candidate_ids and, in general, their
    resolved links can both legitimately differ).

    Deliberately conservative, per the explicit requirement not to
    introduce fuzzy matching that silently converts arbitrary strings into
    entities: this NEVER merges, drops, or relabels either candidate -- it
    only returns human-readable diagnostic notes (the caller logs them via
    the same run_store.save_stage_attempt convention
    _dedupe_candidate_record_ids's own collision_notes already use) so a
    near-duplicate is VISIBLE for review rather than silently sitting in
    the output unexplained. Only flags a pair whose candidate_id differs
    by at most `_NEAR_DUPLICATE_MAX_EDIT_DISTANCE` character -- a
    genuinely different short candidate_id (two different populations,
    two different maturities) essentially never lands this close by
    chance, but this is explicitly NOT a semantic-similarity judgment and
    never decides which (if either) spelling is correct. Exact collisions
    (distance 0) are `_dedupe_candidate_record_ids`'s job, not this one,
    and are skipped here to avoid double-reporting the same pair."""
    notes: list[str] = []
    ids = [c.candidate_id for c in candidates]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if ids[i] == ids[j]:
                continue
            distance = _levenshtein_distance(ids[i], ids[j])
            if distance <= _NEAR_DUPLICATE_MAX_EDIT_DISTANCE:
                notes.append(
                    f"candidate_id {ids[i]!r} and {ids[j]!r} differ by only {distance} character(s) -- "
                    f"possible near-duplicate (e.g. an OCR-corrupted spelling of the same real entity). "
                    f"Neither candidate was modified or dropped; review both against the source before "
                    f"trusting either in isolation."
                )
    return notes


def _cached_table_classifications(run_id: str) -> dict[str, TableClassification]:
    """Every successfully cached Step B classification of this run, keyed by
    its run_store record key (`table_classification__<seed>`)."""
    out: dict[str, TableClassification] = {}
    for record_key in run_store.list_records(run_id):
        if record_key.startswith("table_classification__"):
            classification = _load_cached_table_classification(run_id, record_key)
            if classification is not None:
                out[record_key] = classification
    return out


def _reconciled_classifications(run_id: str, paper_id: str, this_run_records: dict) -> dict[str, TableClassification]:
    pools = _dimension_pools(paper_id, this_run_records)
    return {
        key: _reconcile_factor_dimensions(c, pools)[0]
        for key, c in _cached_table_classifications(run_id).items()
    }


def _treatment_dimension_limitation(run_id: str, paper_id: str, this_run_records: dict) -> Optional[str]:
    """L1 text when the paper's cell-level data tables declare factors but not
    ONE of them is a treatment dimension; None otherwise (including when no
    table declared its factors, or no treatment_response table exists)."""
    declared = [
        c for c in _reconciled_classifications(run_id, paper_id, this_run_records).values()
        if c.table_role == "treatment_response" and c.factors
    ]
    if declared and not any(_has_treatment_dimension(c) for c in declared):
        return LIMITATION_L1
    return None


def summarize_table_pass(run_id: str, paper_id: str, this_run_records: dict) -> dict[str, Any]:
    """What the deterministic table pass established about this paper, for the
    run manifest -- so nothing it found is discarded just because the current
    cell-level path cannot express it:
      - `tables`: every classified table's role, reason, declared dimensions
        and pooled factors;
      - `aggregated_sources`: tables that are valid sources of AGGREGATED data
        (protocol Section 7.4) -- an `aggregated_summary`, or a
        treatment_response table pooled over some factor -- with whether the
        current IR can represent them and, if not, why;
      - `limitations`: explicit current-IR limits (L1/L2) that apply, distinct
        from extraction failures."""
    reconciled = _reconciled_classifications(run_id, paper_id, this_run_records)
    variable_flags = _cached_variable_declaration_flags(run_id)
    tables, sources = [], []
    for key, c in sorted(reconciled.items()):
        tables.append({
            "table": key.split("__", 1)[1], "table_anchors": c.table_anchors, "table_role": c.table_role,
            "reason": c.reason, "row_groups": len(c.row_groups),
            "factors": [f.model_dump() for f in c.factors], "context_levels": c.context_levels,
            "pooled_factors": [pf.name for pf in c.pooled_factors],
            "unit_hint_flags": [f.model_dump() for f in c.unit_hint_flags],
            "method_hint_flags": [f.model_dump() for f in c.method_hint_flags],
            "variable_declaration_flags": variable_flags.get(key, []),
            "time_levels": [{"factor": tl.factor, "level": tl.level, "site": tl.site, "date_text": tl.date_text, "year_text": tl.year_text,
                             "anchors": tl.anchors} for tl in c.time_levels],
            "has_treatment_dimension": _has_treatment_dimension(c) if c.factors else None,
        })
        if c.table_role == "aggregated_summary":
            sources.append({
                "table_anchors": c.table_anchors, "kind": "aggregated_summary", "representable_in_current_ir": False,
                "reason": c.reason,
                "why_not_represented": "rows are pooled/averaged summaries, not treatment-combination cells",
            })
        elif c.table_role == "treatment_response" and c.pooled_factors:
            representable, why = _pooled_representability(c)
            sources.append({
                "table_anchors": c.table_anchors, "kind": "pooled_treatment_response",
                "aggregated_over_factors": [pf.name for pf in c.pooled_factors],
                "representable_in_current_ir": representable, "why_not_represented": why,
            })
    limitations = []
    if _treatment_dimension_limitation(run_id, paper_id, this_run_records):
        limitations.append({"code": "L1", "text": LIMITATION_L1})
    if any(src.get("why_not_represented") == LIMITATION_L2 for src in sources):
        limitations.append({"code": "L2", "text": LIMITATION_L2})
    evidence = []
    for key, records in sorted(_cached_pooling_evidence(run_id).items()):
        anchors = reconciled[key].table_anchors if key in reconciled else [key.split("__", 1)[1]]
        for record in records:
            evidence.append({"table_anchors": anchors, **record})
    failures = [
        {
            "table": key.split("__", 1)[1], "failure_class": f["failure_class"], "failure_kind": f.get("failure_kind"),
            "failure_classes": f.get("failure_classes", []), "numbered_attempts": f.get("numbered_attempts"),
            "provider_failure_rounds": f.get("provider_failure_rounds"), "message": f["message"],
        }
        for key, f in sorted(_cached_table_failures(run_id).items())
    ]
    return {
        "tables": tables, "aggregated_sources": sources, "limitations": limitations, "pooling_evidence": evidence,
        # Step B tables given up on, each with the cause and whether it is the provider's failure
        # ("provider") or the extraction's ("extraction") -- never conflated.
        "table_pass_failures": failures,
    }


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
        if entity_type == "Observation" and "Treatment" in blocked_reason:
            limitation = _treatment_dimension_limitation(run_id, paper_id, this_run_records)
            if limitation:
                blocked_reason = f"{blocked_reason}. {limitation}"
        return [{
            "entity_type": entity_type, "record_id": _entity_record_id(paper_id, entity_type),
            "status": "blocked", "reason": blocked_reason,
        }]

    link_pools = _multi_record_link_pools(paper_id, entity_type, this_run_records)

    # Step D (table enumeration, see the module section above
    # run_table_enumeration's own definition): for entity types with a
    # confirmed free-form under-enumeration problem on dense data tables,
    # deterministically generate candidates from every table first, then
    # tell the free-form pass which anchors are already covered so it
    # doesn't re-report (and duplicate) them.
    table_candidates: list = []
    covered_table_anchors: set[str] = set()
    if entity_type in TABLE_ENUMERATION_ENTITY_TYPES:
        table_candidates, covered_table_anchors = run_table_enumeration(
            run_id=run_id, paper_id=paper_id, entity_type=entity_type, model=model, invoke=invoke,
            link_pools=link_pools or None, dimension_pools=_dimension_pools(paper_id, this_run_records),
        )

    # Item 8: for Treatment, when the tables cover it, free-form candidates are
    # compared with table candidates by canonical identity (declared
    # dimensions), not dropped by anchor or by coarse link overlap. Every other
    # case -- other entity types, and a paper with no applicable table -- keeps
    # the existing behavior untouched.
    semantic_dedup = entity_type == "Treatment" and bool(covered_table_anchors or table_candidates)
    freeform_candidates, enum_error = run_enumeration(
        run_id=run_id, paper_id=paper_id, entity_type=entity_type, model=model, invoke=invoke,
        link_pools=link_pools or None,
        excluded_table_anchors=covered_table_anchors or None,
        covered_conditions=_covered_condition_labels(table_candidates) if semantic_dedup else None,
        declare_dimensions=semantic_dedup,
    )
    if semantic_dedup:
        dimension_pools = _dimension_pools(paper_id, this_run_records)
        # The site pool is the SAME one table candidates resolved their site with:
        # `link_pools["site_id"]`, which exists only when the paper has more than one
        # ready Site. With a single Site the site is implicit and must be ignored on
        # both sides -- a one-entry pool would make a free-form candidate that names
        # the site differ from an identical table candidate that (rightly) does not.
        freeform_candidates, dedup_decisions = _drop_freeform_treatments_covered_by_tables(
            freeform_candidates, table_candidates, (link_pools or {}).get("site_id") or None, dimension_pools,
        )
        if dedup_decisions:
            run_store.save_stage_attempt(
                run_id, f"{entity_type}__enumeration", "freeform_dedup", 1,
                {"decisions": dedup_decisions, "counts": {
                    d: sum(1 for x in dedup_decisions if x["decision"] == d)
                    for d in sorted({x["decision"] for x in dedup_decisions})
                }},
            )
    else:
        freeform_candidates = _drop_candidates_covered_by_tables(freeform_candidates, covered_table_anchors)
        freeform_candidates = _drop_freeform_candidates_subsumed_by_tables(freeform_candidates, table_candidates)

    if entity_type == "Method":
        # Item 10 (decision Q6): a distinct, grounded method hint from the tables may seed a
        # Method candidate the free-form pass did not already produce.
        table_candidates, seed_decisions = _method_hint_seeds(
            run_id=run_id, paper_id=paper_id, model=model, invoke=invoke, freeform_candidates=freeform_candidates,
        )
        if seed_decisions:
            run_store.save_stage_attempt(run_id, f"{entity_type}__enumeration", "method_hint_seeds", 1, {"decisions": seed_decisions})

    candidates = table_candidates + freeform_candidates
    if enum_error is not None and not candidates:
        return []

    # Phase 1.2: never let two distinct candidates silently collide onto
    # the same record_id -- see _dedupe_candidate_record_ids's own
    # docstring. Disclosed on disk (never just swallowed) via the same
    # run_store.save_stage_attempt convention every other stage already
    # uses, under the enumeration's own record_key.
    candidates, collision_notes = _dedupe_candidate_record_ids(candidates)
    if collision_notes:
        run_store.save_stage_attempt(
            run_id, f"{entity_type}__enumeration", "candidate_collision", 1,
            {"collisions": collision_notes},
        )

    # Fix 4: near-duplicate spelling detection (e.g. a Marker OCR typo
    # producing a phantom second population) -- disclosed, never resolved
    # automatically. See _flag_near_duplicate_candidates's own docstring.
    near_duplicate_notes = _flag_near_duplicate_candidates(candidates)
    if near_duplicate_notes:
        run_store.save_stage_attempt(
            run_id, f"{entity_type}__enumeration", "near_duplicate_check", 1,
            {"near_duplicates": near_duplicate_notes},
        )

    record_infos = []
    link_decisions: list[dict] = []
    link_blocks: dict[str, str] = {}
    if entity_type in OPTIONAL_LINKS:
        try:
            link_blocks = _load_rendered_blocks(paper_id)
        except FileNotFoundError:
            link_blocks = {}
    for candidate in candidates:
        record_id = f"{paper_id}_{entity_type.lower()}_{_sanitize_candidate_id(candidate.candidate_id)}"
        candidate_known_refs = _apply_candidate_links(
            paper_id, entity_type, this_run_records, known_refs, candidate,
        )
        context = (
            f"{candidate.description} (identified by an earlier enumeration pass from "
            f"anchor(s): {', '.join(candidate.anchors)})"
        )
        if candidate.context.get("pooling_evidence"):
            evidence = "; ".join(
                f"{e['factor']!r}: \"{e['excerpt']}\" (block {e['anchor']})" for e in candidate.context["pooling_evidence"]
            )
            context += (
                f" NOTE: this table's values are means POOLED over the factor(s) not shown in its rows/columns -- "
                f"{evidence}. Also report that pooling statement as a fact (field_name 'pooled_over', citing the "
                f"block above) so the record can state what the value was averaged over."
            )
        if candidate.context.get("temporal_context"):
            context += _temporal_extraction_note(candidate.context["temporal_context"])
        hints = {k: v for k, v in (("variable_name_hint", candidate.variable_name_hint), ("units_hint", candidate.units_hint)) if v}
        if hints:
            context += _hint_extraction_note(hints)
        if entity_type == "Management":
            verified, decisions = _verified_treatment_links(paper_id, candidate, this_run_records, link_blocks)
            link_decisions.extend(decisions)
            if verified:
                hints["treatment_link"] = {
                    "treatment_ids": [v["record_id"] for v in verified], "names": [v["name"] for v in verified],
                    "evidence_anchors": list(candidate.anchors),
                }
        result = run_record(
            run_id=run_id, paper_id=paper_id, entity_type=entity_type, record_id=record_id,
            model=model, client=client, invoke=invoke, enable_ai_validation=enable_ai_validation,
            known_refs=candidate_known_refs or None, extraction_context=context,
            known_value=candidate.known_value, candidate_context={**candidate.context, **hints} or None,
        )
        record_info = {
            "entity_type": entity_type, "record_id": record_id,
            "status": result.status, "detail": result.detail,
        }
        record_infos.append(record_info)
    if link_decisions:
        run_store.save_stage_attempt(run_id, f"{entity_type}__enumeration", "treatment_links", 1, {"decisions": link_decisions})
    return record_infos


def _temporal_extraction_note(temporal_context: list[dict]) -> str:
    """Appended to a table candidate's extraction context when the paper dates its time level(s): the
    date is NOT in the table block, so without this Extraction (which reads the candidate's anchor)
    never finds it and Conversion, which is sealed, can only mark temporal_info UNRESOLVED."""
    parts = []
    for entry in temporal_context:
        text = " ".join(t for t in (entry.get("date_text"), entry.get("year_text")) if t)
        where = f" at {entry['site']}" if entry.get("site") else ""
        parts.append(f"{entry['factor']}={entry['level']}{where} is dated {text!r} (block(s) {', '.join(entry['anchors'])})")
    return (
        " NOTE: the paper states when this value's time level was sampled -- " + "; ".join(parts) + ". Also report that "
        "date as a fact (field_name 'sampling_date', with the literal source text and citing those block(s)) so the "
        "record's temporal_info can be grounded. If the block does not actually say it, report nothing for it."
    )


def _payload_field_value(payload: dict, field: str) -> Optional[Any]:
    """The `value` of an ExtractedField in a committed payload, or None when absent or UNRESOLVED."""
    entry = (payload or {}).get(field)
    if isinstance(entry, dict) and entry.get("provenance_label") != "UNRESOLVED":
        return entry.get("value")
    return None


def _hint_consistency_flags(payload: dict, candidate_context: Optional[dict[str, Any]]) -> list[dict]:
    """Advisory flags on a committed Observation about its units and variable name. Never blocks, never edits the
    payload: what the source said stays what the record says, and a reviewer sees the disagreement.
      units_placeholder             -- reported_units is a placeholder such as 'unknown';
      units_differ_from_hint        -- the units the record carries are not the units hint (either may be the wrong one);
      variable_name_differs_from_hint -- the record's name is not the canonical one (same variable, another spelling)."""
    flags: list[dict] = []
    quantity = _payload_field_value(payload, "value")
    units = quantity.get("reported_units") if isinstance(quantity, dict) else None
    hints = candidate_context or {}
    if units is not None and _units_key(units) in {_units_key(p) for p in _PLACEHOLDER_UNITS}:
        flags.append({"flag": "units_placeholder", "reported_units": units})
    elif units and hints.get("units_hint") and _units_key(units) != _units_key(hints["units_hint"]):
        flags.append({"flag": "units_differ_from_hint", "reported_units": units, "units_hint": hints["units_hint"]})
    name = _payload_field_value(payload, "variable_name")
    if isinstance(name, str) and hints.get("variable_name_hint") and _normalize_for_matching(name) != _normalize_for_matching(hints["variable_name_hint"]):
        flags.append({"flag": "variable_name_differs_from_hint", "variable_name": name, "variable_name_hint": hints["variable_name_hint"]})
    return flags


def _hint_extraction_note(hints: dict[str, str]) -> str:
    """Appended to a table candidate's extraction context: the table reconstruction's UNVERIFIED variable-name and
    units hints. Extraction must still report what the SOURCE writes."""
    said = []
    if hints.get("variable_name_hint"):
        said.append(f"the measured variable is called {hints['variable_name_hint']!r}")
    if hints.get("units_hint"):
        said.append(f"its units are {hints['units_hint']!r}")
    return (
        " NOTE (unverified hints from the table reconstruction): " + " and ".join(said) + ". Report the variable name and "
        "the units as the SOURCE writes them (field_names 'variable_name' and 'reported_units', with the literal source "
        "text and anchors); where the source text differs from these hints, report the source's."
    )


def _conversion_prompt(
    paper_id: str, entity_type: str, record_id: str, raw_extraction: dict, prior_errors: Optional[list[dict]],
    known_refs: Optional[dict[str, str]] = None,
    candidate_context: Optional[dict[str, Any]] = None,
) -> str:
    parts = [
        f"Map the following sealed raw evidence into a Sage IR `{entity_type}` "
        f"record (record_id=`{record_id}`). Call get_schema first.",
        "",
        "RAW_EVIDENCE:",
        "```json",
        json.dumps(raw_extraction, indent=2),
        "```",
        "",
        "`source.page_number` is filled in by the pipeline from the document's own layout data: leave it out, and never "
        "mark a field UNRESOLVED because a page number is not known.",
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
    if entity_type == "Management":
        link = (candidate_context or {}).get("treatment_link")
        if link:
            parts += [
                "",
                "CANDIDATE_CONTEXT.treatment_link names the Treatment record id(s) whose condition the source text this "
                "event cites NAMES: "
                f"{link['treatment_ids']!r} ({link.get('names')!r}). Set `treatment_ids` to exactly that list, EXTRACTED, "
                "citing the anchor where the source names the condition, when RAW_EVIDENCE contains that statement; "
                "otherwise omit `treatment_ids`. Never add any other Treatment id.",
            ]
        else:
            parts += [
                "",
                "`treatment_ids`: this event is not established to belong to a specific Treatment -- omit `treatment_ids` "
                "(do NOT list every Treatment). It is linked only when the source text the event cites names the condition.",
            ]
    if candidate_context:
        # Sealed, orchestrator-authored hints from the deterministic table
        # reconstruction that produced this candidate (never model-authored,
        # never verified source text). Present ONLY for table candidates that
        # carry context, so every other conversion prompt is byte-identical.
        parts += [
            "",
            "CANDIDATE_CONTEXT -- deterministic hints from the table reconstruction that produced this "
            "candidate (NOT verified source text; use them only where RAW_EVIDENCE above supports them, and "
            "never as a substitute for it):",
            "```json",
            json.dumps(candidate_context, indent=2),
            "```",
        ]
        if candidate_context.get("variable_name_hint") or candidate_context.get("units_hint"):
            parts += [
                "",
                "CANDIDATE_CONTEXT may carry `variable_name_hint` and `units_hint` (unverified). variable_name: when "
                "RAW_EVIDENCE shows this record is that measured quantity, use `variable_name_hint` exactly, so the same "
                "variable carries the same name in every record (a naming judgment: label it INFERRED with a real basis "
                "unless the identical text is in RAW_EVIDENCE, then EXTRACTED). reported_units: take them from the source "
                "text in RAW_EVIDENCE; use `units_hint` only where it agrees with that text -- where they differ keep the "
                "source's units and say so in `unit_basis_notes`. Never write a placeholder such as 'unknown' or 'n/a': if "
                "RAW_EVIDENCE gives no units, the value is UNRESOLVED with a real reason.",
            ]
        if candidate_context.get("temporal_context"):
            parts += [
                "",
                "CANDIDATE_CONTEXT lists where the paper dates this value's time level. Set `temporal_info` from the "
                "`sampling_date` fact in RAW_EVIDENCE when it is there (cite that fact's anchor): call "
                "apply_reconstruction with kind `date_mapping` on the date text followed by the year text (e.g. "
                "\"9 June 1993\") and use its result. If RAW_EVIDENCE has no such fact, or the year is missing, "
                "temporal_info is UNRESOLVED with a real reason -- never compute or assume a date or a year.",
            ]
        if candidate_context.get("aggregated_over_factors"):
            parts += [
                "",
                "CANDIDATE_CONTEXT says this table's values are MEANS POOLED over "
                f"{candidate_context['aggregated_over_factors']!r}. Set `reported_effect_scope` to "
                "\"aggregated_mean\" (never \"treatment_mean\" for a pooled value) and `aggregated_over_factors` "
                "to that list -- EXTRACTED when RAW_EVIDENCE contains the pooling statement (cite its anchor), "
                "otherwise UNRESOLVED with a real reason. Do not invent a mixture- or factor-specific value.",
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
    a value outside this real, already-extracted set).

    A bare-reference field (e.g. `treatment_id`) is documented (converter.md's
    own "Hard rules") to always be a plain string, never the
    `{value, provenance_label, source}` wrapper shape -- but Conversion is an
    LLM and does not always follow that rule (real Daren-1997-Canopy crash,
    run 20260916T132735_dd8d7c47: `treatment_id` came back as an UNRESOLVED
    `ExtractedField`-shaped dict). `set(expected)` membership then tries to
    hash `candidate_value`, and a dict is unhashable -- an unhandled
    `TypeError: unhashable type: 'dict'` that crashed the entire run instead
    of being reported as the deterministic validation error it actually is.
    An unhashable candidate_value can never legitimately be a member of a
    set of known reference ids, so it is always correctly a mismatch."""
    if isinstance(expected, (list, set, tuple)):
        allowed = set(expected)
        if isinstance(candidate_value, list):
            return not any(_is_hashable(v) and v in allowed for v in candidate_value)
        return not _is_hashable(candidate_value) or candidate_value not in allowed
    if isinstance(candidate_value, list):
        return expected not in candidate_value
    return candidate_value != expected


def _is_hashable(value: Any) -> bool:
    """A dict (or anything else unhashable) can never legitimately be a
    known reference id, so callers treat it as "definitely not a member of
    the allowed set" rather than crashing when they try to hash it (real
    Daren-1997-Canopy crash, run 20260916T132735_dd8d7c47: Conversion
    returned `treatment_id` as an UNRESOLVED `ExtractedField`-shaped dict
    instead of the bare string converter.md requires, and `set` membership
    checking tried to hash it, raising an unhandled
    `TypeError: unhashable type: 'dict'` that crashed the whole run)."""
    try:
        hash(value)
    except TypeError:
        return False
    return True


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
    candidate_context: Optional[dict[str, Any]] = None,
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
        _conversion_prompt(paper_id, entity_type, record_id, raw_extraction, correction_errors, known_refs, candidate_context),
    )
    run_store.save_stage_attempt(run_id, record_key, "conversion", stage_counts["conversion"], conv_result.as_artifact())

    if conv_result.parsed_json is None:
        return {
            "ok": False, "payload": None,
            "errors": [{"field": None, "message": f"AI-Validator-triggered correction attempt: {conv_result.parse_error}"}],
        }

    corrected_payload = _apply_source_pages(paper_id, conv_result.parsed_json)

    ref_errors = [
        {"field": field, "message": _ref_expectation_message(field, expected, corrected_payload.get(field))}
        for field, expected in (known_refs or {}).items()
        if field in corrected_payload and _ref_mismatch(corrected_payload.get(field), expected)
    ] + _management_link_errors(entity_type, corrected_payload, candidate_context)
    if ref_errors:
        run_store.save_stage_attempt(
            run_id, record_key, "conversion_validation", stage_counts["conversion"],
            {"valid": False, "errors": ref_errors, "source": "orchestrator_known_refs_check"},
        )
        return {"ok": False, "payload": corrected_payload, "errors": ref_errors}

    propose_result = client.propose_record(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=corrected_payload,
        run_id=run_id,
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


def _raw_extraction_grounding_errors(paper_id: str, extraction: RawExtraction) -> list[dict]:
    """Phase 1.1 (table-enumeration design review, "raw evidence grounding
    gate"): every `RawFact.raw_text_excerpt` claims to be "the literal
    source text this value was read from" (raw_schema.py's own field
    description) -- until now, never re-verified against the paper's own
    content.md before this evidence was handed to the sealed Conversion
    stage. Only the FINAL IR value gets checked against block text
    (`validators.validate_provenance`, at /propose_record, post-Conversion)
    -- a subtly misquoted or fabricated excerpt could otherwise survive all
    the way to a committed record as long as Conversion's own derived
    value happened to still match the (real) cited anchor text some other
    way.

    Reuses `validate_provenance`'s own text-grounding primitive
    (`_value_supported_by_text` -- typographic-equivalence and whitespace-
    collapse aware, the same fixes that resolved the real Daren-1997-Canopy/
    Oceologia-1998 false-rejection cases) rather than a new, weaker
    substring check: an excerpt is exactly the kind of literal string value
    that function already handles. Deliberately never exempts a judgment
    field the way `validate_provenance` does for the final value (no
    `field_name` passed through) -- raw_text_excerpt is documented to
    always be literal quoted/checked text, never a semantic judgment, so
    that carve-out does not apply here.

    Returns a list of errors in the SAME `{"field", "message"}` shape every
    other extraction validation error already uses -- empty means fully
    grounded. A missing content.md (pipeline-level, not per-fact) is folded
    into that same list rather than raising, exactly like every other
    anchor-checking pass in this module (see run_enumeration/
    run_table_classification's own identical FileNotFoundError handling)."""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError as exc:
        return [{"field": None, "message": f"cannot verify raw evidence grounding: {exc}"}]

    errors: list[dict] = []
    for index, fact in enumerate(extraction.facts):
        cited_anchors = [a.strip("[]") for a in fact.anchors]
        missing = [a for a in cited_anchors if a not in blocks]
        if missing:
            errors.append({
                "field": f"facts[{index}].anchors",
                "message": f"fact {fact.field_name!r} cites anchor(s) {missing} which do not exist in "
                           f"content.md for paper '{paper_id}' -- never cite an anchor you did not actually read.",
            })
            continue

        # The cited blocks are read as one text in DOCUMENT order (never the order the model happened to
        # list them in), so a quote that runs from one cited block into the next passes.
        ordered_anchors = sorted(set(cited_anchors), key=content_reader._anchor_sort_key)
        anchor_text = "\n".join(blocks[a] for a in ordered_anchors)
        supported, why = _excerpt_supported(fact.raw_text_excerpt, anchor_text)
        if not supported:
            errors.append({
                "field": f"facts[{index}].raw_text_excerpt",
                "message": why or (
                    f"fact {fact.field_name!r}: raw_text_excerpt {fact.raw_text_excerpt!r} is not found "
                    f"(even accounting for whitespace/typographic differences) in the text of its own cited "
                    f"anchor(s) {cited_anchors} -- raw_text_excerpt must be the literal source text you "
                    f"actually read, never paraphrased, summarized, or recalled from memory."
                ),
            })
    return errors


# Item 13 (grounding): an excerpt may elide stretches of a longer passage with "..." / "…". Every
# stretch that IS quoted must be literal source text, in the order it is written, with no leniency per
# segment; an annotation such as "(header row)" or any invented wording is simply a segment that is not
# in the source, so it still fails. A literal excerpt (no ellipsis, or an ellipsis the source itself
# contains) is checked exactly as before.
_ELLIPSIS_RE = re.compile(r"\.\.\.|\u2026")
_MIN_SEGMENT_ALNUM = 3


def _segments_in_order(segments: list[str], text: str) -> Optional[int]:
    """Index of the first segment that is NOT found (in order, after the previous one) in `text`, or None when
    all are. Compared with the same normalization `_value_supported_by_text` applies; a segment may also match
    with all whitespace removed (source rendering of scientific notation), but every segment must then match
    that way together."""
    def normalize(t: str) -> str:
        return _normalize_typography(" ".join(t.split()).casefold())

    haystack = normalize(text)
    needles = [normalize(seg) for seg in segments]
    first_missing: Optional[int] = None
    for compact in (False, True):
        hay = re.sub(r"\s+", "", haystack) if compact else haystack
        position, missing = 0, None
        for i, needle in enumerate(needles):
            target = re.sub(r"\s+", "", needle) if compact else needle
            found = hay.find(target, position) if target else -1
            if found < 0:
                missing = i
                break
            position = found + len(target)
        if missing is None:
            return None
        first_missing = missing if first_missing is None else first_missing
    return first_missing


def _excerpt_supported(excerpt: str, text: str) -> tuple[bool, Optional[str]]:
    """(supported, error message) for a raw_text_excerpt against its cited text. Without an ellipsis this is
    exactly `_value_supported_by_text`. With one, the excerpt is split on it and EVERY segment must appear in
    order; a segment of fewer than `_MIN_SEGMENT_ALNUM` alphanumeric characters cannot ground anything on its
    own and is refused."""
    if _value_supported_by_text(excerpt, text):
        return True, None
    if not _ELLIPSIS_RE.search(excerpt):
        return False, None
    segments = [seg.strip() for seg in _ELLIPSIS_RE.split(excerpt) if seg.strip()]
    if not segments:
        return False, f"raw_text_excerpt {excerpt!r} quotes nothing (only an ellipsis)."
    for seg in segments:
        if sum(ch.isalnum() for ch in seg) < _MIN_SEGMENT_ALNUM:
            return False, (f"raw_text_excerpt {excerpt!r}: the segment {seg!r} is too short to ground anything; quote "
                           f"each stretch you keep in full, with the ellipsis only where text is left out.")
    missing = _segments_in_order(segments, text)
    if missing is None:
        return True, None
    return False, (
        f"raw_text_excerpt {excerpt!r}: segment {missing + 1} ({segments[missing]!r}) is not found, in the order "
        f"written, in the cited text (every stretch around an ellipsis must be literal source text, in order -- "
        f"never paraphrased, annotated or re-ordered)."
    )


def _extraction_matches_known_value(known_value: str, extraction: RawExtraction) -> bool:
    """Phase 1.3 (table-enumeration design review, Section 4:
    "extraction vs. known table value"): for an Observation candidate
    seeded from Step C's own deterministic table reconstruction, the
    expected reported value is already known beforehand -- it's the exact
    cell text Step C cross-producted this candidate from (see
    `_table_classification_to_candidates`'s `known_value`). If Extraction
    comes back having read the candidate's own cited anchor(s) but reports
    a value that doesn't match this known-correct cell text anywhere, that
    is strong evidence Extraction attributed a DIFFERENT row/column's value
    to this candidate -- exactly the "wrong-cell attribution" failure class
    ordinary provenance validation cannot detect on its own (a wrong-but-
    real value from the SAME table still passes a literal text-in-anchor
    check).

    Deliberately permissive about WHICH fact carries the value: Extraction
    is sealed from the IR schema and free to name its own fields (no
    `field_name` naming convention is enforced), so this checks every
    fact's `raw_value` AND `raw_text_excerpt` for the known value, rather
    than assuming one specific field_name."""
    return any(_fact_bears_value(known_value, fact) for fact in extraction.facts)


def _fact_bears_value(known_value: str, fact: Any) -> bool:
    """Does this fact's raw_value or raw_text_excerpt contain the known table value? (The per-fact test behind
    `_extraction_matches_known_value`.)"""
    if fact.raw_value is not None and _value_supported_by_text(known_value, fact.raw_value):
        return True
    return _value_supported_by_text(known_value, fact.raw_text_excerpt)


def _drop_ungrounded_auxiliary_facts(
    extraction: RawExtraction, known_value: str, grounding_errors: list[dict],
) -> tuple[RawExtraction, list[dict], list[dict]]:
    """Item 13 (decision Q7), TABLE CANDIDATES ONLY (the caller passes `known_value` only for them): an
    UNGROUNDED AUXILIARY fact -- a unit, method, date or note whose excerpt is not in its cited text -- is removed
    from the raw extraction and logged, rather than costing the whole attempt. Returns (extraction, remaining
    errors, dropped-fact log). Nothing is dropped unless ALL of these hold, otherwise the errors stand untouched:
      - every grounding error belongs to one specific fact (no pipeline-level error is hidden);
      - some fact carries the known table value AND is itself grounded -- the value-bearing fact must be real;
      - no ungrounded fact carries the known value (an ungrounded value-bearing fact stays an extraction error).
    A dropped fact never reaches Conversion; it is recorded as `dropped_ungrounded_facts`. The grounding CHECK is
    unchanged, so annotations and fabricated excerpts are still ungrounded -- they are dropped, never accepted."""
    bad: dict[int, list[str]] = {}
    for error in grounding_errors:
        match = re.match(r"facts\[(\d+)\]", error.get("field") or "")
        if match is None:
            return extraction, grounding_errors, []
        bad.setdefault(int(match.group(1)), []).append(error["message"])
    if not bad:
        return extraction, grounding_errors, []
    value_bearing = {i for i, fact in enumerate(extraction.facts) if _fact_bears_value(known_value, fact)}
    if not (value_bearing - set(bad)) or (value_bearing & set(bad)):
        return extraction, grounding_errors, []
    dropped = [
        {"field_name": extraction.facts[i].field_name, "raw_text_excerpt": extraction.facts[i].raw_text_excerpt,
         "anchors": list(extraction.facts[i].anchors), "errors": bad[i]}
        for i in sorted(bad)
    ]
    kept = [fact for i, fact in enumerate(extraction.facts) if i not in bad]
    return extraction.model_copy(update={"facts": kept}), [], dropped


# --- Correction pass, Fix 3: ungrounded AUXILIARY facts / fields (non-table candidates) -----------------------------
#
# Real evidence. Replaying the stored extractions of every record that ended in error in the Daren and Felipe runs
# (tests/fixtures/pass2/replay_extractions.json): in each, the LAST attempt had the identity-bearing fact grounded and
# only auxiliary facts ungrounded -- `definition`, `measurement_units`, `unit`, `effect_size_percent`, a Study's design
# descriptions -- plus two facts with no anchors at all. One paraphrased definition cost the paper its Leaf-area-index
# Variable. Item 13 already drops such a fact for TABLE candidates (where the known cell value proves the value fact is
# real); this generalises it, conservatively, to the other entity types.
#
# The rule is by field NAME per entity type, from the IR schema and protocol -- not "optional means droppable":
# an ungrounded fact is droppable only if it is NOT named like an identity/value-bearing field of its entity type.
# Facts named like one (a Variable's `name`/`variable_name`, a Treatment's `definition`, an event's `date`, ...) are
# never dropped: their failure stands as an error. And nothing is dropped unless a grounded fact remains.
# Observation is excluded: its value-bearing facts have no fixed names outside the table path (`known_value`).
CORE_FACT_FIELDS: dict[str, frozenset[str]] = {
    "Citation": frozenset({"title", "author", "authors", "year", "persistent_identifier", "doi", "pid"}),
    "Site": frozenset({"name", "site_name", "site", "latitude", "longitude"}),
    "Species": frozenset({"scientific_name", "genus", "species_epithet", "species", "name"}),
    "Crop": frozenset({"cultivar", "cultivar_name", "variety", "name", "population", "crop", "species"}),
    "Method": frozenset({"name", "method_name", "description", "method_description", "method"}),
    "Variable": frozenset({"name", "variable_name", "variable", "label"}),
    "Treatment": frozenset({"name", "treatment_name", "treatment", "definition", "treatment_definition"}),
    "TreatmentPair": frozenset({"comparison_factor", "comparison_label", "treatment_1", "treatment_2",
                                "treatment_id_1", "treatment_id_2"}),
    "Management": frozenset({"event_type", "event", "date", "event_date", "date_range", "timing", "amount", "rate"}),
    "Study": frozenset(),      # no ExtractedField at all in the IR: nothing an ungrounded fact could leak into
    "Coverage": frozenset(),   # plain (non-extracted) fields only
}
_NO_AUXILIARY_DROP_ENTITY_TYPES = frozenset({"Observation"})


def _fact_field_key(field_name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(field_name or "").strip().lower()).strip("_")


def _is_droppable_fact_name(entity_type: str, field_name: Any) -> bool:
    """A fact may be dropped for want of grounding only when it has a name and that name is not one of the entity
    type's identity/value-bearing names."""
    key = _fact_field_key(field_name)
    return bool(key) and key not in CORE_FACT_FIELDS.get(entity_type, frozenset())


def _is_droppable_fact(entity_type: str, fact: Any) -> bool:
    """`_is_droppable_fact_name`, plus: a fact that reports NO value (`raw_value` null or blank -- "I looked and the
    field is not stated") is droppable under any usable name. The core-name protection exists so an ungrounded VALUE
    never reaches an identity field; a fact with no value has none to leak, and Conversion marks an absent field
    UNRESOLVED on its own. Takes a RawFact or a raw dict (the shape-recovery path)."""
    name, raw_value = (fact.get("field_name"), fact.get("raw_value")) if isinstance(fact, dict) else (fact.field_name, fact.raw_value)
    if _is_droppable_fact_name(entity_type, name):
        return True
    return bool(_fact_field_key(name)) and (raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()))


def _drop_ungrounded_noncore_facts(
    entity_type: str, extraction: RawExtraction, grounding_errors: list[dict],
) -> tuple[RawExtraction, list[dict], list[dict]]:
    """Returns (extraction, remaining errors, dropped-fact log). Nothing is dropped, and the errors stand untouched,
    unless ALL of these hold:
      - the entity type is eligible (not Observation);
      - every grounding error belongs to one specific fact (a pipeline-level error is never hidden);
      - no ungrounded fact is named like an identity/value-bearing field of this entity type, unless it carries no
        value at all (`raw_value` null or blank -- see `_is_droppable_fact`);
      - at least one grounded fact remains.
    A dropped fact never reaches Conversion; it is logged with its excerpt, anchors and the reason. The grounding
    CHECK itself is unchanged."""
    if entity_type in _NO_AUXILIARY_DROP_ENTITY_TYPES:
        return extraction, grounding_errors, []
    bad: dict[int, list[str]] = {}
    for error in grounding_errors:
        match = re.match(r"facts\[(\d+)\]", error.get("field") or "")
        if match is None:
            return extraction, grounding_errors, []
        bad.setdefault(int(match.group(1)), []).append(error["message"])
    if not bad or len(bad) >= len(extraction.facts):
        return extraction, grounding_errors, []
    if not all(_is_droppable_fact(entity_type, extraction.facts[i]) for i in bad):
        return extraction, grounding_errors, []
    dropped = [
        {"field_name": extraction.facts[i].field_name, "raw_text_excerpt": extraction.facts[i].raw_text_excerpt,
         "anchors": list(extraction.facts[i].anchors), "errors": bad[i], "rule": "ungrounded_auxiliary_fact"}
        for i in sorted(bad)
    ]
    kept = [fact for i, fact in enumerate(extraction.facts) if i not in bad]
    return extraction.model_copy(update={"facts": kept}), [], dropped


def _recover_extraction_shape(parsed: dict, entity_type: str) -> Optional[tuple[RawExtraction, list[dict]]]:
    """A RawExtraction whose only shape problem is auxiliary facts that FAIL RawFact validation (real cases: Felipe
    shoot_biomass `unit` and Daren/Felipe Study facts with no anchors) is recovered by treating those facts as
    ungrounded -- same rule, same log -- so one anchor-less side fact does not cost the whole record. None when
    anything else is wrong: a top-level key, a fact with no usable name or a valued identity-bearing name, no good fact
    left."""
    if entity_type in _NO_AUXILIARY_DROP_ENTITY_TYPES:
        return None
    facts = parsed.get("facts")
    if not isinstance(facts, list):
        return None
    good: list[Any] = []
    bad: list[tuple[Any, str]] = []
    for fact in facts:
        try:
            RawFact.model_validate(fact)
            good.append(fact)
        except ValidationError as exc:
            bad.append((fact, "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())))
    if not bad or not good:
        return None
    if not all(isinstance(fact, dict) and _is_droppable_fact(entity_type, fact) for fact, _ in bad):
        return None
    try:
        extraction = RawExtraction.model_validate({**parsed, "facts": good})
    except ValidationError:
        return None
    dropped = [
        {"field_name": fact.get("field_name"), "raw_text_excerpt": fact.get("raw_text_excerpt"),
         "anchors": list(fact.get("anchors") or []), "errors": [why], "rule": "ungrounded_auxiliary_fact"}
        for fact, why in bad
    ]
    return extraction, dropped


# Conversion side of the same principle. Real cases: Felipe's mustard-CO2 Observation (its optional `notes` failed
# grounding) and Daren's internode-length Variable (`notes` = "Name of the variable as described in the methods.").
# Only DESCRIPTIVE optional fields are listed: never an identity (name, cultivar, event_type), never a value or a
# statistic, never a reference. A payload is demoted only when EVERY validation error sits inside these fields, so the
# re-validated remainder (identity and value included) is proven grounded by the same deterministic validator.
AUXILIARY_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "Variable": frozenset({"description", "units", "notes"}),
    "Site": frozenset({"description", "soil_context", "nearest_city"}),
    "Crop": frozenset({"common_name", "notes"}),
    "Species": frozenset({"common_name"}),
    "Observation": frozenset({"notes", "replicate_id"}),
}


def _apply_source_pages(paper_id: str, payload: Any) -> Any:
    """A copy of a Conversion payload in which every ExtractedField's `source.page_number` is the 1-indexed PDF page of
    its FIRST cited block that provenance.json can place, or None when none can (no provenance, unknown anchor) --
    whatever the model wrote is overwritten. The model has no page data at all (a RawExtraction carries none), so its
    numbers were inventions: in the Daren and Felipe runs 160 of 160 values were 1 or 0 while the cited blocks sit on
    pages 2-6, and two records were made hollow by "page number not available". Deterministic, downstream, no
    Marker involvement."""
    payload = copy.deepcopy(payload)
    provenance = content_reader._load_provenance(paper_id, _papers_root()) or {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            source = node.get("source")
            if "provenance_label" in node and isinstance(source, dict) and isinstance(source.get("locators"), list):
                page = None
                for locator in source["locators"]:
                    anchor = locator.get("block_anchor") if isinstance(locator, dict) else None
                    if anchor:
                        entry = provenance.get(content_reader._normalize_anchor(anchor)) or {}
                        page = content_reader.physical_page(entry.get("page_id"))
                        if page is not None:
                            break
                source["page_number"] = page
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return payload


def _demote_auxiliary_payload_fields(
    entity_type: str, payload: dict, errors: list[dict],
) -> Optional[tuple[dict, list[dict]]]:
    """(payload without the failing auxiliary fields, demotion log), or None when the errors are not exclusively
    about auxiliary optional fields of this entity type (then the record fails or retries exactly as before)."""
    allowed = AUXILIARY_PAYLOAD_FIELDS.get(entity_type)
    if not allowed or not errors or not isinstance(payload, dict):
        return None
    bad: dict[str, list[str]] = {}
    for error in errors:
        path = _error_field(error)
        top = re.split(r"[.\[]", path)[0] if path else None
        if top not in allowed or top not in payload:
            return None
        bad.setdefault(top, []).append(error.get("message") or "")
    demoted = []
    for name in sorted(bad):
        entry = payload[name]
        demoted.append({
            "field": name, "value": entry.get("value") if isinstance(entry, dict) else entry, "errors": bad[name],
            "rule": "ungrounded_auxiliary_field",
        })
    return {k: v for k, v in payload.items() if k not in bad}, demoted


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
    known_value: Optional[str] = None,
    candidate_context: Optional[dict[str, Any]] = None,
) -> RecordResult:
    record_key = f"{entity_type}__{record_id}"
    stage_counts = {"extraction": 0, "conversion": 0, "ai_validation": 0}

    # --- Stage 1: Extraction (sealed evidence gathering) ---
    raw_extraction: Optional[dict] = None
    dropped_ungrounded_facts: list[dict] = []
    extraction_errors: list[dict] = []
    last_extraction_message = "extraction stage never produced a valid RawExtraction"
    # Tracks whether EVERY failed attempt was specifically one of the two
    # provider-level infrastructure-noise classes (invoke_agent's own
    # internal retry already absorbed most transient occurrences of
    # either; this catches the case where it was exhausted on every single
    # numbered attempt too) -- set False the moment any attempt fails for a
    # REAL content/shape reason instead, so a candidate that failed for a
    # genuine reason is never mislabeled. Two independent flags, not one:
    # `saw_genuine_content_failure` wins outright the moment ANY attempt
    # fails for a real reason (a shape-validation error, an empty facts
    # array, or real-but-malformed-JSON final text) -- that must rule out
    # BOTH infrastructure-noise labels, regardless of what the other
    # attempts looked like. `any_malformed_tool_call_failure` then takes
    # priority over the plain empty-response label whenever it fired on
    # ANY attempt (not requiring every attempt to match it) -- it is the
    # more specific, more actionable diagnostic signal, and mixing it with
    # plain-empty attempts (no genuine content failure either way) is still
    # "all infrastructure noise", just not uniformly the same sub-class.
    saw_genuine_content_failure = False
    any_malformed_tool_call_failure = False
    # Provider failures (empty final text, timeout, harmony leak) spend the SEPARATE provider budget, never one of
    # the numbered attempts (correction pass, Fix 2): `numbered` counts the answers the model actually gave.
    provider = _ProviderBudget(run_id, record_key, "extraction")
    numbered = 0
    attempt = 0  # every invocation round: the artifact index
    provider_terminal = False
    while numbered < MAX_EXTRACTION_ATTEMPTS:
        attempt += 1
        stage_counts["extraction"] = attempt
        result = invoke(
            "extractor", model,
            _extraction_prompt(paper_id, entity_type, record_id, extraction_errors, extraction_context),
        )
        artifact = result.as_artifact()

        failure = _provider_failure(result)
        if failure:
            # No answer to correct: the model gets no feedback, the next round repeats the same prompt.
            if result.had_malformed_tool_call:
                any_malformed_tool_call_failure = True
            artifact["validation_errors"] = [{"field": None, "message": result.parse_error}]
            artifact.update(failure_class=failure, failure_kind="provider", numbered_attempt=None)
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"round {attempt}: provider failure ({failure}): {result.parse_error}"
            if provider.failed(failure):
                provider_terminal = True
                break
            provider.cooldown()
            continue
        numbered += 1

        if result.parsed_json is None:
            saw_genuine_content_failure = True  # text came back but is not JSON: the model's own answer
            extraction_errors = [{"field": None, "message": result.parse_error}]
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: {result.parse_error}"
            continue

        # Deterministic shape check -- a loose "did it have a facts key"
        # check is not enough: RawFact's own invariants (anchors required,
        # etc.) must actually hold before this evidence is handed to the
        # sealed Conversion stage.
        shape_dropped: list[dict] = []
        try:
            validated = RawExtraction.model_validate(result.parsed_json)
        except ValidationError as exc:
            # Auxiliary facts that fail RawFact validation (no anchors) are treated as ungrounded auxiliary facts
            # (Fix 3); anything else wrong with the shape is a genuine failure, as before.
            recovered = _recover_extraction_shape(result.parsed_json, entity_type) if known_value is None else None
            if recovered is None:
                saw_genuine_content_failure = True
                extraction_errors = [
                    {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()
                ]
                artifact["validation_errors"] = extraction_errors
                run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
                last_extraction_message = f"attempt {attempt}: RawExtraction shape validation failed: {extraction_errors}"
                continue
            validated, shape_dropped = recovered

        if not validated.facts:
            saw_genuine_content_failure = True
            extraction_errors = [{"field": "facts", "message": "facts array was empty -- report at least one fact or explain in extraction_notes"}]
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: empty facts array"
            continue

        # Phase 1.1: raw evidence grounding gate -- see
        # _raw_extraction_grounding_errors's own docstring.
        grounding_errors = _raw_extraction_grounding_errors(paper_id, validated)
        if grounding_errors and known_value is not None:
            validated, grounding_errors, dropped = _drop_ungrounded_auxiliary_facts(validated, known_value, grounding_errors)
            if dropped:
                artifact["dropped_ungrounded_facts"] = dropped
        elif grounding_errors:
            validated, grounding_errors, dropped = _drop_ungrounded_noncore_facts(entity_type, validated, grounding_errors)
            shape_dropped = shape_dropped + dropped
        if shape_dropped:
            artifact["dropped_ungrounded_facts"] = shape_dropped + artifact.get("dropped_ungrounded_facts", [])
        if grounding_errors:
            saw_genuine_content_failure = True
            extraction_errors = grounding_errors
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: raw evidence grounding failed: {extraction_errors}"
            continue

        # Phase 1.3: extraction-vs-known-table-value cross-check -- only
        # set for a table-enumeration Observation candidate (see
        # _table_classification_to_candidates); a no-op (known_value is
        # None) for every other candidate.
        if known_value is not None and not _extraction_matches_known_value(known_value, validated):
            saw_genuine_content_failure = True
            extraction_errors = [{
                "field": None,
                "message": (
                    f"This candidate was identified from a table cell whose reported value is "
                    f"already known to be {known_value!r} (from the earlier deterministic table "
                    f"reconstruction) -- none of your reported facts' raw_value/raw_text_excerpt "
                    f"contain this value. Re-read the cited anchor(s) and confirm you are reporting "
                    f"THIS candidate's own specific cell, not a different row/column's value."
                ),
            }]
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: known-value cross-check failed (expected {known_value!r})"
            continue

        artifact["validation_errors"] = []
        run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
        raw_extraction = validated.model_dump()
        dropped_ungrounded_facts = artifact.get("dropped_ungrounded_facts", [])
        break

    if provider.rounds:
        stage_counts["provider_failure_rounds"] = provider.rounds
    if raw_extraction is None:
        if saw_genuine_content_failure:
            failure_class = None
        elif any_malformed_tool_call_failure:
            failure_class = "provider_malformed_response"
        else:
            failure_class = "provider_empty_response"
        return _finalize_error(
            run_id, record_key, entity_type, record_id, last_extraction_message, stage_counts, failure_class=failure_class,
            extra=provider.disclosure(numbered, provider_terminal) if provider.rounds else None,
        )

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
    demoted_optional_fields: list[dict] = []
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
            _conversion_prompt(paper_id, entity_type, record_id, raw_extraction, last_errors, known_refs, candidate_context),
        )
        run_store.save_stage_attempt(run_id, record_key, "conversion", attempt, conv_result.as_artifact())

        if conv_result.parsed_json is None:
            last_errors = [{"field": None, "message": f"conversion attempt {attempt}: {conv_result.parse_error}"}]
            continue

        candidate_payload = _apply_source_pages(paper_id, conv_result.parsed_json)

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
        ] + _management_link_errors(entity_type, candidate_payload, candidate_context)
        if ref_errors:
            run_store.save_stage_attempt(
                run_id, record_key, "conversion_validation", attempt,
                {"valid": False, "errors": ref_errors, "source": "orchestrator_known_refs_check"},
            )
            last_errors = ref_errors
            continue

        propose_result = client.propose_record(
            paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=candidate_payload,
            run_id=run_id,
        )
        run_store.save_stage_attempt(run_id, record_key, "conversion_validation", attempt, propose_result)

        if propose_result.get("valid"):
            break
        last_errors = propose_result.get("errors", [])
        if propose_result.get("forced_flag_unresolved"):
            break
        # Fix 3 (conversion side): when EVERY error is about a descriptive optional field, that field is demoted
        # (removed) and logged, and the remainder is re-proposed -- the deterministic validator then proves identity
        # and value grounded. Nothing else about the retry behaviour changes.
        demotion = _demote_auxiliary_payload_fields(entity_type, candidate_payload, last_errors)
        if demotion is not None:
            reduced_payload, demoted = demotion
            reproposal = client.propose_record(
                paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=reduced_payload, run_id=run_id,
            )
            run_store.save_stage_attempt(
                run_id, record_key, "conversion_demotion", attempt, {"demoted": demoted, "reproposal": reproposal},
            )
            if reproposal.get("valid"):
                candidate_payload, propose_result = reduced_payload, reproposal
                demoted_optional_fields.extend(demoted)
                break

    if not propose_result or not propose_result.get("valid"):
        return _finalize_unresolved(
            run_id, client, paper_id, entity_type, record_id, record_key,
            raw_extraction, candidate_payload, last_errors, stage_counts,
        )

    # --- Readiness (Item 15) ---
    # Structurally valid is not the same as READY: an Observation whose `value` or `variable_name` is UNRESOLVED has no
    # measurement to speak of. Such a record is committed as `unresolved` with its payload KEPT (the ir-service rejects
    # it as `ready`), and no AI-validation call is spent on it. This is not a failure of the extraction pipeline: the
    # source may genuinely not give a value, and the reason the extraction stated is preserved.
    if propose_result.get("ready") is False:
        return _finalize_not_ready(
            run_id, client, paper_id, entity_type, record_id, record_key, candidate_payload,
            propose_result.get("readiness_issues") or [], stage_counts, dropped_ungrounded_facts,
            demoted_optional_fields,
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
                stage_counts=stage_counts, candidate_context=candidate_context,
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
    hint_flags = _hint_consistency_flags(candidate_payload, candidate_context)
    if hint_flags:
        final_record["hint_flags"] = hint_flags
    if dropped_ungrounded_facts:
        final_record["dropped_ungrounded_facts"] = dropped_ungrounded_facts
    if demoted_optional_fields:
        final_record["demoted_optional_fields"] = demoted_optional_fields
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": outcome,
        "attempts": stage_counts,
        **({"dropped_ungrounded_facts": len(dropped_ungrounded_facts)} if dropped_ungrounded_facts else {}),
        **({"demoted_optional_fields": [d["field"] for d in demoted_optional_fields]} if demoted_optional_fields else {}),
    })
    return RecordResult(status=outcome, entity_type=entity_type, record_id=record_id, detail=final_record)


def _finalize_not_ready(
    run_id: str, client: IRServiceClient, paper_id: str, entity_type: str, record_id: str, record_key: str,
    candidate_payload: dict, readiness: list[dict], stage_counts: dict, dropped_ungrounded_facts: list[dict],
    demoted_optional_fields: Optional[list[dict]] = None,
) -> RecordResult:
    """Commit a valid-but-not-ready record as `unresolved`, payload kept (the same result shape as
    `_finalize_unresolved`, so results and the review UI read it unchanged), with the readiness issues as the reason."""
    commit_result = client.commit_record(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id,
        payload=candidate_payload, status="unresolved",
        run_metadata={"run_id": run_id, "schema_version": schema_fingerprint(), "readiness_issues": readiness},
    )
    final_record = {
        "status": "unresolved",
        "paper_id": paper_id, "entity_type": entity_type, "record_id": record_id,
        "payload": candidate_payload,
        "last_candidate_payload": candidate_payload,
        "last_errors": [{"field": issue.get("code"), "message": issue.get("message")} for issue in readiness],
        "readiness_issues": readiness,
        "commit_result": commit_result,
    }
    if dropped_ungrounded_facts:
        final_record["dropped_ungrounded_facts"] = dropped_ungrounded_facts
    if demoted_optional_fields:
        final_record["demoted_optional_fields"] = demoted_optional_fields
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": "unresolved", "attempts": stage_counts,
        "not_ready": [issue.get("code") for issue in readiness],
    })
    return RecordResult(status="unresolved", entity_type=entity_type, record_id=record_id, detail=final_record)


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
    failure_class: Optional[str] = None, extra: Optional[dict] = None,
) -> RecordResult:
    """A pipeline-level failure with no ir_service call to make (e.g. the
    Extraction stage never returned parseable evidence at all). Still writes
    a final artifact -- 'failures must never silently disappear' applies
    here too, not just to model/validation failures.

    `failure_class` is an explicit, additive classification ("provider_empty_response"
    or "provider_malformed_response" -- see MAX_EMPTY_RESPONSE_RETRIES's and
    _has_malformed_harmony_tool_call's own docstrings) for when EVERY failed
    attempt was infrastructure noise
    rather than a genuine content/shape problem -- never changes the
    terminal status (still "error": the pipeline genuinely produced no
    usable candidate payload either way, capture-first requires that stay
    disclosed as a real failure, not silently become "ready" or bypass
    deterministic validation), only makes WHY it failed easier to find
    without reading the full attempt history."""
    final_record = {"status": "error", "entity_type": entity_type, "record_id": record_id, "message": message}
    if failure_class:
        final_record["failure_class"] = failure_class
    if extra:  # whose failure it was: `failure_kind` provider vs extraction, the classes and the budgets spent
        final_record.update(extra)
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": "error", "attempts": stage_counts,
        **({"failure_class": failure_class} if failure_class else {}),
        **(extra or {}),
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


def _store_entries(paper_id: str, run_id: Optional[str] = None) -> list[dict]:
    """ir-store entries for one paper, optionally restricted to the entries
    one run committed/flagged (each entry carries its `run_id` from
    run_metadata). With no `run_id` this is every entry, exactly as before --
    ir-store is a shared, append-only ledger across runs, so a caller that
    wants ONE run's view (run isolation) must ask for it explicitly."""
    entries = store.read_all(paper_id)
    if run_id is not None:
        entries = [e for e in entries if e.get("run_id") == run_id]
    return entries


def build_dataset_from_store(
    paper_id: str, dataset_id: Optional[str] = None, run_id: Optional[str] = None,
) -> tuple[dict, list[str]]:
    """Assemble an IRDataset-shaped dict from ir-store's LATEST entry per
    (entity_type, record_id) for one paper -- store.py's own definition of
    "current state" ("a record's current status is whatever the last line
    for that record_key says"). Only status=="ready" entries join the
    graph; "unresolved" entries are real, disclosed evidence of an
    incomplete record and are reported separately rather than silently
    folded in as if they validated. Returns (dataset_dict, skipped_keys).
    """
    entries = _store_entries(paper_id, run_id)
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


def _citation_record_id_conflicts(paper_id: str, run_id: Optional[str] = None) -> list[str]:
    """Citation is documented as 1:1 with 'the paper currently being
    curated' (extractor.md/AGENTS.md) -- unlike Treatment/Observation/etc,
    which legitimately have many record_ids per paper. More than one
    distinct Citation record_id for the same paper_id is a naming/hygiene
    problem (typically: earlier ad hoc testing used an inconsistent
    record_id before `record_id == paper_id` became the convention), not a
    normal outcome. A bare Pydantic "citations.0.persistent_identifier:
    Input should be a valid dictionary" is technically correct but
    unhelpful on its own -- this turns it into an actionable diagnostic."""
    record_ids = sorted({e["record_id"] for e in _store_entries(paper_id, run_id) if e["entity_type"] == "Citation"})
    return record_ids if len(record_ids) > 1 else []


def graph_check(paper_id: str, run_id: Optional[str] = None) -> dict:
    """Deterministic whole-paper audit: does everything currently marked
    'ready' in ir-store for this paper actually form a consistent
    IRDataset per validators.validate_dataset (Table 19)? Read-only --
    never commits, flags, or mutates anything."""
    dataset_dict, skipped = build_dataset_from_store(paper_id, run_id=run_id)
    citation_conflicts = _citation_record_id_conflicts(paper_id, run_id)
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

# Item 14: links that are NOT bare-reference dependencies. Management.treatment_ids is an OPTIONAL list of
# Treatment ids (protocol Section 9.3: the events define each treatment), so Treatment must be extracted first --
# but it is deliberately NOT an ENTITY_DEPENDENCIES entry: that table feeds `known_refs`, which would bind the field
# to the only ready Treatment (or hand Conversion an allowed set to pick from). Here the field is offered to
# enumeration as a link pool and admitted only for a Treatment whose name the event's own cited text states
# (`_verified_treatment_links`); otherwise it stays None.
OPTIONAL_LINKS: dict[str, list[tuple[str, str]]] = {
    "Management": [("treatment_ids", "Treatment")],
}


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
    for et, links in OPTIONAL_LINKS.items():  # order only: an optional link's target is extracted first
        prereqs[et] |= {prereq_type for _, prereq_type in links}
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
            description_field = payload.get("description")
            description = description_field.get("value") if isinstance(description_field, dict) else None
            items.append({
                "slug": _candidate_slug_from_record_id(paper_id, prereq_type, record_id),
                "record_id": record_id, "name": name, "description": description,
            })
        pools[field] = items
    for field, prereq_type in OPTIONAL_LINKS.get(entity_type, []):
        ready = _ready_records(this_run_records, prereq_type)
        if ready:  # an optional link is offered even when only ONE record exists -- it is never assumed
            pools[field] = [
                {"slug": _candidate_slug_from_record_id(paper_id, prereq_type, r["record_id"]), "record_id": r["record_id"],
                 "name": _payload_field_value(((r.get("detail") or {}).get("payload")) or {}, "name"), "description": None}
                for r in ready
            ]
    return pools


def _verified_treatment_links(
    paper_id: str, candidate: EnumerationCandidate, this_run_records: dict, blocks: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Which of a Management candidate's `treatment_ids` links are actually ESTABLISHED, plus one decision per
    proposed slug. The model may propose links, but nothing is trusted (decision: never link every event to every
    treatment). A link stands only when
      - the slug is a READY Treatment of this run, and
      - the Treatment's name appears as a whole phrase BOTH in the event's own description (so the event is about
        that condition, not merely cited from a block that mentions it in passing -- a site-wide operation such as
        land leveling stays unlinked even when its block also names a treatment) AND in the text of the blocks it
        cites (so the link rests on what the source says, never on plausibility).
    Anything else is dropped and logged; an event with no verified link keeps `treatment_ids=None`."""
    proposed = (candidate.linked_candidates or {}).get("treatment_ids")
    if not proposed:
        return [], []
    slugs = list(dict.fromkeys([proposed] if isinstance(proposed, str) else proposed))
    ready = {r["record_id"]: r for r in _ready_records(this_run_records, "Treatment")}
    text = " " + _normalize_for_matching(" ".join(blocks.get(a.strip("[]"), "") for a in candidate.anchors)) + " "
    described = " " + _normalize_for_matching(candidate.description) + " "
    verified: list[dict] = []
    decisions: list[dict] = []
    for slug in slugs:
        record_id = f"{paper_id}_treatment_{_sanitize_candidate_id(slug)}"
        base = {"candidate_id": candidate.candidate_id, "slug": slug, "record_id": record_id}
        record = ready.get(record_id)
        if record is None:
            decisions.append({**base, "decision": "dropped", "reason": "not a ready Treatment of this run"})
            continue
        name = _payload_field_value(((record.get("detail") or {}).get("payload")) or {}, "name")
        key = _normalize_for_matching(name) if isinstance(name, str) else ""
        if len(key) < 3 or f" {key} " not in text:
            decisions.append({**base, "decision": "dropped", "name": name,
                              "reason": "the event's cited text does not name this condition"})
            continue
        if f" {key} " not in described:
            decisions.append({**base, "decision": "dropped", "name": name,
                              "reason": "the event's own description does not name this condition (it is cited text that mentions it in passing)"})
            continue
        verified.append({"record_id": record_id, "name": name})
        decisions.append({**base, "decision": "linked", "name": name})
    return verified, decisions


def _management_link_errors(entity_type: str, payload: dict, candidate_context: Optional[dict[str, Any]]) -> list[dict]:
    """Deterministic guard on Conversion's output: a Management record's `treatment_ids` may only name Treatments the
    orchestrator verified for THIS event (`_verified_treatment_links`, carried in `treatment_link`). Any other id --
    e.g. Conversion linking an event to every Treatment -- is an error; no link at all (absent or UNRESOLVED) is
    always fine."""
    if entity_type != "Management":
        return []
    entry = (payload or {}).get("treatment_ids")
    if isinstance(entry, dict):
        if entry.get("provenance_label") == "UNRESOLVED":
            return []
        ids = entry.get("value")
    else:
        ids = entry
    if not ids:
        return []
    ids = ids if isinstance(ids, list) else [ids]
    allowed = set(((candidate_context or {}).get("treatment_link") or {}).get("treatment_ids") or [])
    extra = [i for i in ids if not (_is_hashable(i) and i in allowed)]
    if not extra:
        return []
    return [{
        "field": "treatment_ids",
        "message": (
            f"treatment_ids {extra} is not established for this event: a Management event is linked to a Treatment only "
            f"when its own cited source text names that condition (verified ids: {sorted(allowed) or 'none'}). Omit "
            f"treatment_ids (or mark it UNRESOLVED with a reason) rather than link every treatment."
        ),
    }]


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
    manifest_extra: Optional[dict] = None,
) -> dict:
    """Run one full-paper extraction, exclusively.

    Run isolation (infrastructure only -- extraction behavior is unchanged
    and pinned by tests/test_run_characterization.py): the run holds a
    per-paper lock (`pipeline.run_lock`) for its whole duration, so a second
    run on the same paper is REFUSED (`run_lock.RunAlreadyActive`) instead of
    silently interleaving with it; its per-entity results are written under
    `results/<paper_id>/<run_id>/` (never overwriting another run's), and
    `results/<paper_id>/LATEST` is pointed at this run only after it
    COMPLETED. `manifest_extra` (model/provider/base-URL/fingerprint
    metadata, see pipeline.run_config) is merged into the run manifest.

    See `_run_paper_locked` for the pipeline itself."""
    run_id = run_id or _new_run_id()
    with run_lock.paper_run_lock(paper_id, run_id):
        return _run_paper_locked(
            paper_id=paper_id, model=model, client=client, invoke=invoke,
            enable_ai_validation=enable_ai_validation, run_id=run_id, manifest_extra=manifest_extra,
        )


def _run_paper_locked(
    *,
    paper_id: str,
    model: str,
    client: IRServiceClient,
    invoke: Callable[..., AgentInvocation],
    enable_ai_validation: bool,
    run_id: str,
    manifest_extra: Optional[dict],
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
    order = _topological_entity_order()
    this_run_records: dict[str, Any] = {}  # dict per entity_type, or list[dict] for a multi-record type

    # One model per run, enforced (not merely conventional): every agent call
    # in this run must request `model`, and the calls are counted per agent
    # for the manifest. Pure pass-through -- arguments and results are not
    # touched, so extraction behavior is identical.
    raw_invoke = invoke
    agent_call_counts: dict[str, int] = {}

    def invoke(agent: str, model_requested: str, prompt: str, *args: Any, **kwargs: Any) -> AgentInvocation:  # noqa: F811
        if model_requested != model:
            raise RuntimeError(
                f"mixed models within one run: this run is pinned to {model!r} but a call requested {model_requested!r}"
            )
        agent_call_counts[agent] = agent_call_counts.get(agent, 0) + 1
        result = raw_invoke(agent, model_requested, prompt, *args, **kwargs)
        if _provider_failure(result) is None:
            _PROVIDER_OUTAGE.pop(run_id, None)   # any answer at all: the provider is back, full patience again
        return result

    started_at = time.time()
    base_manifest = {
        "run_id": run_id, "paper_id": paper_id, "model": model,
        "schema_fingerprint": schema_fingerprint(),
        "opencode_config_fingerprint": config_fingerprint(OPENCODE_CONFIG_PATH),
        "ai_validation_enabled": enable_ai_validation,
        "kind": "run-paper", "entity_order": order,
        "started_at": started_at,
        **(manifest_extra or {}),
    }
    # The manifest exists from the START of the run (a crashed run used to
    # leave none at all); `run_status` says whether it is still going,
    # finished, or failed. `results/<paper>/LATEST` is NOT moved until the
    # run completed, so readers keep seeing the previous completed run.
    run_store.save_run_manifest(run_id, {**base_manifest, "run_status": "running"})
    results_store.mark_run_results(paper_id, run_id)

    try:
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
                    run_id=run_id,
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
                run_id=run_id,
            )
    except BaseException as exc:  # record the failure, never swallow it
        run_store.save_run_manifest(run_id, {
            **base_manifest, "run_status": "failed", "finished_at": time.time(),
            "agent_calls": {"model": model, "by_agent": dict(agent_call_counts), "total": sum(agent_call_counts.values())},
            "provider_failures": summarize_provider_failures(run_id),
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise

    run_store.save_run_manifest(run_id, {
        **base_manifest, "run_status": "completed", "finished_at": time.time(),
        "table_pass": summarize_table_pass(run_id, paper_id, this_run_records),
        "provider_failures": summarize_provider_failures(run_id),
        "agent_calls": {"model": model, "by_agent": dict(agent_call_counts), "total": sum(agent_call_counts.values())},
        "status": {
            et: (r["status"] if not isinstance(r, list) else [x["status"] for x in r])
            for et, r in this_run_records.items()
        },
    })
    results_store.set_latest(paper_id, run_id)

    return {"run_id": run_id, "paper_id": paper_id, "entity_order": order, "records": this_run_records}


# --------------------------------------------------------------------------- #
# Unified full-paper result (finalize)
# --------------------------------------------------------------------------- #


def finalize_paper(paper_id: str, run_id: Optional[str] = None) -> dict:
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
    entries = _store_entries(paper_id, run_id)
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

    graph = graph_check(paper_id, run_id)
    dataset_dict, _skipped = build_dataset_from_store(paper_id, run_id=run_id)

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


def _run_constants() -> dict[str, Any]:
    """The attempt/retry constants in force for a run, recorded in its
    manifest (they shape how many model calls a record can cost)."""
    return {
        "MAX_EXTRACTION_ATTEMPTS": MAX_EXTRACTION_ATTEMPTS,
        "MAX_ENUMERATION_ATTEMPTS": MAX_ENUMERATION_ATTEMPTS,
        "MAX_TABLE_CLASSIFICATION_ATTEMPTS": MAX_TABLE_CLASSIFICATION_ATTEMPTS,
        "MAX_PROVIDER_FAILURE_ROUNDS": MAX_PROVIDER_FAILURE_ROUNDS,
        "TABLE_PROVIDER_COOLDOWN_SECONDS": TABLE_PROVIDER_COOLDOWN_SECONDS,
        "PROVIDER_COOLDOWN_GROWTH": PROVIDER_COOLDOWN_GROWTH,
        "PROVIDER_COOLDOWN_CAP_SECONDS": PROVIDER_COOLDOWN_CAP_SECONDS,
        "MAX_CONSECUTIVE_PROVIDER_TERMINALS": MAX_CONSECUTIVE_PROVIDER_TERMINALS,
        "MAX_CONVERSION_LOOP_SAFETY": MAX_CONVERSION_LOOP_SAFETY,
        "MAX_AI_VALIDATION_CORRECTIONS": MAX_AI_VALIDATION_CORRECTIONS,
        "MAX_EMPTY_RESPONSE_RETRIES": MAX_EMPTY_RESPONSE_RETRIES,
        "EMPTY_RESPONSE_RETRY_BACKOFF_SECONDS": EMPTY_RESPONSE_RETRY_BACKOFF_SECONDS,
        "ir_service_MAX_PROPOSE_ATTEMPTS": 4,
    }


def prepare_run(
    *, config_path: Optional[str] = None, model_override: Optional[str] = None,
    ir_service_url: Optional[str] = None, probe: bool = True,
) -> tuple[run_config.RunConfig, dict, Callable[..., AgentInvocation]]:
    """Resolve the explicit run configuration and everything a run needs that
    is derived from it: (cfg, manifest_extra, invoke).

    - cfg: from the checked-in config (error if absent -- no silent default),
      with an optional explicit `provider/model` override that is recorded.
    - manifest_extra: model/provider/base-URL/timeouts/fingerprints/git state
      plus a `/models` endpoint snapshot. If that snapshot succeeds and the
      configured model is not listed, this raises `run_config.ModelUnavailable`
      (refuse to run); an inconclusive probe is only a recorded warning.
    - invoke: `invoke_agent` bound to the configured per-call timeout.
    """
    cfg = run_config.load_run_config(config_path)
    source = "config"
    if model_override:
        cfg = run_config.with_model_override(cfg, model_override)
        source = "cli_override"
    probe_result = probe_warning = None
    if probe:
        probe_result = run_config.probe_models(cfg)
        probe_warning = run_config.require_model_available(cfg, probe_result)
    import functools

    manifest_extra = run_config.build_manifest_metadata(
        cfg, model_source=source, constants=_run_constants(), probe=probe_result, probe_warning=probe_warning,
        ir_service={"url": ir_service_url or DEFAULT_IR_SERVICE_URL, "schema_fingerprint_on_disk": schema_fingerprint()},
    )
    invoke = functools.partial(invoke_agent, timeout=cfg.agent_call_timeout_seconds)
    return cfg, manifest_extra, invoke


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
    run.add_argument("--model", default=None, help="Explicit 'provider/model' override of the run config (recorded as model_source=cli_override). Default: the model in --config.")
    run.add_argument("--config", default=None, help="Run config JSON (default: src/eval_config.json).")
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
    graph.add_argument("--run-id", default=None, help="Only consider ir-store entries committed by this run (default: all runs, latest per record).")

    finalize = sub.add_parser(
        "finalize",
        help="Aggregate the latest ir-store records for one paper into a unified results/<paper_id>/result.json covering all 12 entity types.",
    )
    finalize.add_argument("--paper-id", required=True)
    finalize.add_argument("--run-id", default=None, help="Only aggregate ir-store entries committed by this run (default: all runs, latest per record).")

    run_paper_cmd = sub.add_parser(
        "run-paper",
        help="Run the complete Extraction->Conversion->validation->AI-Validator->commit pipeline "
             "for all 12 entity types for one paper, in dependency order, and write results/<paper_id>/<Entity>.json.",
    )
    run_paper_cmd.add_argument("--paper-id", required=True)
    run_paper_cmd.add_argument("--model", default=None, help="Explicit 'provider/model' override of the run config (recorded as model_source=cli_override). Default: the model in --config.")
    run_paper_cmd.add_argument("--config", default=None, help="Run config JSON (default: src/eval_config.json).")
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
        result = graph_check(args.paper_id, args.run_id)
        print(json.dumps(result, indent=2))
        has_errors = not result.get("constructed") or any(i["severity"] == "error" for i in result.get("issues", []))
        return 1 if has_errors else 0

    if args.command == "finalize":
        result = finalize_paper(args.paper_id, args.run_id)
        print(_finalize_summary_line(result))
        print(f"\nwritten to: {results_store.result_path(args.paper_id)}")
        has_errors = not result["graph_constructed"] or any(i["severity"] == "error" for i in result["graph_issues"])
        return 1 if has_errors else 0

    if args.command == "run-paper":
        ok, msg = check_health(args.ir_service_url)
        print(msg)
        if not ok:
            return 1

        try:
            cfg, manifest_extra, invoke = prepare_run(
                config_path=args.config, model_override=args.model, ir_service_url=args.ir_service_url,
            )
        except (run_config.RunConfigError, run_config.ModelUnavailable) as exc:
            print(f"run configuration error: {exc}")
            return 1
        print(f"model: {cfg.model_ref}  base_url: {cfg.base_url}")
        if manifest_extra.get("endpoint_probe_warning"):
            print(f"warning: {manifest_extra['endpoint_probe_warning']}")

        http_client = httpx.Client(base_url=args.ir_service_url, timeout=cfg.ir_service_timeout_seconds)
        client = IRServiceClient(http_client)
        try:
            outcome = run_paper(
                paper_id=args.paper_id, model=cfg.model_ref, client=client, invoke=invoke,
                enable_ai_validation=not args.no_ai_validation, run_id=args.run_id, manifest_extra=manifest_extra,
            )
        except run_lock.RunAlreadyActive as exc:
            print(f"refusing to start: {exc}")
            return 1
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

    try:
        cfg, manifest_extra, invoke = prepare_run(
            config_path=args.config, model_override=args.model, ir_service_url=args.ir_service_url,
        )
    except (run_config.RunConfigError, run_config.ModelUnavailable) as exc:
        print(f"run configuration error: {exc}")
        return 1
    print(f"model: {cfg.model_ref}  base_url: {cfg.base_url}")

    run_id = args.run_id or _new_run_id()
    http_client = httpx.Client(base_url=args.ir_service_url, timeout=cfg.ir_service_timeout_seconds)
    client = IRServiceClient(http_client)

    run_store.save_run_manifest(run_id, {
        **manifest_extra,
        "run_id": run_id,
        "paper_id": args.paper_id, "entity_type": args.entity_type, "record_id": args.record_id,
        "model": cfg.model_ref,
        "schema_fingerprint": schema_fingerprint(),
        "opencode_config_fingerprint": config_fingerprint(OPENCODE_CONFIG_PATH),
        "ai_validation_enabled": not args.no_ai_validation,
        "known_refs": known_refs,
        "started_at": time.time(),
        "status": "running",
    })

    result = run_record(
        run_id=run_id, paper_id=args.paper_id, entity_type=args.entity_type, record_id=args.record_id,
        model=cfg.model_ref, client=client, invoke=invoke, enable_ai_validation=not args.no_ai_validation,
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
