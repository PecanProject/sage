"""`pipeline/orchestrator.py` -- the deterministic control loop for:

    PDF (already processed) -> Extraction AI -> sealed raw evidence
        -> Conversion/Reasoning AI -> Sage IR
        -> deterministic validation (hard gate)
        -> bounded correction loop
        -> AI Validator (one bounded correction pass)
        -> final persistence (ir-store)

The outer workflow is fully deterministic
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
    can never itself decide "I'm done, let me commit".
  * Every attempt at every stage is persisted via `pipeline.run_store`
    before this module decides what to do with it -- a failure never
    silently disappears; every record ends as "ready", "unresolved", or
    "error", each with its full artifact trail on disk.
  * The Conversion AI is sealed: it is only ever shown the Extraction AI's
    `RawExtraction` package (`pipeline.raw_schema`), never `content.md`
    itself, so it cannot fabricate a new anchor to rationalize a value --
    it can only choose among anchors an earlier, tool-verified stage
    already read.
  * The AI Validator's critique is always recorded. A "suspicious" verdict
    triggers exactly ONE Conversion correction pass
    (`_attempt_ai_validation_correction`, `MAX_AI_VALIDATION_CORRECTIONS`);
    the corrected payload must pass the same deterministic validation. If
    the correction fails, the concerned fields are demoted
    (`_settle_by_field_demotion`) or, when that is not possible, the record
    is committed as unresolved. The validator's opinion never bypasses
    deterministic validation.
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from pydantic import ValidationError

from pipeline import content_reader, pooling_evidence, results_store, run_config, run_lock, run_store, store, vocab
from pipeline.fingerprint import config_fingerprint, schema_fingerprint
from pipeline import cell_values, causes, context_bundle, coordinates, design, document_map, evidence_index, method_map
from pipeline.ir_schema import IRDataset
from pipeline.raw_schema import (
    DESIGN_FACTOR_RULE, MIXTURE_LEVEL_RULE, CandidateDimension, EnumerationCandidate, EnumerationResult, MethodHintFlag, RawExtraction,
    RawFact, TableClassification, TableVariable, TimeLevel, UnitHintFlag, _variable_key, all_anchors,
)
from pipeline.validators import (  # reuse, don't re-implement anchor parsing/grounding
    _load_rendered_blocks, _normalize_typography, _papers_root, validate_dataset, _value_supported_by_text,
    _unit_supported as _validator_unit_supported,
    _units_key,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENCODE_CONFIG_PATH = PROJECT_ROOT / "opencode.json"

# No default model: it comes from the run config (src/eval_config.json) and is recorded in the manifest.
DEFAULT_IR_SERVICE_URL = os.environ.get("IR_SERVICE_URL", "http://127.0.0.1:8420")

# One spare attempt so a transient provider glitch does not use up the feedback attempts.
MAX_EXTRACTION_ATTEMPTS = 3
MAX_ENUMERATION_ATTEMPTS = 3
# Deterministic-retry safety ceiling. ir_service.MAX_PROPOSE_ATTEMPTS (4) is
# the real, authoritative cap enforced server-side by attempt counters keyed
# on (paper_id, entity_type, record_id); this is a defensive backstop so a
# server-side bug can't turn into a local infinite loop.
MAX_CONVERSION_LOOP_SAFETY = 6
# A "suspicious" verdict triggers exactly one Conversion correction pass (see _attempt_ai_validation_correction).
MAX_AI_VALIDATION_CORRECTIONS = 1
# Step B (per-table classification) retries: a shape, anchor or sanity-check failure is fed back within this budget.
MAX_TABLE_CLASSIFICATION_ATTEMPTS = 3
# A provider failure (empty text, timeout, malformed tool-call stream) never consumes a numbered attempt; it has its
# own budget of rounds per loop, with growing cooldowns (20, 60, 180, 300 s) that wait out a burst of ~10 minutes.
MAX_PROVIDER_FAILURE_ROUNDS = 5
TABLE_PROVIDER_COOLDOWN_SECONDS = 20      # the first cooldown; each later one is PROVIDER_COOLDOWN_GROWTH times longer
PROVIDER_COOLDOWN_GROWTH = 3
PROVIDER_COOLDOWN_CAP_SECONDS = 300
# A real outage must not multiply that patience across every record of the run: after this many loops in a row
# ended on a spent provider budget with no successful model call in between, each further loop gets ONE round
# (`invoke_agent`'s internal retries still apply) until any call succeeds again.
MAX_CONSECUTIVE_PROVIDER_TERMINALS = 3
# Entity types whose candidates come from deterministic table enumeration (Steps A-D) when tables cover them: code,
# not the model, counts the distinct rows/combinations, and a Treatment is the full combination of factor levels.
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
    # True when the tool-call stream shows the harmony channel-token leak: parsed_json/final_text must not be trusted.
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
    """The provider-failure budget shared by every loop that calls a model and counts attempts. A round with no usable
    answer never consumes a numbered attempt; it spends one of `MAX_PROVIDER_FAILURE_ROUNDS` rounds instead, with a
    cooldown between them, and the same prompt is repeated."""

    def __init__(self, run_id: str, record_key: str, stage: str):
        self.run_id, self.record_key, self.stage = run_id, record_key, stage
        self.rounds = 0
        self.classes: list[str] = []
        self.last_result: Optional[AgentInvocation] = None

    def round_limit(self) -> int:
        """The rounds this loop may spend: the full budget, or a single round while the provider is evidently down
        (`MAX_CONSECUTIVE_PROVIDER_TERMINALS` loops in a row ended on a spent budget with no success between)."""
        down = _PROVIDER_OUTAGE.get(self.run_id, 0) >= MAX_CONSECUTIVE_PROVIDER_TERMINALS
        return 1 if down else MAX_PROVIDER_FAILURE_ROUNDS

    def failed(self, failure_class: str, result: Optional[AgentInvocation] = None) -> bool:
        """Record one provider-failed round. True when the loop must stop: the executable is missing (never
        retried) or the budget is spent. `result` is the failed invocation, used by `cooldown` to decide whether there
        is any evidence of an outage to wait out."""
        self.rounds += 1
        self.classes.append(failure_class)
        self.last_result = result
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
        """Wait before the next round only when the last failure shows outage evidence (`_outage_evidence`); a prompt
        empty answer goes again immediately with the answer-now prompt."""
        if _outage_evidence(self.last_result):
            time.sleep(provider_cooldown_seconds(self.rounds))

    def needs_answer_nudge(self) -> bool:
        """After a failure that is not an outage, the next round asks for the answer directly instead of repeating the
        identical prompt (the model's no-answer turn was repeatable: identical token counts round after round)."""
        return self.last_result is not None and not _outage_evidence(self.last_result)

    def disclosure(self, numbered: int, terminal: bool) -> dict[str, Any]:
        """The fields a terminal record/enumeration/table failure carries: whose failure it was."""
        return {
            "failure_kind": "provider" if terminal else "extraction", "failure_classes": list(self.classes),
            "numbered_attempts": numbered, "provider_failure_rounds": self.rounds,
        }


def summarize_provider_failures(run_id: str, *, discard: bool = True) -> dict[str, Any]:
    """Every provider-failed round of this run for the manifest: totals by stage and class, and the loops that ended
    because the provider budget was spent (`terminal`). Provider failures are never counted as extraction failures.
    Stalls (`no_final_answer`: the model's own no-answer turns) are reported separately under `model_no_answer`."""
    log = _PROVIDER_FAILURE_LOG.pop(run_id, []) if discard else list(_PROVIDER_FAILURE_LOG.get(run_id, []))
    stalls = _STALL_LOG.pop(run_id, []) if discard else list(_STALL_LOG.get(run_id, []))
    if discard:
        _PROVIDER_OUTAGE.pop(run_id, None)
    stall_by_stage: dict[str, int] = {}
    for entry in stalls:
        stall_by_stage[entry["stage"]] = stall_by_stage.get(entry["stage"], 0) + 1
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
        "model_no_answer": {
            "total_rounds": len(stalls), "by_stage": stall_by_stage,
            "terminal": [{k: e[k] for k in ("stage", "record_key")} for e in stalls if e["terminal"]],
        },
    }


# --------------------------------------------------------------------------- #
# No-answer turns: a stall is not an outage
# --------------------------------------------------------------------------- #
# A stall (tools used, then no answer text) gets up to MAX_FINAL_ANSWER_RETRIES immediate "answer now" retries outside
# the provider budget; only real outage evidence earns a cooldown.

MAX_FINAL_ANSWER_RETRIES = 4
_STALL_LOG: dict[str, list[dict]] = {}   # run_id -> [{stage, record_key, terminal}]
# A provider error event that is really the gpt-oss/vLLM harmony-format defect (a model output formatting failure, seen
# as "unexpected tokens remaining in message header" / "could not decode header"), not an outage.
_HARMONY_ERROR_RE = re.compile(
    r"unexpected tokens remaining in message header|could not decode header|<\|(?:channel|end|start|message|call)\|>",
    re.IGNORECASE,
)


def _stream_events(stdout: str) -> list[dict]:
    """Every JSON event with a `type` in an `opencode run --format json` stream."""
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type"):
            events.append(obj)
    return events


def _outage_evidence(result: Optional[AgentInvocation]) -> bool:
    """Is there evidence the provider is DOWN (worth waiting for), as opposed to having answered with no usable text?
    Outage: no invocation to judge (conservative), a timeout, a missing executable, a provider error event other than
    the harmony-format defect, or no response events at all. Not an outage: the harmony leak, and a stream that ran and
    finished normally with no text (the provider answered)."""
    if result is None:
        return True
    failure = classify_invocation_failure(result)
    if failure in ("provider_timeout", "provider_unavailable"):
        return True
    if result.had_malformed_tool_call:
        return False
    events = _stream_events(result.stdout)
    errors = [json.dumps(e.get("error") or e) for e in events if e.get("type") == "error"]
    if errors:
        return not all(_HARMONY_ERROR_RE.search(message) for message in errors)
    return not events


def _record_stall(run_id: str, stage: str, record_key: str, terminal: bool) -> None:
    _STALL_LOG.setdefault(run_id, []).append({"stage": stage, "record_key": record_key, "terminal": terminal})


# Every round is a new session with nothing read, so the nudge must still allow reading.
_ANSWER_NOW = (
    "Your previous attempt ended without a final answer. This is a new session: nothing read there carries over. "
    "{where} Output {what} now, from that; use a tool only for one specific block this prompt does not contain."
)


def _final_answer_nudge(stage: str, entity_type: Optional[str] = None) -> str:
    """The short, targeted instruction for the round after a no-answer turn -- never the identical prompt again."""
    if stage == "extraction" and entity_type == "Citation":
        return CITATION_FINAL_ANSWER_NUDGE
    return {
        "extraction": _ANSWER_NOW.format(what="the RawExtraction JSON", where="The evidence packet above holds the text.")
        + " A field the text does not state is reported with raw_value null -- that is a complete answer.",
        "enumeration": _ANSWER_NOW.format(what="the EnumerationResult JSON", where="The evidence packet above holds the text.")
        + " If you found none, an empty candidates array is a complete answer.",
        "table_classification": _ANSWER_NOW.format(
            what="the TableClassification JSON",
            where="The table, its caption and its notes are in this prompt verbatim (THE TABLE section)."),
        "conversion": _ANSWER_NOW.format(what="the candidate record JSON exactly as your system prompt instructs",
                                         where="The raw evidence above is all the conversion needs."),
    }[stage]


# A response with no final-answer text after tool use is a provider/decode failure, not a content problem, so the
# identical prompt is retried here, for every stage, without touching attempt counts.
MAX_EMPTY_RESPONSE_RETRIES = 2
EMPTY_RESPONSE_RETRY_BACKOFF_SECONDS = 1.5


def _has_malformed_harmony_tool_call(stdout: str) -> bool:
    """True when a harmony channel token ("<|channel|>") leaked into a tool-call name: retryable provider noise. A
    substring check, because the malformed name can sit inside a nested error/output string."""
    return "<|channel|>" in stdout and "unavailable tool" in stdout


def _decode_if_bytes(value: Optional[Any]) -> Optional[str]:
    """TimeoutExpired.stdout/.stderr may be bytes even with text=True; normalise to str."""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _invoke_agent_once(agent: str, model: str, prompt: str, timeout: int) -> AgentInvocation:
    """Exactly one real `opencode run` subprocess call and parse attempt --
    factored out of invoke_agent so its internal empty-response retry loop
    (below) can call this repeatedly without duplicating the subprocess/
    parse logic itself."""
    cmd = [
        run_config.opencode_bin_for_subprocess(), "run",
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
        # TimeoutExpired carries partial output as bytes even though text=True was passed.
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
    extraction_context: Optional[str] = None, supplied_context: Optional[str] = None, final_answer_nudge: bool = False,
) -> str:
    # The output contract is repeated in the user turn: models drift toward IR-shaped answers otherwise.
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
        # The enumeration pass already found this candidate and its anchors: targeting context, not a value to copy.
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
    ) + (
        # `supplied_context`: Citation only (`_citation_extraction_note`). `final_answer_nudge`: any entity type, only on
        # the round after a no-answer turn (`_final_answer_nudge`). Absent, the prompt is byte-identical to before.
        f"\n\n{supplied_context}" if supplied_context else ""
    ) + (
        f"\n\n{_final_answer_nudge('extraction', entity_type)}" if final_answer_nudge else ""
    )


# --------------------------------------------------------------------------- #
# Citation: front matter supplied, missing information made explicit
# --------------------------------------------------------------------------- #
#   1. the first-page blocks are supplied verbatim with their anchors, plus a deterministic DOI check of that page;
#   2. a round that used tools and then stopped with no answer (`_ended_without_answer_after_tools`) is a stall and
#      gets a short "answer now" retry, at most MAX_CITATION_FINAL_ANSWER_RETRIES times, outside the provider budget;
#   3. a field the source does not write (journal, DOI) is recorded as NOT WRITTEN (`_citation_not_written`), never
#      as evidence or a value.

MAX_CITATION_FINAL_ANSWER_RETRIES = 3   # Citation's own, tighter bound (it is given its front matter up front)
_CITATION_FRONT_MATTER_MAX_BLOCKS = 12
_CITATION_FRONT_MATTER_MAX_CHARS = 6000
_CITATION_FRONT_MATTER_BLOCK_CHARS = 1200
_CITATION_DOI_SCAN_FALLBACK_BLOCKS = 20        # without provenance.json: how many leading blocks count as the first page
# A heading that opens the article body ends the front matter. The title itself is often a heading, so only these do.
_BODY_HEADING_RE = re.compile(
    r"^\W*(introduction|background|materials?\b|methods?\b|study (area|site)|site description|experimental|results)",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s⟦\])>,;\"']+")
# The Citation information the extractor is asked about, keyed by the canonical name, with the fact names it may use.
# `journal` is not an IR field (Citation holds author, year, title, persistent_identifier); it is still asked for and
# reported as NOT WRITTEN when absent, so the model has an explicit way to say so instead of hunting for it.
CITATION_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "title": frozenset({"title"}),
    "author": frozenset({"author", "authors"}),
    "year": frozenset({"year", "publication_year"}),
    "journal": frozenset({"journal", "journal_name", "journal_title"}),
    "persistent_identifier": frozenset({"persistent_identifier", "doi", "pid"}),
}
# The fields the orchestrator may itself declare NOT WRITTEN. Title, author and year are not: a paper always writes
# them, so a missing one is an extraction omission, left to Conversion's normal handling -- never excused here.
CITATION_MISSABLE_FIELDS = ("journal", "persistent_identifier")
CITATION_NOT_WRITTEN_REASON = "Not written in the paper's text (front matter checked)"

CITATION_FINAL_ANSWER_NUDGE = (
    "Your previous reply ended without a final answer. Stop searching now and do not call any more tools. Using the "
    "CITATION FRONT MATTER above (and anything you have already read), output the RawExtraction JSON now. Every Citation "
    "field that is not written in that text -- for example the journal or the DOI -- is reported as a fact with raw_value "
    "null and notes \"NOT WRITTEN in the paper\"; that is a complete, correct answer."
)


def _canonical_citation_field(field_name: Any) -> Optional[str]:
    key = _fact_field_key(field_name)
    return next((canonical for canonical, names in CITATION_FIELD_ALIASES.items() if key in names), None)


