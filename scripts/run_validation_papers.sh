#!/usr/bin/env bash
# Sequential end-to-end validation runner for the CURRENT Sage pipeline.
#
# Runs each paper, one after another, through the existing, unmodified single-paper command
#
#   python -m pipeline.orchestrator run-paper --paper-id <PAPER> --run-id <RUN_ID>
#
# with the pipeline's own configuration (src/eval_config.json: jetstream-gpt/gpt-oss-120b, AI validation on).
# Nothing about extraction, results or run isolation is reimplemented here: run-paper itself writes
# runs/<run_id>/ , results/<paper>/<run_id>/ (+ the LATEST pointer on completion) and ir-store/.
# This script only picks an explicit run id per paper (so the log maps to the artifacts), keeps going when a
# paper fails, and records what happened.
#
# Usage (from anywhere):
#   scripts/run_validation_papers.sh                       # the six target papers, in order
#   scripts/run_validation_papers.sh Daren-1997-Canopy ... # or just the papers named
#
# Environment:
#   PAPER_MAX_RUNTIME   hard per-paper cap, default 12h (SIGTERM, then SIGKILL after 60s); a safety net against a
#                       hung process only -- the pipeline's own call timeouts and retry budgets still apply.
#
# Logs: scripts/overnight_logs/validation_<stamp>/{status.tsv, runner.log, <paper>.log}

set -u  # deliberately NOT -e: one paper failing must never stop the loop

# `nohup` started from a terminal leaves stdin as an UNREADABLE descriptor, and the `opencode` agent CLI fails every
# call with "EBADF: bad file descriptor, read" (every paper then dies at Citation within ~90 s). The pipeline is
# therefore always started with stdin from /dev/null (below) -- keep it that way.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO/src" || exit 1

PYTHON="$REPO/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python"
UVICORN="$REPO/.venv/bin/uvicorn"
[ -x "$UVICORN" ] || UVICORN="uvicorn"

DEFAULT_PAPERS=(
  "Daren-1997-Canopy"
  "Felipe-2010-Cultivar"
  "Paul-1998-Foliar"
  "Kathryn-2020-Winter"
  "Oceologia-1998"
  "Berntson-1997-Regenerating"
)
if [ "$#" -gt 0 ]; then PAPERS=("$@"); else PAPERS=("${DEFAULT_PAPERS[@]}"); fi

STAMP="$(date +%Y%m%dT%H%M%S)"
LOG_DIR="$REPO/scripts/overnight_logs/validation_${STAMP}"
mkdir -p "$LOG_DIR"
STATUS="$LOG_DIR/status.tsv"
RUNNER_LOG="$LOG_DIR/runner.log"
IR_SERVICE_URL="http://127.0.0.1:8420"
MAX_RUNTIME="${PAPER_MAX_RUNTIME:-12h}"
KILL_GRACE="60s"

printf "paper\trun_id\tstart\tend\truntime_s\tstatus\texit_code\trun_status_in_manifest\n" > "$STATUS"
log() { echo "[$(date -Iseconds)] $*" | tee -a "$RUNNER_LOG"; }

service_healthy() { curl -s -o /dev/null -w "%{http_code}" "$IR_SERVICE_URL/health" 2>/dev/null | grep -q "200"; }

ensure_ir_service() {
  service_healthy && return 0
  log "ir_service not healthy -- starting it."
  nohup "$UVICORN" pipeline.ir_service:app --host 127.0.0.1 --port 8420 < /dev/null >> "$LOG_DIR/ir_service.log" 2>&1 &
  disown
  for _ in $(seq 1 30); do
    sleep 2
    service_healthy && { log "ir_service healthy."; return 0; }
  done
  log "ir_service FAILED to become healthy after 60s."
  return 1
}

log "validation batch ${STAMP}: ${PAPERS[*]} (per-paper cap ${MAX_RUNTIME})"

for paper in "${PAPERS[@]}"; do
  key="${paper%%-*}"
  run_id="val_${STAMP}_${key}"
  paper_log="$LOG_DIR/${paper}.log"
  start_ts="$(date -Iseconds)"; start_epoch="$(date +%s)"
  log "START ${paper} run_id=${run_id}"

  if [ ! -f "paper/${paper}/content.md" ]; then
    echo "no src/paper/${paper}/content.md -- nothing to run" > "$paper_log"
    exit_code=1
  elif ! ensure_ir_service; then
    echo "ir_service unavailable -- run-paper not started" > "$paper_log"
    exit_code=1
  else
    timeout -k "$KILL_GRACE" "$MAX_RUNTIME" \
      "$PYTHON" -u -m pipeline.orchestrator run-paper --paper-id "$paper" --run-id "$run_id" \
      < /dev/null > "$paper_log" 2>&1
    exit_code=$?
  fi

  end_ts="$(date -Iseconds)"; runtime=$(( $(date +%s) - start_epoch ))
  if [ "$exit_code" -eq 124 ] || [ "$exit_code" -eq 137 ]; then status="timed_out"
  elif [ "$exit_code" -eq 0 ]; then status="completed"
  else status="failed"; fi

  # What the pipeline itself recorded for this run (empty when it never got as far as writing a manifest).
  manifest_status="$("$PYTHON" - "$run_id" <<'PY' 2>/dev/null
import json, sys
from pathlib import Path
p = Path("runs") / sys.argv[1] / "manifest.json"
print(json.loads(p.read_text()).get("run_status", "") if p.is_file() else "")
PY
)"

  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$paper" "$run_id" "$start_ts" "$end_ts" "$runtime" "$status" "$exit_code" "$manifest_status" >> "$STATUS"
  log "END   ${paper} status=${status} exit_code=${exit_code} runtime=${runtime}s manifest_run_status=${manifest_status:-n/a}"
done

log "ALL PAPERS DONE"
