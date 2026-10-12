#!/usr/bin/env bash
# Zero-to-one installer for Odicto on macOS and Linux.
# Windows users: use install.ps1.
#
# Flags: -Ollama   -Python <path|3.10|3.11|3.12>   -NoUv   -VenvOnly
# A .venv that cannot run (copied from another machine, base Python removed) is renamed to
# .venv-backup-<timestamp> and rebuilt. Nothing is deleted; .env and prompt.txt are untouched.
set -euo pipefail

SKIP_OLLAMA=0
WITH_OLLAMA=0
PYTHON_SPEC=""
NO_UV=0
VENV_ONLY=0
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b-instruct}"
WHISPER_MODEL="${WHISPER_MODEL:-tiny.en}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    -Ollama|--ollama) WITH_OLLAMA=1 ;;
    -SkipOllama|--skip-ollama) SKIP_OLLAMA=1 ;;  # deprecated no-op: skipped by default
    -Python|--python)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then echo "$1 needs a value: a path to python, or 3.10 / 3.11 / 3.12" >&2; exit 2; fi
      PYTHON_SPEC="$2"; shift ;;
    -NoUv|--no-uv) NO_UV=1 ;;
    -VenvOnly|--venv-only) VENV_ONLY=1 ;;
    *) echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM=macos ;;
  Linux) PLATFORM=linux ;;
  *) echo "Unsupported OS: $OS" >&2; exit 2 ;;
esac

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
# shellcheck source=scripts/posix/install_python.sh
. "$REPO_DIR/scripts/posix/install_python.sh"

# An explicit -Python is checked first, before anything is installed. A bad choice stops
# here; it never falls back to another interpreter.
PY=""
if [ -n "$PYTHON_SPEC" ]; then
  echo "==> Checking -Python $PYTHON_SPEC"
  EARLY_UV=""
  if [ "$NO_UV" = "0" ] && command -v uv >/dev/null 2>&1; then EARLY_UV="$(command -v uv)"; fi
  PY="$(odicto_resolve_python "$PYTHON_SPEC" "$EARLY_UV")" || exit 1
  echo "    Using $PY"
fi

echo "==> Detecting uv (fast, hash-verified package manager)"
UV=""
if [ "$NO_UV" = "1" ]; then
  echo "    uv skipped (-NoUv); using pip"
elif command -v uv >/dev/null 2>&1; then
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
elif [ "$NO_UV" = "0" ]; then
  echo "    Could not install uv; will fall back to pip (slower)."
fi

if [ "$(id -u)" = "0" ]; then
  echo "    Note: do not run this installer with sudo. It installs into your user's .venv;"
  echo "          it only prints the system-package commands for you to run."
fi

# A venv that works needs no base Python found or installed, so it is judged first, by running it.
VENV_DIR="$REPO_DIR/.venv"
VENV_STATE="$(odicto_venv_state "$VENV_DIR")"

# Reuse an installed Python 3.10-3.12 (numpy<2 has no wheels for 3.13+). Nothing is downloaded
# when one exists; uv fetches 3.12 only when none is installed.
if [ -z "$PY" ] && [ "${VENV_STATE%% *}" != "healthy" ]; then
  echo "==> Locating an installed Python 3.10-3.12"
  if PY="$(odicto_find_python "$UV")"; then
    echo "    Using $PY"
  elif [ -n "$UV" ]; then
    PY=""
    echo "    None installed; uv will fetch Python 3.12"
  else
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
      echo "    Install Python 3.12 (or 3.11 / 3.10) and re-run this script, or pass -Python <path>." >&2
      exit 1
    fi
  fi
fi

echo "==> Checking virtual environment (.venv)"
case "$VENV_STATE" in
  healthy*) echo "    .venv works (Python ${VENV_STATE#healthy }); keeping it" ;;
  *)
    if [ "$VENV_STATE" != "missing" ]; then
      echo "    .venv cannot be used: ${VENV_STATE#* }"
      BACKUP="$(odicto_venv_move_aside "$REPO_DIR" "$VENV_DIR")" || exit 1
      echo "    Moved the old .venv to $BACKUP (kept, not deleted; .env and prompt.txt were not touched)."
    fi
    if [ -n "$UV" ]; then
      if [ -n "$PY" ]; then
        "$UV" venv --python "$PY" --no-python-downloads "$VENV_DIR"
      else
        "$UV" venv --python 3.12 "$VENV_DIR"
      fi
    else
      "$PY" -m venv "$VENV_DIR"
    fi
    case "$(odicto_venv_state "$VENV_DIR")" in
      healthy*) echo "    Created .venv" ;;
      *) echo "The new .venv does not work. The old one, if any, was kept beside it." >&2; exit 1 ;;
    esac
    ;;
esac
VENV_PY=".venv/bin/python"

if [ "$VENV_ONLY" = "1" ]; then
  echo "    -VenvOnly: stopping after the virtual environment"
  exit 0
fi

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