def _citation_front_matter(paper_id: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(front-matter blocks, DOI hits) for a paper, both as (anchor, text) in document order.

    Front matter: the rendered blocks up to the first heading that opens the article body (`_BODY_HEADING_RE`), at most
    `_CITATION_FRONT_MATTER_MAX_BLOCKS`. DOI hits: first-page blocks containing a `10.NNNN/...` identifier (later pages
    carry the reference list, whose DOIs belong to other papers)."""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return [], []
    provenance = content_reader._load_provenance(paper_id, _papers_root()) or {}
    ordered = [(a, " ".join(blocks[a].split())) for a in sorted(blocks, key=content_reader._anchor_sort_key)]
    ordered = [(a, t) for a, t in ordered if t]

    front: list[tuple[str, str]] = []
    for anchor, text in ordered:
        block_type = (provenance.get(anchor) or {}).get("block_type")
        is_heading = block_type == "SectionHeader" or text.startswith("#")
        if front and is_heading and _BODY_HEADING_RE.match(text.lstrip("# ").strip()):
            break
        front.append((anchor, text))
        if len(front) >= _CITATION_FRONT_MATTER_MAX_BLOCKS:
            break

    if provenance:
        first_page = [(a, t) for a, t in ordered if (provenance.get(a) or {}).get("page_id") == "page_0"]
    else:
        first_page = ordered[:_CITATION_DOI_SCAN_FALLBACK_BLOCKS]
    hits = [(a, m.group(0).rstrip(".")) for a, t in first_page for m in [_DOI_RE.search(t)] if m]
    return front, hits


def _citation_extraction_note(paper_id: str) -> str:
    """The Citation extraction prompt's supplied context: the front matter verbatim with anchors, the DOI check, and
    the rules for a field that is not written. "" when the paper has no rendered content (the prompt is then unchanged)."""
    front, hits = _citation_front_matter(paper_id)
    if not front:
        return ""
    # A DOI block outside the front matter (e.g. a first-page footnote) is supplied too, so its text is in front of the model.
    blocks = dict(front)
    extra = []
    if any(a not in blocks for a, _ in hits):
        rendered = _load_rendered_blocks(paper_id)
        extra = [(a, " ".join(rendered[a].split())) for a in dict.fromkeys(a for a, _ in hits) if a not in blocks]
    lines, used = [], 0
    for anchor, text in front + extra:
        text = text[:_CITATION_FRONT_MATTER_BLOCK_CHARS]
        if used + len(text) > _CITATION_FRONT_MATTER_MAX_CHARS:
            break
        lines.append(f"[{anchor}] {text}")
        used += len(text)
    if hits:
        doi_check = (
            "DOI CHECK (deterministic scan of the first page): a DOI is written -- "
            + "; ".join(f"{doi!r} in block {anchor}" for anchor, doi in hits)
            + ". Report it as persistent_identifier with its literal text and that block's anchor."
        )
    else:
        doi_check = (
            "DOI CHECK (deterministic scan of the first page): no DOI (a '10.NNNN/...' identifier) is written on the "
            "first page. persistent_identifier is NOT WRITTEN -- do not search for it."
        )
    return (
        "CITATION FRONT MATTER -- the first blocks of this paper, verbatim, each labelled with its content.md anchor, "
        "supplied by the pipeline so you do not have to search for them:\n" + "\n".join(lines) + "\n\n" + doi_check + "\n\n"
        "Rules for this Citation:\n"
        "- Report title, author, year, journal and persistent_identifier, one fact each, from the text above. You may read "
        "other blocks with the listed read tools, but make at most 3 further reads: if a field is still not found, it is "
        "not written.\n"
        "- A value is reported only when it is literally written: raw_text_excerpt is that literal text, anchors the "
        "block it is in. Never invent or complete a journal, DOI, volume, pages or year.\n"
        "- A field that is not written is reported as a fact with raw_value null, anchors = the front-matter block(s) "
        "you checked, and notes \"NOT WRITTEN in the paper\". NOT WRITTEN is not evidence: it never needs to appear in "
        "the paper. Then continue with the other fields -- never keep searching for it.\n"
        "- There is no find or search tool; call only the read tools listed above."
    )


def _tool_events(stdout: str) -> list[dict]:
    """Every tool part of an `opencode run --format json` stream: {tool, status, input}."""
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        part = obj.get("part") if isinstance(obj, dict) else None
        if isinstance(part, dict) and part.get("type") == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            tool = part.get("tool")
            if tool == "invalid" and isinstance(state.get("input"), dict):
                tool = f"invalid:{state['input'].get('tool')}"
            events.append({"tool": tool, "status": state.get("status"), "input": state.get("input")})
    return events


def _stream_has_error_event(stdout: str) -> bool:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                if json.loads(line).get("type") == "error":
                    return True
            except (ValueError, AttributeError):
                continue
    return False


def _ended_without_answer_after_tools(result: AgentInvocation) -> bool:
    """A STALL, told apart from a provider failure: the model used at least one tool successfully and the stream then
    ended normally (no provider error event, no harmony leak) without any answer text. The provider did its job; the
    model kept searching and never answered. Everything else with no answer -- nothing came back at all, a provider
    error event, a timeout, the harmony leak, a missing binary -- stays a provider failure (`classify_invocation_failure`
    is unchanged)."""
    if result.final_text is not None or result.had_malformed_tool_call or result.returncode != 0:
        return False
    if result.parse_error and "opencode executable not found" in result.parse_error:
        return False
    if _stream_has_error_event(result.stdout):
        return False
    return any(event["status"] == "completed" for event in _tool_events(result.stdout))


def _citation_not_written(paper_id: str, extraction: RawExtraction) -> tuple[list[dict], list[dict]]:
    """(not-written entries, errors) for a grounded Citation extraction.

    A missable field (`CITATION_MISSABLE_FIELDS`) with no valued fact is NOT WRITTEN: an entry {field, reason,
    checked_anchors} for Conversion, never a value and never evidence. One exception is an error instead: the DOI check
    found a DOI on the first page but the extraction reported none -- the DOI is written, so it must be reported."""
    front, hits = _citation_front_matter(paper_id)
    checked = [a for a, _ in front]
    valued = {
        _canonical_citation_field(f.field_name) for f in extraction.facts
        if f.raw_value is not None and str(f.raw_value).strip()
    }
    entries, errors = [], []
    for field in CITATION_MISSABLE_FIELDS:
        if field in valued:
            continue
        if field == "persistent_identifier" and hits:
            errors.append({
                "field": "persistent_identifier",
                "message": f"a DOI is written on the first page ({'; '.join(f'{d!r} in block {a}' for a, d in hits)}) but no "
                           f"persistent_identifier fact reports it -- report it with its literal text and that anchor.",
            })
            continue
        entries.append({"field": field, "reason": CITATION_NOT_WRITTEN_REASON, "checked_anchors": checked})
    return entries, errors


# Per-entity-type "what counts as a distinct record" guidance from the Calibration/Validation Protocol. Variable has none:
# it is a registry (one record per named quantity), so per-combination splitting would duplicate it.
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
        "date or year is TIME unless the paper assigns it to plots as a design factor (then each level "
        "is a treatment level), and a location is a SITE: none of them is a Treatment, and naming "
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
    """The enumeration prompt: "how many distinct ones are there, and where", asked of the extractor agent.

    `link_pools` (field_name -> [{slug, name}, ...]) lists the ready records of another multi-record type this one
    depends on; `_ENTITY_IDENTITY_GUIDANCE` adds the per-type identity rule."""
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
        # The example uses a field this entity type is actually offered, so the model copies the right key.
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
    """Added to the free-form prompt only when table enumeration already covers this entity type: the model must
    declare the factor levels that distinguish each candidate, so it is compared by the same canonical identity."""
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
        f"'treatment' (an experimental condition the study ASSIGNS as a design factor -- e.g. a "
        f"cover-crop, tillage, fertilizer or irrigation level, or a harvest schedule), 'crop' (an individual "
        f"cultivar, variety, population or genotype), 'time' (a date, growth stage, year or season at which "
        f"something was measured), 'site' (a location), or 'other'. A single cultivar/population is 'crop' and "
        f"a location is 'site' -- neither is a 'treatment'. {DESIGN_FACTOR_RULE} {MIXTURE_LEVEL_RULE} "
        f"Every level must appear in your description or in a block you cite. "
        f"Something with no treatment-dimension level is not a {entity_type}: do not report it."
    )
    return text


def _unsplit_required_dimensions(
    entity_type: str, candidates: list, link_pools: Optional[dict[str, list[dict]]],
) -> list[tuple[str, str]]:
    """[(field, prereq_type), ...] for every required dependency field whose link pool is ambiguous (>1 ready record)
    but which no candidate linked to -- e.g. the same factor levels at several Sites reported once instead of per site.
    A retry signal only: the result is still accepted once attempts are exhausted."""
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


# {run_id: [table cells extracted but not representable in the current IR]}
_REPRESENTATION_BLOCKED: dict[str, list[dict]] = {}
_UNIDENTIFIED_ROWS: dict[str, list[dict]] = {}
# {(run_id, entity_type): outcome} for an enumeration whose empty answer could not be trusted
_ENUMERATION_OUTCOMES: dict[tuple[str, str], dict[str, Any]] = {}


def run_enumeration(
    *, run_id: str, paper_id: str, entity_type: str, model: str,
    invoke: Callable[..., AgentInvocation] = invoke_agent,
    link_pools: Optional[dict[str, list[dict]]] = None,
    excluded_table_anchors: Optional[set[str]] = None,
    covered_conditions: Optional[list[str]] = None,
    declare_dimensions: bool = False,
    evidence_packet: Optional[str] = None,
    evidence_found: Optional[list[str]] = None,
) -> tuple[list, Optional[str]]:
    """Bounded, deterministically validated enumeration pass for a multi-record entity type. Returns (candidates, None)
    on success (possibly empty) or ([], error_message). Every cited anchor is checked against content.md, and every
    `linked_candidates` value must name a slug in the corresponding pool (the real check happens again before use)."""
    record_key = f"{entity_type}__enumeration"
    errors: list[dict] = []
    last_message = "enumeration never produced a valid EnumerationResult"
    # Provider failures spend the separate provider budget, not a numbered attempt.
    provider = _ProviderBudget(run_id, record_key, "enumeration")
    attempt = 0   # numbered attempts: the model's own answers
    rounds = 0    # every invocation round: the artifact index
    provider_terminal = False
    stalls = 0              # no-answer turns (see "No-answer turns: a stall is not an outage")
    stall_terminal = False
    nudge = False
    empty_reask_done = False   # at most one clean re-ask of an empty answer that cannot be trusted
    _ENUMERATION_OUTCOMES.pop((run_id, entity_type), None)

    while attempt < MAX_ENUMERATION_ATTEMPTS:
        rounds += 1
        prompt = _enumeration_prompt(
            paper_id, entity_type, errors, link_pools, excluded_table_anchors,
            covered_conditions=covered_conditions, declare_dimensions=declare_dimensions,
        ) + (f"\n\n{evidence_packet}" if evidence_packet else "")
        result = invoke("reader", model, prompt + (f"\n\n{_final_answer_nudge('enumeration')}" if nudge else ""))
        artifact = result.as_artifact()

        if _ended_without_answer_after_tools(result):
            stalls += 1
            stall_terminal = stalls > MAX_FINAL_ANSWER_RETRIES
            artifact["validation_errors"] = [{"field": None, "message": "the model used tools and ended without a final answer"}]
            artifact.update(failure_class="no_final_answer", failure_kind="extraction", numbered_attempt=None)
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            _record_stall(run_id, "enumeration", record_key, stall_terminal)
            last_message = f"round {rounds}: no final answer after tool use ({stalls} stall(s))"
            if stall_terminal:
                break
            nudge = True
            continue

        failure = _provider_failure(result)
        if failure:
            artifact["validation_errors"] = [{"field": None, "message": result.parse_error}]
            artifact.update(failure_class=failure, failure_kind="provider", numbered_attempt=None)
            run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
            last_message = f"round {rounds}: provider failure ({failure}): {result.parse_error}"
            if provider.failed(failure, result):
                provider_terminal = True
                break
            provider.cooldown()
            nudge = nudge or provider.needs_answer_nudge()
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

        # One chance to split an ambiguous required dimension (see _unsplit_required_dimensions); never a hard requirement.
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

        if not candidates:
            disturbed = stalls > 0 or provider.rounds > 0 or nudge
            if (disturbed or evidence_found) and not empty_reask_done:
                # An empty list right after a stall, or while the packet holds evidence for this type, gets one clean
                # re-ask naming that evidence.
                empty_reask_done = True
                nudge = False
                attempt -= 1   # the untrusted empty answer does not spend a numbered attempt
                errors = [{"field": "candidates", "message": (
                    f"Your answer listed no {entity_type} candidates"
                    + (" right after an interrupted turn" if disturbed else "")
                    + ". " + (f"The evidence packet contains evidence of: {'; '.join(evidence_found)}. " if evidence_found else "")
                    + f"Read the packet (and the paper) again and report EVERY distinct {entity_type} it states. "
                    f"Answer with an empty list only if, having read that evidence, none of it is a {entity_type}."
                )}]
                artifact["validation_errors"] = errors
                artifact["empty_answer_reasked"] = {"disturbed": disturbed, "evidence_found": evidence_found or []}
                run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
                last_message = f"attempt {attempt + 1}: empty answer re-asked once"
                continue
            if disturbed:
                # Still empty after an interrupted turn and a clean re-ask: nothing was reliably looked at.
                _ENUMERATION_OUTCOMES[(run_id, entity_type)] = {
                    "candidates": 0, "reliable": False, "disturbed": True, "evidence_found": evidence_found or [],
                    "cause": causes.NOT_RETRIEVED,
                }
                artifact["enumeration_outcome"] = _ENUMERATION_OUTCOMES[(run_id, entity_type)]
            elif evidence_found:
                # Read the evidence twice, uninterrupted, and judged none of it an instance: the model's judgment,
                # kept visible -- not turned into an unresolved record (the packet's concept words are generic).
                artifact["enumeration_outcome"] = {"candidates": 0, "reliable": True, "empty_despite_packet_evidence": evidence_found}
        artifact["validation_errors"] = []
        run_store.save_stage_attempt(run_id, record_key, "enumeration", rounds, artifact)
        return candidates, None

    run_store.save_final(run_id, record_key, {
        "status": "error", "message": last_message,
        **(provider.disclosure(attempt, provider_terminal) if provider.rounds else {}),
        **({"failure_class": "no_final_answer", "failure_kind": "extraction", "stall_rounds": stalls} if stall_terminal else {}),
    })
    return [], last_message


# --------------------------------------------------------------------------- #
# Table enumeration (Steps A-D): deterministic candidate generation for dense data tables. Code, not the model, counts
# the distinct rows; the model only interprets one table's structure at a time.
#
# Step A (table discovery, pure code) -> Step B (per-table classification/reconstruction, one bounded LLM call per
# table plus a deterministic sanity check) -> Step C (deterministic cross-product into EnumerationCandidates) ->
# Step D (merge with the free-form pass, told which anchors are already covered).
# --------------------------------------------------------------------------- #


_NUMERIC_TOKEN_RE = re.compile(r"-?\d+\.?\d*")


def _numeric_tokens(text: str) -> list[str]:
    return _NUMERIC_TOKEN_RE.findall(text or "")


def _table_classification_sanity_check(classification: TableClassification, paper_id: str) -> Optional[str]:
    """Deterministic reconstruction check: compares the total count of numeric tokens in the raw table cells with the
    count in the reconstruction (counts, not positions, since Marker's geometric cells are not 1:1 with logical rows).

    Returns None when the counts are within a loose tolerance (footnote/LSD values are legitimately excluded), or a
    retryable diagnostic when the reconstruction grossly under- or over-accounts."""
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


# --- Cell placement against the raw provenance grid ------------------------------------------------------------------

_PLAIN_NUMBER_RE = re.compile(r"^[-–−]?\d+(?:\.\d+)?$")


def _cell_key(text: Any) -> str:
    return " ".join(str(text or "").replace("–", "-").replace("−", "-").split())


def _logical_grid_rows(paper_id: str, table_anchors: list[str]) -> list[tuple[str, int, int, list[str]]]:
    """(anchor, raw row, packed position, cells) for every logical row of the raw provenance grid. A raw row whose
    numeric cells all pack the same number k > 1 of values ("0.19 0.90 1.16") is k logical rows; label cells repeat."""
    out = []
    for anchor in table_anchors:
        for r, cells in enumerate(content_reader.raw_table_grid(paper_id, anchor, papers_root=_papers_root())):
            numeric = {i: c.split() for i, c in enumerate(cells) if c and all(_PLAIN_NUMBER_RE.match(t) for t in c.split())}
            counts = {len(tokens) for tokens in numeric.values()}
            k = counts.pop() if len(counts) == 1 else 1
            for pos in range(k):
                out.append((anchor, r, pos, [numeric[i][pos] if k > 1 and i in numeric else c for i, c in enumerate(cells)]))
    return out


def _cell_placement_findings(classification: TableClassification, paper_id: str) -> list[dict]:
    """Reconstructed values checked against the raw provenance grid, per table block.

    The block's data columns (numeric in most rows) are paired left to right with `value_columns` when their counts
    match; the pairing is trusted only if >= 75% of the matched rows' values agree with it. Each row group is matched
    to the logical grid row holding most of its values (unique best, >= 60%); a raw row whose numbers sit outside the
    data columns (a shifted rendering) is skipped. Findings: a value whose raw cell at that row and column reads
    something else ("misplaced"), and a raw data row no row group accounts for ("unreconstructed_row")."""
    grid = _logical_grid_rows(paper_id, classification.table_anchors)
    if not grid:
        return []

    def numeric_cols(cells: list[str]) -> set[int]:
        return {i for i, c in enumerate(cells) if c and _PLAIN_NUMBER_RE.match(c)}

    matches: dict[str, tuple] = {}
    for rg in classification.row_groups:
        if design._statistic_row(rg):
            continue
        values = [_cell_key(v) for v in (rg.cells or {}).values() if _cell_key(v)]
        if len(values) < 2:
            continue
        scored = [(sum(1 for v in values if v in {_cell_key(x) for x in lr[3]}), lr) for lr in grid]
        best = max(score for score, _ in scored)
        winners = [lr for score, lr in scored if score == best]
        if best >= max(2, -(-len(values) * 3 // 5)) and len(winners) == 1:
            matches[rg.row_group_id] = winners[0]

    columns = [c.value_column_id for c in classification.value_columns]
    findings: list[dict] = []
    for anchor in classification.table_anchors:
        rows = [lr for lr in grid if lr[0] == anchor]
        with_numbers = [lr for lr in rows if len(numeric_cols(lr[3])) >= 2]
        if not with_numbers:
            continue
        counts = collections.Counter(i for lr in with_numbers for i in numeric_cols(lr[3]))
        data_cols = sorted(i for i, n in counts.items() if n * 2 >= len(with_numbers))
        if len(data_cols) != len(columns):
            continue
        column_of = dict(zip(columns, data_cols))
        placed = [(rg, lr) for rg in classification.row_groups
                  if (lr := matches.get(rg.row_group_id)) is not None and lr[0] == anchor
                  and numeric_cols(lr[3]) <= set(data_cols)]
        checked = [(rg, lr, k, v) for rg, lr in placed for k, v in (rg.cells or {}).items() if k in column_of and _cell_key(v)]
        if not checked:
            continue
        agree = sum(1 for _, lr, k, v in checked if _cell_key(lr[3][column_of[k]]) == _cell_key(v))
        if agree < 0.75 * len(checked):
            continue
        for rg, lr, k, v in checked:
            source = lr[3][column_of[k]]
            if _cell_key(source) != _cell_key(v):
                findings.append({"kind": "misplaced", "row_group_id": rg.row_group_id, "value_column_id": k,
                                 "value": v, "source_value": source, "anchor": anchor})
        claimed = {id(lr) for _, lr in placed}
        for lr in with_numbers:
            if id(lr) in claimed or any(design.is_statistic_label(c) for c in lr[3]):
                continue
            if numeric_cols(lr[3]) <= set(data_cols) and not any(matches.get(rg.row_group_id) is lr for rg in classification.row_groups):
                findings.append({"kind": "unreconstructed_row", "anchor": anchor, "source_row": [c for c in lr[3] if c]})
    return findings


def _placement_message(finding: dict) -> str:
    if finding["kind"] == "unreconstructed_row":
        return (f"the source row {finding['source_row']} in {finding['anchor']} is not in row_groups -- every data row "
                f"of the table must be reconstructed.")
    return (f"row '{finding['row_group_id']}', column '{finding['value_column_id']}' has {finding['value']!r}, but the "
            f"source cell at that row and column in {finding['anchor']} reads {finding['source_value']!r} -- put each "
            f"value in the column it is printed under.")


def _withhold_cells(classification: TableClassification, findings: list[dict]) -> TableClassification:
    bad = {(f["row_group_id"], f["value_column_id"]) for f in findings if f["kind"] == "misplaced"}
    rows = [rg.model_copy(update={"cells": {k: (None if (rg.row_group_id, k) in bad else v) for k, v in (rg.cells or {}).items()}})
            for rg in classification.row_groups]
    return classification.model_copy(update={"row_groups": rows})


def _withhold_duplicate_row_groups(classification: TableClassification) -> tuple[TableClassification, list[dict]]:
    """Two row groups claiming the same factor levels cannot both be right (typically a row whose label the source
    rendering lost, filled in from the row above). The first keeps its values; every later one is withheld."""
    seen: dict[tuple, str] = {}
    kept, withheld = [], []
    for rg in classification.row_groups:
        key = tuple(sorted((k, _cell_key(v).lower()) for k, v in (rg.factor_values or {}).items() if _cell_key(v)))
        if key and not design._statistic_row(rg) and key in seen:
            withheld.append({"row_group_id": rg.row_group_id, "factor_values": dict(rg.factor_values or {}),
                             "same_levels_as": seen[key],
                             "reason": "another row of this table claims the same factor levels; which one the source "
                                       "means cannot be established, so this later row is withheld"})
            continue
        if key:
            seen.setdefault(key, rg.row_group_id)
        kept.append(rg)
    if not withheld:
        return classification, []
    return classification.model_copy(update={"row_groups": kept}), withheld


def _table_classification_prompt(
    paper_id: str, seed_table_anchor: str, other_tables: list[dict],
    prior_errors: Optional[list[dict]] = None,
    confirmed_chain: Optional[list[str]] = None,
    methods_context: str = "",
    prior_answer: Optional[dict] = None,
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
    # Continuation chain (content_reader.table_continuation_map), present only for multi-block tables.
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
    # The paper's Methods blocks are supplied verbatim so method hints can be given; a hint is kept only if the prose
    # supports it (`_withhold_ungrounded_method_hints`).
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
        f"dimension is what it IS: 'treatment' (an experimental condition the study assigns as a design "
        f"factor -- e.g. a cover-crop, tillage, fertilizer or irrigation level, or a harvest schedule), 'crop' "
        f"(an individual cultivar, variety, population or genotype), 'time' (a sampling date, growth stage, "
        f"year, season or day after planting at which values were measured), 'site' (a location), 'variable' (WHICH measured quantity a row or "
        f"column reports), 'replicate' (block or plot), or 'other'. encoding is where its levels live: "
        f"'rows' (a label column -- the level goes in each row group's factor_values), 'columns' (column "
        f"headers -- the level goes in that value column's factor_levels), or 'context' (one level for "
        f"the whole table, taken from its caption or a footnote -- goes in context_levels). "
        f"context_levels and each value column's factor_levels are JSON OBJECTS mapping a factor name to "
        f"its level: write {{}} when empty, never []. Every key "
        f"you use in factor_values, factor_levels or context_levels MUST be declared here. A "
        f"single cultivar/population is 'crop' and a location is 'site' -- neither is a 'treatment'. "
        f"{DESIGN_FACTOR_RULE} {MIXTURE_LEVEL_RULE}\n"
        f"- pooled_factors: ONLY when the table's values are means pooled over some factor that does NOT "
        f"appear in its rows or columns (a table note such as 'means across all X'): one entry per such "
        f"factor, {{name, dimension, evidence_anchor, evidence_excerpt}}, where evidence_excerpt is the "
        f"LITERAL source text (caption, footnote or body) stating the pooling. Omit when nothing is pooled.\n"
        f"- time_levels: ONLY for a table with a 'time'- or 'treatment'-dimension factor whose levels (a growth "
        f"stage, a sampling occasion, a harvest schedule, ...) the paper DATES somewhere else, usually in Methods (use read_section): one "
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
    base += _table_text_section(paper_id, seed_table_anchor, confirmed_chain)
    if prior_errors and prior_answer is not None:
        # A retry repairs the previous answer instead of re-classifying from scratch.
        base += (
            "\n\nYour previous answer is below. It failed validation with the errors listed after it. Return the SAME "
            "answer with ONLY what these errors require changed: keep every factor, its name and dimension, and every "
            "row group and cell exactly as they are unless an error is about them.\n\nPREVIOUS ANSWER:\n"
            + json.dumps(prior_answer, indent=1, ensure_ascii=False)
            + "\n\nERRORS:\n" + json.dumps(prior_errors, indent=2)
        )
    elif prior_errors:
        base += (
            "\n\nThe previous attempt failed validation with these errors -- fix exactly these, "
            "the rest of your approach was fine:\n" + json.dumps(prior_errors, indent=2)
        )
    return base


# An answer whose own reason says the table was not read (real reasons, all accepted as non_enumerable before):
# "its content and caption were not accessed", "no table data was read", "the table cells were not read",
# "ambiguous or inaccessible". A judgement about the content ("severely garbled", "reports ANOVA F-statistics") is not.
_NOT_READ_RE = re.compile(
    r"\b(?:not|never)\s+(?:been\s+)?(?:read|accessed|retrieved|opened)\b|\bwithout\s+(?:reading|accessing|opening)\b"
    r"|\b(?:could not|couldn't|unable to|cannot|can't)\s+(?:access|read|retrieve|open)\b|\binaccessible\b"
    r"|\bno\s+(?:table\s+)?(?:data|content|cells?)\s+(?:was|were)\s+(?:read|retrieved|accessed)\b",
    re.IGNORECASE)


TABLE_TEXT_MAX_CHARS = 16000


def _table_text_section(paper_id: str, seed_table_anchor: str, chain: Optional[list[str]] = None) -> str:
    """The table itself -- caption, every block of the logical table, and its notes -- verbatim from the Document Map,
    so an answer never depends on a read that a session may not have made. The read tools stay available (raw per-cell
    data, other blocks); this is the same text read_table/read_nearby return. Empty when the paper cannot be mapped."""
    try:
        dmap = document_map.build_document_map(paper_id, _papers_root())
        raw = _load_rendered_blocks(paper_id)       # line breaks kept: a markdown table stays one row per line
    except (FileNotFoundError, OSError):
        return ""
    table = dmap.table_containing(seed_table_anchor)
    anchors = list(dict.fromkeys([*(table.anchors if table else (seed_table_anchor,)), *(chain or [])]))
    parts = []
    for anchor in (table.caption_anchors if table else ()):
        parts.append(f"[{anchor}] (caption) {dmap.text(anchor)}")
    for anchor in anchors:
        text = (raw.get(anchor) or dmap.text(anchor) or "").strip()
        if text:
            parts.append(f"[{anchor}] (table)\n{text}")
    for anchor in (table.note_anchors if table else ()):
        parts.append(f"[{anchor}] (note) {dmap.text(anchor)}")
    if not parts:
        return ""
    body = "\n\n".join(parts)
    if len(body) > TABLE_TEXT_MAX_CHARS:
        body = body[:TABLE_TEXT_MAX_CHARS] + "\n... (truncated here -- read the rest with read_table)"
    return ("\n\nTHE TABLE -- verbatim from content.md (the same text read_table and read_nearby return), supplied so "
            "you can answer from it directly:\n" + body)


def _factor_structure(answer: Any) -> Optional[list[tuple[str, str]]]:
    """(name, dimension) of every declared factor of a TableClassification answer (model JSON or validated)."""
    factors = answer.get("factors") if isinstance(answer, dict) else getattr(answer, "factors", None)
    if not isinstance(factors, list):
        return None
    out = []
    for f in factors:
        name = f.get("name") if isinstance(f, dict) else getattr(f, "name", None)
        dimension = f.get("dimension") if isinstance(f, dict) else getattr(f, "dimension", None)
        out.append((str(name), str(dimension)))
    return sorted(out)


def _repair_table_classification(answer: Any) -> tuple[Any, list[dict]]:
    """Deterministic repair of a value column naming its variable by `variable_name` instead of its declared `label`;
    only when exactly one declared variable has that name. Each repair is recorded."""
    if not isinstance(answer, dict) or not isinstance(answer.get("variables"), list) or not isinstance(answer.get("value_columns"), list):
        return answer, []
    from pipeline.raw_schema import _variable_key
    variables = [v for v in answer["variables"] if isinstance(v, dict) and isinstance(v.get("label"), str)]
    labels = {_variable_key(v["label"]) for v in variables}
    repairs: list[dict] = []
    columns = []
    for column in answer["value_columns"]:
        named = column.get("variable") if isinstance(column, dict) else None
        if isinstance(named, str) and _variable_key(named) not in labels:
            matches = [v["label"] for v in variables
                       if isinstance(v.get("variable_name"), str) and _variable_key(v["variable_name"]) == _variable_key(named)]
            if len(matches) == 1:
                column = {**column, "variable": matches[0]}
                repairs.append({"value_column_id": column.get("value_column_id"), "field": "variable", "from": named,
                                "to": matches[0], "reason": "the column named the variable by its declared variable_name; "
                                                           "set to that variable's label"})
        columns.append(column)
    if not repairs:
        return answer, []
    return {**answer, "value_columns": columns}, repairs


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
    stalls = 0              # no-answer turns (see "No-answer turns: a stall is not an outage")
    nudge = False
    prior_answer: Optional[dict] = None      # the last parsed answer, shown to the retry to be repaired
    prior_structures: list[list[tuple[str, str]]] = []
    structure: Optional[list[tuple[str, str]]] = None

    def _save(failure_class: Optional[str]) -> None:
        if failure_class in ("schema_invalid", "validation_failure") and structure is not None:
            prior_structures.append(structure)
        artifact["failure_class"] = failure_class
        artifact["failure_kind"] = None if failure_class is None else ("provider" if failure_class in PROVIDER_FAILURE_CLASSES else "extraction")
        artifact["numbered_attempt"] = None if failure_class in PROVIDER_FAILURE_CLASSES or failure_class == "no_final_answer" else numbered
        if failure_class is not None:
            failure_classes.append(failure_class)
        run_store.save_stage_attempt(run_id, record_key, "table_classification", rounds, artifact)

    while numbered < MAX_TABLE_CLASSIFICATION_ATTEMPTS:
        rounds += 1
        prompt = _table_classification_prompt(
            paper_id, seed_table_anchor, other_tables, errors, chain_anchors, methods_context=methods_context,
            prior_answer=prior_answer if errors else None,
        )
        result = invoke("reader", model, prompt + (f"\n\n{_final_answer_nudge('table_classification')}" if nudge else ""))
        artifact = result.as_artifact()

        if _ended_without_answer_after_tools(result):
            # A stall: not a numbered attempt and not the provider's budget; the next round asks for the answer now.
            stalls += 1
            stall_terminal = stalls > MAX_FINAL_ANSWER_RETRIES
            artifact["validation_errors"] = [{"field": None, "message": "the model used tools and ended without a final answer"}]
            _save("no_final_answer")
            _record_stall(run_id, "table_classification", record_key, stall_terminal)
            last_message = f"round {rounds}: no final answer after tool use ({stalls} stall(s))"
            if stall_terminal:
                break
            nudge = True
            continue

        failure = _provider_failure(result)
        if failure:
            # Nothing usable came back: not a numbered attempt, and the model gets no "feedback" about a failure that was
            # not its answer. After an outage the same prompt is repeated after a cooldown; otherwise immediately,
            # asking for the answer directly.
            artifact["validation_errors"] = [{"field": None, "message": result.parse_error}]
            _save(failure)
            last_message = f"round {rounds}: provider failure ({failure}): {result.parse_error}"
            if provider.failed(failure, result):
                terminal_kind = "provider"
                break
            provider.cooldown()
            nudge = nudge or provider.needs_answer_nudge()
            continue

        numbered += 1
        attempt = numbered

        if result.parsed_json is None:
            errors = [{"field": None, "message": result.parse_error}]
            artifact["validation_errors"] = errors
            prior_answer = None
            _save("invalid_json")
            last_message = f"attempt {attempt}: {result.parse_error}"
            continue

        answer = result.parsed_json
        prior_answer = answer if isinstance(answer, dict) else None
        structure = _factor_structure(answer)
        try:
            validated = TableClassification.model_validate(answer)
        except ValidationError as exc:
            repaired, repairs = _repair_table_classification(answer)
            validated = None
            if repairs:
                try:
                    validated = TableClassification.model_validate(repaired)
                    artifact["deterministic_repairs"] = repairs
                except ValidationError:
                    validated = None
            if validated is None:
                errors = [
                    {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()
                ]
                artifact["validation_errors"] = errors
                _save('schema_invalid')
                last_message = f"attempt {attempt}: TableClassification shape validation failed: {errors}"
                continue

        if not validated.applicable and _NOT_READ_RE.search(validated.reason or ""):
            # "Not applicable" judges the table's content: an answer saying the table was not read is a failed attempt.
            errors = [{"field": "applicable", "message": (
                f"the answer says the table was not read ({validated.reason!r}). Whether a table is non_enumerable can "
                f"only be judged from its content: read it with read_table (paper_id={paper_id!r}, anchor "
                f"{seed_table_anchor!r}) and classify what it contains.")}]
            artifact["validation_errors"] = errors
            prior_answer = None               # nothing to repair: the answer is not a classification of the table
            _save("validation_failure")
            last_message = f"attempt {attempt}: the model did not read the table ({validated.reason})"
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

        # Values checked against the raw cell grid: fed back while attempts remain, then misplaced values are withheld.
        placement = _cell_placement_findings(validated, paper_id) if validated.applicable and validated.row_groups else []
        if placement and numbered < MAX_TABLE_CLASSIFICATION_ATTEMPTS:
            errors = [{"field": "row_groups", "message": _placement_message(f)} for f in placement[:20]]
            artifact["validation_errors"] = errors
            _save("validation_failure")
            last_message = f"attempt {attempt}: {len(placement)} value(s) disagree with the source cell grid"
            continue
        if placement:
            validated = _withhold_cells(validated, placement)
            artifact["placement_findings"] = placement
        validated, duplicate_rows = _withhold_duplicate_row_groups(validated)
        if duplicate_rows:
            artifact["withheld_duplicate_rows"] = duplicate_rows

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
        # Method hints the paper's prose does not support are withheld the same way.
        validated = _withhold_ungrounded_method_hints(validated, paper_id)

        # A retry that changed the declared factors is flagged for the cross-table check.
        structure_change = None
        if prior_structures and prior_structures[-1] != structure:
            structure_change = {"before": prior_structures[-1], "after": structure,
                                "note": "the accepted answer declares different factors than the answer it was asked to repair"}
            artifact["structure_changed_on_retry"] = structure_change
        artifact["validation_errors"] = []
        _save(None)
        run_store.save_final(run_id, record_key, {
            "status": "success", "classification": validated.model_dump(), "pooling_evidence": pooling_records,
            **({"variable_declaration_flags": variable_flags} if variable_flags else {}),
            **({"deterministic_repairs": artifact["deterministic_repairs"]} if artifact.get("deterministic_repairs") else {}),
            **({"structure_changed_on_retry": structure_change} if structure_change else {}),
            **({"placement_findings": artifact["placement_findings"]} if artifact.get("placement_findings") else {}),
            **({"withheld_duplicate_rows": duplicate_rows} if duplicate_rows else {}),
        })
        return validated, None

    run_store.save_final(run_id, record_key, {
        "status": "error", "message": last_message,
        # Why the table was given up on (provider or extraction failure), disclosed in the run manifest.
        "failure_class": failure_classes[-1] if failure_classes else "validation_failure",
        "failure_kind": terminal_kind, "failure_classes": failure_classes,
        "numbered_attempts": numbered, "provider_failure_rounds": provider.rounds,
    })
    return None, last_message


def _cached_table_records(run_id: str, load: Callable[[str, str], Any]) -> dict[str, Any]:
    """{record key: load(run_id, key)} for every Step B table record of this run that `load` finds."""
    keys = [k for k in run_store.list_records(run_id) if k.startswith("table_classification__")]
    return {k: v for k in keys if (v := load(run_id, k)) is not None}


def _cached_table_failures(run_id: str) -> dict[str, dict]:
    """{record key: terminal failure record} for every Step B table this run gave up on."""
    return _cached_table_records(run_id, _load_cached_table_failure)


def _load_cached_table_failure(run_id: str, record_key: str) -> Optional[dict]:
    """The terminal failure of an earlier Step B call for this table in this run, or None; a bare error record without
    a `failure_class` is retried."""
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
        if dimension not in ("time", "treatment"):
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


def _cached_classification_field(run_id: str, field_name: str) -> dict[str, Any]:
    """{record key: final.json[field_name]} for every accepted Step B classification that carries that field."""
    out: dict[str, Any] = {}
    for record_key in run_store.list_records(run_id):
        if not record_key.startswith("table_classification__"):
            continue
        path = run_store.record_dir(run_id, record_key) / "final.json"
        try:
            data = run_store.load_json(path) if path.is_file() else None
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("status") == "success" and data.get(field_name):
            out[record_key] = data[field_name]
    return out


def _cached_variable_declaration_flags(run_id: str) -> dict[str, list[dict]]:
    """{record key: variable-declaration flags} saved next to each Step B classification that was accepted with
    undeclared row-encoded variable levels."""
    return {k: list(v) for k, v in _cached_classification_field(run_id, "variable_declaration_flags").items()}


def _normalize_for_matching(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# Words that never justify a token-containment match on their own: stopwords and domain boilerplate common to every
# method hint.
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
    """Match a table row's known values (factor values plus site/method hints) against a link pool entry's name or
    slug. Each entry is scored by how many of the row's values it accounts for; the strictly highest score wins, and
    a zero or tied best score returns None (a tie is never broken arbitrarily).

      1. exact match (2 points): value equals the entry's name/slug after normalisation;
      2. substring (1 point): either side found inside the other, the searched text >= 3 characters;
      3. token containment (1 point): all of an entry's >= 2 significant tokens (`_significant_tokens`) appear as
         whole words in the value -- never a single shared word.

    Failing to match is always safe (the downstream refuse-to-guess gate handles unlinked candidates); a wrong match
    would attach a value to the wrong record."""
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


# Method matching, tier 3: a hint with no other hit may be resolved by a Method's `description` (the Methods sentence
# it was paraphrased from), only when distinctive and unambiguous.
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
    """Seed Method candidates from the method hints of this paper's data tables. A hint seeds a candidate only when:
      - it has enough distinctive tokens (`_MIN_DESCRIPTION_TOKENS`);
      - it is grounded: some prose block contains every significant token, and those blocks become its anchors;
      - no free-form candidate or earlier seed already covers it (`_pool_scores` or by description).
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


# --- Units and canonical variable name -------------------------------------------------------------------------
# A units hint travels only when the source text supports it (`reported_units` is "units as reported by the source");
# an unsupported hint is flagged, never adopted or corrected.
_PLACEHOLDER_UNITS = frozenset({"unknown", "n/a", "na", "none", "not reported", "not stated", "not given", "unspecified", "tbd", "?", "-"})



def _units_supported(hint: str, texts: list[str]) -> bool:
    """Is `hint` written in any of `texts`? Same rule as the grounding validator (`validators._unit_supported`)."""
    if not _units_key(hint):
        return True
    return _validator_unit_supported(hint, texts)


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


# --- Method hints: supplied evidence and grounding ---------------------------------------------------------------
_METHODS_HEADER_RE = re.compile(r"method", re.IGNORECASE)
_METHODS_END_HEADER_RE = re.compile(r"result|discussion|conclusion|acknowledg|reference|literature cited", re.IGNORECASE)
_METHODS_CONTEXT_MAX_CHARS = 9000
_METHODS_CONTEXT_BLOCK_CHARS = 1500


def _methods_context(paper_id: str) -> str:
    """The paper's Methods prose, verbatim and anchor-labelled, for the Step B prompt.

    The span is found positionally (Marker's `section_path` is not reliable for this): from the first SectionHeader
    containing "method" to the next one opening Results / Discussion / Conclusions / References. Each Text/ListItem
    block is cut to `_METHODS_CONTEXT_BLOCK_CHARS`, the whole to `_METHODS_CONTEXT_MAX_CHARS`. "" when there is no
    such header."""
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


_HINT_ANCHOR_RE = re.compile(r"\bb:\d{3,5}\b")


def _hint_grounded_in_cited_block(hint: str, paper_id: str) -> bool:
    """A hint that cites its block ("... - see b:0038") is grounded when that block carries at least one of the hint's
    distinctive words; a hint citing no block needs all its words in one block."""
    anchors = _HINT_ANCHOR_RE.findall(hint or "")
    if not anchors:
        return False
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return False
    words = method_map._distinctive(_HINT_ANCHOR_RE.sub(" ", hint))
    for anchor in anchors:
        text = blocks.get(anchor)
        if text and words & set(evidence_index.significant_words(text)):
            return True
    return False


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
        if variable.method_hint and not (_method_hint_grounded(variable.method_hint, prose, texts)
                                         or _hint_grounded_in_cited_block(variable.method_hint, paper_id)):
            flags.append(MethodHintFlag(scope="variable", key=variable.label, method_hint=variable.method_hint, reason=reason))
            variable = variable.model_copy(update={"method_hint": None})
        variables.append(variable)
    columns = []
    for column in classification.value_columns:
        if column.method_hint and not (_method_hint_grounded(column.method_hint, prose, texts)
                                       or _hint_grounded_in_cited_block(column.method_hint, paper_id)):
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
    """The values one cell may offer the Method matcher: the variable it reports and the method the source names for
    it (or a row factor literally named "Method"). Treatment, site, time and crop levels say which condition was
    measured, never how."""
    values = {
        name: level
        for name, (dimension, level) in _cell_dimension_levels(classification, row, value_column).items()
        if dimension == "variable" or name == "Method"
    }
    if method_hint:
        values.setdefault("Method", method_hint)
    return values


# Row labels that are statistics or summaries, never an experimental condition (shared with design.py).
_STATISTIC_ROW_RE = design.STATISTIC_ROW_RE
_SUMMARY_ROW_RE = design.SUMMARY_ROW_RE


def _summary_row_level(row: Any) -> Optional[str]:
    """The summary/statistic label of a row group ("Mean", "LSD (0.05)", "P value"), or None for a real condition."""
    for value in (getattr(row, "factor_values", None) or {}).values():
        if isinstance(value, str) and (_STATISTIC_ROW_RE.match(value.strip()) or _SUMMARY_ROW_RE.match(value.strip())):
            return value
    return None


def _blocked_cell(classification: TableClassification, row: Any, value_column_id: str, cell_text: str,
                  pooling: Any) -> dict[str, Any]:
    """A table value extracted deterministically (the cell text IS the value, anchored to its table) that the current
    IR cannot represent -- kept with what it is and why, never forced onto an invented Treatment."""
    column = next((c for c in classification.value_columns if c.value_column_id == value_column_id), None)
    return {
        "table_anchor": row.source_table_anchor, "row": row.row_group_id, "factor_levels": dict(row.factor_values or {}),
        "variable": (column.variable or column.variable_name_hint) if column else value_column_id,
        "value_text": cell_text.strip(), "pooled_over": pooling.pooled_over, "status": design.BLOCKED_BY_REPRESENTATION,
        "reason": pooling.reason, "evidence": pooling.evidence,
    }


def _layout_pooling_context(classification: TableClassification, pooling: Any) -> dict[str, Any]:
    """Candidate context for a value the table's LAYOUT shows to be pooled (a main-effect row): aggregated_mean over the
    factors it averages, with the time levels those cover. Not a quoted pooling statement -- the note says so."""
    if pooling is None:
        return {}
    context = {
        "reported_effect_scope": "aggregated_mean", "aggregated_over_factors": pooling.pooled_over,
        "layout_pooling": pooling.evidence,
    }
    span = design.time_span(classification, pooling.pooled_over)
    if span:
        context["pooled_time_levels"] = span
    return context


def _table_classification_to_candidates(
    classification: TableClassification, link_pools: dict[str, list[dict]],
    method_links: Optional[dict[str, Any]] = None,
    blocked_sink: Optional[list[dict]] = None,
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
    main_effects = design.is_main_effect_layout(classification)
    if classification.table_role != "treatment_response" and not (
            classification.table_role == "aggregated_summary" and main_effects):
        # An aggregated summary whose pooling the layout explains (main-effect rows) is expressed per cell as aggregated
        # means below; any other aggregated summary keeps the existing gate.
        return candidates
    # A table pooled over a factor the IR cannot represent (site: L2) or with nothing left to reference (no treatment:
    # L1) is registered as an aggregated source, never expanded into cell-level candidates.
    representable, _why = _pooled_representability(classification)
    if not representable:
        return candidates
    pooled_context = _pooled_context(classification)

    value_columns_by_id = {c.value_column_id: c for c in classification.value_columns}

    for row in classification.row_groups:
        summary = _summary_row_level(row)
        if summary is not None:
            # A Mean/Total row aggregates over the factor it replaces; a statistic row (SE, LSD, P) is not a value. A mean
            # over a treatment factor has no Treatment to reference.
            if blocked_sink is not None and not _STATISTIC_ROW_RE.match(summary.strip()):
                pooling = design.summary_row_pooling(classification, row, summary)
                for value_column_id, cell_text in (row.cells or {}).items():
                    if cell_text and re.search(r"\d", cell_text) and pooling.representation == design.BLOCKED_BY_REPRESENTATION:
                        blocked_sink.append(_blocked_cell(classification, row, value_column_id, cell_text, pooling))
            continue
        for value_column_id, cell_text in (row.cells or {}).items():
            if not cell_text or not cell_text.strip():
                continue
            value_column = value_columns_by_id.get(value_column_id)
            if value_column is None:
                continue  # schema validation already guarantees this can't happen; defensive only
            layout_pooling = design.cell_pooling(classification, row, value_column_id) if main_effects else None
            if layout_pooling is not None and layout_pooling.representation == design.UNIDENTIFIED_ROW:
                if blocked_sink is not None:   # recorded, never counted as a blocked measurement
                    blocked_sink.append({**_blocked_cell(classification, row, value_column_id, cell_text, layout_pooling),
                                         "status": design.UNIDENTIFIED_ROW})
                continue
            if layout_pooling is not None and layout_pooling.representation == design.BLOCKED_BY_REPRESENTATION:
                if blocked_sink is not None and re.search(r"\d", cell_text):   # a "–" is not a reported value
                    blocked_sink.append(_blocked_cell(classification, row, value_column_id, cell_text, layout_pooling))
                continue

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

            # Column-encoded site/method hints are merged in under their own keys so linking works the same way.
            match_values = dict(row.factor_values or {})
            if value_column.site_hint:
                match_values.setdefault("Site", value_column.site_hint)
            if method_hint:
                match_values.setdefault("Method", method_hint)
            if value_column.treatment_level_hint:
                # Column-encoded treatment identity is merged in too, for Observation.treatment_id linking.
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
                    if match is None and method_links:
                        # The paper-level Variable -> Method map (decided once, with evidence).
                        link = method_map.lookup(method_links, variable.label if variable else None, variable_label,
                                                 variable.variable_name if variable else None)
                        if link is not None and link.status == method_map.LINKED:
                            match = link.method_slug
                elif field == "variable_id":
                    # The variable link rests on the variable's own label/name only; every spelling that resolves to one
                    # Variable record shares its name.
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
                context={**pooled_context, **_temporal_context(_time_levels_for_cell(classification, row, value_column)),
                         **_layout_pooling_context(classification, layout_pooling),
                         "cell": {"table_anchor": row.source_table_anchor, "table_anchors": list(classification.table_anchors),
                                  "row_levels": dict(row.factor_values or {}), "column": value_column_id,
                                  "column_label": variable_label, "value_text": cell_text.strip(),
                                  "column_variable": (variable.label if variable else None) or value_column.variable,
                                  "variable_name": variable.variable_name if variable else None,
                                  "column_levels": dict(value_column.factor_levels or {})}},
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

    return _promote_design_factors(run_id, paper_id, classifications)


# A sentence naming the factors assigned to the plots of the design ("whole plots were X and subplots were Y").
_DESIGN_PLOT_RE = re.compile(
    r"\b(?:whole|main|sub|split)[- ]?plots?\s+(?:were|was|consisted of|comprised)\s+((?:[A-Za-z][\w-]*\s*){1,4}?)"
    r"(?=\s*(?:\band\b|[,.;(]|$))", re.I)


def _design_factor_statements(paper_id: str) -> list[tuple[str, str]]:
    """(anchor, phrase) for every phrase the paper uses to say which factor its plots were assigned."""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return []
    return [(anchor, m.group(1).strip()) for anchor, text in blocks.items() for m in _DESIGN_PLOT_RE.finditer(text)
            if not m.group(1).split()[0].lower().endswith("ed")]   # "were harvested ..." is an action, not a factor


def _names_factor(phrase: str, factor_name: str) -> bool:
    words = re.findall(r"[a-z]+", factor_name.lower())
    stems = [w[:max(4, len(w) - 3)] for w in words if len(w) >= 4]
    return bool(stems) and all(stem in phrase.lower() for stem in stems)


def _promote_design_factors(
    run_id: str, paper_id: str, classifications: dict[str, TableClassification],
) -> dict[str, TableClassification]:
    """A `time` factor that the paper's design statement names as a plot factor ("subplots were sward maturities") is
    a `treatment` in every table (protocol Section 6.3). Written back to each table's final.json with the evidence, so
    every later reader of the classification sees the same dimension."""
    statements = _design_factor_statements(paper_id)
    if not statements:
        return classifications
    out = {}
    for seed, classification in classifications.items():
        promoted = []
        factors = []
        for factor in classification.factors:
            evidence = next(((a, ph) for a, ph in statements if _names_factor(ph, factor.name)), None)
            if factor.dimension == "time" and evidence:
                factors.append(factor.model_copy(update={"dimension": "treatment"}))
                promoted.append({"factor": factor.name, "declared": "time", "corrected_to": "treatment",
                                 "evidence_anchor": evidence[0], "evidence_text": evidence[1]})
            else:
                factors.append(factor)
        if promoted:
            classification = classification.model_copy(update={"factors": factors})
            record_key = f"table_classification__{seed.replace(':', '_')}"
            path = run_store.record_dir(run_id, record_key) / "final.json"
            if path.is_file():
                final = run_store.load_json(path)
                final["classification"] = classification.model_dump()
                final["design_factor_promotions"] = promoted
                run_store.save_final(run_id, record_key, final)
        out[seed] = classification
    return out


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
    """Step C, Treatment projection: a Treatment is the distinct combination of factor levels applied to one unit
    (row_group.factor_values plus, for column-encoded tables, the column's site hint) -- the same combination the
    Observation projection uses for linking. Deduplicated by combination across every non-blank cell; links are
    resolved with _match_row_group_to_pool, never guessed."""
    seen: set[tuple] = set()
    candidates: list[EnumerationCandidate] = []
    covered_anchors: set[str] = set()

    for classification in classifications:
        # Same role gate as Observation's projection: aggregated summaries and weather tables mint no Treatments.
        if classification.table_role != "treatment_response":
            continue
        if not _pooled_representability(classification)[0]:
            continue  # registered as an aggregated source instead (see summarize_table_pass)
        value_columns_by_id = {c.value_column_id: c for c in classification.value_columns}
        contributed = False
        # A table whose experimental dimensions were DECLARED (`factors`) has
        # been analysed for what a Treatment is: only treatment-dimension
        # factors (plus the site) can form a Treatment's identity -- a
        # cultivar/population is `crop`, a date or growth stage is `time` unless assigned as a design factor
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
        # A table where any value column sets treatment_level_hint is column-encoded: the treatment comes from the
        # column (plus site), and row factors are context for the observation, not part of the Treatment.
        column_encoded = any(vc.treatment_level_hint for vc in classification.value_columns)

        for row in classification.row_groups:
            if _summary_row_level(row) is not None:
                continue   # "Mean" is never a Treatment
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


def _apply_factor_consistency(
    classifications: dict[str, TableClassification],
) -> tuple[dict[str, TableClassification], "design.FactorConsistency"]:
    """The paper's tables checked against each other before any Treatment or Observation is derived. A withheld table
    (design.factor_consistency) is left out of both projections; a level spelled with another table's name of the
    same factor is compared by that level, so one Treatment is not minted twice."""
    consistency = design.factor_consistency(classifications)
    kept: dict[str, TableClassification] = {}
    for seed, classification in classifications.items():
        if seed in consistency.withheld:
            continue
        aliases = consistency.level_aliases.get(seed) or {}
        if aliases:
            classification = classification.model_copy(update={"row_groups": [
                row.model_copy(update={"factor_values": {
                    name: aliases.get(name, {}).get(value, value) for name, value in (row.factor_values or {}).items()
                }}) for row in classification.row_groups
            ]})
        kept[seed] = classification
    return kept, consistency


def run_table_enumeration(
    *, run_id: str, paper_id: str, entity_type: str, model: str,
    invoke: Callable[..., AgentInvocation] = invoke_agent,
    link_pools: Optional[dict[str, list[dict]]] = None,
    dimension_pools: Optional[dict[str, list[dict]]] = None,
    method_links: Optional[dict[str, Any]] = None,
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

    all_classifications = classifications
    classifications, consistency = _apply_factor_consistency(classifications)
    if consistency.withheld or consistency.level_aliases:
        run_store.save_stage_attempt(
            run_id, f"{entity_type}__enumeration", "factor_consistency", 1, consistency.to_artifact(),
        )
    # A withheld table is accounted for (its finding is recorded): the free-form pass is not invited to re-derive
    # Treatments or Observations from the same table whose structure was just shown to be misread.
    withheld_anchors = {a for seed in consistency.withheld for a in all_classifications[seed].table_anchors}

    if entity_type == "Treatment":
        identity_notes: list[dict] = []
        candidates, covered = _table_classifications_to_treatment_candidates(
            list(classifications.values()), link_pools or {}, identity_notes,
        )
        if identity_notes:
            run_store.save_stage_attempt(
                run_id, f"{entity_type}__enumeration", "identity_check", 1, {"ambiguous_identities": identity_notes},
            )
        return candidates, covered | withheld_anchors

    all_candidates: list[EnumerationCandidate] = []
    covered_anchors: set[str] = set()
    blocked_cells: list[dict] = []
    for classification in classifications.values():
        candidates = _table_classification_to_candidates(classification, link_pools or {}, method_links, blocked_cells)
        if candidates:
            all_candidates.extend(candidates)
            covered_anchors.update(classification.table_anchors)
        elif any(cell["table_anchor"] in classification.table_anchors for cell in blocked_cells):
            covered_anchors.update(classification.table_anchors)   # fully accounted for, as blocked cells

    unidentified = [c for c in blocked_cells if c.get("status") == design.UNIDENTIFIED_ROW]
    blocked_cells = [c for c in blocked_cells if c.get("status") != design.UNIDENTIFIED_ROW]
    if blocked_cells or unidentified:
        # Extracted but not representable: a run artifact and a manifest section, never silently dropped. Rows the
        # classification left unidentified are listed separately.
        run_store.save_stage_attempt(run_id, f"{entity_type}__enumeration", "representation_blocked", 1, {
            "reason": design.L4_TREATMENT_POOLED, "cells": blocked_cells, "unidentified_rows": unidentified})
        _REPRESENTATION_BLOCKED[run_id] = blocked_cells
        _UNIDENTIFIED_ROWS[run_id] = unidentified
    return all_candidates, covered_anchors | withheld_anchors


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
    """Drops a free-form candidate grounded in prose when every link it resolved (e.g. site_id) is also resolved by
    some table candidate. Compares only resolved links, never names; a candidate with no links, or a link no table
    candidate resolved, is kept."""
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
    """The normalised forms under which a declared level may appear in the source: as written, and without a
    qualifier the model added (outside brackets, before ':' ';' ',' or a spaced dash). A form made only of generic
    words is discarded."""
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
    declared_dimensions: Optional[dict[str, str]] = None, declared_factors: Optional[dict[str, str]] = None,
) -> list[CandidateDimension]:
    """A level (or else a factor name) the paper's tables declare with one dimension takes that dimension; otherwise a
    level declared `treatment` that is exactly a ready Crop/Site record of this run is a crop/site (protocol 6.3)."""
    out = []
    for d in candidate.dimensions:
        new_dimension = ((declared_dimensions or {}).get(design._level_key(d.level))
                         or (declared_factors or {}).get(design._key(d.name)) or d.dimension)
        if new_dimension == "treatment":
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
    declared_dimensions: Optional[dict[str, str]] = None, declared_factors: Optional[dict[str, str]] = None,
) -> tuple[list[EnumerationCandidate], list[dict]]:
    """Semantic free-form/table Treatment dedup, by the same canonical identity (treatment-dimension levels + resolved
    site; key names never matter). A free-form candidate is:
      - dropped as covered when its identity equals an unambiguous table candidate's;
      - dropped as not-a-Treatment when it declares no treatment-dimension level (protocol Section 6.3);
      - kept (and flagged) when it declares no dimensions or its identity is ambiguous;
      - kept when its identity matches no table candidate.
    Dedup only removes; it never edits a surviving candidate. Returns (kept, decisions)."""
    reconciled = {c.candidate_id: _reconcile_candidate_dimensions(c, dimension_pools, declared_dimensions, declared_factors)
                  for c in freeform_candidates}
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
    """Table and free-form candidates can choose the same candidate_id; two candidates on one record_id would
    overwrite each other's artifacts. Returns (candidates, collision_notes) in input order, none dropped or merged: a
    second-and-later duplicate gets a stable numeric suffix."""
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
    """Flags candidate pairs whose candidate_id differs by at most `_NEAR_DUPLICATE_MAX_EDIT_DISTANCE` characters
    (e.g. an OCR typo producing a phantom second population). Detects only: never merges, drops or relabels. Exact
    collisions are `_dedupe_candidate_record_ids`'s job."""
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
    """Every successfully cached Step B classification of this run, keyed by its run_store record key."""
    return _cached_table_records(run_id, _load_cached_table_classification)


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
    repairs = _cached_classification_field(run_id, "deterministic_repairs")
    structure_changes = _cached_classification_field(run_id, "structure_changed_on_retry")
    withheld = design.factor_consistency(reconciled).withheld
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
            **({"deterministic_repairs": repairs[key]} if key in repairs else {}),
            **({"structure_changed_on_retry": structure_changes[key]} if key in structure_changes else {}),
            **({"withheld": withheld[key]} if key in withheld else {}),
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


def _species_names(payload: dict, common_names: dict[str, set[str]]) -> set[str]:
    """Every way the paper names one Species record: its binomial, "G. epithet", its genus alone is NOT enough (two
    species may share it), its extracted common name, and the common names the paper itself pairs with it."""
    def value(field: str) -> Optional[str]:
        entry = payload.get(field)
        return entry.get("value") if isinstance(entry, dict) and isinstance(entry.get("value"), str) else None

    names: set[str] = set()
    genus, epithet = value("genus"), value("species_epithet")
    scientific = value("scientific_name") or (f"{genus} {epithet}" if genus and epithet else None)
    if scientific:
        parts = scientific.split()
        if len(parts) >= 2:
            names |= {f"{parts[0]} {parts[1]}".lower(), f"{parts[0][0]}. {parts[1]}".lower()}
            names |= common_names.get(f"{parts[0]} {parts[1]}".lower(), set())
    if value("common_name"):
        names.add(value("common_name").lower())
    return {n for n in names if len(n) > 2}


def _evidenced_species(paper_id: str, candidate: Any, this_run_records: dict) -> tuple[list[str], dict]:
    """The referenceable Species a Crop candidate's OWN evidence names (its anchors and their reading-order
    neighbours; never the model-written description). Returns (record ids, decision log)."""
    species = _referenceable_records(this_run_records, "Species")
    try:
        dmap = document_map.build_document_map(paper_id, _papers_root())
    except (FileNotFoundError, OSError):
        return [], {"decision": "no_document", "message": "species link not verifiable: the paper is not prepared"}
    index = evidence_index.build_evidence_index(dmap)
    anchors = list(candidate.anchors)
    for anchor in candidate.anchors:
        anchors += [b.anchor for b in dmap.neighbors(anchor, before=1, after=1)]
    text = " " + evidence_index.normalize(" ".join(dmap.text(a) for a in dict.fromkeys(anchors))) + " "
    compact = re.sub(r"\s+", "", text)
    common = index.common_names()
    evidenced = []
    for record in species:
        names = _species_names(((record.get("detail") or {}).get("payload")) or {}, common)
        for name in names:
            normalized = evidence_index.normalize(name)
            if re.search(rf"(?<![a-z]){re.escape(normalized)}(?![a-z])", text) or (
                    # a scientific name split by a hyphenation break still names it: compared with spaces removed
                    " " in normalized and "." not in normalized and len(normalized) > 10
                    and re.sub(r"\s+", "", normalized) in compact):
                evidenced.append(record["record_id"])
                break
    if evidenced:
        return evidenced, {"decision": "species_evidenced", "species_ids": evidenced, "anchors": anchors}
    extracted = [r["record_id"] for r in species]
    return [], {"decision": "species_not_evidenced", "anchors": anchors, "extracted_species": extracted, "message": (
        f"the crop's own evidence ({', '.join(dict.fromkeys(anchors))}) names none of the Species extracted in this run "
        f"({', '.join(extracted) or 'none'}): its species link cannot be established without guessing, so the crop is "
        f"not linked by elimination")}


def _build_variable_method_map(
    run_id: str, paper_id: str, model: str, invoke: Callable[..., AgentInvocation], this_run_records: dict,
    link_pools: dict[str, list[dict]],
) -> Optional[dict[str, Any]]:
    """The paper's Variable -> Method map (`pipeline/method_map.py`), decided once before Observation candidates are
    linked, when the paper has more than one referenceable Method. Saved as the `variable_method_map` artifact."""
    record_key = "Observation__enumeration"
    pool = link_pools.get("method_id") or []
    if len(pool) < 2:
        return None
    classifications = run_table_classification_pass(run_id=run_id, paper_id=paper_id, model=model, invoke=invoke)
    entries = method_map.collect_variables(classifications.values())
    if not entries:
        return None
    records = [{**r, "slug": _candidate_slug_from_record_id(paper_id, "Method", r["record_id"])}
               for r in _referenceable_records(this_run_records, "Method")]
    methods = method_map.method_evidence(records)
    try:
        index = evidence_index.build_evidence_index(document_map.build_document_map(paper_id, _papers_root()))
        packet_bundle = context_bundle.build_context_bundle(paper_id, "Method", "enumeration", papers_root=_papers_root())
    except (FileNotFoundError, OSError):
        return None
    calls = {"n": 0}

    def hint_resolver(entry: method_map.VariableEntry) -> Optional[str]:
        for hint in entry.hints:
            match = _match_method_hint({"Variable": entry.label}, hint, pool)
            if match:
                return match
        return None

    def ask_model(prompt: str) -> Optional[dict]:
        calls["n"] += 1
        result = invoke("reader", model, prompt)
        artifact = result.as_artifact()
        failure = _provider_failure(result)
        if failure:
            artifact.update(failure_class=failure, failure_kind="provider")
        run_store.save_stage_attempt(run_id, record_key, "variable_method_map_call", calls["n"], artifact)
        return None if failure else result.parsed_json

    links = method_map.build_map(entries, methods, index, hint_resolver, ask_model,
                                 packet_bundle.render() if packet_bundle is not None else None)
    run_store.save_stage_attempt(run_id, record_key, "variable_method_map", 1, method_map.to_artifact(entries, links))
    return links


def _record_by_id(this_run_records: dict, entity_type: str, record_id: Any) -> Optional[dict]:
    if not isinstance(record_id, str):
        return None
    records = this_run_records.get(entity_type)
    for record in (records if isinstance(records, list) else [records] if records else []):
        if record.get("record_id") == record_id:
            return record
    return None


def _payload_value(record: Optional[dict], field: str) -> Optional[Any]:
    payload = ((record or {}).get("detail") or {}).get("payload") or {}
    entry = payload.get(field)
    return entry.get("value") if isinstance(entry, dict) else None


def _record_anchors(record: Optional[dict], limit: int = 3) -> list[str]:
    return sorted(method_map._payload_anchors(((record or {}).get("detail") or {}).get("payload") or {}))[:limit]


def _observation_experimental_context(
    paper_id: str, candidate: Any, known_refs: dict[str, Any], this_run_records: dict,
    method_links: Optional[dict[str, Any]], design_summary: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The chain cell -> variable -> method -> treatment/factor -> time -> site -> aggregation -> statistics -> design
    for one Observation candidate, read from what earlier stages established. Nothing is decided here: an unresolved
    link is reported as unresolved. Returns (context, {role: evidence anchors})."""
    context: dict[str, Any] = {}
    evidence: dict[str, list[str]] = {}
    cell = (candidate.context or {}).get("cell") or {}
    try:
        dmap = document_map.build_document_map(paper_id, _papers_root())
    except (FileNotFoundError, OSError):
        dmap = None

    # cell
    table = dmap.table_containing(cell["table_anchor"]) if dmap is not None and cell.get("table_anchor") else None
    if cell:
        context["cell"] = {**{k: cell[k] for k in ("table_anchor", "row_levels", "column_label", "value_text")},
                           "table_label": table.label if table else None}
        evidence["table"] = (list(table.caption_anchors) + [table.anchors[0]] + list(table.note_anchors[:1])) if table else [cell["table_anchor"]]

    # variable
    variable_record = _record_by_id(this_run_records, "Variable", known_refs.get("variable_id"))
    label = cell.get("column_label") or candidate.variable_name_hint
    context["variable"] = {
        "label": label, "name": cell.get("variable_name") or _payload_value(variable_record, "name"),
        "header_units": candidate.units_hint, "record_id": (variable_record or {}).get("record_id"),
        "record_units": _payload_value(variable_record, "units"),
    }
    evidence["variable"] = _record_anchors(variable_record, 2)

    # method -- the paper-level map's decision for this variable, never re-decided per cell
    method_id = known_refs.get("method_id")
    link = method_map.lookup(method_links or {}, cell.get("column_variable"), label, cell.get("variable_name")) \
        if method_links else None
    method_record = _record_by_id(this_run_records, "Method", method_id)
    if link is not None:
        context["method"] = {"status": link.status, "tier": link.tier, "reason": link.reason,
                             "record_id": link.method_record_id, "name": _payload_value(
                                 _record_by_id(this_run_records, "Method", link.method_record_id), "name")}
        evidence["method"] = list(link.anchors) or _record_anchors(_record_by_id(this_run_records, "Method", link.method_record_id))
    elif isinstance(method_id, str):
        context["method"] = {"status": "linked", "tier": "single_or_linked", "reason": "resolved by the existing linker",
                             "record_id": method_id, "name": _payload_value(method_record, "name")}
        evidence["method"] = _record_anchors(method_record)
    else:
        context["method"] = {"status": "ambiguous" if isinstance(method_id, list) else "none",
                             "reason": "no established link for this variable", "record_id": None}

    # treatment / factor levels
    treatment_id = known_refs.get("treatment_id")
    treatment_record = _record_by_id(this_run_records, "Treatment", treatment_id)
    levels = {**(cell.get("row_levels") or {}), **(cell.get("column_levels") or {})}
    factors = (design_summary or {}).get("factors") or {}
    context["treatment"] = {
        "record_id": treatment_id if isinstance(treatment_id, str) else None,
        "candidates": treatment_id if isinstance(treatment_id, list) else None,
        "name": _payload_value(treatment_record, "name"), "definition": _payload_value(treatment_record, "definition"),
        "levels": {k: {"level": v, "dimension": (factors.get(k) or {}).get("dimension")} for k, v in levels.items()},
    }
    evidence["treatment"] = _record_anchors(treatment_record, 2)

    # time
    time: dict[str, Any] = {}
    for tl in (candidate.context or {}).get("temporal_context") or []:
        time[f"{tl['factor']}={tl['level']}"] = tl.get("date_text") or tl.get("year_text") or "dated in the paper"
    for name, info in levels.items():
        if (factors.get(name) or {}).get("dimension") == "time":
            time.setdefault(name, info)
    if (candidate.context or {}).get("pooled_time_levels"):
        time["pooled over"] = candidate.context["pooled_time_levels"]
    if time:
        context["time"] = time

    # site
    site_record = _record_by_id(this_run_records, "Site", known_refs.get("site_id"))
    if site_record is not None:
        context["site"] = f"{site_record['record_id']} ({_payload_value(site_record, 'name') or 'name unresolved'})"

    # aggregation
    scope = (candidate.context or {}).get("reported_effect_scope") or "treatment_mean"
    context["aggregation"] = {
        "reported_effect_scope": scope, "aggregated_over_factors": (candidate.context or {}).get("aggregated_over_factors") or [],
        "basis": "table layout (main-effect row)" if (candidate.context or {}).get("layout_pooling")
                 else "pooling statement" if (candidate.context or {}).get("pooling_evidence") else "cell mean",
    }

    # statistics -- the cell's own parts; the statistic named only where the table states it
    parsed = cell_values.parse_cell(cell.get("value_text") or candidate.known_value or "")
    if parsed is not None:
        sources = []
        if dmap is not None and table is not None:
            sources = [(table.anchors[0], "\n".join(dmap.text(table.anchors[0]).splitlines()[:4]))]
            sources += [(a, dmap.text(a)) for a in list(table.caption_anchors) + list(table.note_anchors)]
        basis = cell_values.statistic_basis(sources, parsed)
        statistic_value = (f"[{parsed.interval[0]:g}, {parsed.interval[1]:g}]" if parsed.interval
                           else parsed.dispersion if parsed.dispersion is not None else parsed.parenthetical)
        context["statistics"] = {
            "mean_text": parsed.mean_text, "mean": parsed.mean, "dispersion": parsed.dispersion,
            "interval": list(parsed.interval) if parsed.interval else None,
            "statistic_name": basis.name, "statistic_value": statistic_value if basis.name else None,
            "statistic_source_anchor": basis.source_anchor, "statistic_source_text": basis.source_text,
            "letters": parsed.letters if basis.letters_meaning else None, "letters_meaning": basis.letters_meaning,
            "sample_size_evidence": [e["anchor"] for e in (design_summary or {}).get("sample_size_evidence") or []] or None,
        }
        evidence["design"] = [e["anchor"] for e in (design_summary or {}).get("sample_size_evidence") or []][:1]

    # design
    table_entry = next((t for t in (design_summary or {}).get("tables") or []
                        if cell.get("table_anchor") in (t.get("anchors") or [])), None)
    relevant_conflicts = [c for c in (design_summary or {}).get("conflicts") or [] if c.get("subject") in levels]
    context["design"] = {
        "layout": (table_entry or {}).get("layout"), "factors": list(levels) or None,
        "conflicts": [f"{c['claim_a']} ({c['source_a']}) vs {c['claim_b']} ({c['source_b']})" for c in relevant_conflicts] or None,
    }
    evidence["design"] = evidence.get("design", []) + [c["source_b"] for c in relevant_conflicts]
    return context, evidence


def _packet_evidence_found(bundle: Any) -> Optional[list[str]]:
    """The concepts an enumeration packet FOUND, as "thinning (b:0030, b:0006)" -- for the empty-answer re-ask."""
    if bundle is None:
        return None
    found = [f"{c.concept} ({', '.join(c.anchors[:3])})" for c in bundle.coverage if c.status == context_bundle.FOUND]
    return found or None


def _evidence_target(entity_type: str, candidate: Any) -> Optional[str]:
    """What a record's evidence packet is conditioned on: a Variable is looked up by its own name (the table's variable
    hint, else the candidate slug); other entity types by their anchors alone."""
    if entity_type != "Variable":
        return None
    hint = getattr(candidate, "variable_name_hint", None)
    return hint or str(getattr(candidate, "candidate_id", "") or "").replace("_", " ") or None


def _run_multi_record_entity(
    *, run_id: str, paper_id: str, entity_type: str, model: str, client: IRServiceClient,
    invoke: Callable[..., AgentInvocation] = invoke_agent, enable_ai_validation: bool,
    this_run_records: dict,
) -> list[dict]:
    """Enumeration plus one independent `run_record()` call per candidate. For a type depending on another multi-record
    type (Observation on Treatment/Variable), `_multi_record_link_pools` offers that type's ready records to the
    enumeration prompt and `_apply_candidate_links` turns each candidate's verified links into per-candidate known_refs.
    A structurally blocked entity (a required prerequisite not ready) is short-circuited before enumeration with one
    "blocked" entry."""
    recorded_records = this_run_records
    this_run_records, pending = _with_pending(entity_type, recorded_records)
    known_refs, blocked_reason = _resolve_known_refs(entity_type, this_run_records)
    if blocked_reason is not None:
        if entity_type == "Observation" and "Treatment" in blocked_reason:
            limitation = _treatment_dimension_limitation(run_id, paper_id, recorded_records)
            if limitation:
                blocked_reason = f"{blocked_reason}. {limitation}"
        return [{
            "entity_type": entity_type, "record_id": _entity_record_id(paper_id, entity_type),
            "status": "blocked", "reason": blocked_reason,
        }]

    link_pools = _multi_record_link_pools(paper_id, entity_type, this_run_records)
    prerequisite_notes = _prerequisite_notes(entity_type, recorded_records)

    # Step D (table enumeration, see the module section above
    # run_table_enumeration's own definition): for entity types with a
    # confirmed free-form under-enumeration problem on dense data tables,
    # deterministically generate candidates from every table first, then
    # tell the free-form pass which anchors are already covered so it
    # doesn't re-report (and duplicate) them.
    table_candidates: list = []
    covered_table_anchors: set[str] = set()
    method_links: Optional[dict[str, Any]] = None
    design_summary: Optional[dict[str, Any]] = None
    if entity_type in TABLE_ENUMERATION_ENTITY_TYPES:
        method_links = (
            _build_variable_method_map(run_id, paper_id, model, invoke, this_run_records, link_pools or {})
            if entity_type == "Observation" else None
        )
        design_summary = _design_section(run_id, paper_id) if entity_type == "Observation" else None
        table_candidates, covered_table_anchors = run_table_enumeration(
            run_id=run_id, paper_id=paper_id, entity_type=entity_type, model=model, invoke=invoke,
            link_pools=link_pools or None, dimension_pools=_dimension_pools(paper_id, this_run_records),
            method_links=method_links,
        )

    # For Treatment covered by tables, free-form candidates are compared with table candidates by canonical identity.
    semantic_dedup = entity_type == "Treatment" and bool(covered_table_anchors or table_candidates)
    enumeration_bundle = None
    try:
        enumeration_bundle = context_bundle.build_context_bundle(
            paper_id, entity_type, "enumeration", papers_root=_papers_root())
    except Exception as exc:  # a packet is an aid, never a reason to lose the enumeration
        run_store.save_stage_attempt(run_id, f"{entity_type}__enumeration", "context_bundle_error", 1,
                                     {"error": f"{type(exc).__name__}: {exc}"})
    if enumeration_bundle is not None:
        run_store.save_stage_attempt(run_id, f"{entity_type}__enumeration", "context_bundle", 1, enumeration_bundle.to_dict())
    freeform_candidates, enum_error = run_enumeration(
        run_id=run_id, paper_id=paper_id, entity_type=entity_type, model=model, invoke=invoke,
        link_pools=link_pools or None,
        excluded_table_anchors=covered_table_anchors or None,
        covered_conditions=_covered_condition_labels(table_candidates) if semantic_dedup else None,
        declare_dimensions=semantic_dedup,
        evidence_packet=enumeration_bundle.render() if enumeration_bundle is not None else None,
        evidence_found=_packet_evidence_found(enumeration_bundle),
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
            design.declared_level_dimensions(classifications := _cached_table_classifications(run_id)),
            design.declared_factor_dimensions(classifications),
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
        # A distinct, grounded method hint from the tables may seed a Method candidate free-form did not produce.
        table_candidates, seed_decisions = _method_hint_seeds(
            run_id=run_id, paper_id=paper_id, model=model, invoke=invoke, freeform_candidates=freeform_candidates,
        )
        if seed_decisions:
            run_store.save_stage_attempt(run_id, f"{entity_type}__enumeration", "method_hint_seeds", 1, {"decisions": seed_decisions})

    candidates = table_candidates + freeform_candidates
    if enum_error is not None and not candidates:
        return []
    outcome = _ENUMERATION_OUTCOMES.pop((run_id, entity_type), None)
    if not candidates and outcome is not None:
        # Never a silent zero: the entity type is recorded as unresolved, with why.
        record_id = f"{paper_id}_{entity_type.lower()}_enumeration"
        message = (
            f"no {entity_type} candidates could be established: the enumeration answered with an empty list after an "
            f"interrupted turn (stall / provider failure) and again on a clean re-ask -- not evidence that the paper "
            f"reports none"
            + (f" (the evidence packet holds: {'; '.join(outcome['evidence_found'])})" if outcome["evidence_found"] else "")
        )
        detail = {"status": "unresolved", "last_errors": [{"field": None, "message": message}],
                  "enumeration_outcome": outcome, "unresolved_cause": outcome["cause"]}
        run_store.save_final(run_id, f"{entity_type}__{record_id}", detail)
        return [{"entity_type": entity_type, "record_id": record_id, "status": "unresolved", "detail": detail}]

    # Two candidates never collide onto one record_id (see _dedupe_candidate_record_ids); collisions are logged.
    candidates, collision_notes = _dedupe_candidate_record_ids(candidates)
    if collision_notes:
        run_store.save_stage_attempt(
            run_id, f"{entity_type}__enumeration", "candidate_collision", 1,
            {"collisions": collision_notes},
        )

    # Near-duplicate spellings are disclosed, never resolved automatically (see _flag_near_duplicate_candidates).
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
        refusals = [] if entity_type == "Crop" else _link_refusals(
            paper_id, entity_type, this_run_records, candidate_known_refs or {}, candidate)
        if refusals:
            detail = {"status": "unresolved", "unresolved_cause": refusals[0]["cause"],
                      "last_errors": [{"field": r["field"], "message": r["message"]} for r in refusals]}
            run_store.save_final(run_id, f"{entity_type}__{record_id}", detail)
            link_decisions.append({"candidate_id": candidate.candidate_id, "decision": "refused_link", "refusals": refusals})
            record_infos.append({"entity_type": entity_type, "record_id": record_id, "status": "unresolved",
                                 "detail": detail})
            continue
        if entity_type == "Crop":
            species_ids, species_decision = _evidenced_species(paper_id, candidate, this_run_records)
            link_decisions.append({"candidate_id": candidate.candidate_id, **species_decision})
            if not species_ids:
                # Never bind a crop to a Species by elimination.
                detail = {"status": "unresolved", "unresolved_cause": causes.AMBIGUOUS, "last_errors": [{
                    "field": "species_id", "message": species_decision["message"]}]}
                run_store.save_final(run_id, f"{entity_type}__{record_id}", detail)
                record_infos.append({"entity_type": entity_type, "record_id": record_id, "status": "unresolved",
                                     "detail": detail})
                continue
            candidate_known_refs = {**(candidate_known_refs or {}),
                                    "species_id": species_ids[0] if len(species_ids) == 1 else sorted(species_ids)}
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
        if candidate.context.get("layout_pooling"):
            context += (
                f" NOTE: this value is a main-effect (marginal) mean -- {candidate.context['layout_pooling']}"
                + (f"; it covers {candidate.context['pooled_time_levels']}" if candidate.context.get("pooled_time_levels") else "")
                + ". Report the value exactly as the table cell gives it; there is no separate pooling sentence to find."
            )
        if candidate.context.get("temporal_context"):
            context += _temporal_extraction_note(candidate.context["temporal_context"])
        if entity_type == "Treatment":
            context += _treatment_definition_note(paper_id, candidate)
        hints = {k: v for k, v in (("variable_name_hint", candidate.variable_name_hint), ("units_hint", candidate.units_hint)) if v}
        if hints:
            context += _hint_extraction_note(hints)
        if entity_type == "Management":
            verified, decisions = _verified_treatment_links(paper_id, candidate, recorded_records, link_blocks)
            link_decisions.extend(decisions)
            if verified:
                hints["treatment_link"] = {
                    "treatment_ids": [v["record_id"] for v in verified], "names": [v["name"] for v in verified],
                    "evidence_anchors": list(candidate.anchors),
                }
        observation_bundle = None
        if entity_type == "Observation":
            experimental, evidence = _observation_experimental_context(
                paper_id, candidate, candidate_known_refs or {}, this_run_records, method_links, design_summary)
            hints["experimental_context"] = experimental
            observation_bundle = context_bundle.build_observation_bundle(paper_id, experimental, evidence, _papers_root())
        result = run_record(
            run_id=run_id, paper_id=paper_id, entity_type=entity_type, record_id=record_id,
            model=model, client=client, invoke=invoke, enable_ai_validation=enable_ai_validation,
            known_refs=candidate_known_refs or None, extraction_context=context,
            known_value=candidate.known_value, evidence_bundle=observation_bundle,
            evidence_seeds=list(candidate.anchors),
            evidence_target=_evidence_target(entity_type, candidate), candidate_context={**candidate.context, **hints} or None,
            pending_prerequisites=pending or None,
        )
        record_info = {
            "entity_type": entity_type, "record_id": record_id,
            "status": result.status, "detail": result.detail,
        }
        if prerequisite_notes:
            record_info["prerequisite_notes"] = prerequisite_notes
        record_infos.append(record_info)
    if link_decisions:
        stage = "species_links" if entity_type == "Crop" else "treatment_links"
        run_store.save_stage_attempt(run_id, f"{entity_type}__enumeration", stage, 1, {"decisions": link_decisions})
    return record_infos


def _treatment_definition_note(paper_id: str, candidate: Any) -> str:
    """Where the paper defines this Treatment's factor, for its `definition`: design statements (methods, abstract,
    front matter, a design term such as "classes" or "treatments") that name the factor. None cited when there are
    none."""
    levels = [(d.name, d.level) for d in (getattr(candidate, "dimensions", None) or []) if d.dimension == "treatment"]
    if not levels:
        return ""
    try:
        index = evidence_index.build_evidence_index(document_map.build_document_map(paper_id, _papers_root()))
    except (FileNotFoundError, OSError):
        return ""
    names = {design._key(name) for name, _ in levels}
    anchors = []
    for block in index.dmap.blocks:
        if block.region not in design.DESIGN_REGIONS or block.block_type == "Table":
            continue
        if not index.signals.get(block.anchor, {}).get("design_term"):
            continue
        if any(name and name in design._key(block.text) for name in names):
            anchors.append(block.anchor)
    described = ", ".join(f"{name}={level!r}" for name, level in levels)
    where = (f" The paper describes this factor in {', '.join(anchors[:4])} -- the `definition` is what that text says "
             f"the level is and how it was obtained; quote it from there." if anchors else "")
    return (
        f" NOTE (Treatment): this Treatment is the level {described}. Its `definition` states what that level IS and "
        f"how it was imposed, from the paper's design text, never the level label repeated.{where} The measured values "
        f"a table reports for this level (means of response variables) are Observations, not facts of a Treatment -- do "
        f"not report them. If the text gives the level's bounds differently from the table, report what each says; "
        f"never reconcile them."
    )


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
    if entity_type == "Site":
        # Degree-minute coordinates: the decimal is derived in code from the quoted text (pipeline/coordinates.py).
        parts += [
            "",
            "latitude / longitude: reported_text is the coordinate EXACTLY as the source writes it (e.g. \"45°42′ N\", "
            "\"-121˚32´W\"), one coordinate per field. Do NOT convert it to decimal degrees yourself and do not set "
            "converted_value: the pipeline derives the decimal from reported_text and records the formula. When the text "
            "is in degrees and minutes, leave reported_numeric_value null; reported_units is the degree sign when the "
            "source writes one. If the source gives no coordinates, omit these fields.",
        ]
    if entity_type == "Citation" and raw_extraction.get("orchestrator_not_written"):
        fields = [entry["field"] for entry in raw_extraction["orchestrator_not_written"] if entry["field"] in ("persistent_identifier",)]
        parts += [
            "",
            "RAW_EVIDENCE.orchestrator_not_written lists Citation information the paper does NOT WRITE (a pipeline "
            "determination, not source text). "
            + (
                f"For {', '.join(fields)}: value null, provenance_label UNRESOLVED, unresolved_reason "
                f"\"{CITATION_NOT_WRITTEN_REASON}\", and cite one of the anchors in RAW_EVIDENCE as its locator. "
                if fields else ""
            )
            + "Never fill a not-written field with a value. `journal` has no IR field: do not add one.",
        ]
    if known_refs:
        # Bare-reference fields (site_id, treatment_id, ...) are never UNRESOLVED-able: give the model the real ids of
        # records already extracted in this run.
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
        experimental = candidate_context.get("experimental_context")
        if entity_type == "Observation" and experimental:
            parts += ["", _experimental_conversion_note(experimental)]
        if candidate_context.get("aggregated_over_factors") and candidate_context.get("layout_pooling"):
            # Pooling shown by the table's layout (a main-effect row), not a sentence: labelled INFERRED with that basis.
            parts += [
                "",
                "CANDIDATE_CONTEXT says this value is a MAIN-EFFECT MEAN pooled over "
                f"{candidate_context['aggregated_over_factors']!r}: {candidate_context['layout_pooling']}. Set "
                "`reported_effect_scope` to \"aggregated_mean\" and `aggregated_over_factors` to that list, labelled "
                "INFERRED, citing the table block, with unresolved_reason stating that basis (the table layout). "
                + (f"The value covers the levels {candidate_context['pooled_time_levels']}: do not narrow temporal_info "
                   f"to one of them. " if candidate_context.get("pooled_time_levels") else "")
                + "Never set treatment_mean for this value.",
            ]
        elif candidate_context.get("aggregated_over_factors"):
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
            # Hand back the verbatim text of every anchor already in RAW_EVIDENCE, so nothing has to be recalled; this
            # never widens what Conversion may cite.
            candidate_anchors = all_anchors(raw_extraction)
            candidate_anchor_texts = _anchor_texts(paper_id, candidate_anchors)
            span_hints = _closest_span_hints(paper_id, prior_errors)
            if span_hints:
                parts += ["", "CLOSEST VERBATIM TEXT in the cited block for each rejected value:", *span_hints]
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
            # Spell out the fix for an INFERRED field with no reason.
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
            # Spell out the reported_effect_scope / aggregated_over_factors coupling (see converter.md).
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
    entity_type: str, candidate_payload: dict, raw_extraction: dict, anchor_texts: dict[str, str],
    design_context: Optional[dict] = None,
) -> str:
    design_lines = [] if not design_context else [
        "DESIGN_CONTEXT (the factors this paper's tables declare, with their dimensions -- a Treatment must be a level "
        "(or combination of levels) of a factor of dimension 'treatment'; a level of a time, site or crop factor, such "
        "as a year, is never a Treatment -- flag it if this candidate is one):",
        "```json",
        json.dumps(design_context, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return "\n".join(
        [
            f"Review this already deterministically-valid Sage IR `{entity_type}` candidate record.",
            "",
            *design_lines,
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
    """The known_refs check used by every stage. A list-shaped reference field (Study.citation_ids) matches if the
    expected id is a member; `expected` may itself be an allowed set (a required field with several ready candidates
    and no specific link). An unhashable candidate value (a dict where a bare id is required) is a mismatch."""
    if isinstance(expected, (list, set, tuple)):
        allowed = set(expected)
        if isinstance(candidate_value, list):
            return not any(_is_hashable(v) and v in allowed for v in candidate_value)
        return not _is_hashable(candidate_value) or candidate_value not in allowed
    if isinstance(candidate_value, list):
        return expected not in candidate_value
    return candidate_value != expected


def _is_hashable(value: Any) -> bool:
    """An unhashable value (e.g. a dict where a bare id is required) is never a known reference id."""
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _ref_expectation_message(field: str, expected: Any, actual: Any) -> str:
    """Error text for a `_ref_mismatch` failure; an allowed-set `expected` reads "must be one of ..."."""
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
    """One AI Validator call, recorded under `attempt` (also used to re-check a corrected payload)."""
    anchors = all_anchors(raw_extraction)
    anchor_texts = _anchor_texts(paper_id, anchors)
    val_result = invoke(
        "ir-validator", model,
        _ai_validation_prompt(entity_type, candidate_payload, raw_extraction, anchor_texts,
                              _treatment_design_context(run_id) if entity_type == "Treatment" else None),
    )
    run_store.save_stage_attempt(run_id, record_key, "ai_validation", attempt, val_result.as_artifact())
    verdict = val_result.parsed_json or {
        "verdict": "parse_error", "issues": [], "raw_parse_error": val_result.parse_error,
    }
    return _scope_ai_concerns(verdict)


def _treatment_design_context(run_id: str) -> Optional[dict]:
    """The paper-level factor registry (every factor the run's tables declare: dimensions, levels, tables) for the
    Treatment validator. From the cached table pass only; None when the paper has no classified table."""
    classifications = {key.split("__", 1)[1]: c for key, c in _cached_table_classifications(run_id).items()}
    if not classifications:
        return None
    registry = design.factor_consistency(classifications).to_artifact()["registry"]
    return {"factors": [{"name": f["name"], "dimensions": f["dimensions"], "levels": f["levels"], "tables": f["tables"]}
                        for f in registry.values()]}


# Fields the pipeline owns: a validator concern about them never keeps a record from ready.
PIPELINE_OWNED_FIELDS = frozenset({"id", "record_id", "citation_id", "page_number", "source_document_id"})


def _scope_ai_concerns(verdict: dict) -> dict:
    """The validator's verdict with concerns about pipeline-owned fields removed (kept under `scoped_out`); a
    "suspicious" verdict left with no concern at all becomes "plausible"."""
    if not isinstance(verdict, dict) or not isinstance(verdict.get("issues"), list):
        return verdict
    kept, scoped_out = [], []
    for issue in verdict["issues"]:
        path = str((issue or {}).get("field") or "")
        parts = set(re.split(r"[.\[\]]", path))
        (scoped_out if path and parts & PIPELINE_OWNED_FIELDS else kept).append(issue)
    if not scoped_out:
        return verdict
    scoped = {**verdict, "issues": kept, "scoped_out": scoped_out}
    if verdict.get("verdict") == "suspicious" and not kept:
        scoped["verdict"] = "plausible"
        scoped["verdict_before_scoping"] = "suspicious"
    return scoped


def _attempt_ai_validation_correction(
    *, run_id: str, record_key: str, paper_id: str, entity_type: str, record_id: str,
    raw_extraction: dict, known_refs: Optional[dict[str, str]], ai_validation: dict,
    model: str, client: IRServiceClient, invoke: Callable[..., AgentInvocation], stage_counts: dict,
    candidate_context: Optional[dict[str, Any]] = None,
) -> dict:
    """Exactly one Conversion correction pass, triggered by a "suspicious" verdict. The corrected payload goes through
    the same known_refs check and propose_record validation as every other payload.

    Returns {"ok": True, "payload", "ai_validation"} when the corrected payload passes and was re-checked, or
    {"ok": False, "payload", "errors"} when the correction fails; the caller then falls back to demotion or
    flag_unresolved, never force-committing."""
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

    corrected_payload = coordinates.apply_coordinate_transformations(
        entity_type, _apply_source_pages(paper_id, conv_result.parsed_json)
    )

    ref_errors = [
        {"field": field, "message": _ref_expectation_message(field, expected, corrected_payload.get(field))}
        for field, expected in (known_refs or {}).items()
        if field in corrected_payload and _ref_mismatch(corrected_payload.get(field), expected)
    ] + _management_link_errors(entity_type, corrected_payload, candidate_context) \
      + _observation_relationship_errors(entity_type, corrected_payload, candidate_context)
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


_FACT_INDEX_RE = re.compile(r"^facts[\[.](\d+)")
REGROUNDING_BLOCK_CHARS = 900



# --------------------------------------------------------------------------- #
# Verbatim spans (conversion grounding feedback): a retry is pointed at the exact span the value was meant to copy.
# --------------------------------------------------------------------------- #

_MISMATCH_RE = re.compile(r"^(?P<path>[\w.\[\]]+): value=(?P<value>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") is not supported by block (?P<anchor>b:\d+)")


def _closest_span(value: str, text: str, min_ratio: float = 0.75) -> Optional[str]:
    """The span of `text` (whole words, the value's word count +-1) most similar to `value`, or None below `min_ratio`."""
    import difflib
    words = list(re.finditer(r"\S+", text or ""))
    n = len((value or "").split())
    if not words or not n:
        return None
    best, best_ratio = None, 0.0
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, len(words) - size + 1):
            span = text[words[i].start():words[i + size - 1].end()].strip(" ,.;:()")
            ratio = difflib.SequenceMatcher(None, value.lower(), span.lower()).ratio()
            if ratio > best_ratio:
                best, best_ratio = span, ratio
    return best if best_ratio >= min_ratio else None


def _only_non_ascii_letters_differ(value: str, span: str) -> bool:
    """Same length, and every differing position is a non-ASCII letter on both sides ("Chaène" vs "Chaîne") -- a
    character the model mangled, never a digit, a word or a unit."""
    if len(value) != len(span) or value == span:
        return False
    return all(a == b or (ord(a) > 127 and ord(b) > 127 and a.isalpha() and b.isalpha()) for a, b in zip(value, span))


def _mismatches(errors: list[dict]) -> list[tuple[str, str, str]]:
    """(field path, value, anchor) of every provenance_value_mismatch error."""
    import ast
    out = []
    for error in errors or []:
        if error.get("code") != "provenance_value_mismatch":
            continue
        match = _MISMATCH_RE.match(error.get("message") or "")
        if not match:
            continue
        try:
            value = ast.literal_eval(match.group("value"))
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str):
            out.append((match.group("path"), value, match.group("anchor")))
    return out


def _verbatim_repairs(paper_id: str, payload: dict, errors: list[dict]) -> Optional[tuple[dict, list[dict]]]:
    """The payload with each mismatched TOP-LEVEL text value replaced by the cited block's own span, when the two
    differ only in non-ASCII letters; None when no such repair applies. Recorded, never applied to numbers."""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return None
    repaired = json.loads(json.dumps(payload))
    repairs = []
    for path, value, anchor in _mismatches(errors):
        field = repaired.get(path) if "." not in path and "[" not in path else None
        if not (isinstance(field, dict) and field.get("value") == value):
            continue
        span = _closest_span(value, blocks.get(anchor, ""), min_ratio=0.8)
        if span and _only_non_ascii_letters_differ(value, span):
            field["value"] = span
            repairs.append({"field": path, "from": value, "to": span, "anchor": anchor,
                            "reason": "differs from the cited block's text only in non-ASCII letters; the block's own "
                                      "characters are used"})
    return (repaired, repairs) if repairs else None


def _closest_span_hints(paper_id: str, errors: list[dict]) -> list[str]:
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return []
    hints = []
    for path, value, anchor in _mismatches(errors):
        span = _closest_span(value, blocks.get(anchor, ""))
        if span and span != value:
            hints.append(f"- {path}: you wrote {value!r}; block {anchor} contains {span!r}. If that is the value, copy it "
                         f"character for character (every accent and symbol as it is); if the block does not state it, "
                         f"cite the block that does or mark the field UNRESOLVED.")
    return hints


def _split_withdrawals(entries: Optional[list[dict]]) -> dict[str, list[dict]]:
    """Demotions of optional fields and withdrawals of persistently ungrounded fields travel in one list; a record shows
    them under their own keys (a withdrawn field can be required -- it is never an 'optional demotion')."""
    entries = entries or []
    withdrawn = [e for e in entries if e.get("rule") == "persistently_ungrounded_field"]
    demoted = [e for e in entries if e.get("rule") != "persistently_ungrounded_field"]
    return {**({"demoted_optional_fields": demoted} if demoted else {}), **({"withdrawn_fields": withdrawn} if withdrawn else {})}


GROUNDING_WITHDRAWAL_AFTER = 2


def _withdraw_persistently_ungrounded(
    payload: Any, errors: list[dict], failures: dict[str, int],
) -> Optional[tuple[dict, list[dict]]]:
    """Count this attempt's grounding failures per top-level field; when EVERY error is a grounding error on a
    value-bearing field (never a bare reference, never a shape error) and at least one of those fields has now failed
    GROUNDING_WITHDRAWAL_AFTER times, return (payload with each failing field UNRESOLVED -- the error as its reason --,
    withdrawal log). Otherwise None. The value is withdrawn, never replaced: readiness then decides the record's status,
    and a record with a withdrawn required field is unresolved with the rest of its payload kept."""
    if not isinstance(payload, dict) or not errors or _link_policy() != "extract_first_withdrawal":
        return None
    failing: dict[str, list[str]] = {}
    for error in errors:
        path = _error_field(error)
        top = re.split(r"[.\[]", path)[0] if path else None
        entry = payload.get(top) if top else None
        if not str(error.get("code") or "").startswith("provenance_") or not isinstance(entry, dict) or "value" not in entry:
            return None
        failing.setdefault(top, []).append(error.get("message") or "")
    for top in failing:
        failures[top] = failures.get(top, 0) + 1
    if not any(failures[top] >= GROUNDING_WITHDRAWAL_AFTER for top in failing):
        return None
    reduced = dict(payload)
    withdrawn = []
    for top, messages in sorted(failing.items()):
        entry = payload[top]
        reduced[top] = {**entry, "value": None, "provenance_label": "UNRESOLVED",
                        "unresolved_reason": f"withdrawn: the value never matched its cited text ({messages[0]})"[:1000]}
        withdrawn.append({"field": top, "value": entry.get("value"), "attempts_failed": failures[top], "errors": messages,
                          "rule": "persistently_ungrounded_field"})
    return reduced, withdrawn


def _regrounding_guidance(paper_id: str, parsed: Any, errors: list[dict]) -> list[dict]:
    """For facts whose raw_text_excerpt failed (not in the cited block, or null), one extra retry message quoting the
    cited blocks verbatim, so the next attempt copies a contiguous span, cites the right block, or omits the fact."""
    facts = (parsed or {}).get("facts") if isinstance(parsed, dict) else None
    if not isinstance(facts, list):
        return []
    anchors: list[str] = []
    null_excerpt = False
    for error in errors:
        match = _FACT_INDEX_RE.match(str(error.get("field") or ""))
        if not match or int(match.group(1)) >= len(facts) or not isinstance(facts[int(match.group(1))], dict):
            continue
        fact = facts[int(match.group(1))]
        null_excerpt = null_excerpt or not isinstance(fact.get("raw_text_excerpt"), str)
        anchors += [a for a in (fact.get("anchors") or []) if isinstance(a, str)]
    if not anchors and not null_excerpt:
        return []
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return []
    quoted = []
    for anchor in dict.fromkeys(a.strip("[]") for a in anchors):
        if anchor in blocks:
            text = " ".join(blocks[anchor].split())
            quoted.append(f"[{anchor}] {text[:REGROUNDING_BLOCK_CHARS]}{' ...' if len(text) > REGROUNDING_BLOCK_CHARS else ''}")
    message = (
        "raw_text_excerpt must be a STRING copied character-for-character from the block the fact cites (one contiguous "
        "span; no rewording, no added words, no units or symbols in a different notation). If the cited block does not "
        "state the fact, cite the block that does -- or omit the fact; never send null. "
    ) + ("The blocks you cited read exactly:\n" + "\n".join(quoted) if quoted else "")
    return [{"field": "facts", "message": message}]


def _raw_extraction_grounding_errors(paper_id: str, extraction: RawExtraction) -> list[dict]:
    """Every `RawFact.raw_text_excerpt` must be literal text of its cited blocks (`_value_supported_by_text`, with no
    judgment-field exemption), checked before the evidence reaches Conversion. Returns errors in the usual
    {"field", "message"} shape; a missing content.md is reported the same way."""
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


# An excerpt may elide stretches with "..." / "…": every quoted stretch must be literal source text, in order.
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
    """For a table-seeded Observation candidate the expected value is known (the cell Step C built it from): an
    extraction that read the cited anchors but reports no fact matching it attributed another cell's value. Checks
    every fact's raw_value and raw_text_excerpt, since Extraction names its own fields."""
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
    """Table candidates only: an ungrounded auxiliary fact (unit, method, date, note) is dropped and logged instead of
    failing the attempt. Returns (extraction, remaining errors, dropped-fact log). Nothing is dropped unless:
      - every grounding error belongs to one specific fact;
      - some grounded fact carries the known table value;
      - no ungrounded fact carries the known value.
    The grounding check itself is unchanged."""
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


# --- Ungrounded auxiliary facts (non-table candidates) -------------------------------------------------------------
# An ungrounded fact is droppable only if it is not named like an identity/value-bearing field of its entity type, and
# only while a grounded fact remains. Observation is excluded: its value facts have no fixed names off the table path.
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
    """A RawExtraction whose only shape problem is auxiliary facts that fail RawFact validation (e.g. no anchors) is
    recovered by treating those facts as ungrounded. None when anything else is wrong."""
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


# Conversion side: descriptive optional fields only (never identity, value, statistic or reference). A payload is
# demoted only when every validation error sits in these fields, and the remainder is re-validated.
AUXILIARY_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "Variable": frozenset({"description", "units", "notes"}),
    "Site": frozenset({"description", "soil_context", "nearest_city"}),
    "Crop": frozenset({"common_name", "notes"}),
    "Species": frozenset({"common_name"}),
    "Observation": frozenset({"notes", "replicate_id"}),
}


