# SAGE public deployment (Jetstream VM)

The review UI runs on the Jetstream2 VM as two systemd services behind nginx, so it keeps working without
anyone logged in to the VM:

```
browser ──▶ http://149.165.169.169 (nginx :80) ──▶ 127.0.0.1:8501 sage-streamlit (Streamlit UI)
                                                         ├──▶ 127.0.0.1:8420 sage-ir (FastAPI IR service)
                                                         └──▶ opencode CLI ──▶ Jetstream LLM endpoint (gpt-oss-120b)
```

| Piece | Where | Config in this repo |
|---|---|---|
| `sage-ir.service` | `/etc/systemd/system/` | `deploy/systemd/sage-ir.service` |
| `sage-streamlit.service` | `/etc/systemd/system/` | `deploy/systemd/sage-streamlit.service` |
| nginx site `sage` | `/etc/nginx/sites-available/sage` | `deploy/nginx/sage` |
| opencode CLI | snap, `/snap/bin/opencode` | pinned by `deploy/apply_deploy_fixes.sh` |

The files in `deploy/` are the source of truth. Change them there, then install with
`sudo bash deploy/apply_deploy_fixes.sh`. The script backs up every `/etc` file it replaces and refuses to run
while an extraction is active.

## opencode version

The evaluation runs (Felipe regress, final, e2e, ...) all used **opencode 1.18.27** (snap revision 217). The snap
refreshed itself to 1.18.32 on 2026-09-22. The install script reverts to revision 217 while it is still on disk and
**holds** the snap, so it can no longer change silently. 1.18.27 is no longer in any store channel: once held, the
local copy is the only one, so do not remove it.

```bash
/snap/bin/opencode --version          # expect 1.18.27
snap list --all opencode              # installed revision; "held" in Notes
snap refresh --list                   # what an unheld refresh would install
```

Upgrade deliberately, not automatically. Run `sudo snap refresh --unhold opencode && sudo snap refresh opencode`,
check it against a known paper, then hold it again with `sudo snap refresh --hold opencode`. Every run manifest
records `opencode_cli_version`, so a version change shows up in `runs/<run_id>/manifest.json`.

The pipeline finds the CLI in this order: `OPENCODE_BIN` (if set), then `PATH`, then `/snap/bin/opencode`
(`pipeline.run_config.resolve_opencode_bin`).

## Updating the code

```bash
cd /home/arai/projects/sage
git pull
.venv/bin/pip install -r requirements.txt          # only when requirements.txt changed
sudo systemctl restart sage-ir sage-streamlit      # never while an extraction is running
```

Which service to restart after a change:

| Changed | Restart | Why |
|---|---|---|
| `src/pipeline/ir_schema.py`, `validators.py`, `ir_service.py`, `reconstruction.py` | `sage-ir` **and** `sage-streamlit` | The IR service keeps the code it started with. Every run checks the schema fingerprint and refuses a stale service ("ir_service is running STALE ..."). |
| any other `src/pipeline/*.py`, `src/docproc/*`, `streamlit_app/*` | `sage-streamlit` | Runs execute inside the Streamlit process. |
| `src/opencode.json`, `src/opencode-config/*`, `src/eval_config.json` | nothing | Read at the start of each run. |
| `deploy/*` | `sudo bash deploy/apply_deploy_fixes.sh` | Installs and restarts. |

Check that nothing is running first. A restart kills an in-progress extraction, because runs still execute inside
the Streamlit process:

```bash
for f in src/runs/.locks/*.lock; do cat "$f"; echo; done    # a lock whose pid is alive = a run in progress
```

## Logs

```bash
journalctl -u sage-streamlit -f        # UI and every extraction started from it
journalctl -u sage-ir -f               # IR service
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
```

Per-run artifacts are in `src/runs/<run_id>/` (`manifest.json` has the status, model, fingerprints and opencode
version). Reviewable results are in `src/results/<paper_id>/<run_id>/`.

## Health check

```bash
systemctl is-active sage-ir sage-streamlit nginx                 # all "active"
curl -s http://127.0.0.1:8420/health                             # IR service + its schema_fingerprint
curl -s -o /dev/null -w "%{http_code}\n" http://149.165.169.169/ # 200
sudo -u arai env PATH=/home/arai/projects/sage/.venv/bin:/usr/bin:/bin:/snap/bin \
  /home/arai/projects/sage/.venv/bin/python -c \
  "import sys; sys.path[:0]=['src','streamlit_app']; import sage_paths, marker_pipeline; print(marker_pipeline.preflight())"
```

The last command runs the same pre-flight the UI runs before every extraction: `opencode` found and runs, and the IR
service is reachable and current. When it fails, Extract shows the reason and starts no run.

## Known gaps (not yet addressed)

- **No login:** anyone who reaches the public IP can upload, extract (spending LLM quota), rename and delete.
- **Plain HTTP:** no TLS.
- **Runs die with the UI process:** restarting `sage-streamlit` (or a crash) kills a running extraction. Its
  manifest stays `running` and its paper lock goes stale; the stale lock is replaced automatically by the next run.
