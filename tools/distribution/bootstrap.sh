#!/usr/bin/env bash
# Run as your desktop user. This installs only after verifying the published archive.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  echo "Run this installer as your desktop user. It uses sudo only for missing Linux system packages." >&2
  exit 1
fi

wsl=0
# The rule of running_in_wsl in app/platforms/preflight.py: the two variables that WSL's init sets.
# Both must agree, or the bootstrap would install for a session that Odicto then refuses.
if [ -n "${WSL_DISTRO_NAME:-}" ] || [ -n "${WSL_INTEROP:-}" ]; then
  wsl=1
fi

case "$(uname -s)" in
  Linux)
    # Odicto refuses a Wayland session when it starts. Say it now, before sudo and the download.
    session="$(printf '%s' "${XDG_SESSION_TYPE:-}" | tr '[:upper:]' '[:lower:]')"
    if [ "$wsl" = 1 ]; then
      # WSLg runs an X server and sets DISPLAY, and Odicto uses it, even when WAYLAND_DISPLAY is also set.
      # Without DISPLAY Odicto cannot start, so stop now, whatever the Wayland variables say.
      if [ -z "${DISPLAY:-}" ]; then
        echo "This Linux in WSL has no display. Odicto needs the WSLg display that current WSL provides." >&2
        echo "In Windows, run: wsl --update, then wsl --shutdown. Open this Linux again and run this installer again." >&2
        echo "Nothing was installed." >&2
        exit 1
      fi
    elif [ "$session" = wayland ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
      echo "This desktop runs a Wayland session. Odicto needs an X11 (Xorg) session; it does not work in Wayland yet." >&2
      echo "If your login screen offers a session with Xorg in its name, choose it, log in, and run this installer again." >&2
      echo "Ubuntu 25.10 and newer have no Xorg session, and Odicto does not support them yet." >&2
      echo "Nothing was installed." >&2
      exit 1
    fi
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-query >/dev/null 2>&1; then
      echo "The managed Linux bootstrap currently supports Ubuntu and Debian." >&2
      echo "On another distribution, install PortAudio, xdotool, xclip and Qt's X11/EGL libraries with your package manager, then use the source installer." >&2
      exit 1
    fi
    packages=(curl ca-certificates libportaudio2 xdotool xclip libegl1 libxkbcommon-x11-0 libxcb-cursor0
      libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0
      libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libx11-xcb1)
    if [ "$wsl" = 1 ]; then
      echo "Odicto in WSL is for testing. Its shortcuts work only while a Linux window has focus."
      echo "It types only into Linux (X11) apps, not into Windows apps. For Windows apps, use the Windows installer."
      # Adds the ALSA "pulse" device, so PortAudio finds the WSLg microphone.
      packages+=(libasound2-plugins)
    fi
    missing=()
    for package in "${packages[@]}"; do
      if ! dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
        missing+=("$package")
      fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
      if ! command -v sudo >/dev/null 2>&1; then
        echo "Missing system packages: ${missing[*]}. Ask an administrator to install them, then rerun as your desktop user." >&2
        exit 1
      fi
      echo "Installing missing native dependencies with sudo: ${missing[*]}"
      # </dev/null: when this script comes through a pipe (curl ... | bash), nothing may read the script.
      # sudo asks for a password on the terminal, not on stdin.
      sudo apt-get update </dev/null
      sudo apt-get install -y "${missing[@]}" </dev/null
    fi
    ;;
  Darwin) ;;
  *) echo "Use bootstrap.ps1 on Windows; this installer supports macOS and Linux." >&2; exit 1 ;;
esac