def _apply_source_pages(paper_id: str, payload: Any) -> Any:
    """A copy of a Conversion payload with every ExtractedField's `source.page_number` set from provenance.json (the
    1-indexed page of its first placeable cited block, else None); the model has no page data, so its value is
    always overwritten."""
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


# --- Field-level demotion of an unanswered AI-validator concern ------------------------------------------------------
#   concern about a field that holds a value -> that field becomes UNRESOLVED with the concern as its reason;
#   concern about a field's label only       -> EXTRACTED is downgraded to INFERRED (value kept, concern = basis);
#                                              an INFERRED field stays INFERRED with the concern kept open;
#   concern about an empty field (omission)  -> tolerated only for OMISSION_TOLERATED_FIELDS, kept as an open concern;
#   concern with no field, about a bare reference, or an omission of any other field -> no demotion.
# The demoted payload goes through the same deterministic validation and readiness; no further model call is made.
OMISSION_TOLERATED_FIELDS: dict[str, frozenset[str]] = {
    "Site": frozenset({"latitude", "longitude", "elevation", "soil_context", "description", "nearest_city", "country",
                       "state_or_region"}),
    "Variable": frozenset({"description", "units", "notes"}),
    "Method": frozenset({"description"}),
    "Species": frozenset({"common_name"}),
    "Crop": frozenset({"common_name", "notes"}),
    "Citation": frozenset({"persistent_identifier"}),
    "Treatment": frozenset({"control_status"}),
    "Observation": frozenset({"notes", "replicate_id"}),
}


