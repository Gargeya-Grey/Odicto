"""Managed Odicto releases. No code, configuration or dependency is changed in place."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import stat
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


REPOSITORY = "Gargeya-Grey/Odicto"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_ENTRIES = 10000
CHECK_INTERVAL = 8 * 60 * 60
STARTUP_TIMEOUT = 60.0
CANCEL_TIMEOUT = 10.0
_VERSION = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_RELEASE_DIR = re.compile(r"v\d+\.\d+\.\d+-[a-f0-9]{12}-[a-f0-9]{8}\Z")
_DOWNLOAD_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com",
                   "objects.githubusercontent.com", "github-releases.githubusercontent.com"}


class DistributionError(RuntimeError):
    pass


class BusyError(DistributionError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise DistributionError("Release tag must be vMAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def _trusted_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in _DOWNLOAD_HOSTS
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443))


class _TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_url(newurl):
            raise DistributionError("Release download redirected outside GitHub")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, maximum: int) -> bytes:
    if not _trusted_url(url):
        raise DistributionError("Untrusted release download URL")
    request = urllib.request.Request(url, headers={"User-Agent": "Odicto-managed-installer/1",
                                                  "Accept": "application/vnd.github+json"})
    opener = urllib.request.build_opener(_TrustedRedirect())
    with opener.open(request, timeout=30) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise DistributionError("Release download exceeds its size limit")
    return data


@dataclass(frozen=True)
class Release:
    tag: str
    archive_url: str
    checksum_url: str

    def __post_init__(self):
        version_tuple(self.tag)
        name = f"Odicto-{self.tag}.zip"
        prefix = f"https://github.com/{REPOSITORY}/releases/download/{self.tag}/"
        if self.archive_url != prefix + name or self.checksum_url != prefix + name + ".sha256":
            raise DistributionError("Release assets must belong to the configured Odicto repository")

    @classmethod
    def from_metadata(cls, metadata: dict) -> "Release":
        tag = metadata.get("tag_name")
        version_tuple(tag)
        if metadata.get("draft") or metadata.get("prerelease"):
            raise DistributionError("Automatic updates require a stable published release")
        name = f"Odicto-{tag}.zip"
        prefix = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
        assets = metadata.get("assets")
        if not isinstance(assets, list):
            raise DistributionError("Release assets are missing")
        matches = {}
        for asset in assets:
            if isinstance(asset, dict) and asset.get("name") in (name, name + ".sha256"):
                expected = prefix + asset["name"]
                if asset.get("browser_download_url") != expected or asset["name"] in matches:
                    raise DistributionError("Release asset URL or duplicate asset is invalid")
                matches[asset["name"]] = expected
        if set(matches) != {name, name + ".sha256"}:
            raise DistributionError("Release needs its archive and SHA-256 file")
        return cls(tag, matches[name], matches[name + ".sha256"])


def latest_release(fetch: Callable = download) -> Release:
    try:
        metadata = json.loads(fetch(RELEASE_API, 2 * 1024 * 1024))
    except (ValueError, UnicodeError) as error:
        raise DistributionError("Release metadata is invalid") from error
    if not isinstance(metadata, dict):
        raise DistributionError("Release metadata is invalid")
    return Release.from_metadata(metadata)


def verified_archive(release: Release, fetch: Callable = download) -> tuple[bytes, str]:
    checksum = fetch(release.checksum_url, 1024).decode("ascii").strip().split()
    expected_name = f"Odicto-{release.tag}.zip"
    if len(checksum) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum[0]) or checksum[1] != expected_name:
        raise DistributionError("Release checksum file is invalid")
    archive = fetch(release.archive_url, MAX_ARCHIVE)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != checksum[0].lower():
        raise DistributionError("Release archive SHA-256 does not match")
    return archive, digest


def _archive_members(archive: zipfile.ZipFile):
    members = archive.infolist()
    if len(members) > MAX_ENTRIES or sum(item.file_size for item in members) > MAX_EXPANDED:
        raise DistributionError("Release archive expands beyond its size limit")
    seen = set()
    for member in members:
        raw_name = member.orig_filename
        path = PurePosixPath(member.filename)
        if ("\\" in raw_name or "\0" in raw_name or path.is_absolute() or any(part in ("..", ".") or ":" in part for part in path.parts)
                or not path.parts or path.parts[0] != "Odicto"):
            raise DistributionError("Unsafe path in release archive")
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise DistributionError("Release archive contains a link or special file")
        relative = Path(*path.parts[1:])
        if not path.parts[1:]:
            if member.is_dir():
                continue
            raise DistributionError("Invalid archive root")
        first = path.parts[1].casefold()
        if first in (".env", "prompt.txt", ".venv", ".git", ".odicto-ready.json", ".odicto-managed.json",
                     ".odicto-stage-failed.json", "active.json", "pending.json") or first.startswith("dictation."):
            raise DistributionError("Release archive contains private or runtime files")
        key = relative.as_posix().casefold()
        if key in seen:
            raise DistributionError("Release archive contains duplicate paths")
        seen.add(key)
        yield member, relative


def extract_archive(contents: bytes, destination: Path) -> None:
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            members = list(_archive_members(archive))
            for member, relative in members:
                target = destination / relative
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    if member.external_attr >> 16 & 0o111:
                        target.chmod(target.stat().st_mode | 0o111)
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise DistributionError("Release archive cannot be extracted") from error


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@contextmanager
def file_lock(path: Path):
    """Uses the same POSIX flock / Windows first-byte lock as the app."""
    handle = path.open("a+b")
    handle.seek(0)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            raise BusyError(f"Another Odicto process owns {path.name}; this command has not completed. Retry when it finishes.") from error
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def default_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Odicto"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Odicto"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "odicto"


class Manager:
    def __init__(self, home: Path, *, fetch: Callable = download, installer: Callable | None = None):
        self.home = Path(home).expanduser().resolve()
        self.data = self.home / "data"
        self.releases = self.home / "releases"
        self.pointer = self.home / "active.json"
        self.marker = self.home / ".odicto-managed.json"
        self.fetch = fetch
        self.installer = installer or self._install_dependencies

    @staticmethod
    def support():
        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from tools.distribution import managed_admin
        return managed_admin

    def admin_enabled(self) -> bool:
        return sys.platform == "win32" and self.support().enabled(self.home)

    def _migration_guard(self):
        path = self.home / "migration.json"
        if path.exists() or path.is_symlink():
            if self._read_json(path).get("state") != "complete":
                raise DistributionError("Migration is unfinished. Resume odicto migrate with the same --source before starting or changing versions")

    def initialize(self) -> None:
        if self.home.exists() and not self.marker.exists() and any(self.home.iterdir()):
            raise DistributionError("Destination is not an empty managed installation; existing files were preserved")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.marker.exists():
            marker = self._read_json(self.marker)
            if marker.get("schema") != 1 or marker.get("home") != str(self.home):
                raise DistributionError("Managed-install ownership marker is invalid")
        else:
            atomic_json(self.marker, {"schema": 1, "home": str(self.home), "automatic_updates": True})
        for directory in (self.data, self.releases):
            if directory.is_symlink():
                raise DistributionError("Managed installation directories cannot be symlinks")
            directory.mkdir(exist_ok=True, mode=0o700)

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            if path.is_symlink():
                raise DistributionError("Managed-install metadata cannot be a symlink")
            if path.stat().st_size > 64 * 1024:
                raise DistributionError("Managed-install metadata is too large")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DistributionError(f"Invalid managed-install metadata: {path.name}") from error
        if not isinstance(value, dict):
            raise DistributionError(f"Invalid managed-install metadata: {path.name}")
        return value

    def active(self) -> dict | None:
        if not self.pointer.exists():
            return None
        pointer = self._read_json(self.pointer)
        self.release_path(pointer)
        return pointer

    def release_path(self, record: dict) -> Path:
        version_tuple(record.get("version"))
        name = record.get("directory")
        if not isinstance(name, str) or not _RELEASE_DIR.fullmatch(name):
            raise DistributionError("Active release directory is invalid")
        directory = self.releases / name
        if directory.is_symlink() or directory.resolve().parent != self.releases.resolve():
            raise DistributionError("Active release escaped its managed directory")
        ready = self._read_json(directory / ".odicto-ready.json")
        if (ready.get("version") != record["version"] or ready.get("directory") != name
                or ready.get("archive_sha256") != record.get("archive_sha256")
                or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("archive_sha256", "")))):
            raise DistributionError("Active release is not a verified ready version")
        return directory

    def environment(self) -> dict:
        environment = dict(os.environ)
        environment["ODICTO_DATA_ROOT"] = str(self.data)
        environment["ODICTO_MANAGED_HOME"] = str(self.home)
        return environment

    def _install_dependencies(self, directory: Path, environment: dict) -> None:
        uv = shutil.which("uv")
        if not uv:
            raise DistributionError("uv was not found; rerun the bootstrap installer")
        lock = directory / "tools" / "distribution" / "requirements.lock"
        if not lock.is_file():
            raise DistributionError("This release has no hash-locked dependency file")
        python = directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([uv, "venv", "--python", "3.12", str(directory / ".venv")], check=True,
                       cwd=directory, env=environment, timeout=600)
        subprocess.run([uv, "pip", "sync", "--python", str(python), "--require-hashes", str(lock)],
                       check=True, cwd=directory, env=environment, timeout=1200)
        smoke_environment = dict(environment, QT_QPA_PLATFORM="offscreen")
        subprocess.run([str(python), "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import sounddevice, soundfile, faster_whisper, openai, platforms; "
                        "from google import genai; from config import Config; Config.validate(); "
                        "from PySide6.QtWidgets import QApplication, QLabel; "
                        "app=QApplication([]); label=QLabel('Odicto staged health check'); "
                        "label.resize(240,48); assert not label.grab().isNull()",
                        str(directory / "app")],
                       check=True, cwd=directory, env=smoke_environment, timeout=120)
        result = subprocess.run([str(python), str(directory / "odicto.py"), "version"],
                                check=True, cwd=directory, env=environment, timeout=30,
                                capture_output=True, text=True)
        if (directory / "VERSION").read_text(encoding="utf-8").strip() not in result.stdout:
            raise DistributionError("Staged release did not report its expected version")

    def stage(self, release: Release) -> dict:
        contents, digest = verified_archive(release, self.fetch)
        directory = self.releases / f"{release.tag}-{digest[:12]}-{uuid.uuid4().hex[:8]}"
        directory.mkdir(mode=0o700)
        record = {"version": release.tag, "directory": directory.name, "archive_sha256": digest}
        try:
            extract_archive(contents, directory)
            actual = (directory / "VERSION").read_text(encoding="utf-8").strip()
            if actual != release.tag[1:]:
                raise DistributionError("Archive version and release tag disagree")
            capabilities = self._read_json(directory / "tools" / "distribution" / "capabilities.json")
            if (capabilities.get("schema") != 1 or capabilities.get("stable_data_root") is not True
                    or capabilities.get("setup_writer_lock") is not True):
                raise DistributionError("Release does not support coordinated managed updates")
            self.installer(directory, self.environment())
            atomic_json(directory / ".odicto-ready.json", record)
            atomic_json(self.home / "pending.json", record)
            return record
        except Exception as error:
            atomic_json(directory / ".odicto-stage-failed.json", {"error": type(error).__name__})
            raise

    @contextmanager
    def _activation_locks(self):
        with ExitStack() as stack:
            stack.enter_context(file_lock(self.home / "launch.lock"))
            self._recover_launch_intent()
            stack.enter_context(file_lock(self.data / "setup.lock"))
            stack.enter_context(file_lock(self.data / "dictation.lock"))
            yield

    def activate(self, record: dict) -> None:
        self.release_path(record)
        with self._activation_locks():
            self._migration_guard()
            old = self.active()
            admin_markers = [self.data / ".odicto-admin-enabled"]
            if old:
                admin_markers.append(self.release_path(old) / ".odicto-admin-enabled")
            if any(path.exists() for path in admin_markers):
                capabilities = self._read_json(self.release_path(record) / "tools/distribution/capabilities.json")
                if capabilities.get("managed_admin_mode") is not True:
                    raise DistributionError("This release cannot preserve managed administrator mode; disable Admin mode before switching")
                if any(path.exists() for path in admin_markers[1:]):
                    raise DistributionError("A release-local administrator marker has unknown managed ownership")
                if sys.platform == "win32":
                    self.support().enabled(self.home)
            new = {key: record[key] for key in ("version", "directory", "archive_sha256")}
            if old:
                new["previous"] = {key: old[key] for key in ("version", "directory", "archive_sha256")}
            atomic_json(self.pointer, new)

    def rollback(self) -> dict:
        current = self.active()
        previous = current.get("previous") if current else None
        if not isinstance(previous, dict):
            raise DistributionError("No previous ready release is available")
        self.activate(previous)
        marker = self._read_json(self.marker)
        marker["blocked_version"] = current["version"]
        atomic_json(self.marker, marker)
        return previous

    def update(self, *, force_check: bool = True) -> dict:
        current = self.active()
        marker = self._read_json(self.marker)
        if not force_check and marker.get("automatic_updates") is False:
            return current
        pending_path = self.home / "pending.json"
        if pending_path.exists():
            pending = self._read_json(pending_path)
            blocked = not force_check and pending.get("version") == marker.get("blocked_version")
            if not blocked and (current is None or version_tuple(pending.get("version")) > version_tuple(current["version"])):
                self.activate(pending)
                return self.active()
        now = time.time()
        last_check = marker.get("last_check", 0)
        if (not force_check and current and (marker.get("automatic_updates") is False
                or isinstance(last_check, (int, float)) and 0 <= now - last_check < CHECK_INTERVAL)):
            return current
        release = latest_release(self.fetch)
        marker["last_check"] = now
        atomic_json(self.marker, marker)
        if current and version_tuple(release.tag) <= version_tuple(current["version"]):
            return current
        if not force_check and release.tag == marker.get("blocked_version"):
            return current
        staged = self.stage(release)
        self.activate(staged)
        return self.active()

    def initialize_settings(self, record: dict) -> None:
        example = self.release_path(record) / ".env.example"
        destination = self.data / ".env"
        if not destination.exists():
            contents = example.read_bytes()
            temporary = self.data / (".env.new-" + uuid.uuid4().hex)
            with temporary.open("xb") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            finally:
                temporary.unlink()

    def install_launchers(self) -> None:
        uv = shutil.which("uv")
        if not uv:
            raise DistributionError("uv was not found; launcher creation was deferred")
        if (self.home / "bin").is_symlink():
            raise DistributionError("Launcher directory cannot be a symlink")
        loader = '''"""Stable entry point; the active pointer selects both app and updater."""
import json, os, re, runpy, sys
from pathlib import Path
home = Path(__file__).resolve().parent
marker = json.loads((home / '.odicto-managed.json').read_text(encoding='utf-8'))
if marker.get('schema') != 1 or marker.get('home') != str(home):
    raise SystemExit('Odicto managed-install marker is invalid')
active = json.loads((home / 'active.json').read_text(encoding='utf-8'))
name = active.get('directory', '')
if not isinstance(name, str) or not re.fullmatch(r'v[0-9]+\\.[0-9]+\\.[0-9]+-[a-f0-9]{12}-[a-f0-9]{8}', name):
    raise SystemExit('Odicto active release path is invalid')
release = home / 'releases' / name
if release.is_symlink() or release.resolve().parent != (home / 'releases').resolve():
    raise SystemExit('Odicto active release escaped its managed directory')
ready = json.loads((release / '.odicto-ready.json').read_text(encoding='utf-8'))
if ready.get('version') != active.get('version') or ready.get('directory') != name or ready.get('archive_sha256') != active.get('archive_sha256'):
    raise SystemExit('Odicto active release is not ready')
args = sys.argv[1:]
if not args or args[0] in ('start', 'setup', 'stop', 'restart', 'config', 'version', 'wait-ready', 'autostart', 'remove-autostart'):
    args = ['run', *(args or ['start'])]
script = release / 'tools' / 'distribution' / 'manager.py'
sys.argv = [str(script), '--home', str(home), *args]
runpy.run_path(str(script), run_name='__main__')
'''
        powershell_uv = "'" + uv.replace("'", "''") + "'"
        powershell = ("$ErrorActionPreference = 'Stop'\n"
                      "$launcher = Join-Path (Split-Path -Parent $PSScriptRoot) 'launcher.py'\n"
                      f"& {powershell_uv} run --python 3.12 --no-project --no-config $launcher @args\n"
                      "exit $LASTEXITCODE\n")
        shell = ("#!/bin/sh\nset -eu\n"
                 'script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                 f'exec {shlex.quote(uv)} run --python 3.12 --no-project --no-config "$script_dir/../launcher.py" "$@"\n')
        command = ('@"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                   '-NoProfile -ExecutionPolicy Bypass -File "%~dp0odicto.ps1" %*\r\n')
        files = {"launcher.py": loader.encode(), "bin/odicto": shell.encode(),
                 "bin/odicto.ps1": powershell.encode(), "bin/odicto.cmd": command.encode()}
        marker = self._read_json(self.marker)
        known = marker.get("launchers", {})
        for relative, contents in files.items():
            path = self.home / relative
            accepted = known.get(relative, [])
            if isinstance(accepted, str):
                accepted = [accepted]
            if path.exists() and (path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() not in accepted):
                raise DistributionError(f"Existing launcher {relative} has unknown ownership; it was preserved")
        marker["launchers"] = {
            relative: list(dict.fromkeys(([known[relative]] if isinstance(known.get(relative), str)
                                         else known.get(relative, [])) + [hashlib.sha256(contents).hexdigest()]))
            for relative, contents in files.items()
        }
        atomic_json(self.marker, marker)
        for relative, contents in files.items():
            path = self.home / relative
            path.parent.mkdir(exist_ok=True)
            temporary = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
            temporary.write_bytes(contents)
            if relative == "bin/odicto":
                temporary.chmod(0o755)
            os.replace(temporary, path)
        marker["launchers"] = {relative: hashlib.sha256(contents).hexdigest() for relative, contents in files.items()}
        atomic_json(self.marker, marker)

    def expose_command(self) -> Path:
        """Create a user-level convenience entry, preserving unrelated commands."""
        directory = Path.home() / ".local" / "bin"
        directory.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            command = directory / "odicto.cmd"
            target = str(self.home / "bin" / "odicto.cmd").replace("%", "%%")
            contents = f'@"{target}" %*\r\n'.encode()
            if command.exists():
                if command.is_symlink() or command.read_bytes() != contents:
                    raise DistributionError("An unrelated odicto command already exists; it was preserved")
            else:
                with command.open("xb") as handle:
                    handle.write(contents)
        else:
            command = directory / "odicto"
            target = self.home / "bin" / "odicto"
            if command.exists() or command.is_symlink():
                if not command.is_symlink() or command.resolve() != target.resolve():
                    raise DistributionError("An unrelated odicto command already exists; it was preserved")
            else:
                command.symlink_to(target)
        return command

    def run(self, command: list[str], *, automatic_updates: bool = True) -> int:
        if not command or command[0] not in {"start", "setup", "stop", "restart", "status", "config", "version", "wait-ready", "autostart", "remove-autostart"}:
            raise DistributionError("Unsupported managed Odicto command")
        self.initialize()
        if command[0] not in {"status", "version", "stop", "wait-ready"}:
            self._migration_guard()
        with ExitStack() as launch:
            with file_lock(self.home / "manager.lock"):
                if command[0] not in {"status", "version", "stop", "wait-ready"}:
                    self._migration_guard()
                before = self.active()
                if automatic_updates and command[0] in {"start", "setup"}:
                    try:
                        self.update(force_check=False)
                    except Exception as error:
                        if before is None:
                            raise
                        print(f"Update deferred ({type(error).__name__}); keeping {before['version']}.", file=sys.stderr)
                current = self.active()
                if current is None:
                    raise DistributionError("No active release; run install first")
                directory = self.release_path(current)
                if command[0] in {"start", "setup", "restart", "stop"}:
                    launch.enter_context(file_lock(self.home / "launch.lock"))
                    if command[0] == "stop":
                        self._recover_launch_intent()
            python = self._python(directory)
            admin = self.admin_enabled()
            if (not admin and sys.platform == "win32" and command[0] in {"start", "restart"}
                    and self.support().elevated(directory)):
                raise DistributionError("Admin mode is off. Run odicto start from a normal terminal")
            if admin and command[0] in {"stop", "restart"}:
                result = self.support().stop(self, directory)
                if result or command[0] == "stop":
                    return result
                command = ["start", *command[1:]]
            if admin and command[0] == "start" and not self.support().elevated(directory):
                result = self._launch_admin(directory, python)
            elif command[0] in {"start", "setup", "restart"}:
                result = self._launch(directory, python, command[0])
            else:
                result = subprocess.run([str(python), str(directory / "odicto.py"), *command],
                                        cwd=directory, env=self.environment()).returncode
        if result and command[0] == "start" and before and current["directory"] != before["directory"]:
            try:
                with file_lock(self.home / "manager.lock"):
                    if self.active()["directory"] == current["directory"]:
                        self.rollback()
                print("The new release did not start; the previous version is active again.", file=sys.stderr)
            except BusyError:
                print("The new runtime still owns its lock; rollback was deferred.", file=sys.stderr)
        return result

    def configure_admin(self, enabled: bool) -> None:
        if sys.platform != "win32":
            raise DistributionError("Admin mode is available only on Windows")
        self.initialize()
        self._migration_guard()
        with file_lock(self.home / "manager.lock"), file_lock(self.home / "launch.lock"):
            self._migration_guard()
            self._recover_launch_intent()
            directory = self.release_path(self.active())
            support = self.support()
            if support.enabled(self.home) == enabled:
                return
            if support.stop(self, directory):
                raise DistributionError("Odicto did not stop; administrator mode was preserved")
            with file_lock(self.data / "dictation.lock"):
                support.assert_inactive(self.data, directory, allow_setup=True)
                support.helper(self, "Enable" if enabled else "Disable")
                if support.enabled(self.home) != enabled:
                    raise DistributionError("Administrator mode did not read back correctly")

    def _launch_admin(self, directory: Path, python: Path) -> int:
        self._recover_launch_intent()
        if self._ready(directory, python):
            return 0
        token = uuid.uuid4().hex
        intent = self.home / "launch-intent.json"
        request = self.home / "admin-request.json"
        result_path = self.home / ("admin-result-" + token + ".json")
        if request.exists() or request.is_symlink():
            previous = self._read_json(request)
            previous_token = previous.get("token", "")
            if (previous.get("schema") != 1 or previous.get("home") != str(self.home)
                    or not isinstance(previous_token, str) or not re.fullmatch(r"[a-f0-9]{32}", previous_token)
                    or previous.get("state") not in {"pending", "claimed", "released", "cancelled"}):
                raise DistributionError("Administrator startup request has unknown ownership; it was preserved")
        reservation = {"token": token, "directory": directory.name, "supervisor_pid": os.getpid(),
                       "child_pid": None, "receipt": "launch-receipt-" + token + ".json"}
        with file_lock(self.home / "launch-intent.lock"):
            atomic_json(intent, reservation)
            atomic_json(request, {"schema": 1, "home": str(self.home), "token": token, "directory": directory.name,
                                  "launcher_pid": os.getpid(), "state": "pending"})
        try:
            self.support().start_task(self.home, directory)
            deadline = time.monotonic() + STARTUP_TIMEOUT + CANCEL_TIMEOUT
            while time.monotonic() < deadline:
                if result_path.exists():
                    result = self._read_json(result_path)
                    if result.get("token") != token or type(result.get("code")) is not int:
                        raise DistributionError("Administrator startup result is invalid")
                    return result["code"]
                time.sleep(0.1)
            raise BusyError("Administrator startup has not confirmed completion; its reservation was preserved")
        except Exception:
            with file_lock(self.home / "launch-intent.lock"):
                state = self._read_json(request)
                if state.get("state") != "released":
                    state["state"] = "cancelled"
                atomic_json(request, state)
                if state.get("supervisor_pid") is None:
                    intent.unlink(missing_ok=True)
            raise

    def admin_task(self) -> int:
        """Task Scheduler entry. Claim only the medium launcher's current reservation."""
        active = self.active()
        directory = self.release_path(active)
        support = self.support()
        if not self.admin_enabled() or not support.elevated(directory):
            raise DistributionError("The managed administrator task requires its enabled elevated owner")
        with file_lock(self.home / "admin-task.lock"):
            request = self.home / "admin-request.json"
            with file_lock(self.home / "launch-intent.lock"):
                value = self._read_json(request)
                token = value.get("token", "")
                if (value.get("schema") != 1 or value.get("state") != "pending"
                        or value.get("home") != str(self.home)
                        or value.get("directory") != directory.name or not re.fullmatch(r"[a-f0-9]{32}", token)):
                    raise DistributionError("Administrator startup reservation is invalid or cancelled")
                reservation = self._read_json(self.home / "launch-intent.json")
                if (reservation.get("token") != token or reservation.get("directory") != directory.name
                        or not self._pid_alive(reservation.get("supervisor_pid"))):
                    raise DistributionError("Administrator startup supervisor no longer owns its reservation")
                value.update(state="claimed", supervisor_pid=os.getpid())
                atomic_json(request, value)
                reservation["supervisor_pid"] = os.getpid()
                atomic_json(self.home / "launch-intent.json", reservation)
            result = self._launch(directory, self._python(directory), "start", reservation=reservation)
            atomic_json(self.home / ("admin-result-" + token + ".json"), {"token": token, "code": result})
            return result

    @staticmethod
    def _python(directory: Path) -> Path:
        return directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @staticmethod
    def _pid_alive(pid) -> bool:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return True
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87
            try:
                code = ctypes.c_uint32()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True

    def _recover_launch_intent(self) -> None:
        intent = self.home / "launch-intent.json"
        if not intent.exists():
            return
        with file_lock(self.home / "launch-intent.lock"):
            record = self._read_json(intent)
            token = record.get("token", "")
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
                raise DistributionError("The pending startup reservation is invalid; it was preserved")
            receipt = self.home / ("launch-receipt-" + token + ".json")
            if record.get("receipt") != receipt.name:
                raise DistributionError("The pending startup receipt is invalid; it was preserved")
            if receipt.exists():
                child = self._read_json(receipt)
                safe = child.get("token") == token and (
                    child.get("state") == "setup-owned" or not self._pid_alive(child.get("pid")))
                if not safe and child.get("token") == token and child.get("state") == "owner-starting":
                    active = self.active()
                    if active and active["directory"] == record.get("directory"):
                        directory = self.release_path(active)
                        request = self.home / "admin-request.json"
                        admin_released = True
                        if request.exists():
                            state = self._read_json(request)
                            if state.get("token") == token:
                                admin_released = state.get("state") == "released"
                        safe = admin_released and self._ready(directory, self._python(directory), token)
            else:
                safe = not self._pid_alive(record.get("supervisor_pid")) and (
                    record.get("child_pid") is None or not self._pid_alive(record.get("child_pid")))
            if not safe:
                raise BusyError("A managed startup has not confirmed exit; activation and rollback remain deferred")
            intent.unlink()
            receipt.unlink(missing_ok=True)

    def _ready(self, directory: Path, python: Path, token: str = "") -> bool:
        command = [str(python), str(directory / "odicto.py"), "wait-ready", "--timeout", "0",
                   "--expected-root", str(directory)]
        if token:
            command += ["--launch-token", token]
        try:
            return subprocess.run(command, cwd=directory, env=self.environment(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _launch(self, directory: Path, python: Path, command: str, *, reservation: dict | None = None) -> int:
        intent = self.home / "launch-intent.json"
        if reservation is None:
            self._recover_launch_intent()
        if command == "restart":
            result = subprocess.run([str(python), str(directory / "odicto.py"), "stop"],
                                    cwd=directory, env=self.environment()).returncode
            if result:
                return result
            command = "start"
        if reservation is None and command == "start" and self._ready(directory, python):
            return 0
        token = reservation["token"] if reservation else uuid.uuid4().hex
        receipt = self.home / ("launch-receipt-" + token + ".json")
        if reservation is None:
            atomic_json(intent, {"token": token, "directory": directory.name,
                                 "supervisor_pid": os.getpid(), "child_pid": None, "receipt": receipt.name})
        environment = self.environment()
        environment["ODICTO_LAUNCH_TOKEN"] = token
        environment["ODICTO_LAUNCH_RECEIPT"] = str(receipt)
        process = None
        confirmed = False
        try:
            options = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen([str(python), str(directory / "odicto.py"),
                                        "_managed-owner" if command == "start" else "_managed-setup"],
                                       cwd=directory, env=environment, stdin=subprocess.PIPE, **options)
            child_reservation = self._read_json(intent)
            child_reservation["child_pid"] = process.pid
            atomic_json(intent, child_reservation)
            with file_lock(self.home / "launch-intent.lock"):
                if reservation is not None and self._read_json(self.home / "admin-request.json").get("state") != "claimed":
                    raise OSError("Administrator startup was cancelled")
                process.stdin.write(b"S")
                process.stdin.flush()
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                if reservation is not None:
                    state = self._read_json(self.home / "admin-request.json")
                    if state.get("state") != "claimed" or not self._pid_alive(state.get("launcher_pid")):
                        return 1
                status = process.poll()
                if status is not None:
                    confirmed = True
                    return status if status else (0 if command == "setup" else 1)
                owned_setup = False
                if command == "setup" and receipt.exists():
                    state = self._read_json(receipt)
                    owned_setup = state.get("token") == token and state.get("state") == "setup-owned"
                if owned_setup or command == "start" and self._ready(directory, python, token):
                    with file_lock(self.home / "launch-intent.lock"):
                        if reservation is not None:
                            state = self._read_json(self.home / "admin-request.json")
                            if state.get("state") != "claimed" or not self._pid_alive(state.get("launcher_pid")):
                                return 1
                        process.stdin.write(b"R")
                        process.stdin.flush()
                        if reservation is not None:
                            state["state"] = "released"
                            atomic_json(self.home / "admin-request.json", state)
                    process.stdin.close()
                    confirmed = True
                    return 0
                time.sleep(0.1)
            print("Odicto did not complete startup; cancelling the unconfirmed child.", file=sys.stderr)
            return 1
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Odicto startup failed ({type(error).__name__}).", file=sys.stderr)
            return 1
        finally:
            if process is None:
                confirmed = True
            elif not confirmed:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                try:
                    process.wait(timeout=CANCEL_TIMEOUT)
                    confirmed = True
                except subprocess.TimeoutExpired:
                    print("The startup child has not confirmed exit; activation and rollback remain deferred.", file=sys.stderr)
            if confirmed:
                intent.unlink(missing_ok=True)
                receipt.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install, update or run managed Odicto releases")
    parser.add_argument("--home", type=Path, default=default_home())
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "check", "stage", "update", "rollback", "status"):
        sub.add_parser(command)
    automatic = sub.add_parser("auto-updates")
    automatic.add_argument("setting", choices=("on", "off"))
    admin = sub.add_parser("admin")
    admin.add_argument("setting", choices=("on", "off"))
    sub.add_parser("admin-task", help=argparse.SUPPRESS)
    migration = sub.add_parser("migrate")
    migration.add_argument("--source", type=Path, required=True)
    migration.add_argument("--check", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--no-auto-updates", action="store_true")
    run.add_argument("app_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    manager = Manager(args.home)
    try:
        if args.command == "check":
            print(latest_release().tag)
            return 0
        if args.command == "run":
            return manager.run(args.app_args or ["start"], automatic_updates=not args.no_auto_updates)
        if args.command == "admin-task":
            return manager.admin_task()
        if args.command == "admin":
            manager.configure_admin(args.setting == "on")
            print("Admin mode changed. Start Odicto with odicto start.")
            return 0
        if args.command == "migrate":
            support = manager.support()
            from tools.distribution.migration import migrate
            result = migrate(manager, args.source, check=args.check, adapter=support.MigrationAdapter(), lock_factory=file_lock)
            print(json.dumps(result.as_dict() if hasattr(result, "as_dict") else result, indent=2))
            return 0
        manager.initialize()
        with file_lock(manager.home / "manager.lock"):
            if args.command == "status":
                print(json.dumps(manager.active(), indent=2))
            elif args.command == "auto-updates":
                marker = manager._read_json(manager.marker)
                marker["automatic_updates"] = args.setting == "on"
                atomic_json(manager.marker, marker)
                print(f"Automatic updates are {args.setting}.")
            elif args.command == "rollback":
                print(json.dumps(manager.rollback(), indent=2))
            elif args.command == "stage":
                print(json.dumps(manager.stage(latest_release(manager.fetch)), indent=2))
            else:
                record = manager.update()
                if args.command == "install":
                    manager.initialize_settings(record)
                    manager.install_launchers()
                    try:
                        command_path = manager.expose_command()
                        print(f"Command installed at {command_path}")
                    except DistributionError as error:
                        print(f"Command alias unchanged: {error}", file=sys.stderr)
                    print(f"Configure with: {manager.home / 'bin' / ('odicto.cmd' if os.name == 'nt' else 'odicto')} setup")
                print(f"Odicto {record['version']} is active.")
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Odicto: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
