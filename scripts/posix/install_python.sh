#!/usr/bin/env bash
# Installer helpers for Odicto (macOS / Linux). Sourced by install.sh; defines functions only.
#
# Same rules as scripts/windows/install_helpers.ps1:
#   * A Python or a .venv is judged by running it, never by checking that a file exists.
#   * Supported versions are 3.10 to 3.12 (numpy<2 has no wheels for 3.13+).
#   * Nothing is deleted. A broken .venv is renamed aside, never removed.
#   * Nothing here reads .env, prompt.txt or any other private file.

# odicto_python_probe EXE -> prints "MAJOR MINOR PATCH"; fails when EXE cannot run.
odicto_python_probe() {
  local out line
  out="$("$1" -I -c 'import sys;print(sys.version_info[0],sys.version_info[1],sys.version_info[2])' 2>/dev/null)" || return 1
  line="$(printf '%s\n' "$out" | tail -n 1)"
  [[ "$line" =~ ^[0-9]+\ [0-9]+\ [0-9]+$ ]] || return 1
  printf '%s\n' "$line"
}

# odicto_python_supported MAJOR MINOR
odicto_python_supported() {
  [ "$1" = 3 ] && [ "$2" -ge 10 ] && [ "$2" -le 12 ]
}

# odicto_is_venv_interpreter EXE: true for python inside a virtual environment.
odicto_is_venv_interpreter() {
  local dir
  dir="$(dirname "$1")"
  [ -f "$dir/pyvenv.cfg" ] || [ -f "$dir/../pyvenv.cfg" ]
}

# odicto_python_candidates [UV]: installed interpreters, one per line. Nothing is downloaded.
odicto_python_candidates() {
  local uv="${1:-}" name path
  for name in python3.12 python3.11 python3.10; do
    path="$(command -v "$name" 2>/dev/null || true)"
    [ -n "$path" ] && printf '%s\n' "$path"
  done
  if [ -n "$uv" ]; then
    "$uv" python list --only-installed --output-format json 2>/dev/null \
      | grep -o '"path": *"[^"]*"' | sed 's/^"path": *"//; s/"$//' || true
  fi
  for name in python3 python; do
    path="$(command -v "$name" 2>/dev/null || true)"
    [ -n "$path" ] && printf '%s\n' "$path"
  done
  return 0
}

