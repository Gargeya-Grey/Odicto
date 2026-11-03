#!/usr/bin/env bash
# Run Odicto in the foreground with console logs (macOS/Linux).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"
if [ -e .odicto-migration.json ] || [ -L .odicto-migration.json ]; then
  if [ ! -x .venv/bin/python ]; then
    echo "This source installation was migrated. Use the installed odicto command. Source Python is missing." >&2
    exit 1
  fi
  exec .venv/bin/python odicto.py restart
fi

if [ -x ".venv/bin/python" ]; then
  .venv/bin/python odicto.py stop >/dev/null 2>&1 || true
  echo "Running Odicto in DEBUG mode (Ctrl+C to stop)."
  exec .venv/bin/python main.py
else
  echo "ERROR: .venv/bin/python not found. Run install.sh first." >&2
  exit 1
fi
