"""Build install-odicto.sh, the one-line installer for macOS and Linux.

The file is bootstrap.sh unchanged, turned into a shell function, plus the steps after the
install: start at sign-in (new installs only), start Odicto, check that it is ready, open
Setup. It mirrors build_easy_installer.py, the Windows one-file installer. Usage:
    python tools/distribution/build_posix_installer.py out/install-odicto.sh

A user pastes MAC_LINE or LINUX_LINE. Bash runs a script from a pipe while it reads it. So
every line of the file is a function, and the one call to the main function is the last line:
a download that breaks off halfway has no call and runs nothing.
"""
from pathlib import Path
import sys

BOOTSTRAP = Path(__file__).resolve().with_name("bootstrap.sh")
URL = "https://github.com/Gargeya-Grey/Odicto/releases/latest/download/install-odicto.sh"
# The lines that the docs give. Ubuntu's desktop has wget but no curl (the 24.04.3 and 26.04 desktop
# manifests, read 16 Nov 2026); the bootstrap installs curl with the other packages. macOS has curl.
MAC_LINE = f"curl -fsSL {URL} | bash"
LINUX_LINE = f"wget -nv -O- {URL} | bash"

HEADER = f"""#!/usr/bin/env bash
# Odicto installer for macOS and Linux. Built by tools/distribution/build_posix_installer.py.
# Paste one line in a terminal. On a Mac:
#   {MAC_LINE}
# On Linux:
#   {LINUX_LINE}
# Every line below is inside a function and the call is the last line. Bash runs a script from a
# pipe while it reads it, so a download that stops halfway has no call and runs nothing.
"""

# The body of bootstrap.sh goes here, line for line. Its `exit` codes stay: the main function
# runs it in a subshell, so an exit ends only that subshell, and the main function exits with
# the same code after it says what happened.
FUNCTION_OPEN = "odicto_bootstrap() {\n"
FUNCTION_CLOSE = "}\n"

