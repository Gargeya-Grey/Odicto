#!/usr/bin/env bash
# Zero-to-one installer for Odicto on macOS and Linux.
# Windows users: use install.ps1.
set -euo pipefail

SKIP_OLLAMA=0
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b-instruct}"
WHISPER_MODEL="${WHISPER_MODEL:-tiny.en}"

for arg in "$@"; do
  case "$arg" in
    -SkipOllama|--skip-ollama) SKIP_OLLAMA=1 ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM=macos ;;
  Linux) PLATFORM=linux ;;
  *) echo "Unsupported OS: $OS" >&2; exit 2 ;;
esac

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> Detecting Python 3.10+"
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "    Python 3.10+ not found."
  if [ "$PLATFORM" = macos ] && command -v brew >/dev/null 2>&1; then
    echo "    Installing Python via Homebrew..."
    brew install python@3.12
    PY="$(brew --prefix python@3.12)/bin/python3"
  else
    echo "    Install Python 3.10+ and re-run this script." >&2
    exit 1
  fi
fi
echo "    Using $PY"

echo "==> Creating virtual environment"
if [ ! -x ".venv/bin/python" ]; then
  "$PY" -m venv .venv
fi
VENV_PY=".venv/bin/python"

echo "==> Upgrading pip / wheel"
"$VENV_PY" -m pip install --upgrade pip wheel setuptools

echo "==> Installing Python requirements"
"$VENV_PY" -m pip install -r requirements.txt

echo "==> Preparing .env"
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "    Copied .env.example -> .env"
else
  echo "    .env already present (left unchanged)"
fi

if [ "$SKIP_OLLAMA" = "0" ]; then
  echo "==> Checking Ollama"
  if ! command -v ollama >/dev/null 2>&1; then
    if [ "$PLATFORM" = macos ] && command -v brew >/dev/null 2>&1; then
      brew install ollama
    else
      echo "    Install Ollama from https://ollama.com/download, then re-run." >&2
      exit 1
    fi
  fi
  echo "==> Pulling LLM model: $OLLAMA_MODEL"
  ollama pull "$OLLAMA_MODEL" || true
else
  echo "    Skipping Ollama (-SkipOllama). Raw dictation still works."
fi

echo "==> Pre-downloading Whisper model ($WHISPER_MODEL)"
"$VENV_PY" -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER_MODEL', device='cpu', compute_type='int8')"

echo
echo "Install complete."
echo "Next:"
echo "  1. Run:  .venv/bin/python odicto.py setup   (pick provider + paste key)"
echo "  2. Run:  ./run_debug.sh                     (grant permissions if asked)"
echo "  3. Stop:  .venv/bin/python odicto.py stop"
