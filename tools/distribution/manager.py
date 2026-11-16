"""Managed Odicto releases. No code, configuration or dependency is changed in place."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
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
# The Release workflow publishes this file for a stable release. Unlike the API it has no hourly limit.
RELEASE_MANIFEST = f"https://github.com/{REPOSITORY}/releases/latest/download/odicto-release.json"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_ENTRIES = 10000
CHECK_INTERVAL = 8 * 60 * 60
STARTUP_TIMEOUT = 60.0
CANCEL_TIMEOUT = 10.0
CLEANUP_BUDGET = 15.0
# A launch that is not strict accepts an owner that still downloads its speech model after
# this long. The admin launcher gives up at 70 s and Setup Save at 90 s.
DOWNLOAD_ACCEPT_SECONDS = 45.0
# A child that never got the hand-off byte ends itself when its launcher's pipe closes; it polls
# that pipe every 0.05 s. One that still runs this long after the launcher is gone was handed off.
HANDOFF_SETTLE = 1.0
# What a running owner may still wait for (see `odicto.py wait-ready --report-waiting`), with the
# exit code that goes with each state. A state counts only when the code and the marker agree.
_WAITING_CODES = {"mic": 20, "speech": 20, "speech-download": 21}
_WAITING_MARKER = "ODICTO-WAITING"
_LAUNCH_NOTICES = {
    "mic": "Odicto is running, but it cannot hear a microphone. Connect one or choose one in "
           "Odicto Setup. Odicto tries again by itself.",
    "speech": "Odicto is running, but its speech model did not load. Check the internet "
              "connection. Odicto tries again by itself.",
    "speech-download": "Odicto is running and still downloads its speech model. Dictation works "
                       "when the download ends.",
}
_VERSION = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_RELEASE_DIR = re.compile(r"v\d+\.\d+\.\d+-[a-f0-9]{12}-[a-f0-9]{8}\Z")
_TRASH = ".trash-"
_DOWNLOAD_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com",
                   "objects.githubusercontent.com", "github-releases.githubusercontent.com"}


class DistributionError(RuntimeError):
    pass


class BusyError(DistributionError):
    pass


class MenuRemovalError(DistributionError):
    """A menu entry that Odicto wrote could not be deleted. `lines` holds what happened to the others."""

    def __init__(self, message: str, lines: list[str]):
        super().__init__(message)
        self.lines = lines


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


_UPDATE_STEPS = {"check": "Checking for updates", "download": "Downloading and verifying",
                 "install": "Installing", "verify": "Checking that it works",
                 "speech": "Getting the speech model"}


class _UpdateSteps:
    """Prints one line per update step: the step name, then 'done' when the next one starts."""

    def __init__(self) -> None:
        self.open = False

    last = ""

    def __call__(self, name: str) -> None:
        self.end(" done")
        self.last = name
        print(f"  {_UPDATE_STEPS.get(name, name):<28}...", end="", flush=True)
        self.open = True

    def end(self, word: str) -> None:
        if self.open:
            print(word, flush=True)
            self.open = False


def _update_result(before: dict | None, after: dict | None) -> str:
    version = (after or {}).get("version", "")
    if before and before.get("version") == version:
        return f"Odicto {version[1:]} is up to date."
    return (f"\nOdicto {version[1:]} is ready.\n"
            f"  What's new: https://github.com/{REPOSITORY}/releases/tag/{version}\n"
            "  Start it:   odicto start")


def _release_from_json(data: bytes) -> Release:
    try:
        metadata = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise DistributionError("Release metadata is invalid") from error
    if not isinstance(metadata, dict):
        raise DistributionError("Release metadata is invalid")
    return Release.from_metadata(metadata)


def latest_release(fetch: Callable = download) -> Release:
    """The manifest first. The rate-limited API is the fallback for an HTTP error or an invalid manifest.

    A connection error is not an HTTP error: offline, the API would only add a second wait."""
    try:
        return _release_from_json(fetch(RELEASE_MANIFEST, 2 * 1024 * 1024))
    except (urllib.error.HTTPError, DistributionError):
        return _release_from_json(fetch(RELEASE_API, 2 * 1024 * 1024))


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


def _sddl_aces(dacl: str) -> tuple[str, list[str]]:
    """Split DACL-only SDDL into its "D:<flags>" prefix and its ACE strings. Unknown text fails closed."""
    start = dacl.find("(")
    prefix = dacl if start < 0 else dacl[:start]
    aces, depth, begin = [], 0, start
    for index in range(max(start, 0), len(dacl) if start >= 0 else 0):
        if dacl[index] == "(":
            begin = index if depth == 0 else begin
            depth += 1
        elif dacl[index] == ")":
            depth -= 1
            if depth < 0:
                break
            if depth == 0:
                aces.append(dacl[begin:index + 1])
    if not re.fullmatch(r"D:(P|AI|AR|NO_ACCESS_CONTROL)*", prefix) or depth or prefix + "".join(aces) != dacl:
        raise DistributionError("A private directory has an unrecognized access list; it was preserved")
    return prefix, aces


def _with_token_user(dacl: str, sid: str, canonical: Callable[[str], str] = lambda value: value) -> str | None:
    """Return the DACL plus one explicit inheritable full-control grant for sid, or None when that
    grant already exists. Every existing entry is kept; no other principal is added. The grant goes
    after the leading explicit denies and before inherited entries (canonical order).

    The grant does not prove effective access: Windows also applies deny entries, including ones that
    change meaning on inheritance (CREATOR OWNER) or match logon and deny-only token groups. Odicto's
    own new directories carry no deny entries (atomic protected creation); a reader that is still
    denied reports a clear access error instead of a misleading success."""
    prefix, aces = _sddl_aces(dacl)
    if "NO_ACCESS_CONTROL" in prefix:
        return None
    position = None
    for index, ace in enumerate(aces):
        fields = ace[1:-1].split(";")
        flags = {fields[1][at:at + 2] for at in range(0, len(fields[1]), 2)} if len(fields) > 1 else set()
        # Only an explicit grant that reaches every descendant counts: an inherited one (ID) can
        # vanish with the parent's list, and NP stops at the first level.
        if (len(fields) >= 6 and fields[0] == "A" and {"OI", "CI"} <= flags and not {"IO", "ID", "NP"} & flags
                and fields[2].upper() in {"FA", "0X1F01FF"} and canonical(fields[5]) == sid):
            return None
        if position is None and not (fields[0].upper() in {"D", "OD", "XD"} and "ID" not in flags):
            position = index
    position = len(aces) if position is None else position
    return prefix + "".join(aces[:position] + [f"(A;OICI;FA;;;{sid})"] + aces[position:])


def _grant_token_user(path: Path, *, create: bool = False, exist_ok: bool = False) -> None:
    """Make a Windows directory private to this account and readable when elevated files are inside.

    Python's mode 0o700 grants only SYSTEM, Administrators and OWNER RIGHTS (and Pythons without the
    CVE-2024-4030 fix inherit the parent's list instead). A file an elevated process writes belongs
    to Administrators, so this account could not read it at medium integrity. With create=True the
    directory is made atomically with an explicit protected list: those three plus this account,
    inheriting nothing. An existing directory gains one explicit inheritable grant for the token user
    (see _with_token_user for what that does not prove). SetFileSecurity writes this directory's DACL
    only: no SACL privilege, no walk over children."""
    import ctypes
    import errno
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, pointer(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, pointer(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, pointer(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, pointer(ctypes.c_void_p)]
    advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, pointer(wintypes.DWORD)]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, pointer(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, pointer(ctypes.c_void_p), ctypes.c_void_p]
    dacl_information = 0x4

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    def check(succeeded) -> None:
        if not succeeded:
            code = ctypes.get_last_error()
            raise DistributionError(f"This account cannot be given access to private directory {path.name} "
                                    f"(Windows error {code}); it was preserved")

    def owned_text(address: ctypes.c_void_p) -> str:
        try:
            return ctypes.wstring_at(address.value)
        finally:
            kernel.LocalFree(address)

    def sid_text(binary) -> str:
        text = ctypes.c_void_p()
        check(advapi.ConvertSidToStringSidW(binary, ctypes.byref(text)))
        return owned_text(text)

    def canonical(value: str) -> str:
        if value.startswith("S-"):
            return value
        binary = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(value, ctypes.byref(binary)):
            return value
        try:
            return sid_text(binary)
        finally:
            kernel.LocalFree(binary)

    def token_information(token, kind: int):
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        check(needed.value and advapi.GetTokenInformation(token, kind, buffer, needed, ctypes.byref(needed)))
        return buffer

    def read_dacl() -> str:
        needed = wintypes.DWORD()
        advapi.GetFileSecurityW(str(path), dacl_information, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        check(needed.value and advapi.GetFileSecurityW(str(path), dacl_information, buffer, needed, ctypes.byref(needed)))
        text = ctypes.c_void_p()
        check(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, dacl_information, ctypes.byref(text), None))
        return owned_text(text)

    def with_descriptor(sddl: str, action):
        descriptor = ctypes.c_void_p()
        check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        try:
            return action(descriptor)
        finally:
            kernel.LocalFree(descriptor)

    token = wintypes.HANDLE()
    check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)))
    try:
        user = token_information(token, 1)  # TOKEN_USER starts with the SID pointer
        sid = sid_text(ctypes.c_void_p.from_buffer(user).value)
    finally:
        kernel.CloseHandle(token)

    if create:
        def create_directory(descriptor) -> None:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            if kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                return
            code = ctypes.get_last_error()
            if code == 183 and exist_ok and path.is_dir():  # ERROR_ALREADY_EXISTS: repair it below
                return
            if code == 183:
                raise FileExistsError(errno.EEXIST, "Private directory already exists", str(path))
            raise ctypes.WinError(code)

        with_descriptor(f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)(A;OICI;FA;;;{sid})", create_directory)

    # A junction (or any reparse point) would repair its target's list, not this folder's.
    if getattr(os.lstat(path), "st_file_attributes", 0) & 0x400:
        raise DistributionError(f"Private directory {path.name} is a link; it was preserved")

    updated = _with_token_user(read_dacl(), sid, canonical)
    if updated is None:
        return
    check(with_descriptor(updated, lambda descriptor: advapi.SetFileSecurityW(str(path), dacl_information, descriptor)))
    if _with_token_user(read_dacl(), sid, canonical) is not None:
        raise DistributionError(f"Private directory {path.name} did not keep this account's access; it was preserved")


def private_directory(path: Path, *, exist_ok: bool = False, parents: bool = False) -> Path:
    """Owner-only directory: POSIX mode 0o700; on Windows an explicit protected list, made atomically,
    that also lets this same account read files an elevated process owns. Never grants any other account."""
    if os.name != "nt":
        path.mkdir(mode=0o700, exist_ok=exist_ok, parents=parents)
        return path
    if parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    _grant_token_user(path, create=True, exist_ok=exist_ok)
    return path


# Start Menu items of a managed Windows install: (file name, command, description).
SHORTCUTS = (("Odicto", "start", "Start Odicto dictation"),
             ("Odicto Setup", "setup", "Open the Odicto setup page"),
             ("Stop Odicto", "stop", "Stop Odicto"),
             ("Remove Odicto", "uninstall --ask", "Remove Odicto from this computer"))

# Values arrive in environment variables, so no path is ever quoted into the script.
SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$programs = $env:ODICTO_PROGRAMS
if (-not $programs) { $programs = [Environment]::GetFolderPath('Programs') }
if (-not $programs) { throw 'The Start Menu folder of this user could not be found.' }
$folder = Join-Path $programs 'Odicto'
if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }
$launcher = [IO.Path]::GetFullPath($env:ODICTO_LAUNCHER)
$shell = New-Object -ComObject WScript.Shell
foreach ($entry in ($env:ODICTO_SHORTCUTS | ConvertFrom-Json)) {
    $name, $command, $text = $entry
    $path = Join-Path $folder ($name + '.lnk')
    if (Test-Path -LiteralPath $path) {
        $existing = [string]$shell.CreateShortcut($path).TargetPath
        if (-not $existing -or -not [string]::Equals([IO.Path]::GetFullPath($existing), $launcher,
                [StringComparison]::OrdinalIgnoreCase)) {
            Write-Output "Kept $name (it runs another program)"
            continue
        }
    }
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $launcher
    $link.Arguments = $command
    $link.WorkingDirectory = $env:ODICTO_HOME
    # The remove item asks a question, so its window must show; the others run minimized.
    if ($command -like 'uninstall*') { $link.WindowStyle = 1 } else { $link.WindowStyle = 7 }
    $link.Description = $text
    if ($env:ODICTO_ICON) { $link.IconLocation = $env:ODICTO_ICON + ',0' }
    $link.Save()
    Write-Output "Start Menu: $name"
}
"""

