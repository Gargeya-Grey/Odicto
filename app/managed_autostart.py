"""Optional startup entries point at the stable managed launcher, never a release."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import uuid


def _location():
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents/com.odicto.managed.plist"
    if sys.platform.startswith("linux"):
        return Path.home() / ".config/autostart/odicto-managed.desktop"
    directory = subprocess.check_output(["powershell", "-NoProfile", "-Command",
        "[Environment]::GetFolderPath('Startup')"], text=True, timeout=5).strip()
    if not directory:
        raise RuntimeError("The Windows Startup folder could not be found")
    return Path(directory) / "Odicto managed.cmd"


class NotOurs(RuntimeError):
    """The startup entry was read and Odicto did not write it: it is kept. Not a failure."""


class CannotCheck(OSError):
    """Odicto could not tell whether the startup entry is its own (an unreadable or malformed receipt).
    That is not the same as "not ours": the uninstall stops and keeps everything."""


def _read_receipt(receipt: Path) -> dict:
    try:
        value = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
    except OSError as error:
        raise CannotCheck(f"{receipt.name} cannot be read ({error.strerror or error})") from error
    except ValueError as error:  # JSON or UTF-8
        raise CannotCheck(f"{receipt.name} is damaged ({error})") from error
    if not isinstance(value, dict):
        raise CannotCheck(f"{receipt.name} is damaged (it is not a record)")
    return value


def inspect(home: Path):
    home = home.resolve()
    target = _location()
    receipt = home / "autostart.json"
    if receipt.is_symlink():
        raise NotOurs("Startup ownership metadata is a symlink")
    if target.exists() or target.is_symlink():
        # The receipt is read only when there is an entry to check: a bad receipt with no entry is harmless.
        owned = _read_receipt(receipt)
        accepted = {owned.get("sha256")}
        if owned.get("pending") is True:
            accepted.add(owned.get("previous_sha256"))
        if (target.is_symlink() or owned.get("path") != str(target)
                or hashlib.sha256(target.read_bytes()).hexdigest() not in accepted):
            raise NotOurs("The existing startup entry has unknown ownership and was preserved")
    return target, receipt, target.exists()


MACOS_LABEL = "com.odicto.managed"


def _not_loaded(shown) -> bool:
    """launchctl print says that the service does not exist (exit 113, "Could not find service")."""
    return shown.returncode == 113 or "could not find service" in f"{shown.stdout}{shown.stderr}".lower()


def _unload(target: Path, strict: bool = False) -> None:
    """macOS: take this LaunchAgent out of launchd before its file goes.

    A job is booted out only when launchd says that it was loaded from this very file, so a
    foreign job that has the same label is never touched.

    strict (the uninstall): fail closed. A `launchctl print` that fails for any reason except "no such
    service", a `bootout` that fails, a missing launchctl and a timeout all raise (OSError, or
    SubprocessError for a timeout), so the caller keeps the file and its receipt: a job that stays loaded
    would start a program that is gone. Not strict (turning start-at-sign-in off in Setup): best effort."""
    if sys.platform != "darwin":
        return
    try:
        service = f"gui/{os.getuid()}/{MACOS_LABEL}"
        shown = subprocess.run(["launchctl", "print", service], capture_output=True, text=True, timeout=10)
        if shown.returncode != 0:
            if strict and not _not_loaded(shown):
                raise OSError(shown.returncode, f"launchctl print failed ({(shown.stderr or '').strip() or shown.returncode})")
            return
        paths = [line.split("=", 1)[1].strip() for line in shown.stdout.splitlines()
                 if "=" in line and line.split("=", 1)[0].strip() == "path"]
        if str(target) in paths:
            booted = subprocess.run(["launchctl", "bootout", service], capture_output=True, text=True, timeout=10)
            if strict and booted.returncode != 0:
                raise OSError(booted.returncode, f"launchctl bootout failed ({(booted.stderr or '').strip() or booted.returncode})")
    except (OSError, subprocess.SubprocessError, AttributeError):
        if strict:
            raise


def remove(home: Path) -> bool:
    """Delete this home's startup entry. True when one was deleted, False when there was none.

    An entry that Odicto did not write is kept and raises NotOurs (a RuntimeError; see inspect). An
    entry that cannot be checked (CannotCheck), a LaunchAgent that cannot be unloaded and a failed
    delete raise OSError (or SubprocessError) and keep the entry and the receipt: the receipt lives in
    the managed home, which the uninstall deletes only after every outside item is gone."""
    target, _, present = inspect(home)
    if present:
        _unload(target, strict=True)
        target.unlink()
    return present


def configure(home: Path, enabled: bool):
    home = home.resolve()
    target, receipt, _ = inspect(home)
    if not enabled:
        if target.exists():
            _unload(target)
            target.unlink()
        return
    launcher = home / "bin" / ("odicto.cmd" if os.name == "nt" else "odicto")
    if not launcher.is_file():
        raise RuntimeError("The stable managed launcher is missing; rerun the bootstrap installer")
    if sys.platform == "darwin":
        contents = plistlib.dumps({"Label": "com.odicto.managed", "ProgramArguments": [str(launcher), "start"],
                                  "RunAtLoad": True, "KeepAlive": False})
    elif sys.platform.startswith("linux"):
        command = str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        contents = ("[Desktop Entry]\nType=Application\nName=Odicto\n"
                    f'Exec="{command}" start\nTerminal=false\nX-GNOME-Autostart-enabled=true\n').encode()
    else:
        command = str(launcher).replace("%", "%%")
        contents = f'@echo off\r\nsetlocal DisableDelayedExpansion\r\ncall "{command}" start\r\n'.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
    ownership = {"path": str(target), "sha256": hashlib.sha256(contents).hexdigest()}
    pending = ownership | {"pending": True, "previous_sha256": previous}
    marker = receipt.with_name(receipt.name + ".new-" + uuid.uuid4().hex)
    marker.write_text(json.dumps(pending), encoding="utf-8")
    os.replace(marker, receipt)
    temporary = target.with_name(target.name + ".new-" + uuid.uuid4().hex)
    temporary.write_bytes(contents)
    os.replace(temporary, target)
    marker = receipt.with_name(receipt.name + ".new-" + uuid.uuid4().hex)
    marker.write_text(json.dumps(ownership), encoding="utf-8")
    os.replace(marker, receipt)
