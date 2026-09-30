#!/usr/bin/env bash
# Live, read-only view of what the SAGE pipeline is doing right now -- the same thing whether the run was started from
# the Streamlit UI or from the CLI. Changes nothing.
#
#   bash scripts/watch_run.sh                 # refresh every 5 s (Ctrl-C to stop)
#   bash scripts/watch_run.sh --once          # print once
#   bash scripts/watch_run.sh --run <run_id>  # a specific (also finished) run
#
# Shows: the stage (Marker / document preparation / extraction), the model call in flight (which agent, entity type and
# record), the status of every record finished so far, and the last records to complete.
cd "$(dirname "${BASH_SOURCE[0]}")/../src" || exit 1

ONCE=0; RUN_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --once) ONCE=1 ;;
    --run) RUN_ID="$2"; shift ;;
  esac
  shift
done

show() {
  RUN_ID="$RUN_ID" python3 - <<'EOF'
import json, os, re, subprocess, time
from pathlib import Path

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

def alive(pid):
    try:
        os.kill(int(pid), 0); return True
    except (OSError, ValueError, TypeError):
        return False

print(time.strftime("SAGE pipeline status  %Y-%m-%d %H:%M:%S"))
print("=" * 78)

# ---- Stage 1: Marker (PDF -> Marker JSON) ---------------------------------------------------------------
marker = [l for l in sh("pgrep -af 'bin/marker' | grep -v pgrep").splitlines() if l.strip()]
if marker:
    print("STAGE: MARKER is running (PDF -> Marker JSON)")
    for l in marker:
        print("   ", l[:160])

# ---- Stage 2: document preparation (Marker JSON -> content.md / provenance.json) -------------------------
papers = sorted(Path("paper").glob("*/content.md"), key=lambda p: p.stat().st_mtime, reverse=True)
if papers:
    p = papers[0]
    age = time.time() - p.stat().st_mtime
    print(f"Last document prepared: {p.parent.name}  (content.md written {int(age // 60)} min ago)")

# ---- Stage 3: extraction -------------------------------------------------------------------------------------
active = []
for lock in Path("runs/.locks").glob("*.lock"):
    try:
        info = json.loads(lock.read_text())
    except (OSError, ValueError):
        continue
    info["alive"] = alive(info.get("pid"))
    active.append(info)
for info in active:
    state = "RUNNING" if info["alive"] else "stale lock (process gone)"
    print(f"Extraction lock: {info['paper_id']}  run {info['run_id']}  -> {state}")

run_id = os.environ.get("RUN_ID") or next((i["run_id"] for i in active if i["alive"]), None)
if not run_id:
    runs = sorted((d for d in Path("runs").iterdir() if (d / "manifest.json").is_file()), key=lambda d: d.stat().st_mtime)
    run_id = runs[-1].name if runs else None
    if run_id:
        print(f"No extraction running. Most recent run: {run_id}")
if not run_id:
    raise SystemExit

run = Path("runs") / run_id
m = json.loads((run / "manifest.json").read_text())
started = m.get("started_at") or 0
elapsed = (m.get("finished_at") or time.time()) - started
print("-" * 78)
print(f"RUN {run_id}   paper: {m.get('paper_id')}   status: {m.get('run_status')}   elapsed: {int(elapsed // 60)} min")
print(f"model: {m.get('model')}   opencode: {m.get('opencode_cli_version')}")

# ---- the model call in flight (one `opencode run` process) ----------------------------------------------
calls = [l for l in sh("pgrep -af 'opencode run' | grep -v pgrep").splitlines() if l.strip()]
for l in calls:
    agent = (re.search(r"--agent (\S+)", l) or [None, "?"])[1]
    text = l.split("--format json", 1)[-1]
    what = "?"
    for pattern, label in (
        (r"Sage IR `(\w+)` record \(record_id=`([^`]+)`\)", "{0}  {1}"),
        (r"Identify every DISTINCT real-world (\w+)", "{0}  (enumerating candidates)"),
        (r"table at content\.md anchor '(b:\d+)'", "table {0}  (Step B: table classification)"),
        (r"deterministically-valid Sage IR `(\w+)`", "{0}  (AI validation)"),
    ):
        hit = re.search(pattern, text)
        if hit:
            what = label.format(*hit.groups()); break
    print(f"NOW: {agent:12s} -> {what}")
if not calls and m.get("run_status") == "running":
    # The newest attempt artifact says what the pipeline is waiting on.
    attempts = sorted((run / "records").glob("*/*/attempt*.json"), key=lambda p: p.stat().st_mtime)
    last = None
    if attempts:
        try:
            last = json.loads(attempts[-1].read_text())
        except (OSError, ValueError):
            last = None
    record = attempts[-1].parent.parent.name if attempts else "?"
    fc = (last or {}).get("failure_class") or ""
    if fc.startswith("provider_"):
        rounds = len([p for p in attempts[-1].parent.glob("attempt*.json")
                      if (json.loads(p.read_text()).get("failure_class") or "").startswith("provider_")])
        since = int(time.time() - attempts[-1].stat().st_mtime)
        print(f"NOW: WAITING -- provider cooldown on {record} after {rounds} empty/failed model repl(y/ies) "
              f"({fc}, last one {since} s ago); the same call is retried after the wait")
    elif fc == "no_final_answer":
        print(f"NOW: Citation stall on {record}: retrying with the 'answer now' instruction")
    else:
        print("NOW: no model call in flight (validating / committing the last answer)")

# ---- every finished record, grouped by entity type, in pipeline order ---------------------------------------
order = m.get("entity_order") or []
finals = []
for d in (run / "records").iterdir() if (run / "records").is_dir() else []:
    f = d / "final.json"
    if f.is_file():
        try:
            finals.append((f.stat().st_mtime, d.name, json.loads(f.read_text()).get("status")))
        except (OSError, ValueError):
            pass
print("-" * 78)
by_type = {}
for _, key, status in finals:
    et = key.split("__", 1)[0]
    if et.startswith("table_classification"):
        et = "Step B tables"
    by_type.setdefault(et, {}).setdefault(status or "?", 0)
    by_type[et][status or "?"] += 1
seen = [et for et in order if et in by_type] + [et for et in by_type if et not in order]
for et in seen:
    counts = ", ".join(f"{n} {s}" for s, n in sorted(by_type[et].items()))
    print(f"  {et:15s} {counts}")
pending = [et for et in order if et not in by_type]
if pending and m.get("run_status") == "running":
    print(f"  not started yet: {', '.join(pending)}")

print("-" * 78)
print("Last finished:")
for mtime, key, status in sorted(finals)[-8:]:
    print(f"  {time.strftime('%H:%M:%S', time.localtime(mtime))}  {status or '?':10s} {key[:90]}")
EOF
}

if [ "$ONCE" = 1 ]; then show; exit 0; fi
while true; do clear; show; sleep 5; done