_NAMING_STYLE_RE = re.compile(
    r"\b(omit\w*|lacks?|missing (?:the )?(?:qualifier|word|prefix|context)|qualifier|incomplete|more (?:specific|descriptive|complete)"
    r"|full(?:er)? (?:name|form|term)|abbreviat\w*|shortened|less specific|does not (?:fully )?(?:reflect|capture))\b", re.I)
_BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))


def _is_naming_style_concern(concern: str) -> bool:
    return bool(_NAMING_STYLE_RE.search(concern or ""))


def _balanced(text: str) -> bool:
    """False for a truncated name such as "maximum electron transport rate (Jmax"."""
    return all(text.count(a) == text.count(b) for a, b in _BRACKET_PAIRS) and text.count("$") % 2 == 0


def _demote_concerned_fields(
    entity_type: str, payload: dict, ai_validation: dict,
) -> Optional[tuple[dict, list[dict], list[dict]]]:
    """(payload with each concerned value-bearing field UNRESOLVED, demotions, open omission concerns), or None when
    any concern cannot be settled at field level."""
    issues = [i for i in (ai_validation or {}).get("issues") or [] if isinstance(i, dict)]
    if not issues or not isinstance(payload, dict):
        return None
    demoted_payload = copy.deepcopy(payload)
    demotions: list[dict] = []
    open_concerns: list[dict] = []
    tolerated = OMISSION_TOLERATED_FIELDS.get(entity_type, frozenset())
    for issue in issues:
        path = issue.get("field")
        concern = str(issue.get("concern") or "").strip()
        if not path or not concern:
            return None
        top = re.split(r"[.\[]", str(path))[0]
        entry = demoted_payload.get(top)
        if isinstance(entry, dict) and "provenance_label" in entry:
            label_concern = str(path).endswith((".provenance_label", ".unresolved_reason"))
            if label_concern and entry.get("provenance_label") == "INFERRED" and entry.get("value") is not None:
                # Already INFERRED: a stronger claim is never granted on the validator's word; the concern stays open.
                open_concerns.append({"field": top, "concern": concern, "rule": "label_concern_kept_weaker"})
                continue
            if (str(path).endswith(".provenance_label") and entry.get("provenance_label") == "EXTRACTED"
                    and entry.get("value") is not None):
                # A concern about the label, not the value: downgrade to INFERRED with the concern as its basis.
                demotions.append({"field": top, "value": entry.get("value"), "concern": concern,
                                  "rule": "ai_concern_label_downgraded"})
                demoted_payload[top] = {
                    **entry, "provenance_label": "INFERRED",
                    "unresolved_reason": f"AI validator concern (label downgraded to INFERRED): {concern}"[:1000],
                }
                continue
            if (entity_type in ("Variable", "Method") and top == "name" and isinstance(entry.get("value"), str)
                    and _is_naming_style_concern(concern) and _balanced(entry["value"])):
                # A grounded name the validator would merely phrase differently keeps its value; a truncated one is
                # withdrawn below.
                open_concerns.append({"field": top, "concern": concern, "rule": "style_concern_kept"})
                continue
            if entry.get("provenance_label") in ("EXTRACTED", "INFERRED") and entry.get("value") is not None:
                demotions.append({"field": top, "value": entry.get("value"), "concern": concern,
                                  "rule": "ai_concern_value_withdrawn"})
                demoted_payload[top] = {
                    **entry, "value": None, "provenance_label": "UNRESOLVED",
                    "unresolved_reason": f"AI validator concern (value withdrawn): {concern}"[:1000],
                }
                continue
            if top in tolerated:     # already UNRESOLVED/empty: an omission
                open_concerns.append({"field": top, "concern": concern, "rule": "omission_tolerated"})
                continue
            return None
        if entry is None and top in tolerated:
            open_concerns.append({"field": top, "concern": concern, "rule": "omission_tolerated"})
            continue
        return None                  # a bare reference, a list/plain field, or an omission of a non-tolerated field
    return demoted_payload, demotions, open_concerns


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
    evidence_seeds: Optional[list[str]] = None,
    evidence_target: Optional[str] = None,
    evidence_bundle: Optional[Any] = None,
    pending_prerequisites: Optional[dict[str, dict]] = None,
) -> RecordResult:
    """One record, end to end. Stage 4: before extraction, the record's evidence packet (`context_bundle`) is built
    deterministically from the Document Map -- seeded with the candidate's own anchors, conditioned on its target -- and
    handed to the extractor; the packet is saved as a stage artifact and its coverage audit travels with the record so
    an unresolved outcome can say whether the evidence was retrieved, absent, or retrieved but not resolved."""
    bundle = evidence_bundle   # an Observation packet is composed upstream from its experimental context
    if bundle is None and entity_type != "Citation":
        try:
            bundle = context_bundle.build_context_bundle(
                paper_id, entity_type, "extraction", target=evidence_target, seed_anchors=evidence_seeds or (),
                papers_root=_papers_root(),
            )
        except Exception as exc:  # a packet is an aid, never a reason to lose the record
            bundle = None
            run_store.save_stage_attempt(run_id, f"{entity_type}__{record_id}", "context_bundle_error", 1,
                                         {"error": f"{type(exc).__name__}: {exc}"})
    if bundle is not None:
        run_store.save_stage_attempt(run_id, f"{entity_type}__{record_id}", "context_bundle", 1, bundle.to_dict())
    result = _run_record_inner(
        run_id=run_id, paper_id=paper_id, entity_type=entity_type, record_id=record_id, model=model, client=client,
        invoke=invoke, enable_ai_validation=enable_ai_validation, known_refs=known_refs,
        extraction_context=extraction_context, known_value=known_value, candidate_context=candidate_context,
        evidence_packet=bundle.render() if bundle is not None else None,
        pending_prerequisites=pending_prerequisites,
    )
    if pending_prerequisites and isinstance(result.detail, dict) and "pending_links" not in result.detail:
        payload = result.detail.get("payload") or result.detail.get("last_candidate_payload")
        links = _pending_refs(payload, pending_prerequisites, entity_type)
        if links:
            result.detail["pending_links"] = links
            run_store.save_final(run_id, f"{entity_type}__{record_id}", result.detail)
    if bundle is not None and isinstance(result.detail, dict):
        result.detail["context_bundle"] = {
            "bundle_id": bundle.bundle_id,
            "coverage": {entry.concept: entry.status for entry in bundle.coverage},
        }
        run_store.save_final(run_id, f"{entity_type}__{record_id}", result.detail)
    return result