# The same shortcuts removed again. Only a shortcut that runs this home's launcher is deleted.
REMOVE_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$programs = $env:ODICTO_PROGRAMS
if (-not $programs) { $programs = [Environment]::GetFolderPath('Programs') }
if (-not $programs) { exit 0 }
$folder = Join-Path $programs 'Odicto'
if (-not (Test-Path -LiteralPath $folder)) { exit 0 }
$launcher = [IO.Path]::GetFullPath($env:ODICTO_LAUNCHER)
$shell = New-Object -ComObject WScript.Shell
foreach ($entry in ($env:ODICTO_SHORTCUTS | ConvertFrom-Json)) {
    $name, $command, $text = $entry
    $path = Join-Path $folder ($name + '.lnk')
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $existing = [string]$shell.CreateShortcut($path).TargetPath
    if ($existing -and [string]::Equals([IO.Path]::GetFullPath($existing), $launcher,
            [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $path -Force
        Write-Output "Removed Start Menu item: $name"
    } else {
        Write-Output "Kept $name (it runs another program)"
    }
}
if (-not (Get-ChildItem -LiteralPath $folder -Force | Select-Object -First 1)) {
    Remove-Item -LiteralPath $folder -Force
}
"""

# Linux application menu: the file name of each shortcut above, in a folder every desktop reads.
MENU_FILES = {"Odicto": "odicto.desktop", "Odicto Setup": "odicto-setup.desktop",
              "Stop Odicto": "odicto-stop.desktop", "Remove Odicto": "odicto-remove.desktop"}

# A marker that the uninstall writes while it deletes. Start, update and rollback refuse while it
# exists; only `odicto uninstall` (to finish) and `odicto install` (to start over) go on.
REMOVAL_NOTICE = "uninstalling.json"
# The files that run `odicto uninstall` again: they go last, so a failed removal can be retried.
_RETRY_FILES = ("bin", "launcher.py", "active.json")
_LOCK_FILES = ("manager.lock", "launch.lock")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


def _desktop_text(text: str) -> str:
    """A string value of a .desktop file: a backslash is written twice."""
    if "\n" in text or "\r" in text:
        raise DistributionError("A path with a line break cannot go into a menu entry")
    return text.replace("\\", "\\\\")


def _desktop_command(path: str) -> str:
    """One quoted Exec argument. The Exec rule puts a backslash before a quote, a backtick, a
    dollar sign and a backslash, and a percent sign is written twice. The file then doubles
    every backslash again (_desktop_text)."""
    quoted = "".join("\\" + c if c in '"`$\\' else c for c in path).replace("%", "%%")
    return '"' + _desktop_text(quoted) + '"'


# The owner's exit code when another Odicto holds the lock (app/main.py). The release is fine.
EXIT_ANOTHER_OWNER = 75


def setup_command(home: Path) -> str:
    """The Setup command line as the user can paste it; a macOS home has a space in its path."""
    if os.name == "nt":
        return subprocess.list2cmdline([str(home / "bin" / "odicto.cmd"), "setup"])
    return shlex.join([str(home / "bin" / "odicto"), "setup"])


def default_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Odicto"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Odicto"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "odicto"


# The files of the private uv in <home>/tools/uv: uv itself, then uvx (optional).
PRIVATE_UV_NAMES = ("uv.exe", "uvx.exe") if os.name == "nt" else ("uv", "uvx")


def _posix_host() -> bool:
    """macOS and Linux run bin/odicto; Windows runs bin/odicto.cmd. Tests patch this to reach both."""
    return os.name != "nt"


def _runnable(candidate) -> Path | None:
    """An absolute path to a file that can run, else None."""
    path = Path(candidate) if candidate else None
    if path is not None and path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
        return path
    return None


def _private_uv(home) -> Path | None:
    """<home>/tools/uv/uv, when it is a plain file that can run and both folders above it are real folders.

    A link or a junction anywhere below the home is refused, so a stale link never runs another program.
    The home itself is the user's private folder (private_directory): a program that can change a folder
    in it between this check and the run can also change launcher.py, so this is no guard against that."""
    tools = Path(home) / "tools"
    private = tools / "uv" / PRIVATE_UV_NAMES[0]
    if not (_real_folder(tools) and _real_folder(tools / "uv")):
        return None
    try:
        info = os.lstat(private)
    except OSError:
        return None
    if stat.S_ISREG(info.st_mode) and os.access(private, os.X_OK):
        return private
    return None


def find_uv(home) -> str | None:
    """The uv that the manager keeps in the managed home, else a uv on PATH.

    The private copy comes first, so an update that a LaunchAgent or a desktop autostart starts with a
    minimal PATH still finds it. A home that has none (an older install) uses PATH.
    """
    private = _private_uv(home)
    return str(private) if private else shutil.which("uv")


def bootstrap_uv() -> Path | None:
    """The uv that this install was started with, to copy into the managed home.

    The bootstrap passes its absolute path in ODICTO_BOOTSTRAP_UV and puts its folder first on PATH. An
    older bootstrap passes nothing, and then the uv on PATH is the one. Under a launcher, uv itself sets UV
    to its own path for the processes it starts (uv releases since February 2025), so a start at sign-in without uv on PATH
    still finds it. Only an absolute path to a file that can run is used; a uv found on PATH must also be a
    real uv.exe on Windows, not a .cmd shim."""
    given = os.environ.get("ODICTO_BOOTSTRAP_UV") or ""
    running = os.environ.get("UV") or ""
    found = shutil.which("uv") or ""
    for candidate, from_path in ((given, False), (running, False), (found, True)):
        path = _runnable(candidate)
        if path is not None and not (from_path and os.name == "nt" and path.name.lower() != "uv.exe"):
            return path
    return None


def _real_folder(path) -> bool:
    """A folder that is not a symlink, a junction or another reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400)


class _HeldHome:
    """The home folder that was checked, kept open. fd: the open folder on POSIX, None on Windows (a
    handle that forbids a rename holds it). identity: its (device, inode). marker: the marker as it was
    read from this very folder."""

    def __init__(self, fd, identity):
        self.fd = fd
        self.identity = identity
        self.marker: dict = {}


class _BudgetEnded(Exception):
    pass


def _within(deadline: float, clock: Callable[[], float]) -> None:
    if clock() >= deadline:
        raise _BudgetEnded


# The delete code below never trusts a path it checked earlier. A folder is deleted through an
# open handle to it, so another program cannot put a link in its place between the check and the
# delete (measured on Windows: a held folder and every folder above it refuse a rename).

@contextmanager
def _held_folder(path: str):
    """Windows: hold path open and yield whether it is a real folder.

    The handle shares read and write, not delete: until it closes, nobody can rename or replace
    this folder or a folder above it, so what is listed is what was checked. A link is opened
    itself, never its target, and yields False."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    # FILE_LIST_DIRECTORY; FILE_SHARE_READ | FILE_SHARE_WRITE; OPEN_EXISTING;
    # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
    handle = kernel32.CreateFileW(path, 0x0001, 0x0003, None, 3, 0x02000000 | 0x00200000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    try:
        attributes = os.fstat(descriptor).st_file_attributes
        yield bool(attributes & 0x10) and not attributes & 0x400
    finally:
        os.close(descriptor)


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _unlink(path: str) -> None:
    """Windows: remove one file or one link. A read-only file is made writable, once."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        info = os.lstat(path)
        if _is_link(info):
            os.rmdir(path)  # a folder link: this removes the link, never what it points at
        else:
            os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IWRITE)
            os.unlink(path)


def _rmdir(path: str) -> None:
    """Windows: remove one empty folder. A read-only folder is made writable, once."""
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        info = os.lstat(path)
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise
        os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IWRITE)
        os.rmdir(path)


def _delete_held(path: str, deadline: float, clock: Callable[[], float], top: bool = False) -> None:
    """Windows: delete the folder at path and everything in it, while a handle holds each level."""
    _within(deadline, clock)
    with _held_folder(path) as real:
        if real:
            with os.scandir(path) as entries:
                children = []
                for entry in entries:
                    _within(deadline, clock)
                    children.append((entry.path, entry.is_dir(follow_symlinks=False)))
            for child, folder in children:
                _within(deadline, clock)
                if folder:
                    _delete_held(child, deadline, clock)
                else:
                    _unlink(child)
    _within(deadline, clock)
    if real:
        _rmdir(path)  # the handle is closed; a link put here now is removed itself by a later run
    elif top:
        raise OSError(errno.ENOTDIR, "not a real folder", path)
    else:
        _unlink(path)


def _in_folder(action, name: str, folder: int) -> None:
    """POSIX: action(name) inside the open folder. A folder that is not writable is made so, once."""
    try:
        action(name, dir_fd=folder)
    except FileNotFoundError:
        pass
    except PermissionError:
        os.fchmod(folder, stat.S_IMODE(os.fstat(folder).st_mode) | stat.S_IRWXU)
        action(name, dir_fd=folder)


def _delete_in(parent: int, name: str, deadline: float, clock: Callable[[], float], top: bool = False) -> None:
    """POSIX: delete the folder `name` inside the open folder `parent`, and everything in it.

    Every step is relative to an open folder and never goes through a link (O_NOFOLLOW)."""
    _within(deadline, clock)
    try:
        folder = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return
    except OSError as error:
        if top or error.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise
        _in_folder(os.unlink, name, parent)  # it is a link or a file now: remove that entry itself
        return
    try:
        with os.scandir(folder) as entries:
            children = []
            for entry in entries:
                _within(deadline, clock)
                children.append((entry.name, entry.is_dir(follow_symlinks=False)))
        for child, is_folder in children:
            _within(deadline, clock)
            if is_folder:
                _delete_in(folder, child, deadline, clock)
            else:
                _in_folder(os.unlink, child, folder)
    finally:
        os.close(folder)
    _within(deadline, clock)
    _in_folder(os.rmdir, name, parent)


