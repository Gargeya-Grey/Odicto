#!/usr/bin/env bash
# Open the local Odicto setup page (provider, model, API keys).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if [ ! -x ".venv/bin/python" ]; then
  echo "ERROR: .venv/bin/python not found. Run install.sh first." >&2
  exit 1
fi

echo "Opening the Odicto setup page in your browser..."
echo "Press Ctrl+C here when you are done."
exec .venv/bin/python odicto.py setup
