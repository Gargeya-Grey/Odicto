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

if [ "$(id -u)" = "0" ]; then
  echo "    Note: do not run this installer with sudo. It installs into your user's .venv;"
  echo "          it only prints the system-package commands for you to run."
fi

PY=""
if [ -z "$UV" ]; then
  # numpy<2 has no wheels for Python 3.13+, so only 3.10 to 3.12 are accepted.
  echo "==> Detecting Python 3.10-3.12 (needed only for the pip fallback)"
  for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
      fi
    fi
  done

  if [ -z "$PY" ]; then
    echo "    No Python 3.10, 3.11 or 3.12 found."
    for candidate in python3 python; do
      if command -v "$candidate" >/dev/null 2>&1; then
        echo "    Found $candidate = $("$candidate" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo unknown)"
        echo "    Python 3.13+ is not supported (no numpy<2 wheels); Python below 3.10 is too old."
        break
      fi
    done
    if [ "$PLATFORM" = macos ] && command -v brew >/dev/null 2>&1; then
      echo "    Installing Python via Homebrew..."
      brew install python@3.12
      PY="$(brew --prefix python@3.12)/bin/python3.12"
    else
      echo "    Install Python 3.12 (or 3.11 / 3.10) and re-run this script." >&2
      exit 1
    fi
  fi
  echo "    Using $PY"
fi

echo "==> Creating virtual environment"
if [ ! -x ".venv/bin/python" ]; then
  if [ -n "$UV" ]; then
    "$UV" venv --python 3.12 .venv
  else
    "$PY" -m venv .venv
  fi
fi
VENV_PY=".venv/bin/python"

if [ "$PLATFORM" = linux ]; then
  echo "==> Checking Linux system packages"
  PM=""
  for candidate in apt-get dnf pacman zypper; do
    if command -v "$candidate" >/dev/null 2>&1; then PM="$candidate"; break; fi
  done
  MISSING=""
  command -v xdotool >/dev/null 2>&1 || MISSING="$MISSING xdotool"
  if ! command -v xclip >/dev/null 2>&1 && ! command -v xsel >/dev/null 2>&1 && ! command -v wl-copy >/dev/null 2>&1; then
    MISSING="$MISSING clipboard"
  fi
  if ! "$VENV_PY" -c 'import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("portaudio") else 1)' 2>/dev/null; then
    MISSING="$MISSING portaudio"
  fi
  if [ -z "$MISSING" ]; then
    echo "    Required system packages look present."
  else
    echo "    Missing:$MISSING"
    case "$PM" in
      apt-get) echo "    Run:  sudo apt-get install -y libportaudio2 xclip wl-clipboard xdotool libegl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0" ;;
      dnf) echo "    Run:  sudo dnf install -y portaudio xclip wl-clipboard xdotool mesa-libEGL libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil" ;;
      pacman) echo "    Run:  sudo pacman -S --needed portaudio xclip wl-clipboard xdotool libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil" ;;
      zypper) echo "    Run:  sudo zypper install portaudio xclip wl-clipboard xdotool libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0" ;;
      *) echo "    Install PortAudio, xclip (or wl-clipboard), xdotool and the Qt xcb libraries (libxcb-cursor0) with your package manager." ;;
    esac
    echo "    Continuing the install; run the command above before starting Odicto."
  fi
fi


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

echo "==> Checking the runtime environment"
PROBLEMS="$("$VENV_PY" -c "
import sys
sys.path.insert(0, 'app')
from platforms.preflight import environment_problems
for p in environment_problems():
    print('    [%s] %s: %s' % (p.severity, p.code, p.message))
" 2>&1 || true)"
if [ -n "$PROBLEMS" ]; then
  echo "$PROBLEMS"
  echo "    Fix the items above, then run: .venv/bin/python odicto.py status"
else
  echo "    No known problems."
fi

echo
echo "Install complete."
echo "Next:"
echo "  1. Run:  bash scripts/posix/setup.sh   (pick provider + paste key)"
echo "  2. Run:  bash scripts/posix/run_debug.sh                     (grant permissions if asked)"
echo "  3. Stop:  .venv/bin/python odicto.py stop"