# bash 3.2 (the bash on a Mac) must read this: no arrays that can be empty, no mapfile, no ${x,,}.
# The parent shell sets no `set -e`: each step is checked by hand, as the Windows installer does.
AFTER = r"""
odicto_say() { printf '%s\n' "$@"; }

odicto_step() { odicto_say '' "Step $1 of 3: $2"; }

# The same folder as default_home() in tools/distribution/manager.py, or the --home argument.
odicto_home_from() {
  local home
  case "$(uname -s)" in
    Darwin) home="${HOME:-}/Library/Application Support/Odicto" ;;
    *) home="${XDG_DATA_HOME:-${HOME:-}/.local/share}/odicto" ;;
  esac
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --home) if [ "$#" -gt 1 ]; then home="$2"; shift; fi ;;
      --home=*) home="${1#--home=}" ;;
    esac
    shift
  done
  printf '%s\n' "$home"
}

# The start-at-sign-in entry of a source copy (app/odicto.py `autostart`), not the managed one.
odicto_source_entry() {
  local entry
  case "$(uname -s)" in
    Darwin) entry="${HOME:-}/Library/LaunchAgents/com.odicto.plist" ;;
    *) entry="${HOME:-}/.config/autostart/odicto.desktop" ;;
  esac
  if [ -e "$entry" ] || [ -L "$entry" ]; then printf '%s\n' "$entry"; return 0; fi
  return 1
}

odicto_process_cwd() {
  if [ -L "/proc/$1/cwd" ]; then
    readlink "/proc/$1/cwd" 2>/dev/null
  elif command -v lsof >/dev/null 2>&1; then
    lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
  fi
}

# odicto_owner_script PID SCRIPT INSIDE: prints SCRIPT, made absolute with the process's folder,
# and returns 0 when it is a main.py with an odicto.py beside it, outside the folder INSIDE.
odicto_owner_script() {
  local script="$2" dir cwd
  case "$script" in
    /*) ;;
    *)
      cwd="$(odicto_process_cwd "$1")" || cwd=""
      [ -n "$cwd" ] || return 1
      script="$cwd/${script#./}" ;;
  esac
  dir="${script%/*}"
  [ -f "$dir/odicto.py" ] || return 1
  dir="$(cd -P "$dir" 2>/dev/null && pwd)" || return 1
  case "$dir" in "$3"|"$3"/*) return 1 ;; esac
  printf '%s\n' "$script"
}

# odicto_source_owner HOME: prints the main.py of a running Odicto outside HOME and returns 0.
# A copy older than 1.5.0 takes no session hook lock, so a start entry is not the only sign.
# A process counts when a word of its command names a Python (python3, python3.12, Python, pypy)
# and a later run of words is a main.py with an odicto.py beside it. Every run is tried, so options
# before the script (-W ignore, -X utf8), launchers (env, uv run) and a space in the path do not
# hide it. Best effort: ps joins the words with one space, so a path with two spaces in a row is
# missed, and a machine with no ps shows no process.
odicto_source_owner() {
  local listing pid cmd inside words count python first last script
  listing="$(ps -axww -o pid= -o command= 2>/dev/null)" || return 1
  inside="$(cd -P "$1" 2>/dev/null && pwd)" || inside="$1"
  set -f
  while read -r pid cmd; do
    words=($cmd)
    count=${#words[@]}
    python=0
    while [ "$python" -lt "$count" ]; do
      case "${words[$python]##*/}" in
        [Pp]ython|[Pp]ython[0-9]*|pypy|pypy[0-9]*) break ;;
      esac
      python=$((python + 1))
    done
    first=$((python + 1))
    while [ "$first" -lt "$count" ]; do
      script=""
      last=$first
      while [ "$last" -lt "$count" ]; do
        script="${script:+$script }${words[$last]}"
        case "${words[$last]}" in
          main.py|*/main.py)
            if odicto_owner_script "$pid" "$script" "$inside"; then set +f; return 0; fi ;;
        esac
        last=$((last + 1))
      done
      first=$((first + 1))
    done
  done <<<"$listing"
  set +f
  return 1
}

odicto_install_main() {
  local home odicto installed=0 migrated=0 chosen=0 status entry owner failed=" " state=unconfirmed code
  home="$(odicto_home_from "$@")"
  odicto="$home/bin/odicto"
  if [ -e "$odicto" ]; then installed=1; fi
  if [ -f "$home/migration.json" ] \
      && grep -Eq '"state"[[:space:]]*:[[:space:]]*"complete"' "$home/migration.json"; then migrated=1; fi
  # A new install turns on start at sign-in. An existing one keeps its choice (on or off), except
  # an unfinished attempt: a pending receipt is tried again.
  if [ "$migrated" = 1 ] || [ "$installed" = 1 ]; then chosen=1; fi
  if [ "$migrated" = 0 ] && [ -f "$home/autostart.json" ] \
      && grep -Eq '"pending"[[:space:]]*:[[:space:]]*true' "$home/autostart.json"; then chosen=0; fi

  odicto_say '' '  Odicto installer' \
    '  This terminal installs Odicto, free voice typing for macOS and Linux.' \
    '  It takes a few minutes. You do not need to type anything here.' \
    '  Technical text scrolls by while it works. That is normal.'
  if [ "$(uname -s)" = Linux ]; then
    odicto_say '  Linux can ask for your password once. It is used only to install system packages.'
  fi
  odicto_step 1 'Downloading and installing Odicto (the longest step)'
  ( odicto_bootstrap "$@" )
  status=$?
  if [ "$status" -ne 0 ]; then
    odicto_say '' 'Odicto was not installed. The lines above say why.'
    if [ "$installed" = 1 ]; then odicto_say "If Odicto is open, stop it first: \"$odicto\" stop"; fi
    odicto_say 'After you fix that, paste the install line again.'
    exit "$status"
  fi
  if [ ! -x "$odicto" ]; then
    odicto_say "Odicto was not installed: $odicto is missing."
    exit 1
  fi

  # A source copy's start-at-sign-in entry, or a source copy that runs now: two copies must never
  # run together. A migration refuses to run while a source entry exists, so an entry found after a
  # finished migration belongs to another copy, which may be older than 1.5.0 and hook beside this one.
  entry="$(odicto_source_entry)" || entry=""
  owner="$(odicto_source_owner "$home")" || owner=""
  if [ -n "$entry" ] || [ -n "$owner" ]; then
    odicto_say '' 'Odicto is installed, but another copy of Odicto is on this computer.'
    if [ -n "$entry" ]; then odicto_say "That copy starts when you sign in: $entry"; fi
    if [ -n "$owner" ]; then odicto_say "That copy is running now: $owner"; fi
    odicto_say 'Two copies must never run together, so the new copy was not started.' \
      'Move your settings to the new copy first:' \
      '  https://github.com/Gargeya-Grey/Odicto#moving-from-a-source-checkout'
    exit 0
  fi

  odicto_step 2 'Starting Odicto'
  if [ "$chosen" = 0 ]; then
    if ! "$odicto" autostart </dev/null; then failed="${failed}autostart "; fi
  fi
  if ! "$odicto" start </dev/null; then
    odicto_say '' 'Odicto did not start. The lines above say why.'
    if [ "$(uname -s)" = Darwin ]; then
      odicto_say 'On a Mac, a missing permission is the usual cause. Open System Settings, Privacy & Security,' \
        'and turn on Accessibility and Input Monitoring for Terminal.'
    fi
    odicto_say 'After you fix that, paste the install line again. It starts Odicto and opens Setup.'
    exit 1
  fi
  # Odicto can run and still wait for a microphone or its speech model. The start said so above;
  # this asks again, with its output discarded, so the summary does not claim more than is true.
  # Any answer other than ready (0) or waiting (20, 21) leaves readiness unconfirmed.
  "$odicto" wait-ready --timeout 5 --report-waiting </dev/null >/dev/null 2>&1
  code=$?
  case "$code" in 0) state=ready ;; 20|21) state=waiting ;; esac
  odicto_step 3 'Opening Odicto Setup in your browser'
  if ! "$odicto" setup </dev/null; then failed="${failed}setup "; fi

  odicto_say '' '  =========================================='
  if [ "$state" = waiting ]; then
    odicto_say '  Odicto is running. It is not ready to dictate yet.'
    if [ "$failed" != " " ]; then odicto_say '  Some steps did not finish.'; fi
  elif [ "$state" != ready ]; then
    odicto_say '  Odicto started. It did not confirm that it is ready.'
    if [ "$failed" != " " ]; then odicto_say '  Some steps did not finish.'; fi
  elif [ "$failed" != " " ]; then
    odicto_say '  Odicto is running. Some steps did not finish.'
  else
    odicto_say '  Odicto is ready.'
  fi
  odicto_say '  ==========================================' ''
  if [ "$state" = waiting ]; then
    odicto_say 'The lines above say what it waits for. It gets ready by itself.' '' 'When it is ready:'
  else
    if [ "$state" != ready ]; then
      odicto_say 'If dictation does not work, run this line to start Odicto again:' "  \"$odicto\" start" ''
    fi
    odicto_say 'Try it now:'
  fi
  odicto_say '  1. Click in any text box, for example a new email.' \
    '  2. Press Ctrl and the ` key together (the key under Esc, left of 1).' \
    '  3. Speak. Then press Ctrl and ` again. Your words appear.' \
    '  The first time can take a little longer.' '' \
    'Odicto runs in the background. To stop it, run this line:' "  \"$odicto\" stop"
  case "$failed" in
    *" autostart "*) odicto_say 'It does not start by itself when you sign in.' ;;
    *) if [ "$chosen" = 0 ]; then odicto_say 'It starts by itself when you sign in.'; fi ;;
  esac
  case "$failed" in
    *" setup "*) odicto_say 'Odicto Setup did not open. Open it with this line:' "  \"$odicto\" setup" ;;
    *) odicto_say 'Your browser shows Odicto Setup. Change the microphone or the keys there.' ;;
  esac
  if [ "$failed" != " " ]; then exit 1; fi
  exit 0
}

odicto_install_main "$@"
"""


def build(bootstrap: str) -> bytes:
    lines = bootstrap.replace("\r\n", "\n").split("\n")
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    body = "\n".join(lines).rstrip("\n") + "\n"
    text = HEADER + "\n" + FUNCTION_OPEN + body + FUNCTION_CLOSE + AFTER
    return text.encode("ascii")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    Path(argv[0]).write_bytes(build(BOOTSTRAP.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
