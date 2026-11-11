#!/usr/bin/env bash
# Run as your desktop user. This installs only after verifying the published archive.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  echo "Run this installer as your desktop user. It uses sudo only for missing Linux system packages." >&2
  exit 1
fi

case "$(uname -s)" in
  Linux)
    # Odicto refuses a Wayland session when it starts. Say it now, before sudo and the download.
    session="$(printf '%s' "${XDG_SESSION_TYPE:-}" | tr '[:upper:]' '[:lower:]')"
    if [ "$session" = wayland ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
      echo "This desktop runs a Wayland session. Odicto needs an X11 (Xorg) session; it does not work in Wayland yet." >&2
      echo "Log out. On the login screen, choose a session with Xorg in its name, log in, and run this installer again." >&2
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
      sudo apt-get update
      sudo apt-get install -y "${missing[@]}"
    fi
    ;;
  Darwin) ;;
  *) echo "Use bootstrap.ps1 on Windows; this installer supports macOS and Linux." >&2; exit 1 ;;
esac

if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  installer="$(mktemp -t odicto-uv.XXXXXX)"
  trap 'rm -f -- "$installer"' EXIT
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o "$installer"
  sh "$installer"
  UV="${UV_INSTALL_DIR:-$HOME/.local/bin}/uv"
  if [ ! -x "$UV" ]; then UV="$HOME/.cargo/bin/uv"; fi
fi

export PATH="$(dirname "$UV"):$PATH"
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
def get(url, maximum):
    if not trusted(url): raise SystemExit('Untrusted download URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'Odicto-bootstrap/1'})
    try:
        with urllib.request.build_opener(Redirect()).open(request, timeout=30) as response:
            data = response.read(maximum + 1)
    except urllib.error.HTTPError as error:
        if error.code == 429 or (error.code == 403 and (error.headers or {}).get('X-RateLimit-Remaining') == '0'):
            raise SystemExit('GitHub refused the download for now: too many requests from this network. Wait one hour, then run the installer again. Nothing was installed.')
        raise SystemExit(f'The download failed: GitHub answered HTTP {error.code}. Run the installer again later. Nothing was installed.')
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit(f'The download failed: no connection to GitHub ({getattr(error, "reason", error)}). Check the internet connection, then run the installer again. Nothing was installed.')
    if len(data) > maximum: raise SystemExit('Download exceeds size limit')
    return data
metadata = json.loads(get(f'https://api.github.com/repos/{repo}/releases/latest', 2*1024*1024))
tag = metadata.get('tag_name', '')
import re
if not re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag) or metadata.get('draft') or metadata.get('prerelease'):
    raise SystemExit('A stable versioned release is required')
name = f'Odicto-{tag}.zip'
prefix = f'https://github.com/{repo}/releases/download/{tag}/'
for asset in (name, name + '.sha256'):
    matches = [a for a in metadata.get('assets', []) if a.get('name') == asset]
    if len(matches) != 1 or matches[0].get('browser_download_url') != prefix + asset:
        raise SystemExit('Release archive or checksum is missing')
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
