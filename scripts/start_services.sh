#!/usr/bin/env bash
# Start the local services afterstage talks to: Ollama (systemd) and ComfyUI (background, loopback only).
#   bash scripts/start_services.sh          # start
#   bash scripts/start_services.sh stop     # stop ComfyUI
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$REPO/scripts/afterstage.env" ]] && source "$REPO/scripts/afterstage.env"
COMFYUI_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
PIDFILE="$REPO/projects/.comfyui.pid"
LOG="$REPO/projects/comfyui.log"
mkdir -p "$REPO/projects"

if [[ "${1:-start}" == "stop" ]]; then
  [[ -f "$PIDFILE" ]] && kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE" && echo "ComfyUI stopped"
  exit 0
fi

systemctl is-active --quiet ollama || sudo systemctl start ollama

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "ComfyUI already running (pid $(cat "$PIDFILE"))"
else
  # --disable-api-nodes: no paid cloud nodes, the whole graph runs on the GB10.
  # Unified memory: let ComfyUI keep models resident between shots instead of offloading.
  (cd "$COMFYUI_DIR" && nohup .venv/bin/python main.py --listen 127.0.0.1 --port 8188 \
      --disable-api-nodes --highvram --preview-method none >"$LOG" 2>&1 & echo $! >"$PIDFILE")
  echo "ComfyUI starting (pid $(cat "$PIDFILE")), log: $LOG"
fi

for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:8188/system_stats >/dev/null && { echo "ComfyUI ready on 127.0.0.1:8188"; exit 0; }
  sleep 1
done
echo "ComfyUI did not come up; see $LOG" >&2
exit 1
