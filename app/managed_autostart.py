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


def inspect(home: Path):
    home = home.resolve()
    target = _location()
    receipt = home / "autostart.json"
    if receipt.is_symlink():
        raise RuntimeError("Startup ownership metadata is a symlink")
    owned = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
    if target.exists() or target.is_symlink():
        accepted = {owned.get("sha256")}
        if owned.get("pending") is True:
            accepted.add(owned.get("previous_sha256"))
        if (target.is_symlink() or owned.get("path") != str(target)
                or hashlib.sha256(target.read_bytes()).hexdigest() not in accepted):
            raise RuntimeError("The existing startup entry has unknown ownership and was preserved")
    return target, receipt, target.exists()


def configure(home: Path, enabled: bool):
    home = home.resolve()
    target, receipt, _ = inspect(home)
    if not enabled:
        if target.exists():
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
