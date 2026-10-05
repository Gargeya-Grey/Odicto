#!/usr/bin/env bash
# Start Odicto in the background (macOS/Linux).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

VENV_PY=".venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: .venv/bin/python not found. Run install.sh first." >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: .env not found. Run install.sh or copy .env.example to .env." >&2
  exit 1
fi

# A crash can leave dictation.pid behind. Remove it when its process is gone,
# so a stale file can never look like a running app.
if [ -f dictation.pid ]; then
  OLD_PID="$(tr -dc '0-9' < dictation.pid || true)"
  if [ -z "$OLD_PID" ] || ! kill -0 "$OLD_PID" 2>/dev/null; then
    rm -f dictation.pid
  fi
fi

# An ordinary start must leave an existing owner alone. Use odicto.py stop
# explicitly before start when a restart is intended.
nohup "$VENV_PY" main.py >/dev/null 2>&1 &
PID=$!

# Confirm a fresh owner heartbeat and microphone callbacks, not just a PID file.
# Same 30 s timeout as scripts/windows/start_dictation.bat.
if "$VENV_PY" odicto.py wait-ready --timeout 30; then
  exit 0
fi
if ! kill -0 "$PID" 2>/dev/null; then
  echo "FAILED to start - process exited early. Check dictation.log and .env." >&2
fi
exit 1
