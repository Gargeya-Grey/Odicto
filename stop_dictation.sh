#!/usr/bin/env bash
# Stop all Odicto instances (macOS/Linux).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if [ -x ".venv/bin/python" ]; then
  .venv/bin/python odicto.py stop
else
  python3 odicto.py stop
fi