def _run_record_inner(
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
    evidence_packet: Optional[str] = None,
    pending_prerequisites: Optional[dict[str, dict]] = None,
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
    # Provider failures spend the separate provider budget; `numbered` counts the answers the model actually gave.
    provider = _ProviderBudget(run_id, record_key, "extraction")
    numbered = 0
    attempt = 0  # every invocation round: the artifact index
    provider_terminal = False
    # Citation only (see the "Citation: front matter supplied" section): supplied front matter and NOT WRITTEN entries.
    # Every entity type: a stall gets a targeted final-answer retry (see "No-answer turns: a stall is not an outage").
    is_citation = entity_type == "Citation"
    supplied_context = _citation_extraction_note(paper_id) if is_citation else (evidence_packet or "")
    stalls = 0
    stall_terminal = False
    final_answer_nudge = False
    not_written: list[dict] = []
    while numbered < MAX_EXTRACTION_ATTEMPTS:
        attempt += 1
        stage_counts["extraction"] = attempt
        result = invoke(
            "extractor", model,
            _extraction_prompt(
                paper_id, entity_type, record_id, extraction_errors, extraction_context,
                supplied_context=supplied_context or None, final_answer_nudge=final_answer_nudge,
            ),
        )
        artifact = result.as_artifact()

        if _ended_without_answer_after_tools(result):
            # A stall is the model's behaviour, not the provider's: it neither spends the provider budget nor a numbered
            # attempt, and the next round is a short targeted instruction to answer now -- never the same prompt again.
            stalls += 1
            tool_calls = [{"tool": e["tool"], "status": e["status"]} for e in _tool_events(result.stdout)]
            artifact["validation_errors"] = [{"field": None, "message": "the model used tools and ended without a final answer"}]
            artifact.update(failure_class="no_final_answer", failure_kind="extraction", numbered_attempt=None, tool_calls=tool_calls)
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            stall_limit = MAX_CITATION_FINAL_ANSWER_RETRIES if is_citation else MAX_FINAL_ANSWER_RETRIES
            last_extraction_message = (
                f"round {attempt}: no final answer after {len(tool_calls)} tool call(s) "
                f"({stalls} stall(s); {stall_limit} targeted retries allowed)"
            )
            stall_terminal = stalls > stall_limit
            _record_stall(run_id, "extraction", record_key, stall_terminal)
            if stall_terminal:
                break
            final_answer_nudge = True
            continue

        failure = _provider_failure(result)
        if failure:
            # No answer to correct: the model gets no feedback about it. After an outage the same prompt is repeated
            # after a cooldown; after a failure that is not an outage, immediately, asking for the answer directly.
            if result.had_malformed_tool_call:
                any_malformed_tool_call_failure = True
            artifact["validation_errors"] = [{"field": None, "message": result.parse_error}]
            artifact.update(failure_class=failure, failure_kind="provider", numbered_attempt=None)
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"round {attempt}: provider failure ({failure}): {result.parse_error}"
            if provider.failed(failure, result):
                provider_terminal = True
                break
            provider.cooldown()
            final_answer_nudge = final_answer_nudge or provider.needs_answer_nudge()
            continue
        numbered += 1

        if result.parsed_json is None:
            saw_genuine_content_failure = True  # text came back but is not JSON: the model's own answer
            extraction_errors = [{"field": None, "message": result.parse_error}]
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: {result.parse_error}"
            continue

        # RawFact's own invariants must hold before the evidence reaches Conversion.
        shape_dropped: list[dict] = []
        try:
            validated = RawExtraction.model_validate(result.parsed_json)
        except ValidationError as exc:
            # Auxiliary facts that fail RawFact validation are treated as ungrounded; any other shape problem fails.
            recovered = _recover_extraction_shape(result.parsed_json, entity_type) if known_value is None else None
            if recovered is None:
                saw_genuine_content_failure = True
                extraction_errors = [
                    {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()
                ]
                extraction_errors += _regrounding_guidance(paper_id, result.parsed_json, extraction_errors)
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

        # Raw evidence grounding gate (_raw_extraction_grounding_errors).
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
            extraction_errors = grounding_errors + _regrounding_guidance(paper_id, result.parsed_json, grounding_errors)
            artifact["validation_errors"] = extraction_errors
            run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
            last_extraction_message = f"attempt {attempt}: raw evidence grounding failed: {extraction_errors}"
            continue

        # Extraction vs known table value (table-seeded Observation candidates only).
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

        if is_citation:
            not_written, written_but_missing = _citation_not_written(paper_id, validated)
            if written_but_missing:
                saw_genuine_content_failure = True
                extraction_errors = written_but_missing
                artifact["validation_errors"] = extraction_errors
                run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
                last_extraction_message = f"attempt {attempt}: a DOI written on the first page was not reported"
                continue
            if not_written:
                artifact["not_written"] = not_written

        artifact["validation_errors"] = []
        run_store.save_stage_attempt(run_id, record_key, "extraction", attempt, artifact)
        raw_extraction = validated.model_dump()
        if not_written:
            # Orchestrator-authored and labelled as such: a semantic "the paper does not write this" state for
            # Conversion, never a fact and never evidence (all_anchors reads `facts` only).
            raw_extraction["orchestrator_not_written"] = not_written
        dropped_ungrounded_facts = artifact.get("dropped_ungrounded_facts", [])
        break

    if provider.rounds:
        stage_counts["provider_failure_rounds"] = provider.rounds
    if stalls:
        stage_counts["final_answer_retries"] = min(stalls, MAX_CITATION_FINAL_ANSWER_RETRIES if is_citation else MAX_FINAL_ANSWER_RETRIES)
    if raw_extraction is None:
        if saw_genuine_content_failure:
            failure_class = None
        elif stall_terminal:
            failure_class = "no_final_answer"   # the model's behaviour, disclosed as such -- never a provider failure
        elif any_malformed_tool_call_failure:
            failure_class = "provider_malformed_response"
        else:
            failure_class = "provider_empty_response"
        return _finalize_error(
            run_id, record_key, entity_type, record_id, last_extraction_message, stage_counts, failure_class=failure_class,
            extra=provider.disclosure(numbered, provider_terminal) if provider.rounds else None,
        )

    # Refuse-to-guess gate: a list in known_refs is a required reference the candidate's own links did not resolve.
    # Allowed-set membership cannot verify that a choice is the right one for the evidence, so the candidate is refused
    # rather than letting Conversion pick.
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
    conversion_nudge = False            # the round after a no-answer turn asks for the answer now
    conversion_no_answers = 0
    conversion_no_answer_last = False
    grounding_failures: dict[str, int] = {}     # top-level field -> conversion attempts it failed grounding in
    for attempt in range(1, MAX_CONVERSION_LOOP_SAFETY + 1):
        if propose_result is not None:
            # Stop before another conversion call once ir_service reports no propose attempts remaining.
            if propose_result.get("forced_flag_unresolved") or propose_result.get("attempts_remaining") == 0:
                break
            # Stagnation: the same validation-error signature on two consecutive attempts stops the loop.
            current_signature = _error_signature(last_errors)
            if not conversion_no_answer_last and current_signature is not None and current_signature == previous_error_signature:
                break
            previous_error_signature = current_signature

        stage_counts["conversion"] = attempt
        conv_result = invoke(
            "converter", model,
            _conversion_prompt(paper_id, entity_type, record_id, raw_extraction, last_errors, known_refs, candidate_context)
            + (f"\n\n{_final_answer_nudge('conversion')}" if conversion_nudge else ""),
        )
        conv_artifact = conv_result.as_artifact()
        conversion_no_answer_last = False

        no_answer = "no_final_answer" if _ended_without_answer_after_tools(conv_result) else _provider_failure(conv_result)
        if no_answer:
            # No answer at all (a stall or a provider-classed failure): the previous round's real validation feedback is
            # kept, the next round asks for the answer now, and this round never counts as a stagnating error. Bounded by
            # MAX_FINAL_ANSWER_RETRIES; an outage (`_outage_evidence`) is waited out first.
            conversion_no_answers += 1
            conv_artifact.update(failure_class=no_answer, failure_kind="extraction" if no_answer == "no_final_answer" else "provider")
            run_store.save_stage_attempt(run_id, record_key, "conversion", attempt, conv_artifact)
            stage_counts["conversion_no_answer_rounds"] = conversion_no_answers
            if no_answer == "no_final_answer":
                _record_stall(run_id, "conversion", record_key, conversion_no_answers > MAX_FINAL_ANSWER_RETRIES)
            if conversion_no_answers > MAX_FINAL_ANSWER_RETRIES or no_answer == "provider_unavailable":
                last_errors = last_errors or [{"field": None, "message": f"conversion produced no answer ({no_answer})"}]
                break
            if no_answer != "no_final_answer" and _outage_evidence(conv_result):
                time.sleep(provider_cooldown_seconds(conversion_no_answers))
            conversion_nudge = True
            conversion_no_answer_last = True
            continue
        run_store.save_stage_attempt(run_id, record_key, "conversion", attempt, conv_artifact)

        if conv_result.parsed_json is None:
            last_errors = [{"field": None, "message": f"conversion attempt {attempt}: {conv_result.parse_error}"}]
            continue

        # Deterministic, downstream: page numbers from provenance, coordinate decimals from the quoted text.
        candidate_payload = coordinates.apply_coordinate_transformations(
            entity_type, _apply_source_pages(paper_id, conv_result.parsed_json)
        )
        injected = _inject_fault(entity_type, candidate_payload)
        if injected is not None:
            candidate_payload, fault = injected
            run_store.save_stage_attempt(run_id, record_key, "injected_fault", attempt, fault)

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
        ] + _management_link_errors(entity_type, candidate_payload, candidate_context) \
      + _observation_relationship_errors(entity_type, candidate_payload, candidate_context)
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
        # A value that differs from its cited block only in mangled non-ASCII letters takes the block's own characters
        # (recorded) and is re-proposed -- no model call.
        verbatim = _verbatim_repairs(paper_id, candidate_payload, last_errors)
        if verbatim is not None:
            repaired_payload, repairs = verbatim
            reproposal = client.propose_record(
                paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=repaired_payload, run_id=run_id,
            )
            run_store.save_stage_attempt(
                run_id, record_key, "conversion_verbatim_repair", attempt, {"repairs": repairs, "reproposal": reproposal},
            )
            if reproposal.get("valid"):
                candidate_payload, propose_result = repaired_payload, reproposal
                break
            if not reproposal.get("forced_flag_unresolved"):
                candidate_payload, propose_result = repaired_payload, reproposal
                last_errors = reproposal.get("errors", [])
        # A field that keeps failing grounding is withdrawn (UNRESOLVED, the error as its reason) and the rest of the
        # record re-proposed, so one bad field never costs the grounded ones.
        withdrawal = _withdraw_persistently_ungrounded(candidate_payload, last_errors, grounding_failures)
        if withdrawal is not None and (propose_result.get("attempts_remaining") or 0) > 0:
            reduced_payload, withdrawn = withdrawal
            reproposal = client.propose_record(
                paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=reduced_payload, run_id=run_id,
            )
            run_store.save_stage_attempt(
                run_id, record_key, "conversion_withdrawal", attempt, {"withdrawn": withdrawn, "reproposal": reproposal},
            )
            if reproposal.get("valid"):
                candidate_payload, propose_result = reduced_payload, reproposal
                demoted_optional_fields.extend(withdrawn)
                break
        # When every error is about a descriptive optional field, that field is removed and logged, and the remainder
        # re-proposed.
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

    # --- Readiness ---
    # A valid record whose core fields are UNRESOLVED is committed as `unresolved` with its payload kept, and no
    # AI-validation call is spent on it.
    if propose_result.get("ready") is False:
        return _finalize_not_ready(
            run_id, client, paper_id, entity_type, record_id, record_key, candidate_payload,
            propose_result.get("readiness_issues") or [], stage_counts, dropped_ungrounded_facts,
            demoted_optional_fields,
        )

    # --- AI Validator (one bounded correction) ---
    ai_validation: Optional[dict] = None
    field_demotions: list[dict] = []
    open_concerns: list[dict] = []
    settled: Optional[dict] = None
    if enable_ai_validation:
        stage_counts["ai_validation"] = 1
        ai_validation = _run_ai_validation(
            run_id, record_key, paper_id, entity_type, candidate_payload, raw_extraction, model, invoke, attempt=1,
        )

        if ai_validation.get("verdict") == "suspicious" and MAX_AI_VALIDATION_CORRECTIONS > 0:
            # The last deterministically valid payload is kept as the fallback if the correction fails validation.
            last_known_good_payload = candidate_payload
            last_known_good_ai_validation = ai_validation

            correction = _attempt_ai_validation_correction(
                run_id=run_id, record_key=record_key, paper_id=paper_id, entity_type=entity_type,
                record_id=record_id, raw_extraction=raw_extraction, known_refs=known_refs,
                ai_validation=ai_validation, model=model, client=client, invoke=invoke,
                stage_counts=stage_counts, candidate_context=candidate_context,
            )
            if not correction["ok"]:
                settled = _settle_by_field_demotion(
                    client, run_id, record_key, paper_id, entity_type, record_id,
                    last_known_good_payload, last_known_good_ai_validation, stage_counts,
                )
                if settled is not None and not settled["ready"]:
                    return _finalize_not_ready(
                        run_id, client, paper_id, entity_type, record_id, record_key, settled["payload"],
                        settled["readiness"], stage_counts, dropped_ungrounded_facts, demoted_optional_fields,
                    )
                if settled is not None:
                    candidate_payload, ai_validation = settled["payload"], last_known_good_ai_validation
                    field_demotions, open_concerns = settled["demotions"], settled["open_concerns"]
                if settled is None:
                    # The correction failed validation (or crashed): the last valid payload is kept, but the concern was
                    # never answered, so the record is committed `unresolved` with its payload (as _finalize_not_ready).
                    return _finalize_suspicious(
                        run_id, client, paper_id, entity_type, record_id, record_key,
                        last_known_good_payload, last_known_good_ai_validation,
                        correction.get("errors") or [], stage_counts, dropped_ungrounded_facts,
                    )
            else:
                candidate_payload = correction["payload"]
                ai_validation = correction["ai_validation"]
                settled = None
                if ai_validation.get("verdict") == "suspicious":
                    settled = _settle_by_field_demotion(
                        client, run_id, record_key, paper_id, entity_type, record_id,
                        candidate_payload, ai_validation, stage_counts,
                    )
                    if settled is not None and not settled["ready"]:
                        return _finalize_not_ready(
                            run_id, client, paper_id, entity_type, record_id, record_key, settled["payload"],
                            settled["readiness"], stage_counts, dropped_ungrounded_facts, demoted_optional_fields,
                        )
                    if settled is not None:
                        candidate_payload = settled["payload"]
                        field_demotions, open_concerns = settled["demotions"], settled["open_concerns"]
                if ai_validation.get("verdict") == "suspicious" and settled is None:
                    # Different from the branch above: HERE the correction
                    # DID pass deterministic validation, so there is no
                    # provenance-truth conflict to refuse -- it's simply
                    # still flagged after the one bounded attempt is
                    # exhausted (MAX_AI_VALIDATION_CORRECTIONS == 1), and the
                    # concern could not be settled field by field. Do not
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

    # --- Extract first, link later: a record referencing a PENDING prerequisite is never ready ---
    pending_links = _pending_refs(candidate_payload, pending_prerequisites, entity_type)
    if pending_links:
        readiness = [{
            "code": f"{field}_prerequisite_pending",
            "message": (f"{field} is {ref}, which is {pending_prerequisites[ref]['status']} in this run: every other "
                        f"value of this record is extracted and validated; it becomes ready once that "
                        f"{pending_prerequisites[ref]['prerequisite']} is"),
        } for field, ref in sorted(pending_links.items())]
        result = _finalize_not_ready(
            run_id, client, paper_id, entity_type, record_id, record_key, candidate_payload, readiness,
            stage_counts, dropped_ungrounded_facts, demoted_optional_fields,
        )
        result.detail.update(unresolved_cause=causes.BLOCKED_PREREQUISITE, pending_links=pending_links)
        if ai_validation is not None:
            result.detail["ai_validation"] = ai_validation
        run_store.save_final(run_id, record_key, result.detail)
        return result

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
    final_record.update(_split_withdrawals(demoted_optional_fields))
    if not_written:
        final_record["not_written"] = not_written
    if field_demotions:
        final_record["field_demotions"] = field_demotions
    if open_concerns:
        final_record["open_concerns"] = open_concerns
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": outcome,
        "attempts": stage_counts,
        **({"dropped_ungrounded_facts": len(dropped_ungrounded_facts)} if dropped_ungrounded_facts else {}),
        **{k: [d["field"] for d in v] for k, v in _split_withdrawals(demoted_optional_fields).items()},
        **({"field_demotions": [d["field"] for d in field_demotions]} if field_demotions else {}),
        **({"open_concerns": [c["field"] for c in open_concerns]} if open_concerns else {}),
    })
    return RecordResult(status=outcome, entity_type=entity_type, record_id=record_id, detail=final_record)


