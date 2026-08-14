#!/usr/bin/env bash
# Start Odicto in the background (macOS/Linux).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if [ ! -x ".venv/bin/python" ]; then
  echo "ERROR: .venv/bin/python not found. Run install.sh first." >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: .env not found. Run install.sh or copy .env.example to .env." >&2
  exit 1
fi

.venv/bin/python odicto.py stop >/dev/null 2>&1 || true
nohup .venv/bin/python main.py >/dev/null 2>&1 &
PID=$!

# Wait up to 30s for dictation.pid (cold starts import PySide6/Whisper slowly).
# If the app is still alive but booting, report in-progress instead of failing.
DEADLINE=$((SECONDS + 30))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  if [ -f dictation.pid ]; then
    echo "Started Odicto (PID $PID, dictation.pid present)"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "FAILED to start - process exited early. Check dictation.log and .env." >&2
    exit 1
  fi
  sleep 0.5
done
echo "Started - boot still in progress (PID $PID). The HUD appears when Whisper is ready."
