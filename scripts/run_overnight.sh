#!/usr/bin/env bash
# Overnight batch runner for the CURRENT Sage pipeline.
#
# Runs every paper in PAPERS sequentially through the existing, unmodified
# single-paper command:
#
#   python -m pipeline.orchestrator run-paper --paper-id <PAPER_ID>
#
# No model/prompt/validation flags are added -- this is the pipeline's own
# default configuration (the model pinned in src/eval_config.json, AI
# validation on). Each paper gets a hard cap (MAX_RUNTIME, default 3h;
# SIGTERM at the cap, SIGKILL 60s later if it hasn't exited) via `timeout`;
# whatever happens to one
# paper (success, error, timeout, non-zero exit) never stops the loop --
# there is no `set -e`, and each iteration's exit status is captured and
# logged, never checked with anything that would abort the script.
#
# Output storage is untouched: run-paper already writes to
# results/<paper_id>/ (and runs/, ir-store/) itself -- this script adds
# nothing there, only its own execution log alongside the per-paper stdout
# capture, both under scripts/overnight_logs/.
#
# Usage:
#   bash scripts/run_overnight.sh                        # every paper in PAPERS, 3h cap each
#   MAX_RUNTIME=6h bash scripts/run_overnight.sh A B C   # only these papers, in this order
#
# The ir_service is only STARTED when it is not healthy -- a healthy service
# keeps whatever validator code it loaded. Restart it yourself after changing
# pipeline/validators.py (or anything else it imports) before a batch.

set -u  # deliberately NOT -e: one paper's failure must never abort the loop
cd "$(dirname "${BASH_SOURCE[0]}")/../src" || exit 1

LOG_DIR="../scripts/overnight_logs"
mkdir -p "$LOG_DIR"
EXEC_LOG="$LOG_DIR/execution_log.tsv"
IR_SERVICE_URL="http://127.0.0.1:8420"
MAX_RUNTIME="${MAX_RUNTIME:-3h}"
KILL_GRACE="60s"

PAPERS=(
  "Berntson-1997-Regenerating"
  "Daren-1997-Canopy"
  "Felipe-2010-Cultivar"
  "Kathryn-2020-Winter"
  "Paul-1998-Foliar"
  "Philippe-2007-Six"
  "Smulker-2012-Assessment"
)
if [ "$#" -gt 0 ]; then
  PAPERS=("$@")
fi

if [ ! -f "$EXEC_LOG" ]; then
  printf "paper_name\tstart_time\tend_time\truntime_seconds\tstatus\texit_code\n" > "$EXEC_LOG"
fi

ensure_ir_service() {
  if curl -s -o /dev/null -w "%{http_code}" "$IR_SERVICE_URL/health" 2>/dev/null | grep -q "200"; then
    return 0
  fi
  echo "[$(date -Iseconds)] ir_service not healthy -- starting it." >> "$LOG_DIR/runner.log"
  nohup uvicorn pipeline.ir_service:app --host 127.0.0.1 --port 8420 \
    >> "$LOG_DIR/ir_service.log" 2>&1 &
  disown
  for _ in $(seq 1 30); do
    sleep 2
    if curl -s -o /dev/null -w "%{http_code}" "$IR_SERVICE_URL/health" 2>/dev/null | grep -q "200"; then
      echo "[$(date -Iseconds)] ir_service healthy." >> "$LOG_DIR/runner.log"
      return 0
    fi
  done
  echo "[$(date -Iseconds)] ir_service FAILED to become healthy after 60s." >> "$LOG_DIR/runner.log"
  return 1
}

for paper in "${PAPERS[@]}"; do
  start_ts=$(date -Iseconds)
  start_epoch=$(date +%s)
  paper_log="$LOG_DIR/${paper}.log"
  echo "[$(date -Iseconds)] START $paper" >> "$LOG_DIR/runner.log"

  if [ ! -f "paper/${paper}/content.md" ]; then
    # Prerequisite docproc output missing -- run-paper would just fail every
    # entity type anyway; recording this directly keeps the log honest
    # about WHY without spending a wasted attempt.
    echo "no paper/${paper}/content.md -- docproc output missing, skipping run-paper for this one" > "$paper_log"
    exit_code=1
  else
    ensure_ir_service
    timeout -k "$KILL_GRACE" "$MAX_RUNTIME" \
      python -m pipeline.orchestrator run-paper --paper-id "$paper" \
      > "$paper_log" 2>&1
    exit_code=$?
  fi

  end_ts=$(date -Iseconds)
  end_epoch=$(date +%s)
  runtime=$((end_epoch - start_epoch))

  if [ "$exit_code" -eq 124 ] || [ "$exit_code" -eq 137 ]; then
    status="timed_out"
  elif [ "$exit_code" -eq 0 ]; then
    status="completed"
  else
    status="failed"
  fi

  printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$paper" "$start_ts" "$end_ts" "$runtime" "$status" "$exit_code" >> "$EXEC_LOG"
  echo "[$(date -Iseconds)] END $paper status=$status exit_code=$exit_code runtime=${runtime}s" >> "$LOG_DIR/runner.log"
done

echo "[$(date -Iseconds)] ALL PAPERS DONE" >> "$LOG_DIR/runner.log"
