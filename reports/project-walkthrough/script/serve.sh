#!/usr/bin/env bash
# Serve the afterstage walkthrough site locally.
#
#   reports/project-walkthrough/script/serve.sh            # http://127.0.0.1:8642
#   PORT=9000 reports/project-walkthrough/script/serve.sh
#   reports/project-walkthrough/script/serve.sh --rebuild  # regenerate site/data from the code first
#   reports/project-walkthrough/script/serve.sh --open     # also open a browser
#
# Needs only python3. --rebuild needs the repo's .venv (pip install -e ".[dev]").
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE="$HERE/../site"
REPO="$(cd "$HERE/../../.." && pwd)"
PORT="${PORT:-8642}"
HOST="${HOST:-127.0.0.1}"
OPEN=0

for arg in "$@"; do
  case "$arg" in
    --rebuild)
      PY="$REPO/.venv/bin/python"
      [[ -x "$PY" ]] || { echo "no $PY — create the venv first (see README)" >&2; exit 1; }
      "$PY" "$HERE/build_data.py"
      ;;
    --open) OPEN=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[[ -f "$SITE/data/project.json" ]] || { echo "missing site/data/project.json — run with --rebuild" >&2; exit 1; }

if python3 -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('$HOST', $PORT)) != 0)"; then
  echo "port $PORT is already in use — try: PORT=8700 $0" >&2
  exit 1
fi

URL="http://$HOST:$PORT/"
echo "afterstage walkthrough → $URL  (Ctrl-C to stop)"
if [[ "$OPEN" == "1" ]]; then
  ( sleep 1; command -v open >/dev/null && open "$URL" || xdg-open "$URL" >/dev/null 2>&1 || true ) &
fi
exec python3 -m http.server "$PORT" --bind "$HOST" --directory "$SITE"
