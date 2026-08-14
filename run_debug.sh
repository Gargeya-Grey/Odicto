#!/usr/bin/env bash
# Run Odicto in the foreground with console logs (macOS/Linux).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if [ -x ".venv/bin/python" ]; then
  .venv/bin/python odicto.py stop >/dev/null 2>&1 || true
  echo "Running Odicto in DEBUG mode (Ctrl+C to stop)."
  exec .venv/bin/python main.py
else
  echo "ERROR: .venv/bin/python not found. Run install.sh first." >&2
  exit 1
fi