# The bootstrap never touches the managed home. It keeps its own uv in a folder of the user's cache, and the
# manager copies that uv into <home>/tools/uv after it has made the home (ensure_private_uv in manager.py).
# A uv that is already on PATH is not used, so the version is the one Odicto installs.
# The folder is the user's own cache, not the managed home, so a uv that already sits there may be reused.
case "$(uname -s)" in
  Darwin) uv_dir="$HOME/Library/Caches/Odicto-bootstrap/uv" ;;
  *)
    cache="${XDG_CACHE_HOME:-}"
    case "$cache" in /*) ;; *) cache="$HOME/.cache" ;; esac
    uv_dir="$cache/odicto-bootstrap/uv"
    ;;
esac
# Options of the Astral installer (https://astral.sh/uv/install.sh; read 2026-11-16):
#   UV_INSTALL_DIR         the folder that gets uv and uvx (flat); it wins over every other folder variable.
#   UV_UNMANAGED_INSTALL   the same folder; also turns off the PATH edit, the env script and the updater
#                          receipt, so nothing outside that folder is written.
#   UV_NO_MODIFY_PATH      no edit of ~/.profile, ~/.bashrc and the like; the second guard, if the first changes.
#   INSTALLER_NO_MODIFY_PATH  the same, under the name of the older installers.
#   UV_DISABLE_UPDATE      no uv updater and no receipt in ~/.config/uv.
# CARGO_DIST_FORCE_INSTALL_DIR is cleared because it would rank after UV_INSTALL_DIR but above the default.
UV="$uv_dir/uv"
if ! { [ -x "$UV" ] && "$UV" --version >/dev/null 2>&1; }; then
  mkdir -p "$uv_dir"
  installer="$(mktemp -t odicto-uv.XXXXXX)"
  trap 'rm -f -- "$installer"' EXIT
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o "$installer"
  env -u CARGO_DIST_FORCE_INSTALL_DIR UV_INSTALL_DIR="$uv_dir" UV_UNMANAGED_INSTALL="$uv_dir" \
    UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 UV_DISABLE_UPDATE=1 sh "$installer" </dev/null
  if [ ! -x "$UV" ]; then
    echo "The uv tool was not installed in $uv_dir. Check the internet connection, then run this installer again." >&2
    exit 1
  fi
fi

# An older manager looks for uv on PATH and bakes that path into its launchers; a newer one copies
# ODICTO_BOOTSTRAP_UV into the managed home. Both are served from here.
export ODICTO_BOOTSTRAP_UV="$UV"
export PATH="$uv_dir:$PATH"
"$UV" run --python 3.12 --no-project --no-config python - "$@" <<'PY'
import hashlib, io, json, stat, sys, urllib.error, urllib.parse, urllib.request, zipfile
repo = 'Gargeya-Grey/Odicto'
hosts = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
         'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}
def trusted(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == 'https' and parsed.hostname in hosts and parsed.port in (None, 443) and not parsed.username and not parsed.password
class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, url):
        if not trusted(url): raise SystemExit('Untrusted download redirect')
        return super().redirect_request(req, fp, code, msg, headers, url)
def get(url, maximum, soft=False):
    if not trusted(url): raise SystemExit('Untrusted download URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'Odicto-bootstrap/1'})
    try:
        with urllib.request.build_opener(Redirect()).open(request, timeout=30) as response:
            data = response.read(maximum + 1)
    except urllib.error.HTTPError as error:
        if soft: return None
        if error.code == 429 or (error.code == 403 and (error.headers or {}).get('X-RateLimit-Remaining') == '0'):
            raise SystemExit('GitHub refused the download for now: too many requests from this network. Wait one hour, then run the installer again. Odicto was not installed.')
        raise SystemExit(f'The download failed: GitHub answered HTTP {error.code}. Run the installer again later. Odicto was not installed.')
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit(f'The download failed: no connection to GitHub ({getattr(error, "reason", error)}). Check the internet connection, then run the installer again. Odicto was not installed.')
    if len(data) > maximum:
        if soft: return None
        raise SystemExit('Download exceeds size limit')
    return data
import re
def pick(data):
    try: metadata = json.loads(data)
    except ValueError: metadata = None
    tag = metadata.get('tag_name') if isinstance(metadata, dict) else None
    if not isinstance(tag, str) or not re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag) or metadata.get('draft') or metadata.get('prerelease'):
        return None, 'A stable versioned release is required'
    assets = metadata.get('assets') if isinstance(metadata.get('assets'), list) else []
    for asset in (f'Odicto-{tag}.zip', f'Odicto-{tag}.zip.sha256'):
        matches = [a for a in assets if isinstance(a, dict) and a.get('name') == asset]
        if len(matches) != 1 or matches[0].get('browser_download_url') != f'https://github.com/{repo}/releases/download/{tag}/' + asset:
            return None, 'Release archive or checksum is missing'
    return tag, ''
# The release manifest has no hourly limit; the API answers only when the manifest gives an HTTP error or is invalid.
data = get(f'https://github.com/{repo}/releases/latest/download/odicto-release.json', 2*1024*1024, True)
tag, problem = pick(data) if data is not None else (None, '')
if tag is None:
    tag, problem = pick(get(f'https://api.github.com/repos/{repo}/releases/latest', 2*1024*1024))
    if tag is None: raise SystemExit(problem)
name = f'Odicto-{tag}.zip'
prefix = f'https://github.com/{repo}/releases/download/{tag}/'
checksum = get(prefix + name + '.sha256', 1024).decode('ascii').strip().split()
if len(checksum) != 2 or not re.fullmatch('[0-9a-fA-F]{64}', checksum[0]) or checksum[1] != name:
    raise SystemExit('Invalid release checksum')
archive = get(prefix + name, 128*1024*1024)
if hashlib.sha256(archive).hexdigest() != checksum[0].lower(): raise SystemExit('Release checksum mismatch')
with zipfile.ZipFile(io.BytesIO(archive)) as release:
    matches = [i for i in release.infolist() if i.filename == 'Odicto/tools/distribution/manager.py']
    if len(matches) != 1 or matches[0].file_size > 512*1024 or stat.S_ISLNK(matches[0].external_attr >> 16):
        raise SystemExit('This release does not include the managed installer; use the release installation instructions')
    source = release.read(matches[0])
import types
module = types.ModuleType('odicto_verified_bootstrap')
sys.modules[module.__name__] = module
namespace = module.__dict__
namespace['__file__'] = '<verified-release-manager>'
exec(compile(source, '<verified-release-manager>', 'exec'), namespace)
raise SystemExit(namespace['main']([*sys.argv[1:], 'install']))
PY