def _settle_by_field_demotion(
    client: IRServiceClient, run_id: str, record_key: str, paper_id: str, entity_type: str, record_id: str,
    payload: dict, ai_validation: dict, stage_counts: dict,
) -> Optional[dict]:
    """Apply `_demote_concerned_fields` and re-run the SAME deterministic validation (propose_record) on the result.
    None when the concerns cannot be settled field by field or the demoted payload is not valid; otherwise
    {payload, ready, readiness, demotions, open_concerns}. Recorded as its own stage attempt."""
    demotion = _demote_concerned_fields(entity_type, payload, ai_validation)
    if demotion is None:
        return None
    demoted_payload, demotions, open_concerns = demotion
    proposal = client.propose_record(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id, payload=demoted_payload, run_id=run_id,
    )
    stage_counts["ai_field_demotion"] = stage_counts.get("ai_field_demotion", 0) + 1
    run_store.save_stage_attempt(run_id, record_key, "ai_field_demotion", stage_counts["ai_field_demotion"], {
        "demotions": demotions, "open_concerns": open_concerns, "proposal": proposal,
    })
    if not proposal.get("valid"):
        return None
    return {
        "payload": demoted_payload, "ready": bool(proposal.get("ready", True)),
        "readiness": proposal.get("readiness_issues") or [], "demotions": demotions, "open_concerns": open_concerns,
    }


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
    final_record.update(_split_withdrawals(demoted_optional_fields))
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": "unresolved", "attempts": stage_counts,
        "not_ready": [issue.get("code") for issue in readiness],
    })
    return RecordResult(status="unresolved", entity_type=entity_type, record_id=record_id, detail=final_record)


