#!/usr/bin/env bash
# Zero-to-one installer for Odicto on macOS and Linux.
# Windows users: use install.ps1.
set -euo pipefail

SKIP_OLLAMA=0
WITH_OLLAMA=0
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b-instruct}"
WHISPER_MODEL="${WHISPER_MODEL:-tiny.en}"

for arg in "$@"; do
  case "$arg" in
    -Ollama|--ollama) WITH_OLLAMA=1 ;;
    -SkipOllama|--skip-ollama) SKIP_OLLAMA=1 ;;  # deprecated no-op: skipped by default
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

echo "==> Detecting uv (fast, hash-verified package manager)"
UV=""
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  echo "    uv not found. Installing the standalone uv binary..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh || true
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh || true
  fi
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
  elif [ -x "$HOME/.cargo/bin/uv" ]; then
    UV="$HOME/.cargo/bin/uv"
  elif command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
  fi
fi
if [ -n "$UV" ]; then
  echo "    uv found: $UV"
else
  echo "    Could not install uv; will fall back to pip (slower)."
fi

PY=""
if [ -z "$UV" ]; then
  echo "==> Detecting Python 3.10+ (needed only for the pip fallback)"
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
fi

echo "==> Creating virtual environment"
if [ ! -x ".venv/bin/python" ]; then
  if [ -n "$UV" ]; then
    "$UV" venv .venv
  else
    "$PY" -m venv .venv
  fi
fi
VENV_PY=".venv/bin/python"

echo "==> Installing Python requirements"
if [ -n "$UV" ]; then
  "$UV" pip install --python "$VENV_PY" -r requirements.txt
else
  "$VENV_PY" -m pip install --upgrade pip wheel setuptools
  "$VENV_PY" -m pip install -r requirements.txt
fi

echo "==> Preparing .env"
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "    Copied .env.example -> .env"
else
  echo "    .env already present (left unchanged)"
fi

if [ "$WITH_OLLAMA" = "1" ]; then
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
  echo "    Ollama skipped. Pick Ollama in the setup page later if you want a local LLM; the model downloads on demand."
fi

echo "==> Pre-downloading Whisper model ($WHISPER_MODEL)"
"$VENV_PY" -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER_MODEL', device='cpu', compute_type='int8')"

echo
echo "Install complete."
echo "Next:"
echo "  1. Run:  ./setup.sh   (pick provider + paste key)"
echo "  2. Run:  ./run_debug.sh                     (grant permissions if asked)"
echo "  3. Stop:  .venv/bin/python odicto.py stop"
