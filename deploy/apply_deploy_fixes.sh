#!/usr/bin/env bash
# Install the SAGE public-deployment configuration kept in this directory, and pin the opencode CLI.
# See docs/deployment.md. Run as root:   sudo bash deploy/apply_deploy_fixes.sh
#
#   1. systemd units  -> /etc/systemd/system/  (sage-streamlit gets /snap/bin on its PATH, so `opencode` is found)
#   2. nginx site     -> /etc/nginx/sites-available/sage  (client_max_body_size 100m, so PDF uploads fit)
#   3. opencode snap  -> revision 217 (1.18.27, the version every evaluation run used), held against auto-refresh
#
# Refuses to run while an extraction is active (a restart would kill it). Every replaced /etc file is backed up
# next to itself as <file>.bak-<timestamp>; a failing nginx config test restores the backup and stops.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$REPO/deploy"
STAMP="$(date +%Y%m%dT%H%M%S)"
OPENCODE_REVISION=217        # opencode 1.18.27
OPENCODE_VERSION=1.18.27

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0" >&2; exit 1; }

echo "== 0. no extraction may be running"
for lock in "$REPO"/src/runs/.locks/*.lock; do
  [ -f "$lock" ] || continue
  pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid", 0))' "$lock")"
  if [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null; then
    echo "refusing: an extraction run is active ($lock, pid $pid) -- wait for it to finish" >&2
    exit 1
  fi
done
echo "   none active"

install_file() {  # src dest
  if [ -f "$2" ]; then cp -p "$2" "$2.bak-$STAMP"; echo "   backup: $2.bak-$STAMP"; fi
  install -m 0644 "$1" "$2"
  echo "   installed: $2"
}

echo "== 1. systemd units"
install_file "$DEPLOY/systemd/sage-ir.service" /etc/systemd/system/sage-ir.service
install_file "$DEPLOY/systemd/sage-streamlit.service" /etc/systemd/system/sage-streamlit.service
systemctl daemon-reload

echo "== 2. nginx site"
install_file "$DEPLOY/nginx/sage" /etc/nginx/sites-available/sage
if ! nginx -t; then
  echo "nginx config test FAILED -- restoring the previous site file" >&2
  cp -p "/etc/nginx/sites-available/sage.bak-$STAMP" /etc/nginx/sites-available/sage
  exit 1
fi
systemctl reload nginx

echo "== 3. opencode $OPENCODE_VERSION (snap revision $OPENCODE_REVISION), held"
current="$(snap list opencode | awk 'NR==2{print $3}')"
if [ "$current" != "$OPENCODE_REVISION" ]; then
  if snap list --all opencode | awk 'NR>1{print $3}' | grep -qx "$OPENCODE_REVISION"; then
    snap revert opencode --revision "$OPENCODE_REVISION"
  else
    echo "   revision $OPENCODE_REVISION is no longer on disk -- NOT changing the version; holding the current one" >&2
  fi
fi
snap refresh --hold opencode
echo "   opencode now: $(/snap/bin/opencode --version)"

echo "== 4. restart services (IR service first; the UI requires it)"
systemctl restart sage-ir
systemctl restart sage-streamlit
sleep 5
systemctl --no-pager --lines=0 status sage-ir sage-streamlit nginx | grep -E "●|Active:"
echo "   health: $(curl -s http://127.0.0.1:8420/health)"
echo "done."
