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
echo "Started Odicto (PID $!)"