def _finalize_suspicious(
    run_id: str, client: IRServiceClient, paper_id: str, entity_type: str, record_id: str, record_key: str,
    candidate_payload: dict, ai_validation: dict, correction_errors: list[dict], stage_counts: dict,
    dropped_ungrounded_facts: list[dict],
) -> RecordResult:
    """A deterministic-valid record the AI Validator judged suspicious, whose one correction attempt failed: committed as
    `unresolved` with its payload kept (for review), never as ready. The reasons are the validator's concerns and why
    the correction was rejected."""
    concerns = [
        {"field": issue.get("field"), "message": f"AI Validator concern: {issue.get('concern')}"}
        for issue in (ai_validation.get("issues") or [])
    ] or [{"field": None, "message": "AI Validator judged this record suspicious."}]
    last_errors = concerns + [
        {"field": e.get("field"), "message": f"correction rejected: {e.get('message')}"} for e in correction_errors
    ]
    commit_result = client.commit_record(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id,
        payload=candidate_payload, status="unresolved",
        run_metadata={"run_id": run_id, "schema_version": schema_fingerprint(), "ai_validation": ai_validation},
    )
    final_record = {
        "status": "unresolved",
        "paper_id": paper_id, "entity_type": entity_type, "record_id": record_id,
        "payload": candidate_payload, "last_candidate_payload": candidate_payload,
        "last_errors": last_errors, "ai_validation": ai_validation, "commit_result": commit_result,
        "unresolved_by": "ai_validation",
    }
    if dropped_ungrounded_facts:
        final_record["dropped_ungrounded_facts"] = dropped_ungrounded_facts
    run_store.save_final(run_id, record_key, final_record)
    run_store.save_record_manifest(run_id, record_key, {
        "entity_type": entity_type, "record_id": record_id, "status": "unresolved", "attempts": stage_counts,
        "unresolved_by": "ai_validation",
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
    """A pipeline-level failure with no ir_service call to make (e.g. no parseable evidence at all); still writes a
    final artifact. `failure_class` ("provider_empty_response" / "provider_malformed_response") says when every failed
    attempt was provider noise; the status stays "error" either way."""
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
    """An IRDataset-shaped dict from ir-store's latest entry per (entity_type, record_id). Only "ready" entries join
    the graph; "unresolved" ones are reported separately. Returns (dataset_dict, skipped_keys)."""
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
    """Citation is 1:1 with the paper; more than one distinct Citation record_id for a paper_id gets an actionable
    diagnostic instead of a bare Pydantic error."""
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
    # TreatmentPair needs two distinct Treatment records: blocked until the run has at least 2 ready Treatments.
    "TreatmentPair": [
        ("citation_id", "Citation", True),
        ("treatment_id_1", "Treatment", True),
        ("treatment_id_2", "Treatment", True),
    ],
}

assert set(ENTITY_DEPENDENCIES.keys()) == set(ENTITY_TYPE_TO_PLURAL.keys())

# Links that are not bare-reference dependencies. Management.treatment_ids (optional; protocol Section 9.3) needs
# Treatment extracted first but is offered to enumeration as a link pool, admitted only for a Treatment the event's
# cited text names (`_verified_treatment_links`), never bound through known_refs.
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
    """Resolve known_refs for `entity_type` from records produced earlier in this same run only, never from ir-store
    history. Returns (known_refs, None), or (None, reason) when a required prerequisite is missing.

    A prerequisite needed more than once (TreatmentPair.treatment_id_1/2) is resolvable only when it is a multi-record
    type with at least that many ready records; each slot then gets a different record."""
    deps = ENTITY_DEPENDENCIES.get(entity_type, [])
    if not deps:
        return {}, None

    required_counts: dict[str, int] = {}
    for _, prereq_type, required in deps:
        if required:
            required_counts[prereq_type] = required_counts.get(prereq_type, 0) + 1

    for prereq_type, needed in required_counts.items():
        ready = _referenceable_records(this_run_records, prereq_type)
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
        ready = _referenceable_records(this_run_records, prereq_type)
        if required_counts.get(prereq_type, 0) > 1:
            # Several slots for the same prerequisite type (TreatmentPair.treatment_id_1/2). For a multi-record entity
            # they are left for _apply_candidate_links to resolve per candidate (binding them here would force every
            # candidate onto the same records); a single-record entity consumes one ready record per slot.
            if entity_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
                idx = consumed.get(prereq_type, 0)
                known_refs[field] = ready[idx]["record_id"]
                consumed[prereq_type] = idx + 1
        elif len(ready) == 1 and _pool_complete(this_run_records, prereq_type):
            # One ready record, but others of this multi-record type were attempted and are not referenceable: binding
            # to it would be binding by elimination, so it is left to the candidate's own verified link.
            known_refs[field] = ready[0]["record_id"]
        # 0 ready, or >1 ready for a single slot: omitted here; _apply_candidate_links refines it per candidate.
    return known_refs, None


def _ready_records(this_run_records: dict, prereq_type: str) -> list[dict]:
    """The 'ready' records of `this_run_records[prereq_type]` (a single record_info dict or a list), as a list."""
    available = this_run_records.get(prereq_type)
    if available is None:
        return []
    records = available if isinstance(available, list) else [available]
    return [r for r in records if r.get("status") == "ready"]


# --- Identity-based prerequisites -------------------------------------------------------------------------------------
# A context prerequisite (e.g. a Site) may be referenced once its identity is established, even if not ready: it was
# committed with a valid payload, at least one identity field is EXTRACTED/INFERRED with a value, and no open concern
# is about an identity field. Treatment and Method are not listed: they are an Observation's scientific links.
IDENTITY_REFERENCEABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "Citation": ("title",),
    "Site": ("name", "latitude", "longitude", "description"),   # grounded coordinates identify a site as well as a name
    "Species": ("scientific_name",),
}


def _open_concern_fields(detail: dict) -> set[str]:
    """Top-level payload fields a record's unanswered concerns (AI validator issues, last errors) are about."""
    fields: set[str] = set()
    for issue in ((detail.get("ai_validation") or {}).get("issues") or []):
        path = issue.get("field") if isinstance(issue, dict) else None
        if path:
            fields.add(re.split(r"[.\[]", str(path))[0])
    for error in detail.get("last_errors") or []:
        path = _error_field(error) if isinstance(error, dict) else None
        if path:
            fields.add(re.split(r"[.\[]", str(path))[0])
    return fields


def _identity_established(record: dict, identity_fields: tuple[str, ...]) -> bool:
    if record.get("status") != "unresolved":
        return False
    detail = record.get("detail") or {}
    payload = detail.get("payload")
    if not isinstance(payload, dict):
        return False   # the committed payload never passed deterministic validation
    in_doubt = _open_concern_fields(detail)
    # Established by ANY identity field that holds a grounded value and is not itself in doubt: a Site whose name was
    # withdrawn is still identified by its grounded coordinates, and one whose coordinates are missing by its name.
    return any(
        f not in in_doubt and isinstance(payload.get(f), dict)
        and payload[f].get("provenance_label") in ("EXTRACTED", "INFERRED") and payload[f].get("value") not in (None, "")
        for f in identity_fields
    )


def _referenceable_records(this_run_records: dict, prereq_type: str) -> list[dict]:
    """The records of `prereq_type` another record may reference: every ready one, plus -- for a context prerequisite
    only (IDENTITY_REFERENCEABLE_FIELDS) -- an unresolved one whose identity is established."""
    identity_fields = IDENTITY_REFERENCEABLE_FIELDS.get(prereq_type)
    available = this_run_records.get(prereq_type)
    if available is None:
        return []
    records = available if isinstance(available, list) else [available]
    return [
        r for r in records
        if r.get("status") == "ready" or (identity_fields and _identity_established(r, identity_fields))
    ]


def _prerequisite_notes(entity_type: str, this_run_records: dict) -> list[dict]:
    """The non-ready records this entity type's REQUIRED prerequisites resolve to -- recorded on every dependent, so a
    reader sees that e.g. an Observation's Site has unresolved fields, never a silently weaker link."""
    notes = []
    for field, prereq_type, required in ENTITY_DEPENDENCIES.get(entity_type, []):
        if not required:
            continue
        for record in _referenceable_records(this_run_records, prereq_type):
            if record.get("status") != "ready":
                notes.append({
                    "field": field, "prerequisite": prereq_type, "record_id": record.get("record_id"),
                    "status": record.get("status"),
                    "open_fields": sorted(_open_concern_fields(record.get("detail") or {})),
                })
    return notes


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
    """For each dependency field whose prerequisite is a multi-record type with more than one ready record, a pool of
    {slug, record_id, name} shown in the enumeration prompt so the model can link a candidate to a specific record by a
    verifiable slug. {} when nothing is ambiguous."""
    pools: dict[str, list[dict]] = {}
    for field, prereq_type, _required in ENTITY_DEPENDENCIES.get(entity_type, []):
        if prereq_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
            continue
        ready = _referenceable_records(this_run_records, prereq_type)
        if not ready or (len(ready) == 1 and _pool_complete(this_run_records, prereq_type)):
            continue            # one record and no failed siblings: `_resolve_known_refs` binds it
        # One ready record of an INCOMPLETE pool is offered too: it is not bound by default any more (that would be
        # binding by elimination), so a candidate reaches it only through its own link.
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


def _experimental_conversion_note(experimental: dict[str, Any]) -> str:
    """The fields the established experimental context determines, stated as instructions for Conversion. Every one
    is then checked (`_observation_relationship_errors`)."""
    lines = ["EXPERIMENTAL_CONTEXT (established by the pipeline for this value -- build the record FROM it):"]
    stats = experimental.get("statistics") or {}
    variable = experimental.get("variable") or {}
    if stats:
        lines.append(f"- value: reported_text {stats['mean_text']!r} (the cell's mean, literally), reported_numeric_value "
                     f"{stats['mean']}" + (f", reported_units as the source writes them ({variable['header_units']!r} in the header)"
                                           if variable.get("header_units") else "") + ".")
        if stats.get("statistic_name"):
            lines.append(f"- statistical_encoding: statistic_name {stats['statistic_name']!r}, statistic_value "
                         f"{stats['statistic_value']!r}, EXTRACTED, citing {stats['statistic_source_anchor']} (where the "
                         f"table says {stats['statistic_source_text']!r}) and the table block.")
        else:
            lines.append("- statistical_encoding: leave it out -- the table does not name a statistic for this cell.")
    method = experimental.get("method") or {}
    if method.get("status") == "linked" and method.get("record_id"):
        lines.append(f"- method_id: {method['record_id']} ({method.get('tier')} link).")
    aggregation = experimental.get("aggregation") or {}
    if aggregation.get("reported_effect_scope") == "treatment_mean":
        lines.append("- reported_effect_scope: treatment_mean, aggregated_over_factors: EXTRACTED empty list.")
    time = experimental.get("time") or {}
    if time.get("pooled over"):
        lines.append(f"- temporal_info covers {time['pooled over']}: never one of those levels alone.")
    return "\n".join(lines)