def _delete_trash_folder(releases: str, name: str, deadline: float, clock: Callable[[], float]) -> None:
    """Delete releases/name and everything in it. Raises OSError when it is not a real folder,
    and _BudgetEnded at the first check after the deadline."""
    if os.name == "nt":
        _delete_held(os.path.join(releases, name), deadline, clock, top=True)
        return
    parent = os.open(releases, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _delete_in(parent, name, deadline, clock, top=True)
    finally:
        os.close(parent)


class Manager:
    def __init__(self, home: Path, *, fetch: Callable = download, installer: Callable | None = None,
                 speech_preparer: Callable | None = None):
        self.home = Path(home).expanduser().resolve()
        self.data = self.home / "data"
        self.releases = self.home / "releases"
        self.pointer = self.home / "active.json"
        self.marker = self.home / ".odicto-managed.json"
        self.fetch = fetch
        self.platform = sys.platform  # a test sets another one to reach a platform's code
        self.installer = installer or self._install_dependencies
        self.speech_preparer = speech_preparer or self._download_speech_model
        # The state a start accepted while the owner still waited ("" when it was ready); run()
        # prints the matching line. Reset at the start of every run and every launch.
        self.launch_notice = ""
        self._waiting = "none"
        # Called with a step name ("check", "download", "install", "verify"); the CLI prints it.
        self.progress: Callable[[str], None] = lambda step: None
        self.update_log = self.data / "update.log"

    # Migration runs where only the manager is known to be importable (see bootstrap).
    private_directory = staticmethod(private_directory)

    @staticmethod
    def support(root: Path | None = None):
        root = root if root is not None else Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from tools.distribution import managed_admin
        return managed_admin

    def admin_enabled(self) -> bool:
        return sys.platform == "win32" and self.support().enabled(self.home)

    def bind_dependencies(self) -> None:
        """Keep supervisor identity while loading ABI-matched release dependencies."""
        directory = self.release_path(self.active())
        environment = directory / ".venv"
        configuration = environment / "pyvenv.cfg"
        if environment.resolve() != environment or environment.is_symlink() or configuration.is_symlink():
            raise DistributionError("The active Python environment has unknown ownership")
        text = configuration.read_text(encoding="utf-8")
        match = re.search(r"(?m)^(?:version_info|version)\s*=\s*(\d+)\.(\d+)", text)
        if match is None or tuple(map(int, match.groups())) != sys.version_info[:2]:
            raise DistributionError("Use the installed odicto command with the active environment's Python version")
        packages = environment / ("Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
        if not packages.is_dir() or packages.is_symlink() or packages.resolve() != packages:
            raise DistributionError("The active Python dependency directory is unavailable or unowned")
        if str(packages) not in sys.path:
            sys.path.insert(0, str(packages))

    def _migration_guard(self):
        path = self.home / "migration.json"
        if path.exists() or path.is_symlink():
            if self._read_json(path).get("state") != "complete":
                raise DistributionError("Migration is unfinished. Resume odicto migrate with the same --source before starting or changing versions")

    def initialize(self, *, reinstall: bool = False) -> None:
        if not reinstall:
            self._refuse_while_removed()  # before it makes any folder in a home that is being removed
        if self.home.exists() and not self.marker.exists() and any(self.home.iterdir()):
            raise DistributionError("Destination is not an empty managed installation; existing files were preserved")
        private_directory(self.home, parents=True, exist_ok=True)
        if self.marker.exists():
            marker = self._read_json(self.marker)
            if marker.get("schema") != 1 or marker.get("home") != str(self.home):
                raise DistributionError("Managed-install ownership marker is invalid")
        else:
            atomic_json(self.marker, {"schema": 1, "home": str(self.home), "automatic_updates": True})
        for directory in (self.data, self.releases):
            if directory.is_symlink():
                raise DistributionError("Managed installation directories cannot be symlinks")
            private_directory(directory, exist_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            if path.is_symlink():
                raise DistributionError("Managed-install metadata cannot be a symlink")
            if path.stat().st_size > 64 * 1024:
                raise DistributionError("Managed-install metadata is too large")
            value = json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as error:
            raise DistributionError(
                f"This account cannot read managed-install metadata {path.name} (access denied). An elevated "
                f"step may have written it; check that {path.parent} grants this account access and has no "
                f"deny entry for it") from error
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

    def ensure_private_uv(self, source: Path | None = None) -> Path | None:
        """Keep a copy of a uv in <home>/tools/uv, unless one is there: source, else the uv this install
        started with (bootstrap_uv).

        Call it with the home initialized: the home, its marker and its private-directory policy come
        first, and this makes only the two folders below it, with the same policy. The copy is made under
        a temporary name and renamed, so a stop in the middle leaves no half file that find_uv accepts.
        Returns the private uv, or None when there is no uv to copy or the copy failed (find_uv then
        falls back to PATH). A folder that is a link is refused and left as it is."""
        folder = self.home / "tools" / "uv"
        private = folder / PRIVATE_UV_NAMES[0]
        existing = _private_uv(self.home)
        if existing is not None:
            return existing
        source = source or bootstrap_uv()
        if source is None:
            return None
        for directory in (self.home / "tools", folder):
            if os.path.lexists(directory):
                if not _real_folder(directory):
                    raise DistributionError(f"{directory} is not a real folder; it was preserved")
            else:
                private_directory(directory)
        if private.is_dir() and not private.is_symlink():
            raise DistributionError(f"{private} is a folder; it was preserved")
        # uvx first and uv last: uv is the file that find_uv looks for.
        extra = source.parent / PRIVATE_UV_NAMES[1]
        pairs = ([(extra, folder / PRIVATE_UV_NAMES[1])] if extra.is_file() else []) + [(source, private)]
        try:
            for origin, destination in pairs:
                temporary = destination.with_name(destination.name + ".new-" + uuid.uuid4().hex)
                try:
                    shutil.copyfile(origin, temporary)
                    if os.name != "nt":
                        temporary.chmod(0o755)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
        except OSError as error:
            self._log(f"uv was not copied into the managed home: {error}")
            return None
        self._log(f"uv copied into the managed home from {source}")
        return private

    def _install_dependencies(self, directory: Path, environment: dict) -> None:
        self.ensure_private_uv()
        uv = find_uv(self.home)
        if not uv:
            raise DistributionError("uv was not found; rerun the bootstrap installer")
        lock = directory / "tools" / "distribution" / "requirements.lock"
        if not lock.is_file():
            raise DistributionError("This release has no hash-locked dependency file")
        python = directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.update_log.parent.mkdir(parents=True, exist_ok=True)
        # Tool output goes to the log: a user sees steps, not package lists or Qt warnings.
        log = self.update_log.open("a", encoding="utf-8", errors="replace")
        quiet = {"stdout": log, "stderr": subprocess.STDOUT}
        try:
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} install {directory.name}\n")
            log.flush()
            self._install_steps(uv, lock, python, directory, environment, quiet)
        finally:
            log.close()

    def _install_steps(self, uv: str, lock: Path, python: Path, directory: Path, environment: dict,
                       quiet: dict) -> None:
        subprocess.run([uv, "venv", "--python", "3.12", str(directory / ".venv")], check=True,
                       cwd=directory, env=environment, timeout=600, **quiet)
        subprocess.run([uv, "pip", "sync", "--python", str(python), "--require-hashes", str(lock)],
                       check=True, cwd=directory, env=environment, timeout=1200, **quiet)
        self.progress("verify")
        smoke_environment = dict(environment, QT_QPA_PLATFORM="offscreen")
        subprocess.run([str(python), "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import sounddevice, soundfile, faster_whisper, openai, platforms; "
                        "from google import genai; from config import Config; Config.validate(); "
                        "from PySide6.QtWidgets import QApplication, QLabel; "
                        "app=QApplication([]); label=QLabel('Odicto staged health check'); "
                        "label.resize(240,48); assert not label.grab().isNull()",
                        str(directory / "app")],
                       check=True, cwd=directory, env=smoke_environment, timeout=120, **quiet)
        result = subprocess.run([str(python), str(directory / "odicto.py"), "version"],
                                check=True, cwd=directory, env=environment, timeout=30,
                                capture_output=True, text=True)
        if (directory / "VERSION").read_text(encoding="utf-8").strip() not in result.stdout:
            raise DistributionError("Staged release did not report its expected version")

    def _download_speech_model(self, directory: Path, environment: dict) -> None:
        """Download the configured speech model with the release's own Python. Output goes to the log."""
        self.update_log.parent.mkdir(parents=True, exist_ok=True)
        with self.update_log.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} speech model {directory.name}\n")
            log.flush()
            subprocess.run([str(self._python(directory)), str(directory / "odicto.py"), "_prepare-speech"],
                           check=True, cwd=directory, env=environment, timeout=900,
                           stdout=log, stderr=subprocess.STDOUT)

    def stage(self, release: Release) -> dict:
        self.progress("download")
        contents, digest = verified_archive(release, self.fetch)
        directory = self.releases / f"{release.tag}-{digest[:12]}-{uuid.uuid4().hex[:8]}"
        private_directory(directory)
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
            self.progress("install")
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
            self._refuse_while_removed()
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
                    self.support(self.release_path(record)).enabled(self.home)
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
        self.progress("check")
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

    def _referenced_releases(self) -> set[str] | None:
        """The folders the pointers name, or None when one cannot be read: then nothing is retired."""
        try:
            active = self._read_json(self.pointer)
            records = [active]
            if active.get("previous") is not None:
                records.append(active["previous"])
            pending = self.home / "pending.json"
            if os.path.lexists(pending):
                records.append(self._read_json(pending))
        except DistributionError:
            return None
        names = set()
        for record in records:
            name = record.get("directory") if isinstance(record, dict) else None
            if not isinstance(name, str) or not _RELEASE_DIR.fullmatch(name):
                return None
            names.add(name)
        return names

    def _log(self, text: str) -> None:
        try:
            self.update_log.parent.mkdir(parents=True, exist_ok=True)
            with self.update_log.open("a", encoding="utf-8", errors="replace") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    def retire_old_releases(self, *, deadline: float | None = None,
                            clock: Callable[[], float] = time.monotonic) -> list[str]:
        """Rename each release folder that nothing needs to .trash-<name>. Call it with manager.lock held.

        Kept: the active release, its previous one, the pending one, and the newest folder that is not
        ready (the evidence of a failed stage). A rename that fails is skipped: on Windows a file in use
        blocks it. Nothing is deleted here. It stops at the deadline, when one is given. It never raises."""
        retired: list[str] = []
        try:
            keep = self._referenced_releases()
            if keep is None or not _real_folder(self.releases):
                return retired
            found, unready = [], []
            for name in sorted(os.listdir(self.releases)):
                path = self.releases / name
                if not _RELEASE_DIR.fullmatch(name) or not _real_folder(path):
                    continue
                try:
                    modified = os.lstat(path).st_mtime
                except OSError:
                    continue
                found.append(name)
                if not os.path.lexists(path / ".odicto-ready.json"):
                    unready.append((modified, name))
            if unready:
                keep.add(max(unready)[1])
            for name in found:
                target = self.releases / (_TRASH + name)
                if name in keep or os.path.lexists(target):
                    continue
                if deadline is not None and clock() >= deadline:
                    break
                try:
                    os.rename(self.releases / name, target)
                except OSError:
                    continue
                retired.append(name)
                self._log(f"retired old release {name}")
        except Exception:
            pass
        return retired

    def delete_trash(self, budget: float = CLEANUP_BUDGET, *, deadline: float | None = None,
                     clock: Callable[[], float] = time.monotonic) -> int:
        """Delete the .trash-<release> folders until the budget (seconds) ends; a later run does the rest.

        Call it with manager.lock released. The time is checked before every file operation; one
        call into the file system that blocks is not interrupted. `deadline` replaces the budget
        when the retire step already used part of it. A link is removed itself, never followed.
        It never raises. Returns how many folders are gone."""
        deleted = 0
        try:
            if deadline is None:
                deadline = clock() + budget
            if not _real_folder(self.releases):
                return deleted
            for name in sorted(os.listdir(self.releases)):
                if (not name.startswith(_TRASH) or not _RELEASE_DIR.fullmatch(name[len(_TRASH):])
                        or not _real_folder(self.releases / name)):
                    continue
                if clock() >= deadline:
                    break
                try:
                    _delete_trash_folder(str(self.releases), name, deadline, clock)
                    deleted += 1
                except _BudgetEnded:
                    break
                except OSError:
                    continue
        except Exception:
            pass
        return deleted

    def _tidy_releases(self) -> None:
        """After a successful start: retire under manager.lock (skipped while another command holds it),
        then delete with the lock released. Both steps share one deadline."""
        deadline = time.monotonic() + CLEANUP_BUDGET
        try:
            with file_lock(self.home / "manager.lock"):
                self.retire_old_releases(deadline=deadline)
        except Exception:
            pass
        self.delete_trash(deadline=deadline)

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
        self.ensure_private_uv()
        uv = find_uv(self.home)
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
os.environ['ODICTO_LAUNCHER_HOME'] = str(Path(os.path.abspath(__file__)).parent)  # the home before a link is resolved
sys.argv = [str(script), '--home', str(home), *args]
runpy.run_path(str(script), run_name='__main__')
'''
        powershell_uv = "'" + uv.replace("'", "''") + "'"
        powershell = ("$ErrorActionPreference = 'Stop'\n"
                      "$launcher = Join-Path (Split-Path -Parent $PSScriptRoot) 'launcher.py'\n"
                      f"& {powershell_uv} run --python 3.12 --no-project --no-config $launcher @args\n"
                      "exit $LASTEXITCODE\n")
        shell = self._posix_launcher(uv)
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

    def _posix_launcher(self, uv: str) -> str:
        """The text of bin/odicto. Both paths are absolute: expose_command links ~/.local/bin/odicto to this
        file, and a path taken from $0 would then point into ~/.local."""
        return ("#!/bin/sh\nset -eu\n"
                f'exec {shlex.quote(uv)} run --python 3.12 --no-project --no-config '
                f'{shlex.quote(str(self.home / "launcher.py"))} "$@"\n')

    def _owned_launcher(self) -> bytes | None:
        """The bytes of bin/odicto when Odicto wrote it (its hash is in the marker), else None."""
        path = self.home / "bin" / "odicto"
        if path.is_symlink():
            return None
        contents = path.read_bytes()
        accepted = self._read_json(self.marker).get("launchers", {}).get("bin/odicto", [])
        if isinstance(accepted, str):
            accepted = [accepted]
        return contents if hashlib.sha256(contents).hexdigest() in accepted else None

    @staticmethod
    def _launcher_uv(contents: bytes) -> Path | None:
        """The uv that a launcher runs: the word after exec, in the line `exec <uv> run ...`."""
        try:
            for line in contents.decode("utf-8").splitlines():
                if line.startswith("exec "):
                    words = shlex.split(line)
                    if len(words) > 2 and words[2] == "run":
                        return _runnable(words[1])
        except ValueError:  # not UTF-8, or not shell words
            pass
        return None

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

    def install_shortcuts(self, folder: Path | None = None) -> list[str]:
        """Start Menu items (Windows) or application menu entries (Linux) that run the stable
        command, so no terminal is needed. Other systems have none. Returns one line per item."""
        if self.platform == "win32":
            return self._install_start_menu(folder)
        if self.platform.startswith("linux"):
            return self.install_menu(folder)
        return []

    def remove_shortcuts(self, folder: Path | None = None) -> list[str]:
        """Remove what install_shortcuts wrote, and only that. Returns one line per item."""
        if self.platform == "win32":
            return self._remove_start_menu(folder)
        if self.platform.startswith("linux"):
            return self.remove_menu(folder)
        return []

    def _remove_start_menu(self, programs: Path | None) -> list[str]:
        environment = dict(os.environ, ODICTO_LAUNCHER=str(self.home / "bin" / "odicto.cmd"),
                           ODICTO_PROGRAMS=str(programs) if programs else "",
                           ODICTO_SHORTCUTS=json.dumps(SHORTCUTS))
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", REMOVE_SHORTCUT_SCRIPT], env=environment, capture_output=True,
                              text=True, timeout=60)
        if done.returncode:
            raise DistributionError("Start Menu shortcuts were not removed: "
                                    + (done.stderr.strip().splitlines() or ["unknown error"])[-1])
        return [line for line in done.stdout.splitlines() if line.strip()]

    @staticmethod
    def _menu_folder() -> Path:
        base = os.environ.get("XDG_DATA_HOME", "")
        root = Path(base) if base and os.path.isabs(base) else Path.home() / ".local" / "share"
        return root / "applications"

    @staticmethod
    def _owned_file(path: Path) -> tuple[str, tuple[int, int]] | None:
        """(SHA-256, identity) of the plain file at path, read through one open handle.

        None for a missing file, a link, a folder, any other kind, or a file over 1 MiB. The identity
        (device, inode) is what a later delete or replace checks again, so that it acts on the file
        that was read and never on one that took its place."""
        try:
            descriptor = os.open(path, os.O_RDONLY | _BINARY | _NOFOLLOW)
        except OSError:
            return None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > 1024 * 1024:
                return None
            chunks = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            named = os.lstat(path)  # a link, or another file under the same name, is not what was opened
            if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                return None
            return hashlib.sha256(b"".join(chunks)).hexdigest(), (opened.st_dev, opened.st_ino)
        except OSError:
            return None
        finally:
            os.close(descriptor)

    @classmethod
    def _regular_digest(cls, path: Path) -> str | None:
        """The SHA-256 of a plain file, or None for a missing file, a link or any other kind."""
        found = cls._owned_file(path)
        return found[0] if found else None

    @staticmethod
    def _unlink_if_same(path: Path, identity: tuple[int, int]) -> bool:
        """Delete path only when it is still the file with this identity. True: it is gone.
        False: another file has taken its place, and nothing was deleted. A failed delete raises."""
        try:
            named = os.lstat(path)
        except FileNotFoundError:
            return True
        if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != identity:
            return False
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return True

    @staticmethod
    def _put(contents: bytes, path: Path, identity: tuple[int, int] | None) -> bool:
        """Write contents to path through a temporary file, renamed into place.

        identity None: nothing may exist at path, and a file that appeared meanwhile is never
        overwritten. Otherwise path must still be the file with that identity. False: the check
        failed and nothing was written."""
        temporary = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
        temporary.write_bytes(contents)
        try:
            if identity is None:
                try:
                    os.link(temporary, path)  # fails when anything exists at path, a dangling link too
                except FileExistsError:
                    return False
                except OSError:  # a file system without hard links
                    try:
                        with open(path, "xb") as handle:
                            handle.write(contents)
                    except FileExistsError:
                        return False
                return True
            try:
                named = os.lstat(path)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != identity:
                return False
            os.replace(temporary, path)
            return True
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _menu_text(self, name: str, command: str, text: str, launcher: Path, icon: Path | None) -> bytes:
        # The remove entry asks a question, so a terminal window must open for it.
        terminal = "true" if command.startswith("uninstall") else "false"
        lines = ["[Desktop Entry]", "Type=Application", f"Name={_desktop_text(name)}",
                 f"Comment={_desktop_text(text)}", f"Exec={_desktop_command(str(launcher))} {command}"]
        if icon:
            lines.append(f"Icon={_desktop_text(str(icon))}")
        lines += [f"Terminal={terminal}", "StartupNotify=false", "Categories=Utility;",
                  "Keywords=dictation;voice;speech;typing;"]
        return ("\n".join(lines) + "\n").encode("utf-8")

    def install_menu(self, folder: Path | None = None) -> list[str]:
        """Linux: application menu entries that run the stable command, so no terminal is needed.

        An entry that exists and is not one Odicto wrote (the marker keeps the hash of every
        version Odicto wrote) is kept, as a Windows shortcut that runs another program is.
        Each call writes the current text, so the entries follow the stable launcher. A file is
        replaced only when it is still the very file that was checked (`_put`).
        """
        launcher = self.home / "bin" / "odicto"
        if not launcher.is_file():
            raise DistributionError("The stable launcher is missing; menu entries were not created")
        marker = self._read_json(self.marker)
        folder = Path(folder) if folder else self._menu_folder()
        # A copy in the home, because a release directory goes away after later updates.
        icon = self.home / "odicto.png"
        current = self.active()
        source = self.release_path(current) / "assets" / "odicto-icon-256.png" if current else None
        present = icon.exists() or icon.is_symlink()
        seen = self._owned_file(icon)
        owned = seen is not None and seen[0] == marker.get("menu_icon")
        if (owned or not present) and source and source.is_file():
            contents = source.read_bytes()
            if self._put(contents, icon, seen[1] if owned else None):
                owned = True
                marker["menu_icon"] = hashlib.sha256(contents).hexdigest()
            else:
                owned = False  # another file took the name meanwhile: it is not used
        known = marker.get("menu_entries", {})
        if not isinstance(known, dict):
            known = {}
        lines, writes = [], []
        for name, command, text in SHORTCUTS:
            filename = MENU_FILES[name]
            contents = self._menu_text(name, command, text, launcher, icon if owned else None)
            digest = hashlib.sha256(contents).hexdigest()
            path = folder / filename
            identity = None
            if path.exists() or path.is_symlink():
                accepted = [*(known.get(filename) or []), digest]
                found = self._owned_file(path)
                if found is None or found[0] not in accepted:
                    lines.append(f"Kept {name} (it is not an Odicto file)")
                    continue
                identity = found[1]
            writes.append((name, path, contents, identity))
            known[filename] = list(dict.fromkeys([*(known.get(filename) or []), digest]))
        # The hash is recorded before the file exists, so an interrupted run never leaves an
        # entry that Odicto wrote and cannot recognise.
        marker["menu_entries"] = known
        atomic_json(self.marker, marker)
        folder.mkdir(parents=True, exist_ok=True)
        for name, path, contents, identity in writes:
            if self._put(contents, path, identity):
                lines.append(f"Menu: {name}")
            else:
                lines.append(f"Kept {name} (it is not an Odicto file)")
        return lines

    def refresh_menu(self, folder: Path | None = None) -> list[str]:
        """After an update or an upgrade: make the menu entries current. Linux only.

        A home from before the menu existed gets its entries now. A name that holds a file Odicto
        did not write is kept. A home whose launchers were deferred (no uv when they were due) gets
        none yet: the entries would run a missing command, and the refresh after the launchers
        are made adds them."""
        if not self.platform.startswith("linux") or not self.marker.is_file():
            return []
        if not (self.home / "bin" / "odicto").is_file():
            return []
        return self.install_menu(folder)

    def remove_menu(self, folder: Path | None = None) -> list[str]:
        """Linux: delete the menu entries that Odicto wrote. Any other file of that name stays.

        A file is deleted only when it is still the file that was hashed. When a delete fails, the
        others still run, and MenuRemovalError carries the lines so far."""
        known = self._read_json(self.marker).get("menu_entries", {})
        if not isinstance(known, dict):
            known = {}
        folder = Path(folder) if folder else self._menu_folder()
        lines, failures = [], []
        for name, _, _ in SHORTCUTS:
            filename = MENU_FILES[name]
            path = folder / filename
            if not (path.exists() or path.is_symlink()):
                continue
            found = self._owned_file(path)
            if found is None or found[0] not in (known.get(filename) or []):
                lines.append(f"Kept {name} (it is not an Odicto file)")
                continue
            try:
                if self._unlink_if_same(path, found[1]):
                    lines.append(f"Removed menu entry: {name}")
                else:
                    lines.append(f"Kept {name} (it is not an Odicto file)")
            except OSError as error:
                failures.append(f"{name} ({error.strerror or error})")
        if failures:
            raise MenuRemovalError("Could not remove the menu entry " + ", ".join(failures), lines)
        return lines

    def _install_start_menu(self, programs: Path | None = None) -> list[str]:
        """Windows: Start Menu items that run the stable command, so no terminal is needed.

        A shortcut that already exists and runs something else (a source checkout's
        shortcut, a user's own) is preserved. Returns one line per shortcut.
        """
        launcher = self.home / "bin" / "odicto.cmd"
        if not launcher.is_file():
            raise DistributionError("The stable launcher is missing; shortcuts were not created")
        # A copy in the home, because a release directory goes away after later updates.
        icon = self.home / "odicto.ico"
        current = self.active()
        source_icon = self.release_path(current) / "assets" / "odicto.ico" if current else None
        marker = self._read_json(self.marker) if self.marker.exists() else {}
        present = icon.exists() or icon.is_symlink()
        owned = (present and icon.is_file() and not icon.is_symlink()
                 and hashlib.sha256(icon.read_bytes()).hexdigest() == marker.get("shortcut_icon"))
        if (owned or not present) and source_icon and source_icon.is_file():
            contents = source_icon.read_bytes()
            temporary = icon.with_name(icon.name + ".new-" + uuid.uuid4().hex)
            temporary.write_bytes(contents)
            os.replace(temporary, icon)
            owned = True
            if self.marker.exists():
                marker["shortcut_icon"] = hashlib.sha256(contents).hexdigest()
                atomic_json(self.marker, marker)
        environment = dict(os.environ, ODICTO_LAUNCHER=str(launcher), ODICTO_HOME=str(self.home),
                           ODICTO_ICON=str(icon) if owned else "",
                           ODICTO_PROGRAMS=str(programs) if programs else "",
                           ODICTO_SHORTCUTS=json.dumps(SHORTCUTS))
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", SHORTCUT_SCRIPT], env=environment, capture_output=True,
                              text=True, timeout=60)
        if done.returncode:
            raise DistributionError("Start Menu shortcuts were not created: "
                                    + (done.stderr.strip().splitlines() or ["unknown error"])[-1])
        return [line for line in done.stdout.splitlines() if line.strip()]

    @staticmethod
    def _autostart_module():
        """app/managed_autostart.py next to this file: the active release's copy in a managed home."""
        import importlib.util
        path = Path(__file__).resolve().parents[2] / "app" / "managed_autostart.py"
        spec = importlib.util.spec_from_file_location("odicto_uninstall_autostart", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _read_marker_held(self, held: "_HeldHome") -> dict:
        """The marker, read through the held folder: the file that sits in the folder that was checked."""
        if held.fd is None:
            return self._read_json(self.marker)
        try:
            descriptor = os.open(self.marker.name, os.O_RDONLY | _NOFOLLOW, dir_fd=held.fd)
        except OSError as error:
            raise DistributionError(f"Invalid managed-install metadata: {self.marker.name}") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                raise DistributionError(f"Invalid managed-install metadata: {self.marker.name}")
            data = os.read(descriptor, 64 * 1024 + 1)
        finally:
            os.close(descriptor)
        try:
            value = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise DistributionError(f"Invalid managed-install metadata: {self.marker.name}") from error
        if not isinstance(value, dict):
            raise DistributionError(f"Invalid managed-install metadata: {self.marker.name}")
        return value

    @contextmanager
    def _hold_home(self, requested: Path | None):
        """Check the home and keep the very folder that was checked open until the block ends.

        Yields None when nothing is installed there. Raises unless the home is a real folder that
        Odicto made: not a link (`requested` is the path as the user gave it; the manager has
        already resolved `self.home`), with a marker that names this very folder, never the user's
        own home folder or a folder above it.

        Everything after the check goes through the held folder, so a program that swaps the home
        or a folder above it afterwards cannot make the delete follow a link. POSIX: an open
        folder (O_NOFOLLOW) and calls relative to it. Windows: a handle that forbids a rename of
        this folder and of every folder above it (`_held_folder`)."""
        given = Path(requested).expanduser() if requested is not None else self.home
        if not os.path.lexists(given) and not os.path.lexists(self.home):
            yield None
            return
        refused = f"{given} is not an Odicto folder, so nothing was removed"
        if not _real_folder(given) or not _real_folder(self.home):
            raise DistributionError(f"{refused}: it is a link or not a folder.")
        with ExitStack() as stack:
            if os.name == "nt":
                if not stack.enter_context(_held_folder(str(self.home))):
                    raise DistributionError(f"{refused}: it is a link or not a folder.")
                info = os.lstat(self.home)
                held = _HeldHome(None, (info.st_dev, info.st_ino))
            else:
                try:
                    descriptor = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                except OSError as error:
                    raise DistributionError(f"{refused}: it cannot be opened as a folder ({error.strerror}).") from error
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                for path in (given, self.home):
                    try:
                        named = os.lstat(path)
                    except OSError as error:
                        raise DistributionError(f"{refused}: it changed while it was checked.") from error
                    if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                        raise DistributionError(f"{refused}: it changed while it was checked.")
                held = _HeldHome(descriptor, (info.st_dev, info.st_ino))
            try:
                value = self._read_marker_held(held)
            except DistributionError as error:
                raise DistributionError(f"{refused}: its Odicto marker is missing or unreadable ({error}).") from error
            if value.get("schema") != 1 or value.get("home") != str(self.home):
                raise DistributionError(f"{refused}: its Odicto marker names another folder.")
            try:
                user = Path.home().resolve()
            except (OSError, RuntimeError):
                user = None
            if self.home.parent == self.home or (user is not None and (user == self.home or self.home in user.parents)):
                raise DistributionError(f"{refused}: it holds your own home folder.")
            held.marker = value
            yield held

    def _stop_for_uninstall(self) -> None:
        """Stop Odicto through the managed stop, with manager.lock and launch.lock already held by
        the caller (the managed `run` would take them again and find itself busy).

        Fail closed: a release record that cannot be read, or a stop that fails, ends the uninstall
        before it deletes anything."""
        try:
            current = self.active()
            directory = self.release_path(current) if current else None
        except DistributionError as error:
            raise DistributionError(f"Odicto cannot tell whether it is running, because its release record is "
                                    f"not readable ({error}). Nothing was removed. Run the Odicto installer "
                                    "again to repair it, then run odicto uninstall.") from error
        if directory is None:
            return
        self._recover_launch_intent()
        code = subprocess.run([str(self._python(directory)), str(directory / "odicto.py"), "stop"],
                              cwd=directory, env=self.environment()).returncode
        if code:
            raise DistributionError(f"Odicto did not stop (exit {code}). Nothing was removed. "
                                    "Quit it, then run odicto uninstall again.")

    def _remove_command(self, directory: Path | None) -> str:
        """Delete the `odicto` command that expose_command wrote, if it is still Odicto's.
        Returns a line to show, or an empty string when there was none. A failed delete raises OSError."""
        directory = Path(directory) if directory else Path.home() / ".local" / "bin"
        if os.name == "nt":
            command = directory / "odicto.cmd"
            target = str(self.home / "bin" / "odicto.cmd").replace("%", "%%")
            found = self._owned_file(command)
            ours = found is not None and found[0] == hashlib.sha256(f'@"{target}" %*\r\n'.encode()).hexdigest()
        else:
            command = directory / "odicto"
            ours = command.is_symlink() and command.resolve() == (self.home / "bin" / "odicto").resolve()
        if not (command.exists() or command.is_symlink()):
            return ""
        if not ours:
            return f"Kept {command} (it is not Odicto's command)"
        command.unlink()
        return f"Removed the command {command}"

    def _delete_children(self, held: "_HeldHome", relative: str, *, skip: frozenset = frozenset(),
                         only: frozenset | None = None) -> list[tuple[str, OSError]]:
        """Delete the entries of the held home (or of its folder `relative`) except the names in
        skip, or only the names in only. A link is removed itself, never followed. An entry that
        cannot go is returned, not raised."""
        failures: list[tuple[str, OSError]] = []

        def attempt(name, action):
            try:
                action()
            except OSError as error:
                failures.append((name, error))

        def wanted(name):
            return name not in skip and (only is None or name in only)

        never = float("inf")
        if os.name == "nt":
            folder = self.home / relative if relative else self.home
            if relative and not _real_folder(folder):
                return failures  # missing, or a link: the home's own listing removes a link itself
            with (_held_folder(str(folder)) if relative else ExitStack()) as real:
                if relative and not real:
                    return failures
                with os.scandir(folder) as entries:
                    children = [(entry.name, entry.path, entry.is_dir(follow_symlinks=False)) for entry in entries]
                for name, child, is_folder in children:
                    if wanted(name):
                        attempt(name, (lambda child=child: _delete_held(child, never, time.monotonic)) if is_folder
                                else (lambda child=child: _unlink(child)))
            return failures
        if relative:
            try:
                descriptor = os.open(relative, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=held.fd)
            except OSError:
                return failures  # missing, or a link: the home's own listing removes a link itself
        else:
            descriptor = held.fd
        try:
            with os.scandir(descriptor) as entries:
                children = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
            for name, is_folder in children:
                if wanted(name):
                    attempt(name, (lambda name=name: _delete_in(descriptor, name, never, time.monotonic)) if is_folder
                            else (lambda name=name: _in_folder(os.unlink, name, descriptor)))
        finally:
            if relative:
                os.close(descriptor)
        return failures

    def _write_json_held(self, held: "_HeldHome", name: str, value: dict) -> None:
        """atomic_json for a file of the held home. POSIX: relative to the open folder."""
        if held.fd is None:
            atomic_json(self.home / name, value)
            return
        temporary = name + ".tmp-" + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=held.fd)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=held.fd, dst_dir_fd=held.fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=held.fd)
            except OSError:
                pass
            raise

    def _remove_empty_home(self, held: "_HeldHome") -> None:
        """Remove the home folder itself, after the held folder is closed. Only the very folder that
        was checked is removed, and only when it is empty."""
        if os.name == "nt":
            if _real_folder(self.home):
                os.rmdir(self.home)
            return
        parent = os.open(self.home.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                named = os.stat(self.home.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if (named.st_dev, named.st_ino) == held.identity:
                os.rmdir(self.home.name, dir_fd=parent)
        finally:
            os.close(parent)

    def _refuse_while_removed(self) -> None:
        """Start, update and rollback do not run while an uninstall has begun and not finished."""
        if os.path.lexists(self.home / REMOVAL_NOTICE):
            raise DistributionError("Odicto is being removed from this computer. Run odicto uninstall again "
                                    "to finish, or run the Odicto installer to put it back.")

    def clear_removal_notice(self) -> None:
        """Delete the notice of a failed uninstall. The caller holds manager.lock: no uninstall runs now."""
        try:
            (self.home / REMOVAL_NOTICE).unlink()
        except FileNotFoundError:
            pass

    def _take_locks(self, locks: ExitStack) -> None:
        """Hold manager.lock and launch.lock until the stack closes; a start, an update and a rollback
        need them and find them busy."""
        for name in _LOCK_FILES:
            try:
                locks.enter_context(file_lock(self.home / name))
            except BusyError as error:
                raise BusyError("Odicto is busy with another command (a start or an update). Wait for it to "
                                "finish, then run odicto uninstall again. Nothing was removed.") from error

    def _take_runtime_locks(self, locks: ExitStack) -> None:
        """After the stop: Setup's writer lock and the app's instance lock must be free, and stay held."""
        if not _real_folder(self.data):
            return
        for name, message in (("setup.lock", "Odicto Setup is open. Close its page, then run odicto uninstall "
                                             "again. Nothing was removed."),
                              ("dictation.lock", "Odicto is still running. Quit it, then run odicto uninstall "
                                                 "again. Nothing was removed.")):
            try:
                locks.enter_context(file_lock(self.data / name))
            except BusyError as error:
                raise BusyError(message) from error

    def uninstall(self, *, everything: bool = False, ask: Callable[[], str | None] | None = None,
                  requested: Path | None = None, stop: Callable[[], None] | None = None,
                  say: Callable[[str], None] = print, programs: Path | None = None,
                  command_directory: Path | None = None) -> bool:
        """Remove this install: stop it, take out what it put in the user's account, then the home.

        Outside the home, only an item that Odicto wrote is deleted: its start-at-sign-in entry,
        its Start Menu or application menu entries, its `odicto` command. The settings (the
        `data` folder) stay unless `everything`. `ask` replaces `everything`: it returns "keep",
        "all", or None to cancel. `requested` is the home as the user gave it, so that a link is
        refused before it is resolved. Returns False when something stayed.

        The validated home is held open from the check to the end, and manager.lock and
        launch.lock are held for the whole run, so a start or an update cannot interleave. The
        home gets a removal notice before anything is deleted, and keeps it until the end: a
        failed removal can be run again, and nothing else starts in between.
        """
        locks = ExitStack()
        try:
            with self._hold_home(requested) as held:
                if held is None:
                    say(f"Odicto is not installed in {self.home}. Nothing was removed.")
                    return True
                # Windows admin mode owns a logon task that only the Setup switch removes. Nothing is
                # touched first, and nothing here runs schtasks or the admin helper.
                if self.admin_enabled():
                    raise DistributionError("Admin mode is on. Open Odicto Setup, turn off admin mode, then run "
                                            "odicto uninstall again. Nothing was removed.")
                self._migration_guard()
                if ask is not None:
                    answer = ask()
                    if answer is None:
                        say("Nothing was removed.")
                        return True
                    everything = answer == "all"
                again = "odicto uninstall --all" if everything else "odicto uninstall"
                self._take_locks(locks)
                (stop or self._stop_for_uninstall)()
                say("Odicto is stopped.")
                runtime = locks.enter_context(ExitStack())
                self._take_runtime_locks(runtime)
                self._write_json_held(held, REMOVAL_NOTICE, {"schema": 1, "home": str(self.home), "pid": os.getpid()})
                failed = False
                try:
                    removed = self._autostart_module().remove(self.home)
                    say("Removed start at sign-in." if removed else "Start at sign-in was off.")
                except (RuntimeError, ValueError) as error:  # not Odicto's, or a receipt that cannot be read
                    say(f"Kept the start-at-sign-in entry: {error}")
                except (OSError, subprocess.SubprocessError) as error:
                    failed = True
                    say(f"Could not remove the start-at-sign-in entry: {getattr(error, 'strerror', None) or error}")
                try:
                    for line in self.remove_shortcuts(programs):
                        say(line)
                except MenuRemovalError as error:
                    failed = True
                    for line in error.lines:
                        say(line)
                    say(f"{error}.")
                except (DistributionError, OSError, ValueError, subprocess.SubprocessError) as error:
                    failed = True
                    say(f"Could not remove the menu entries: {error}")
                try:
                    line = self._remove_command(command_directory)
                    if line:
                        say(line)
                except (OSError, RuntimeError) as error:
                    failed = True
                    say(f"Could not remove the odicto command: {getattr(error, 'strerror', None) or error}")
                if failed:
                    # The receipts stay in the home, so the next run still knows what is Odicto's.
                    say(f"Nothing else was removed. Close the program that uses the item above, then run {again} again.")
                    return False

                def stayed(failures) -> bool:
                    for name, error in failures:
                        say(f"Could not remove {name}: {error.strerror or error}")
                    if failures:
                        say(f"Close the program that uses it, then run {again} again.")
                        if not everything:
                            say(f"Keep the folder {self.data}: it holds your settings. Do not delete the folder "
                                f"{self.home}.")
                    return bool(failures)

                marker = self.marker.name
                keep = frozenset({marker, REMOVAL_NOTICE, "data", *_LOCK_FILES})
                # The files that run this command again go last, so that a failed removal can be run again.
                if stayed(self._delete_children(held, "", skip=keep | {*_RETRY_FILES, "releases"})):
                    return False
                active = (self._read_json_quietly(self.pointer) or {}).get("directory")
                active = active if isinstance(active, str) and _RELEASE_DIR.fullmatch(active) else None
                if stayed(self._delete_children(held, "releases", skip=frozenset({active} if active else ()))):
                    return False
                if everything:
                    runtime.close()  # the lock files of `data` go with it
                    if stayed(self._delete_children(held, "", only=frozenset({"data"}))):
                        return False
                if stayed(self._delete_children(held, "", only=frozenset({"releases", *_RETRY_FILES}))):
                    return False
                locks.close()
                # Last: whatever a command that began before the notice made again, the lock files, the
                # marker, then the removal notice, so that nothing starts in between.
                sweep = frozenset({marker, REMOVAL_NOTICE} | (set() if everything else {"data"}))
                if stayed(self._delete_children(held, "", skip=sweep)):
                    return False
                if everything:
                    if stayed(self._delete_children(held, "", only=frozenset({marker}))):
                        return False
                else:
                    self._write_json_held(held, marker, {"schema": 1, "home": str(self.home),
                                                         "automatic_updates": held.marker.get("automatic_updates", True)})
                if stayed(self._delete_children(held, "", only=frozenset({REMOVAL_NOTICE}))):
                    return False
            if everything:
                try:
                    self._remove_empty_home(held)
                except OSError as error:
                    say(f"Could not remove the folder {self.home}: {error.strerror or error}")
                    return False
                say("Removed Odicto and its settings.")
            else:
                say("Removed Odicto.")
                say(f"Your settings are kept in {self.data}. They hold your keys and your prompt.")
                say("Install Odicto again to use them, or delete that folder to remove them too.")
            return True
        finally:
            locks.close()

    def _read_json_quietly(self, path: Path) -> dict | None:
        try:
            return self._read_json(path)
        except DistributionError:
            return None

    def run(self, command: list[str], *, automatic_updates: bool = True) -> int:
        if not command or command[0] not in {"start", "setup", "stop", "restart", "status", "config", "version", "wait-ready", "autostart", "remove-autostart", "--help", "-h"}:
            raise DistributionError("Unsupported managed Odicto command")
        self.launch_notice = ""
        requested = command[0]  # an admin restart is rewritten to start below
        self.initialize()
        if command[0] not in {"status", "version", "stop", "wait-ready", "--help", "-h"}:
            self._migration_guard()
        with ExitStack() as launch:
            with file_lock(self.home / "manager.lock"):
                if command[0] not in {"status", "version", "stop", "wait-ready", "--help", "-h"}:
                    self._migration_guard()
                    self._refuse_while_removed()
                before = self.active()
                if automatic_updates and command[0] in {"start", "setup"}:
                    try:
                        self.update(force_check=False)
                    except Exception as error:
                        if before is None:
                            raise
                        detail = (f" Details: {self.update_log}"
                                  if isinstance(error, subprocess.CalledProcessError) else "")
                        print(f"Update deferred ({type(error).__name__}); keeping {before['version']}.{detail}",
                              file=sys.stderr)
                current = self.active()
                if current is None:
                    raise DistributionError("No active release; run install first")
                directory = self.release_path(current)
                # A start that follows an activation must reach full readiness, or the rollback
                # below applies. Any other start also accepts an owner that waits for a
                # microphone or its speech model.
                strict = before is not None and current["directory"] != before["directory"]
                if command[0] in {"start", "setup", "restart", "stop"}:
                    launch.enter_context(file_lock(self.home / "launch.lock"))
                    if command[0] == "stop" and not self.admin_enabled():
                        self._recover_launch_intent()
            python = self._python(directory)
            admin = self.admin_enabled()
            if (not admin and sys.platform == "win32" and command[0] in {"start", "restart"}
                    and self.support().elevated(directory)):
                raise DistributionError("Admin mode is off. Run odicto start from a normal terminal")
            if admin and command[0] in {"stop", "restart"}:
                result = self.support().stop(self, directory)
                if not result:
                    try:
                        self._recover_launch_intent()
                    except BusyError:
                        if command[0] != "stop":
                            raise
                        print("Odicto stopped; startup supervisor cleanup still defers another launch.", file=sys.stderr)
                if result or command[0] == "stop":
                    return result
                command = ["start", *command[1:]]
            if admin and command[0] == "start" and not self.support().elevated(directory):
                result = self._launch_admin(directory, python, strict)
            elif command[0] in {"start", "setup", "restart"}:
                result = self._launch(directory, python, command[0], strict=strict)
            else:
                result = subprocess.run([str(python), str(directory / "odicto.py"), *command],
                                        cwd=directory, env=self.environment()).returncode
        if result == 0 and self.launch_notice in _LAUNCH_NOTICES:
            print(_LAUNCH_NOTICES[self.launch_notice], file=sys.stderr)
        if result == EXIT_ANOTHER_OWNER and command[0] in {"start", "restart"}:
            print("Another copy of Odicto is running. Quit it, then start Odicto again.", file=sys.stderr)
        elif result and command[0] == "start" and before and current["directory"] != before["directory"]:
            try:
                with file_lock(self.home / "manager.lock"):
                    if self.active()["directory"] == current["directory"]:
                        self.rollback()
                print("The new release did not start; the previous version is active again.", file=sys.stderr)
            except BusyError:
                print("The new runtime still owns its lock; rollback was deferred.", file=sys.stderr)
        elif result == 0 and requested == "start":
            self._tidy_releases()
            self._refresh_launchers()
        return result

    def _refresh_launchers(self) -> None:
        """After a successful start on macOS or Linux: a home that an older manager set up gets its private
        uv copy and launchers that work through the ~/.local/bin/odicto link (the old text took its folder
        from $0).

        The source is the uv that ran this start: UV, which uv sets for the processes it starts, else the
        uv named in the launcher, which Odicto wrote (its hash is in the marker). ODICTO_BOOTSTRAP_UV is
        not read here: it belongs to the bootstrap's process, and a copy of it in a shell would be stale.
        A launcher that Odicto did not write is kept. The launcher is rewritten only after the private copy
        exists, so a failed copy leaves a launcher that works. Best effort: the start has already
        succeeded, nothing here may change its result, and a later start tries again."""
        if not _posix_host():
            return
        try:
            owned = self._owned_launcher()
            if owned is None:
                return
            private = _private_uv(self.home)
            if private is not None and owned == self._posix_launcher(str(private)).encode():
                return
            source = _runnable(os.environ.get("UV") or "") or self._launcher_uv(owned)
            if source is None and private is None:
                return
            with file_lock(self.home / "manager.lock"):
                if self.ensure_private_uv(source) is None:
                    return
                self.install_launchers()
            self._log("Launchers refreshed after the start")
        except Exception as error:
            self._log(f"Launchers were not refreshed: {error}")

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

    def _sweep_admin_results(self) -> None:
        """Delete result files that earlier admin launches left behind. The caller holds launch.lock,
        so no launch of this home is in flight; a result file only matters to the launch that made it."""
        try:
            for path in self.home.glob("admin-result-*.json"):
                if (re.fullmatch(r"admin-result-[a-f0-9]{32}\.json", path.name)
                        and path.is_file() and not path.is_symlink()):
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def _launch_admin(self, directory: Path, python: Path, strict: bool = True) -> int:
        self._sweep_admin_results()
        self._recover_launch_intent()
        state = self._started_state(directory, python, strict=strict)
        if state != "none":
            self.launch_notice = state if state in _WAITING_CODES else ""
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
                                  "launcher_pid": os.getpid(), "state": "pending", "strict": bool(strict)})
        try:
            self.support().start_task(self.home, directory)
            deadline = time.monotonic() + STARTUP_TIMEOUT + CANCEL_TIMEOUT
            while time.monotonic() < deadline:
                if result_path.exists():
                    try:
                        result = self._read_json(result_path)
                    finally:
                        try:
                            result_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    if result.get("token") != token or type(result.get("code")) is not int:
                        raise DistributionError("Administrator startup result is invalid")
                    notice = result.get("notice")
                    self.launch_notice = notice if isinstance(notice, str) and notice in _WAITING_CODES else ""
                    if result["code"]:
                        print(f"Odicto did not start through admin mode (code {result['code']}); "
                              f"see {self.data / 'dictation.log'}.", file=sys.stderr)
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
            # A request without the field comes from a launcher that predates it: it stays strict.
            strict = value.get("strict") is not False
            self.launch_notice = ""
            state = self._started_state(directory, self._python(directory), strict=strict)
            if state != "none":
                with file_lock(self.home / "launch-intent.lock"):
                    current = self._read_json(request)
                    if (current.get("token") != token or current.get("state") != "claimed"
                            or not self._pid_alive(current.get("launcher_pid"))):
                        raise DistributionError("Administrator startup was cancelled")
                    current["state"] = "released"
                    atomic_json(request, current)
                    (self.home / "launch-intent.json").unlink()
                result = 0
                self.launch_notice = state if state in _WAITING_CODES else ""
            else:
                result = self._launch(directory, self._python(directory), "start", reservation=reservation,
                                      strict=strict)
            payload = {"token": token, "code": result}
            if result == 0 and self.launch_notice in _WAITING_CODES:
                payload["notice"] = self.launch_notice
            atomic_json(self.home / ("admin-result-" + token + ".json"), payload)
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
            request_path = self.home / "admin-request.json"
            if request_path.exists():
                request_owner = self._read_json(request_path)
                if (request_owner.get("token") == token
                        and self._pid_alive(record.get("supervisor_pid"))):
                    raise BusyError("The administrator startup supervisor still owns its reservation")
            if receipt.exists():
                child = self._read_json(receipt)
                safe = child.get("token") == token and (
                    child.get("state") == "setup-owned" or not self._pid_alive(child.get("pid")))
                if not safe and child.get("token") == token and child.get("state") == "owner-starting":
                    active = self.active()
                    if active and active["directory"] == record.get("directory"):
                        directory = self.release_path(active)
                        request = request_path
                        admin_release = None
                        if request.exists():
                            state = self._read_json(request)
                            if state.get("token") == token:
                                admin_release = (
                                    state.get("schema") == 1 and state.get("home") == str(self.home)
                                    and state.get("directory") == record.get("directory")
                                    and state.get("supervisor_pid") == record.get("supervisor_pid")
                                    and state.get("state") == "released"
                                )
                        if admin_release is not None:
                            safe = admin_release
                        else:
                            # A launcher killed after it handed off may have left an owner that
                            # still waits for a microphone or its speech model. It must not block
                            # `odicto stop` and updates for ever, but a waiting owner counts only
                            # with proof of the hand-off; a ready owner is the rule from before.
                            state = self._started_state(directory, self._python(directory), token, strict=False)
                            safe = state == "ready" or (state != "none" and self._outlived_its_launcher(record, child))
            else:
                safe = not self._pid_alive(record.get("supervisor_pid")) and (
                    record.get("child_pid") is None or not self._pid_alive(record.get("child_pid")))
            if not safe:
                raise BusyError("A managed startup has not confirmed exit; activation and rollback remain deferred")
            intent.unlink()
            receipt.unlink(missing_ok=True)

    def _outlived_its_launcher(self, record: dict, child: dict) -> bool:
        """True when the child still runs a moment after its supervising launcher is gone.

        Without the hand-off byte the child exits when the launcher's pipe closes
        (`odicto.py`, `_managed_startup_guard`), so a survivor was handed off."""
        if self._pid_alive(record.get("supervisor_pid")):
            return False
        time.sleep(HANDOFF_SETTLE)
        return self._pid_alive(child.get("pid"))

    def _startup_state(self, directory: Path, python: Path, token: str = "") -> str:
        """`ready`, `mic`, `speech`, `speech-download` or `none`, from one `wait-ready` probe.

        A waiting state counts only when the exit code and the `ODICTO-WAITING` line agree. A probe
        that cannot run, times out, or ends with any other code is `none`."""
        command = [str(python), str(directory / "odicto.py"), "wait-ready", "--timeout", "0",
                   "--report-waiting", "--expected-root", str(directory)]
        if token:
            command += ["--launch-token", token]
        try:
            done = subprocess.run(command, cwd=directory, env=self.environment(), stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=15, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return "none"
        if done.returncode == 0:
            return "ready"
        text = getattr(done, "stdout", None)
        if done.returncode not in _WAITING_CODES.values() or not isinstance(text, str):
            return "none"
        marks = [words for words in (line.split() for line in text.splitlines())
                 if words[:1] == [_WAITING_MARKER]]
        if len(marks) == 1 and len(marks[0]) == 2 and _WAITING_CODES.get(marks[0][1]) == done.returncode:
            return marks[0][1]
        return "none"

    def _ready(self, directory: Path, python: Path, token: str = "") -> bool:
        """The strict question: is the owner fully ready. A waiting owner is kept in `_waiting`
        for `_started_state`, so one probe answers both questions."""
        state = self._startup_state(directory, python, token)
        self._waiting = state if state in _WAITING_CODES else "none"
        return state == "ready"

    def _started_state(self, directory: Path, python: Path, token: str = "", *, strict: bool = True) -> str:
        """What counts as started: `ready`, or (not strict only) a waiting state; else `none`."""
        self._waiting = "none"
        if self._ready(directory, python, token):
            return "ready"
        return "none" if strict else self._waiting

    def _launch(self, directory: Path, python: Path, command: str, *, reservation: dict | None = None,
                strict: bool = True) -> int:
        intent = self.home / "launch-intent.json"
        if reservation is None:
            self._recover_launch_intent()
        if command == "restart":
            result = subprocess.run([str(python), str(directory / "odicto.py"), "stop"],
                                    cwd=directory, env=self.environment()).returncode
            if result:
                return result
            command = "start"
        if reservation is None and command == "start":
            state = self._started_state(directory, python, strict=strict)
            if state != "none":
                self.launch_notice = state if state in _WAITING_CODES else ""
                return 0
        token = reservation["token"] if reservation else uuid.uuid4().hex
        receipt = self.home / ("launch-receipt-" + token + ".json")
        if reservation is None:
            atomic_json(intent, {"token": token, "directory": directory.name,
                                 "supervisor_pid": os.getpid(), "child_pid": None, "receipt": receipt.name})
        self.launch_notice = ""
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
            began = time.monotonic()
            deadline = began + STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                if reservation is not None:
                    state = self._read_json(self.home / "admin-request.json")
                    if state.get("state") != "claimed" or not self._pid_alive(state.get("launcher_pid")):
                        return 1
                status = process.poll()
                if status is not None:
                    confirmed = True
                    if command == "start" and status != EXIT_ANOTHER_OWNER:
                        print(f"Odicto exited during startup (code {status}); see {self.data / 'dictation.log'}.",
                              file=sys.stderr)
                    return status if status else (0 if command == "setup" else 1)
                owned_setup = False
                accepted = "none"
                if command == "setup" and receipt.exists():
                    state = self._read_json(receipt)
                    owned_setup = state.get("token") == token and state.get("state") == "setup-owned"
                if command == "start":
                    accepted = self._started_state(directory, python, token, strict=strict)
                    if accepted == "speech-download" and time.monotonic() - began < DOWNLOAD_ACCEPT_SECONDS:
                        accepted = "none"
                if owned_setup or accepted != "none":
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
                    self.launch_notice = accepted if accepted in _WAITING_CODES else ""
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


class _Parser(argparse.ArgumentParser):
    """Errors name the way to the full command list instead of a bare usage line."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"odicto: error: {message}\nRun 'odicto --help' to see every command.\n")


class _HelpShown(Exception):
    """Help was printed. The caller must not run the command."""


class _HelpAction(argparse.Action):
    """-h / --help, and every prefix argparse accepts (--h, --he, --hel).

    It raises instead of exiting, so a caller can tell "help was shown" from "the
    arguments are valid" without comparing strings with the spellings it knows.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help()
        raise _HelpShown


class _AppParser(_Parser):
    """The parser of the application commands. Its subcommands are built from this class too."""

    def __init__(self, **kwargs):
        kwargs["add_help"] = False
        super().__init__(**kwargs)
        self.add_argument("-h", "--help", action=_HelpAction, help="show this help message and exit")


def _guide(home: Path) -> str:
    """The text of `odicto --help`: every command, grouped by what you want to do."""
    command = home / "bin" / ("odicto.cmd" if os.name == "nt" else "odicto")
    return f"""\
Odicto - voice dictation for your desktop.

Everyday commands:
  odicto setup             Open the settings page in your browser
  odicto start             Start dictation in the background
  odicto stop              Stop dictation
  odicto restart           Stop, then start again (no update check)
  odicto config            Show the main settings and where each value came from
  odicto version           Show the Odicto version
  odicto run status        Show runtime status: running, microphone, problems

Updates:
  odicto update            Apply a ready update, or check for a newer one
  odicto check             Print the latest release tag; change nothing
  odicto stage             Download and verify a version without switching to it
  odicto rollback          Go back to the previous version
  odicto auto-updates on|off
                           Turn the automatic update check on or off
  odicto status            Show the installed and the previous release

Start at login:
  odicto autostart         Start Odicto when you sign in
  odicto remove-autostart  Stop starting Odicto when you sign in

Remove Odicto:
  odicto uninstall         Stop Odicto and remove it. Your settings stay in a folder
                           whose path the command prints.
  odicto uninstall --all   The same, and delete your settings too

Windows only:
  odicto admin on|off      Let Odicto type into windows that run as administrator.
                           When the mode changes, Windows asks you to approve it and
                           dictation is left stopped: run 'odicto start' afterwards.

Moving from a source checkout:
  odicto migrate --source PATH --check    Look first; changes nothing
  odicto migrate --source PATH            Apply, or resume, the move

Other:
  odicto help [command]    Show this guide, or the help for one command
  odicto <command> --help  Show the help for one command. It never runs the command.
  odicto install           Installs or repairs the managed release (the installer runs it)
  odicto run <command>     The same as 'odicto <command>' for setup, start, stop, ...
                           Add --no-auto-updates before the command to skip the
                           update check:  odicto run --no-auto-updates start

Default shortcuts (change them in 'odicto setup'):
  Ctrl+`        dictate: tap, speak, tap again
  Ctrl+Shift+`  ask the AI: tap, speak, tap again
  F7            live dictation: tap to start, tap to finish
  F8            polish the selected text
  Esc           cancel while processing

Good to know:
  'odicto' with no command is the same as 'odicto start'.
  Change the indicator's size, colour and look on the Appearance page of 'odicto setup'.
  Settings and keys are kept when Odicto updates.
  If your shell says 'odicto' is not found, run it by its full path:
    {command}
  or add that folder to your PATH.
"""


def _app_parser() -> argparse.ArgumentParser:
    """Checks arguments of the commands the application runs (`odicto start`, ...).

    This runs before anything starts or stops, so `odicto stop --help` shows help
    and `odicto stop now` is refused. It must accept every form the manager itself
    passes to the application (for example the hidden wait-ready options).
    """
    parser = _AppParser(prog="odicto", usage="odicto <command> [options]",
                        description="Commands that run Odicto itself.",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>", prog="odicto")

    def add(name: str, summary: str, detail: str = "") -> argparse.ArgumentParser:
        return sub.add_parser(name, help=summary, description=f"{summary}.\n\n{detail}".strip(),
                              formatter_class=argparse.RawDescriptionHelpFormatter)

    add("setup", "Open the settings page in your browser",
        "The page runs on this computer only, usually at http://127.0.0.1:8765. If that\n"
        "port is busy, Odicto opens and prints another address. Choose the speech engine,\n"
        "microphone, keys, vocabulary, Polish and shortcuts. Closing the page stops its\n"
        "server. Dictation is a separate background app. Odicto checks for an update first\n"
        "when one is due.")
    add("start", "Start dictation in the background",
        "Odicto runs in the background and shows a small indicator at the bottom of the\n"
        "screen. This command returns when Odicto is ready. It also returns when Odicto\n"
        "runs but still waits for a microphone or its speech model, and says which. If\n"
        "Odicto already runs, nothing changes. Odicto checks for an update first when one\n"
        "is due. Run only one Odicto install at a time: two installs would each hook the\n"
        "keyboard.")
    add("restart", "Stop Odicto, then start it again",
        "Like 'odicto stop' followed by 'odicto start', except that it does not check for\n"
        "an update. A ready update is applied by 'odicto start' or 'odicto update'.")
    add("stop", "Stop dictation",
        "Stops every Odicto process of this install. It never updates first.")
    add("status", "Show runtime status",
        "Shows whether Odicto runs, whether the microphone is ready, and any problem.\n"
        "Use it as 'odicto run status'. Plain 'odicto status' shows the installed release.")
    ready = add("wait-ready", "Wait until Odicto and the microphone are ready",
                "Exits 0 when ready, or 1 when the time runs out.")
    ready.add_argument("--timeout", type=float, default=30.0,
                       help="seconds to wait (default 30)")
    ready.add_argument("--launch-token", help=argparse.SUPPRESS)
    ready.add_argument("--expected-root", help=argparse.SUPPRESS)
    ready.add_argument("--report-waiting", action="store_true", help=argparse.SUPPRESS)
    add("config", "Show the main settings and where each value came from",
        "Shows the main settings, their values, and whether each value is a default or\n"
        "comes from your settings file. It is not a full list: some values are only\n"
        "summarized. A key shows only as <set>. It changes nothing. Use 'odicto setup'\n"
        "to change a setting.")
    add("version", "Show the Odicto version")
    add("autostart", "Start Odicto when you sign in",
        "Adds one startup entry for this install. Undo it with 'odicto remove-autostart'.")
    add("remove-autostart", "Stop starting Odicto when you sign in",
        "Removes the startup entry of this install.")
    return parser


def _show_help(parser: argparse.ArgumentParser, argv: list[str]) -> int:
    """Print help (or the argument error) for ``argv``. Nothing is run."""
    try:
        parser.parse_args(argv)
    except _HelpShown:
        return 0
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 1
    return 0


def _app_args_refused(argv: list[str]) -> int | None:
    """None when ``argv`` is a valid application command that may run.

    Otherwise the exit code: help or the argument error was already printed, and
    nothing may start, stop or change. Help is recognised by the parser itself, so
    every spelling it accepts (``-h``, ``--help``, ``--h``, ``--hel``) counts.
    """
    try:
        _app_parser().parse_args(argv)
    except _HelpShown:
        return 0
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) and stop.code else 2
    return None


def _ask_about_settings(read: Callable[[str], str] = input, show: Callable[[str], None] = print) -> str | None:
    """The one question of `odicto uninstall --ask`: "keep", "all", or None (cancel).

    A terminal is required, so that an answer is never taken from nothing."""
    show("Remove Odicto from this computer.")
    show("Your settings stay by default: your keys and your prompt. A new install uses them again.")
    for _ in range(3):
        try:
            answer = read("Press Enter to remove Odicto and keep your settings. Type DELETE to remove your "
                          "settings too. Type N to cancel: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in ("", "keep"):
            return "keep"
        if answer in ("delete", "all"):
            return "all"
        if answer in ("n", "no", "cancel"):
            return None
        show("Please press Enter, type DELETE, or type N.")
    return None


def _pause(read: Callable[[str], str] = input) -> None:
    """Keep a window that was opened for the command on screen until the person has read it."""
    try:
        read("Press Enter to close this window.")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def _uninstall(manager: "Manager", args, requested: Path) -> int:
    """`odicto uninstall` for main(): exit 0 when it finished, 1 when it refused or something stayed."""
    ask = _ask_about_settings if args.ask else None
    try:
        if ask and not getattr(sys.stdin, "isatty", lambda: False)():
            raise DistributionError("odicto uninstall --ask needs a terminal window. "
                                    "Run odicto uninstall in a terminal instead.")
        code = 0 if manager.uninstall(everything=args.all, ask=ask, requested=requested) else 1
    except (RuntimeError, OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        print(f"Odicto: {error}", file=sys.stderr)
        code = 1
    if args.ask:
        _pause()
    return code


def _refresh_menu_after(manager: "Manager", before: dict | None) -> None:
    """After a command that may have switched the active release (an automatic update at start or
    setup): bring the application menu entries along. Takes manager.lock; busy means a later run does it."""
    try:
        after = manager.active()
        if after is None or (before or {}).get("directory") == after.get("directory"):
            return
        if not manager.platform.startswith("linux"):
            return
        with file_lock(manager.home / "manager.lock"):
            for line in manager.refresh_menu():
                print(line)
    except BusyError:
        pass
    except (DistributionError, OSError, ValueError) as error:
        print(f"Menu entries unchanged: {error}", file=sys.stderr)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The guide names this install's command path, so it needs --home before the real parse.
    early = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    early.add_argument("--home", type=Path, default=default_home())
    try:
        guide_home = early.parse_known_args(argv)[0].home
    except SystemExit:
        guide_home = default_home()  # a broken --home is reported by the real parse below
    # No abbreviations here: `odicto stop --h` would otherwise be an ambiguous --help/--home
    # before the application parser sees it. Each command's own parser keeps its own rules.
    # The guide lists everything a user needs; the -h and --home rows would only add noise.
    parser = _Parser(prog="odicto", usage="odicto <command> [options]",
                     description=_guide(guide_home), allow_abbrev=False, add_help=False,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-h", "--help", action="help", help=argparse.SUPPRESS)
    parser.add_argument("--home", type=Path, default=default_home(), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>", prog="odicto",
                                help=argparse.SUPPRESS)
    summaries = {
        "install": "Install or repair the managed release (the installer runs it)",
        "check": "Print the latest release tag; change nothing",
        "stage": "Download and verify a version without switching to it",
        "update": "Apply a ready update, or check for a newer one",
        "rollback": "Go back to the previous version",
        "status": "Show the installed and the previous release",
    }
    for command, summary in summaries.items():
        sub.add_parser(command, help=summary, description=f"{summary}.")
    automatic = sub.add_parser("auto-updates", help="Turn the automatic update check on or off",
                               description="Turn the automatic update check on or off.")
    automatic.add_argument("setting", choices=("on", "off"))
    admin = sub.add_parser("admin", help="Windows: type into administrator windows",
                           description="Windows only. 'on' lets Odicto type into windows that "
                                       "run as administrator. If the mode changes, Windows asks "
                                       "you to approve it and dictation is left stopped; run "
                                       "'odicto start' afterwards. If it already has that "
                                       "setting, nothing happens.")
    admin.add_argument("setting", choices=("on", "off"))
    sub.add_parser("admin-task", help=argparse.SUPPRESS)
    removal = sub.add_parser("uninstall", help="Stop Odicto and remove it",
                             description="Stop Odicto and remove it: start at sign-in, the menu entries "
                                         "and the odicto command that Odicto made, then its folder. Your "
                                         "settings stay in a folder whose path this command prints. "
                                         "Windows admin mode must be off first (Odicto Setup).")
    choice = removal.add_mutually_exclusive_group()
    choice.add_argument("--all", action="store_true", help="delete your settings too")
    choice.add_argument("--ask", action="store_true",
                        help="ask whether to delete your settings too, and wait before the window closes "
                             "(the Remove Odicto shortcut uses this)")
    migration = sub.add_parser("migrate", help="Move a source checkout to this install",
                               description="Move a source checkout to this install. Run with "
                                           "--check first. Quit Odicto and close its setup "
                                           "page before you apply it.")
    migration.add_argument("--source", type=Path, required=True,
                           help="the folder of the source checkout")
    migration.add_argument("--check", action="store_true", help="look only; change nothing")
    run = sub.add_parser("run", help="Run an application command (setup, start, stop, ...)",
                         description="Run an application command. 'odicto start' is the same as "
                                     "'odicto run start'. Use --no-auto-updates before the "
                                     "command to skip the update check.")
    run.add_argument("--no-auto-updates", action="store_true")
    run.add_argument("app_args", nargs=argparse.REMAINDER, metavar="<command> ...")
    helper = sub.add_parser("help", help="Show this guide, or the help for one command",
                            description="Show this guide, or the help for one command.")
    helper.add_argument("topic", nargs="?", metavar="command")
    args = parser.parse_args(argv)
    if args.command == "help":
        if not args.topic:
            parser.print_help()
            return 0
        known = parser if args.topic in sub.choices else _app_parser()
        return _show_help(known, [args.topic, "--help"])
    if args.command == "run":
        # Check first: a help flag or a stray word must not start or stop anything.
        checked = args.app_args[1:] if args.app_args[:1] == ["--"] else args.app_args
        if checked and (code := _app_args_refused(checked)) is not None:
            return code
    # The stable launcher sets this to the home as the user reached it, before any link is resolved.
    # An environment variable, not an option: a rolled-back release must still accept the launcher.
    launcher_home = os.environ.pop("ODICTO_LAUNCHER_HOME", "")
    manager = Manager(args.home)
    try:
        if args.command == "uninstall":
            # Before initialize(): that would make the folder if it is missing.
            return _uninstall(manager, args, Path(launcher_home) if launcher_home else args.home)
        if args.command in {"run", "migrate", "admin", "admin-task"}:
            current = manager.active()
            if current is None:
                raise DistributionError("Install a verified managed release first")
            manager.bind_dependencies()
        if args.command == "check":
            print(latest_release().tag)
            return 0
        if args.command == "run":
            app_args = args.app_args[1:] if args.app_args[:1] == ["--"] else args.app_args
            code = manager.run(app_args or ["start"], automatic_updates=not args.no_auto_updates)
            _refresh_menu_after(manager, current)
            return code
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
        manager.initialize(reinstall=args.command == "install")
        cleanup_deadline = None
        with file_lock(manager.home / "manager.lock"):
            if args.command == "install":
                # Holding manager.lock means no uninstall runs now: a notice left by a failed one is stale.
                manager.clear_removal_notice()
            else:
                manager._refuse_while_removed()
            if args.command == "status":
                print(json.dumps(manager.active(), indent=2))
            elif args.command == "auto-updates":
                marker = manager._read_json(manager.marker)
                marker["automatic_updates"] = args.setting == "on"
                atomic_json(manager.marker, marker)
                print(f"Automatic updates are {args.setting}.")
            elif args.command == "rollback":
                print(json.dumps(manager.rollback(), indent=2))
                try:
                    for line in manager.refresh_menu():
                        print(line)
                except (DistributionError, OSError, ValueError) as error:
                    print(f"Menu entries unchanged: {error}", file=sys.stderr)
            elif args.command == "stage":
                print(json.dumps(manager.stage(latest_release(manager.fetch)), indent=2))
            else:
                before = manager.active()
                steps = _UpdateSteps()
                manager.progress = steps
                try:
                    record = manager.update()
                except BusyError:
                    pending = manager.home / "pending.json"
                    if args.command != "update" or not pending.exists():
                        steps.end(" failed")
                        raise
                    # Staged and checked; a running Odicto holds the lock, so it switches at next start.
                    steps.end(" done")
                    version = manager._read_json(pending).get("version", "")
                    print(f"\nOdicto {version[1:]} is ready. Odicto or its setup page is open, so it switches"
                          f" to the new version the next time it starts:\n  odicto restart")
                    return 0
                except Exception:
                    failed_step = steps.last
                    steps.end(" failed")
                    kept = f"Odicto {before['version'][1:]} still works. " if before else ""
                    where = (f" Details: {manager.update_log}"
                             if failed_step in ("install", "verify") else "")
                    if args.command == "update":
                        print(f"The update did not finish. {kept}{where}".rstrip(), file=sys.stderr)
                    elif where:
                        print(f"Installation did not finish.{where}", file=sys.stderr)
                    raise
                steps.end(" done")
                if args.command == "update":
                    print(_update_result(before, record))
                    try:
                        for line in manager.refresh_menu():
                            print(line)
                    except (DistributionError, OSError, ValueError) as error:
                        print(f"Menu entries unchanged: {error}", file=sys.stderr)
                if args.command == "install":
                    manager.initialize_settings(record)
                    manager.progress("speech")
                    try:
                        manager.speech_preparer(manager.release_path(record), manager.environment())
                        steps.end(" done")
                    except Exception:
                        # The first start downloads the model instead; the install itself worked.
                        steps.end(" skipped")
                        print("The speech model was not downloaded now. Odicto downloads it at its first start.")
                    manager.install_launchers()
                    try:
                        command_path = manager.expose_command()
                        print(f"Command installed at {command_path}")
                    except DistributionError as error:
                        print(f"Command alias unchanged: {error}", file=sys.stderr)
                    if manager.platform == "win32" or manager.platform.startswith("linux"):
                        try:
                            for line in manager.install_shortcuts():
                                print(line)
                        except (DistributionError, OSError, subprocess.SubprocessError) as error:
                            what = "Start Menu" if manager.platform == "win32" else "Menu entries"
                            print(f"{what} unchanged: {error}", file=sys.stderr)
                    print(f"Configure with: {setup_command(manager.home)}")
                    print(f"Odicto {record['version']} is active.")
                if before is None or record.get("directory") != before.get("directory"):
                    cleanup_deadline = time.monotonic() + CLEANUP_BUDGET
                    manager.retire_old_releases(deadline=cleanup_deadline)
        if cleanup_deadline is not None:
            manager.delete_trash(deadline=cleanup_deadline)
        return 0
    except (RuntimeError, OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        print(f"Odicto: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