# odicto_find_python [UV [CANDIDATE...]]: prints the best installed 3.10-3.12 interpreter
# (highest minor first), or fails when there is none. Every candidate is run.
odicto_find_python() {
  local uv="${1:-}" exe probe major minor best_minor=0 best=""
  shift || true
  local candidates
  if [ "$#" -gt 0 ]; then candidates="$(printf '%s\n' "$@")"; else candidates="$(odicto_python_candidates "$uv")"; fi
  while IFS= read -r exe; do
    [ -n "$exe" ] && [ -x "$exe" ] || continue
    odicto_is_venv_interpreter "$exe" && continue
    probe="$(odicto_python_probe "$exe")" || continue
    read -r major minor _ <<<"$probe"
    odicto_python_supported "$major" "$minor" || continue
    if [ "$minor" -gt "$best_minor" ]; then best_minor="$minor"; best="$exe"; fi
  done <<<"$candidates"
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

# odicto_resolve_python SPEC [UV [CANDIDATE...]]: checks the explicit -Python (a path, or
# 3.10 / 3.11 / 3.12 that is already installed). Prints the interpreter path. A choice that is
# missing, will not run or is unsupported FAILS with a message; it never falls back.
odicto_resolve_python() {
  local spec="$1" uv="${2:-}" exe probe major minor want candidates
  shift 2 || shift $#
  if [[ "$spec" =~ ^3\.([0-9]+)$ ]]; then
    want="${BASH_REMATCH[1]}"
    if ! odicto_python_supported 3 "$want"; then
      echo "-Python $spec is not supported. Odicto needs Python 3.10, 3.11 or 3.12 (numpy<2 has no wheels for 3.13+)." >&2
      return 1
    fi
    if [ "$#" -gt 0 ]; then candidates="$(printf '%s\n' "$@")"; else candidates="$(odicto_python_candidates "$uv")"; fi
    while IFS= read -r exe; do
      [ -n "$exe" ] && [ -x "$exe" ] || continue
      odicto_is_venv_interpreter "$exe" && continue
      probe="$(odicto_python_probe "$exe")" || continue
      read -r major minor _ <<<"$probe"
      if [ "$major" = 3 ] && [ "$minor" = "$want" ]; then printf '%s\n' "$exe"; return 0; fi
    done <<<"$candidates"
    echo "-Python $spec was requested but no installed Python $spec was found. Install it, or pass the full path to its python." >&2
    return 1
  fi
  exe="$spec"
  if [ ! -x "$exe" ]; then
    case "$spec" in */*) exe="" ;; *) exe="$(command -v "$spec" 2>/dev/null || true)" ;; esac
  fi
  if [ -z "$exe" ] || [ ! -x "$exe" ]; then
    echo "-Python '$spec' was not found. Pass the full path to python, or a version such as 3.12." >&2
    return 1
  fi
  if ! probe="$(odicto_python_probe "$exe")"; then
    echo "-Python '$spec' cannot run. Pass a working python." >&2
    return 1
  fi
  read -r major minor patch <<<"$probe"
  if ! odicto_python_supported "$major" "$minor"; then
    echo "-Python '$spec' is Python $major.$minor.$patch. Odicto needs Python 3.10, 3.11 or 3.12 (numpy<2 has no wheels for 3.13+). Pass another interpreter, or omit -Python to let the installer pick one." >&2
    return 1
  fi
  printf '%s\n' "$exe"
}

# odicto_venv_state VENV -> prints "missing", "healthy X.Y.Z", "unsupported X.Y.Z" or "broken REASON".
odicto_venv_state() {
  local venv="$1" probe major minor patch home
  if [ ! -e "$venv" ] && [ ! -L "$venv" ]; then echo "missing"; return 0; fi
  if [ ! -d "$venv" ]; then echo "broken .venv is not a directory"; return 0; fi
  if ! probe="$(odicto_python_probe "$venv/bin/python")"; then
    home="$(sed -n 's/^[[:space:]]*home[[:space:]]*=[[:space:]]*//p' "$venv/pyvenv.cfg" 2>/dev/null | head -n 1)"
    if [ -n "$home" ] && [ ! -e "$home" ]; then
      echo "broken bin/python cannot run; its base Python '$home' (pyvenv.cfg home) no longer exists"
    else
      echo "broken bin/python cannot run"
    fi
    return 0
  fi
  read -r major minor patch <<<"$probe"
  if odicto_python_supported "$major" "$minor"; then
    echo "healthy $major.$minor.$patch"
  else
    echo "unsupported $major.$minor.$patch"
  fi
}

# odicto_process_cwd PID: prints the working folder of PID as a physical path (Linux /proc,
# else lsof), or nothing when it cannot be read (for example another user's process).
odicto_process_cwd() {
  if [ -L "/proc/$1/cwd" ]; then
    readlink "/proc/$1/cwd" 2>/dev/null || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true
  fi
}

# odicto_venv_users ROOT...: lists processes ("PID <pid>: <command>") that run from ROOT/.venv
# or run ROOT/main.py. Pass every known spelling of the install folder: ps shows a command as
# it was typed, so a symlinked folder (or /var against /private/var on macOS) is different
# text. On Linux /proc/<pid>/exe also names each program by its physical path. A relative
# command (the launchers run ".venv/bin/python main.py" from the install folder) names the
# folder only through its working folder, which is read for those commands alone. Fails (so
# the caller refuses) when the process list cannot be read.
odicto_venv_users() {
  local listing pid cmd exe cwd root proc=""
  listing="$(ps -A -o pid= -o command= 2>/dev/null)" || return 2
  if [ -L /proc/self/exe ]; then proc=1; fi
  while read -r pid cmd; do
    exe=""
    cwd=""
    if [ -n "$proc" ]; then exe="$(readlink "/proc/$pid/exe" 2>/dev/null)" || exe=""; fi
    case "$cmd" in
      .venv/*|./.venv/*|*" main.py"|*" main.py "*|*" ./main.py"|*" ./main.py "*)
        cwd="$(odicto_process_cwd "$pid")" || cwd="" ;;
    esac
    for root in "$@"; do
      case "$cmd" in
        "$root/.venv"/*|*" $root/.venv/"*|*" $root/main.py"*|"$root/main.py"*)
          printf 'PID %s: %s\n' "$pid" "$cmd"; continue 2 ;;
      esac
      case "$exe" in
        "$root/.venv"/*) printf 'PID %s: %s\n' "$pid" "$cmd"; continue 2 ;;
      esac
      if [ -n "$cwd" ] && [ "$cwd" = "$root" ]; then
        printf 'PID %s: %s\n' "$pid" "$cmd"; continue 2
      fi
    done
  done <<<"$listing"
  return 0
}

# odicto_venv_move_aside ROOT VENV: renames VENV to ROOT/.venv-backup-<timestamp> (unique) and
# prints the new path. It never deletes. It refuses when VENV is not exactly ROOT/.venv, is a
# symlink, or is in use by a running process (including Odicto) that names the install folder
# by its physical path or by ROOT as given, or that runs .venv/... or main.py relative to it
# (on Linux also any program file inside .venv).
odicto_venv_move_aside() {
  local root given venv users stamp backup n=0 suffix
  root="$(cd -P "$1" 2>/dev/null && pwd)" || { echo "Install folder '$1' does not exist." >&2; return 1; }
  given="$(cd "$1" 2>/dev/null && pwd)" || given="$root"
  venv="$root/.venv"
  if [ "$(cd -P "$(dirname "$2")" 2>/dev/null && pwd)/$(basename "$2")" != "$venv" ]; then
    echo "Refusing to move '$2': it is not the .venv directly inside the install folder." >&2
    return 1
  fi
  if [ -L "$venv" ]; then
    echo "Refusing to move '$venv': it is a symlink. Fix the link yourself, then re-run." >&2
    return 1
  fi
  if [ ! -d "$venv" ]; then
    echo "Refusing to move '$venv': it is not a directory." >&2
    return 1
  fi
  if ! users="$(odicto_venv_users "$root" "$given")"; then
    echo "Cannot tell whether Odicto is running. Quit Odicto from its tray icon (Quit Odicto), then re-run." >&2
    return 1
  fi
  if [ -n "$users" ]; then
    echo "Odicto or another program is using this .venv. Quit Odicto from its tray icon (Quit Odicto), then re-run:" >&2
    printf '%s
' "$users" | head -n 3 | cut -c1-120 | sed 's/^/    /' >&2
    return 1
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  while :; do
    suffix=""; [ "$n" -gt 0 ] && suffix="-$n"
    backup="$root/.venv-backup-$stamp$suffix"
    [ -e "$backup" ] || [ -L "$backup" ] || break
    n=$((n + 1))
  done
  # The real guard: hold the same flock the app takes on dictation.lock while renaming. A
  # running or starting Odicto owns it, so we refuse. The file is opened for append and never
  # truncated, written or deleted. No perl means we cannot prove the app is stopped: refuse.
  command -v perl >/dev/null 2>&1 || { echo "perl is needed to take Odicto's install lock; install it, or quit Odicto and rename .venv yourself." >&2; return 1; }
  perl -e 'use Fcntl qw(:flock); open(my $fh, ">>", $ARGV[0]) or exit 3; flock($fh, LOCK_EX | LOCK_NB) or exit 4; system("mv", $ARGV[1], $ARGV[2]) == 0 or exit 5; exit 0;'     "$root/dictation.lock" "$venv" "$backup"
  case "$?" in
    0) printf '%s
' "$backup" ;;
    4) echo "Odicto's install lock (dictation.lock) is held. Quit Odicto from its tray icon (Quit Odicto), then re-run." >&2; return 1 ;;
    *) echo "Could not rename '$venv' (lock file not writable, or the rename failed)." >&2; return 1 ;;
  esac
}