def _observation_relationship_errors(entity_type: str, payload: dict, candidate_context: Optional[dict[str, Any]]) -> list[dict]:
    """The Observation must agree with its experimental context: the cell's mean, the named statistic, the mapped
    method, the aggregation, the variable's units and a pooled time span. Each disagreement is a validation error fed
    back to Conversion; nothing is overwritten on the model's behalf."""
    experimental = (candidate_context or {}).get("experimental_context")
    if entity_type != "Observation" or not isinstance(experimental, dict) or not isinstance(payload, dict):
        return []
    errors: list[dict] = []

    def field_value(name: str) -> tuple[Optional[str], Any]:
        entry = payload.get(name)
        if isinstance(entry, dict) and "provenance_label" in entry:
            return entry.get("provenance_label"), entry.get("value")
        return None, entry

    stats = experimental.get("statistics") or {}
    label, value = field_value("value")
    if stats and isinstance(value, dict) and value.get("reported_numeric_value") is not None:
        if abs(float(value["reported_numeric_value"]) - float(stats["mean"])) > 1e-9 * max(1.0, abs(float(stats["mean"]))):
            errors.append({"field": "value", "message": (
                f"value.reported_numeric_value={value['reported_numeric_value']} but the cell's mean is {stats['mean']} "
                f"(cell text {experimental.get('cell', {}).get('value_text')!r}) -- report the mean, not the SE or a letter")})
    s_label, encoding = field_value("statistical_encoding")
    if stats.get("statistic_name"):
        expected_name, expected_value = stats["statistic_name"], stats["statistic_value"]
        if not isinstance(encoding, dict) or s_label not in ("EXTRACTED", "INFERRED"):
            errors.append({"field": "statistical_encoding", "message": (
                f"the table states {stats['statistic_source_text']!r} ({stats['statistic_source_anchor']}): give "
                f"statistical_encoding {expected_name!r} = {expected_value!r}")})
        else:
            name_ok = _units_key(str(encoding.get("statistic_name"))) == _units_key(expected_name)
            got = encoding.get("statistic_value")
            value_ok = (abs(float(got) - float(expected_value)) < 1e-9) if isinstance(expected_value, (int, float)) and \
                isinstance(got, (int, float)) else _units_key(str(got)) == _units_key(str(expected_value))
            if not (name_ok and value_ok):
                errors.append({"field": "statistical_encoding", "message": (
                    f"statistical_encoding is {encoding!r} but the cell and table give {expected_name!r} = {expected_value!r}")})
    elif isinstance(encoding, dict) and s_label in ("EXTRACTED", "INFERRED") and encoding.get("statistic_name"):
        errors.append({"field": "statistical_encoding", "message": (
            f"statistical_encoding names {encoding.get('statistic_name')!r}, but the table never states which statistic "
            f"this cell reports -- leave it out rather than assume one")})

    method = experimental.get("method") or {}
    if method.get("status") == "linked" and method.get("record_id") and payload.get("method_id") not in (None, method["record_id"]):
        errors.append({"field": "method_id", "message": (
            f"method_id {payload.get('method_id')!r}, but the paper-level Variable->Method map links this variable to "
            f"{method['record_id']} ({method.get('reason')})")})

    aggregation = experimental.get("aggregation") or {}
    _, scope = field_value("reported_effect_scope")
    if scope and aggregation.get("reported_effect_scope") and scope != aggregation["reported_effect_scope"]:
        errors.append({"field": "reported_effect_scope", "message": (
            f"reported_effect_scope {scope!r}, but this value is a {aggregation['reported_effect_scope']} "
            f"({aggregation.get('basis')})")})
    over_label, over = field_value("aggregated_over_factors")
    expected_over = {re.sub(r"\s+", "", f.lower()) for f in aggregation.get("aggregated_over_factors") or []}
    if expected_over and over_label in ("EXTRACTED", "INFERRED") and isinstance(over, list) \
            and {re.sub(r"\s+", "", str(f).lower()) for f in over} != expected_over:
        errors.append({"field": "aggregated_over_factors", "message": (
            f"aggregated_over_factors {over!r}, but the value is pooled over {aggregation['aggregated_over_factors']!r}")})

    variable = experimental.get("variable") or {}
    units = (value or {}).get("reported_units") if isinstance(value, dict) else None
    record_units = variable.get("record_units")
    if units and record_units and _units_key(units) != _units_key(record_units) and _units_key(record_units) not in _PLACEHOLDER_UNITS:
        errors.append({"field": "value.reported_units", "message": (
            f"reported_units {units!r}, but the linked Variable {variable.get('record_id')} is measured in "
            f"{record_units!r} -- a unit of another variable, or the wrong Variable link")})

    time = experimental.get("time") or {}
    _, temporal = field_value("temporal_info")
    if time.get("pooled over") and isinstance(temporal, dict):
        earliest, latest = str(temporal.get("earliest") or ""), str(temporal.get("latest") or "")
        levels = [l.strip() for l in str(time["pooled over"]).split(",") if l.strip()]
        if len(levels) > 1 and earliest[:4] and earliest[:4] == latest[:4] and earliest[:4] in levels:
            errors.append({"field": "temporal_info", "message": (
                f"temporal_info narrows a value pooled over {time['pooled over']} to {earliest[:4]} alone")})
    return errors


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
    """Refine the entity-wide `known_refs` into per-candidate known_refs, for each dependency field left unresolved
    (more than one ready record):

      1. the candidate's own `linked_candidates` names a slug matching one of this run's ready records -> bind to it
         (re-checked here; this is the safety boundary);
      2. otherwise, a required field gets the full set of ready record_ids as an allowed set: Conversion must pick one
         from it and can never invent a value outside it;
      3. an optional field that is still ambiguous is omitted."""
    resolved = dict(known_refs)
    linked = getattr(candidate, "linked_candidates", None) or {}
    for field, prereq_type, required in ENTITY_DEPENDENCIES.get(entity_type, []):
        if field in resolved:
            continue
        if prereq_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
            continue
        ready = _referenceable_records(this_run_records, prereq_type)
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

        if required and _pool_complete(this_run_records, prereq_type) and not any(r.get("pending") for r in ready):
            resolved[field] = sorted(ready_ids)
        # Optional and still ambiguous -> omitted. A required field whose pool is incomplete is left unresolved too
        # (an allowed set of the ready ones would be a choice by elimination); `_link_refusals` reports it.
    return resolved


# --------------------------------------------------------------------------- #
# Ablation controls (off by default: the default policy is the pipeline's normal behaviour)
# --------------------------------------------------------------------------- #
# SAGE_LINK_POLICY selects how a failed upstream record affects its dependents:
#   strict                    -- a prerequisite that is not ready blocks its dependents (no pending links), and a field
#                                that keeps failing grounding fails the whole record (no withdrawal);
#   extract_first             -- dependents are extracted with a pending link (never ready), no withdrawal;
#   extract_first_withdrawal  -- both (the default).
# SAGE_INJECT_FAULT=site_name corrupts the Site name the converter returns, on every attempt, with an ASCII letter
# change no repair can undo -- the same upstream failure for every policy under comparison.
LINK_POLICIES = ("strict", "extract_first", "extract_first_withdrawal")


def _link_policy() -> str:
    policy = os.environ.get("SAGE_LINK_POLICY", "extract_first_withdrawal")
    if policy not in LINK_POLICIES:
        raise ValueError(f"SAGE_LINK_POLICY={policy!r}; one of {LINK_POLICIES}")
    return policy


def _inject_fault(entity_type: str, payload: Any) -> Optional[tuple[dict, dict]]:
    if os.environ.get("SAGE_INJECT_FAULT") != "site_name" or entity_type != "Site" or not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not (isinstance(name, dict) and isinstance(name.get("value"), str) and re.search(r"[A-Za-z]", name["value"])):
        return None
    value = name["value"]
    i = max(j for j, ch in enumerate(value) if ch.isascii() and ch.isalpha())
    swapped = value[:i] + ("q" if value[i].lower() != "q" else "x") + value[i + 1:]
    return {**payload, "name": {**name, "value": swapped}}, {"fault": "site_name", "from": value, "to": swapped}


def _with_pending(entity_type: str, this_run_records: dict) -> tuple[dict, dict[str, dict]]:
    """Extract first, link later. A view of the run's records in which every required prerequisite that was attempted
    but is not referenceable (unresolved or error, never blocked) stands as a pending link target, plus
    {record_id: {"prerequisite", "status"}} of those records. A record referencing a pending target is committed
    `unresolved` (BLOCKED_PREREQUISITE, `pending_links`) with everything it extracted. A pending target is reached only
    by a single-candidate pool or the candidate's own link, never through an allowed set."""
    if _link_policy() == "strict":
        return this_run_records, {}
    view = dict(this_run_records)
    pending: dict[str, dict] = {}
    for _field, prereq_type, required in ENTITY_DEPENDENCIES.get(entity_type, []):
        if not required:
            continue
        records = _all_records(this_run_records, prereq_type)
        referenceable = {r.get("record_id") for r in _referenceable_records(this_run_records, prereq_type)}
        shadowed = []
        for r in records:
            if r.get("record_id") not in referenceable and r.get("status") in ("unresolved", "error"):
                pending[r["record_id"]] = {"prerequisite": prereq_type, "status": r.get("status")}
                shadowed.append({**r, "status": "ready", "pending": True, "pending_status": r.get("status")})
            else:
                shadowed.append(r)
        if shadowed and any(r.get("pending") for r in shadowed):
            available = this_run_records.get(prereq_type)
            view[prereq_type] = shadowed if isinstance(available, list) else shadowed[0]
    return view, pending


def _pending_refs(payload: Any, pending: Optional[dict[str, dict]], entity_type: str) -> dict[str, str]:
    """{field: record_id} of the payload's reference fields (ENTITY_DEPENDENCIES) that point at a pending prerequisite."""
    if not pending or not isinstance(payload, dict):
        return {}
    fields = {f for f, _t, _r in ENTITY_DEPENDENCIES.get(entity_type, [])}
    out = {}
    for field, value in payload.items():
        if field not in fields:
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, str) and v in pending:
                out[field] = v
    return out


def _all_records(this_run_records: dict, prereq_type: str) -> list[dict]:
    available = this_run_records.get(prereq_type)
    if available is None:
        return []
    return list(available) if isinstance(available, list) else [available]


def _pool_complete(this_run_records: dict, prereq_type: str) -> bool:
    """Every record of `prereq_type` this run attempted is referenceable -- only then can "the ready ones" stand for
    the type as a whole (a single-record type is always complete: it has one record)."""
    if prereq_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
        return True
    records = _all_records(this_run_records, prereq_type)
    return len(_referenceable_records(this_run_records, prereq_type)) == len(records)


def _link_refusals(paper_id: str, entity_type: str, this_run_records: dict, resolved: dict, candidate: Any) -> list[dict]:
    """Required multi-record references `_apply_candidate_links` could not establish for this candidate: its own link
    names no referenceable record, and the pool of that type is incomplete. The candidate is refused with this reason
    (never bound to whichever record survived) -- BLOCKED_PREREQUISITE when its link names a record that failed,
    AMBIGUOUS otherwise."""
    refusals = []
    linked = getattr(candidate, "linked_candidates", None) or {}
    for field, prereq_type, required in ENTITY_DEPENDENCIES.get(entity_type, []):
        if not required or field in resolved or prereq_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
            continue
        records = _all_records(this_run_records, prereq_type)
        ready = _referenceable_records(this_run_records, prereq_type)
        if _pool_complete(this_run_records, prereq_type) and not any(r.get("pending") for r in ready):
            continue            # every attempted record is referenceable: the allowed set is not an elimination
        ready = [r for r in ready if not r.get("pending")]
        slug = linked.get(field)
        named = f"{paper_id}_{prereq_type.lower()}_{_sanitize_candidate_id(slug)}" if slug else None
        failed = next((r for r in records if named and r.get("record_id") == named), None)
        refusals.append({
            "field": field, "prerequisite": prereq_type,
            "cause": causes.BLOCKED_PREREQUISITE if failed else causes.AMBIGUOUS,
            "message": (
                f"{field}: this candidate's own link names {named}, which is {failed.get('status')} in this run -- the "
                f"record is not bound to another {prereq_type} instead" if failed else
                f"{field}: no link of this candidate names a {prereq_type} of this run, and {len(records) - len(ready)} "
                f"of the run's {len(records)} {prereq_type} record(s) are not ready -- choosing among the "
                f"{len(ready)} that are would be a choice by elimination"
            ),
        })
    return refusals


def _entity_record_id(paper_id: str, entity_type: str) -> str:
    # Citation is 1:1 with the paper, so its record_id is the paper_id; others are <paper_id>_<entity_type>.
    if entity_type == "Citation":
        return paper_id
    return f"{paper_id}_{entity_type.lower()}"


def _design_section(run_id: str, paper_id: str) -> Optional[dict[str, Any]]:
    """The run's experimental-design summary (factors and levels per table, layouts, sample-size evidence, conflicts)
    from the cached table pass; no model call."""
    classifications = {key.split("__", 1)[1]: c for key, c in _cached_table_classifications(run_id).items()}
    if not classifications:
        return None
    try:
        index = evidence_index.build_evidence_index(document_map.build_document_map(paper_id, _papers_root()))
    except (FileNotFoundError, OSError):
        return None
    return design.design_summary(classifications, index)


def _outcome_causes(paper_id: str, run_id: str, this_run_records: dict) -> dict[str, dict[str, int]]:
    """{entity_type: {cause: count}} for every non-ready record of the run -- the Stage 4 taxonomy, so a run's losses
    read as "12 AMBIGUOUS, 3 NOT_RETRIEVED" instead of "15 unresolved"."""
    summary: dict[str, dict[str, int]] = {}
    for entity_type, records in this_run_records.items():
        for info in (records if isinstance(records, list) else [records]):
            cause = _entity_result_file(paper_id, entity_type, run_id, info.get("record_id") or "", info).get("unresolved_cause")
            if cause:
                summary.setdefault(entity_type, {}).setdefault(cause, 0)
                summary[entity_type][cause] += 1
    return summary


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
    if record_info.get("prerequisite_notes"):
        base["prerequisite_notes"] = record_info["prerequisite_notes"]
    coverage = (detail.get("context_bundle") or {}).get("coverage")
    cause = causes.classify(status, detail, record_info.get("reason"), coverage, entity_type)
    if cause:
        base["unresolved_cause"] = cause
    if coverage:
        base["evidence_coverage"] = coverage
    for key in ("field_demotions", "open_concerns", "withdrawn_fields", "pending_links"):
        if detail.get(key):
            base[key] = detail[key]
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
    resume_from: Optional[str] = None,
) -> dict:
    """Run one full-paper extraction, exclusively. With `resume_from` (an entity type) and the `run_id` of an existing
    run, only that entity type and the ones depending on it are re-run, inside that run (see `_resume_state`).

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
    if resume_from and not run_id:
        raise ValueError("resume_from needs the run_id of the run to resume")
    run_id = run_id or _new_run_id()
    with run_lock.paper_run_lock(paper_id, run_id):
        return _run_paper_locked(
            paper_id=paper_id, model=model, client=client, invoke=invoke,
            enable_ai_validation=enable_ai_validation, run_id=run_id, manifest_extra=manifest_extra,
            resume_from=resume_from,
        )


def _resume_state(paper_id: str, run_id: str, order: list[str], resume_from: str) -> tuple[dict[str, Any], dict]:
    """Re-run one failed step without re-running the paper. Returns (the records of every entity type that is not
    re-run, reloaded from each record's final.json; the resume note).

    `resume_from` and every entity type depending on it (transitively) are re-run under the same run_id, in dependency
    order. Their previous artifacts, and any failed table classification (a successful one is reused), are moved to
    runs/<run_id>/superseded/<timestamp>/, never deleted."""
    if resume_from not in order:
        raise ValueError(f"unknown entity type {resume_from!r}; one of {order}")
    manifest = run_store.load_run_manifest(run_id)
    if not isinstance(manifest, dict) or manifest.get("paper_id") != paper_id:
        raise ValueError(f"run {run_id} is not a run of {paper_id}")
    rerun = {resume_from}
    changed = True
    while changed:                     # every entity type that depends on a re-run one, transitively
        changed = False
        for entity_type in order:
            deps = {t for _f, t, _r in ENTITY_DEPENDENCIES.get(entity_type, [])} | {t for _f, t in OPTIONAL_LINKS.get(entity_type, [])}
            if entity_type not in rerun and deps & rerun:
                rerun.add(entity_type)
                changed = True
    kept_types = [t for t in order if t not in rerun]
    records_root = run_store.run_dir(run_id) / "records"
    stamp = time.strftime("%Y%m%dT%H%M%S")
    superseded_dir = run_store.run_dir(run_id) / "superseded" / stamp
    moved = []
    for key in run_store.list_records(run_id):
        entity = key.split("__", 1)[0]
        retry_table = key.startswith("table_classification__") and _load_cached_table_failure(run_id, key) is not None
        if (entity in order and entity not in kept_types) or retry_table:
            superseded_dir.mkdir(parents=True, exist_ok=True)
            (records_root / key).rename(superseded_dir / key)
            moved.append(key)

    def record_info(entity_type: str, record_id: str, fallback: dict) -> dict:
        path = run_store.record_dir(run_id, f"{entity_type}__{record_id}") / "final.json"
        detail = run_store.load_json(path) if path.is_file() else {}
        status = (detail or {}).get("status") or fallback.get("status")
        info = {"entity_type": entity_type, "record_id": record_id, "status": status, "detail": detail}
        if status == "blocked":
            info["reason"] = (detail or {}).get("reason") or fallback.get("reason")
        return info

    records: dict[str, Any] = {}
    for entity_type in kept_types:
        stored = results_store.load_any_entity_results(paper_id, entity_type, run_id=run_id)
        infos = [record_info(entity_type, r["record_id"], r) for r in stored if isinstance(r, dict) and r.get("record_id")]
        if entity_type in results_store.MULTI_RECORD_ENTITY_TYPES:
            records[entity_type] = infos
        elif infos:
            records[entity_type] = infos[0]
    return records, {"resumed_from": resume_from, "resumed_at": time.time(), "kept": kept_types,
                     "superseded": {"dir": str(superseded_dir), "records": moved},
                     "previous_started_at": manifest.get("started_at")}


def _run_paper_locked(
    *,
    paper_id: str,
    model: str,
    client: IRServiceClient,
    invoke: Callable[..., AgentInvocation],
    enable_ai_validation: bool,
    run_id: str,
    manifest_extra: Optional[dict],
    resume_from: Optional[str] = None,
) -> dict:
    """Run Extraction -> Conversion -> deterministic validation -> AI Validator -> commit for every entity type of one
    paper, in dependency order, under one run_id. Multi-record types (`results_store.MULTI_RECORD_ENTITY_TYPES`) run an
    enumeration pass and one record per candidate (`_run_multi_record_entity`).

    Writes results/<paper_id>/<run_id>/ from what this call produced only, never from ir-store history."""
    order = _topological_entity_order()
    this_run_records: dict[str, Any] = {}  # dict per entity_type, or list[dict] for a multi-record type
    resume_note: Optional[dict] = None
    if resume_from:
        this_run_records, resume_note = _resume_state(paper_id, run_id, order, resume_from)

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
        **({"resume": resume_note} if resume_note else {}),
    }
    # The manifest exists from the START of the run (a crashed run used to
    # leave none at all); `run_status` says whether it is still going,
    # finished, or failed. `results/<paper>/LATEST` is NOT moved until the
    # run completed, so readers keep seeing the previous completed run.
    run_store.save_run_manifest(run_id, {**base_manifest, "run_status": "running"})
    results_store.mark_run_results(paper_id, run_id)

    try:
        for entity_type in order:
            if resume_note and entity_type in resume_note["kept"]:
                continue            # reloaded from the run being resumed
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
            link_view, pending = _with_pending(entity_type, this_run_records)
            known_refs, blocked_reason = _resolve_known_refs(entity_type, link_view)

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
                    known_refs=known_refs or None, pending_prerequisites=pending or None,
                )
                record_info = {
                    "entity_type": entity_type, "record_id": record_id,
                    "status": result.status, "detail": result.detail,
                }
                prerequisite_notes = _prerequisite_notes(entity_type, this_run_records)
                if prerequisite_notes:
                    record_info["prerequisite_notes"] = prerequisite_notes

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
        "outcome_causes": _outcome_causes(paper_id, run_id, this_run_records),
        "design": _design_section(run_id, paper_id),
        "representation_blocked": {
            "count": len(_REPRESENTATION_BLOCKED.get(run_id, [])), "reason": design.L4_TREATMENT_POOLED,
            "cells": _REPRESENTATION_BLOCKED.pop(run_id, []),
            "unidentified_rows": _UNIDENTIFIED_ROWS.pop(run_id, []),
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
             "committed.",
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
    run_paper_cmd.add_argument("--resume-run", default=None,
                               help="Re-run inside this existing run, from --from on (earlier entity types are reused).")
    run_paper_cmd.add_argument("--from", dest="resume_from", default=None,
                               help="With --resume-run: the entity type to re-run from (e.g. Site).")

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
                enable_ai_validation=not args.no_ai_validation, run_id=args.resume_run or args.run_id,
                manifest_extra=manifest_extra, resume_from=args.resume_from if args.resume_run else None,
            )
        except (run_lock.RunAlreadyActive, ValueError) as exc:
            print(f"refusing to start: {exc}")
            return 1
        print(f"\nrun_id: {outcome['run_id']}")
        print(f"entity_order: {outcome['entity_order']}")
        any_error = False
        for entity_type in outcome["entity_order"]:
            record = outcome["records"][entity_type]
            if isinstance(record, list):
                # Multi-record entity type: summarise every record produced this run.
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
