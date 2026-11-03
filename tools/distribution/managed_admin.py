"""Windows managed ownership, independent of any active release directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def identity(home: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(home.resolve())).encode()).hexdigest()[:12]


def task_name(home: Path) -> str:
    return "Odicto-managed-" + identity(home)


def enabled(home: Path) -> bool:
    path = home / "data" / ".odicto-admin-enabled"
    if path.is_symlink():
        raise RuntimeError("Managed administrator marker is a symlink")
    if not path.exists():
        return False
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value != {"schema": 1, "home": str(home.resolve()), "task": task_name(home)}:
        raise RuntimeError("Managed administrator marker has unknown ownership")
    return True


def _windows_admin(directory: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("odicto_managed_windows_admin", directory / "app/windows_admin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def elevated(directory: Path) -> bool:
    return _windows_admin(directory).caller_is_elevated()


def stop(manager, directory: Path) -> int:
    admin = _windows_admin(directory)
    pid = admin._owner_pid(manager.data)
    if pid is None:
        # A missing PID cannot authorize a sweep of an elevated unknown owner.
        with manager_lock(manager.data / "dictation.lock"):
            assert_inactive(manager.data, directory, allow_setup=True)
        return 0
    return 0 if admin.request_quit(manager.data, 15) else 1


def start_task(home: Path, directory: Path) -> None:
    admin = _windows_admin(directory)
    result = subprocess.run([admin._system32("schtasks.exe"), "/Run", "/TN", task_name(home)],
                            capture_output=True, timeout=30, creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError("The managed administrator task could not start; change Admin mode in Setup to repair it")


def helper(manager, action: str, *, source: Path | None = None, transaction: Path | None = None) -> dict:
    active = manager.active()
    if active is None:
        raise RuntimeError("A verified active release is required")
    directory = manager.release_path(active)
    admin = _windows_admin(directory)
    receipt = manager.home / ("admin-receipt-" + uuid.uuid4().hex + ".json")
    command = [admin._powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(directory / "scripts/windows/managed_admin.ps1"), "-Action", action,
               "-HomePath", str(manager.home)]
    if action != "Inspect":
        command += ["-ReceiptPath", str(receipt)]
    if source is not None:
        command += ["-SourceRoot", str(source)]
    if transaction is not None:
        command += ["-TransactionPath", str(transaction)]
    result = subprocess.run(command, timeout=180, creationflags=0x08000000, capture_output=True)
    value = json.loads(result.stdout.decode("utf-8-sig")) if action == "Inspect" else (manager._read_json(receipt) if receipt.exists() else {})
    if result.returncode or value.get("ok") is not True or value.get("home") != str(manager.home):
        raise RuntimeError(value.get("error") or "Administrator change was cancelled or could not be verified")
    return value.get("result", {})


def assert_inactive(data: Path, code: Path, *, allow_setup: bool = False) -> None:
    """Do not trust stale PID files or unavailable elevated process metadata."""
    import psutil
    for name in ("dictation.pid", "dictation-health.json"):
        path = data / name
        if path.is_symlink():
            raise RuntimeError("Runtime ownership metadata is a symlink")
        if not path.exists():
            continue
        try:
            value = path.read_text(encoding="utf-8")
            pid = int(value) if name.endswith(".pid") else json.loads(value).get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise RuntimeError("Runtime ownership metadata is uncertain") from None
        if psutil.pid_exists(pid):
            try:
                owner_name = psutil.Process(pid).name().lower()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                raise RuntimeError("Runtime PID ownership is unreadable; migration is blocked") from None
            if owner_name.startswith(("python", "pypy")):
                raise RuntimeError("An owner PID is live or uncertain; stop Odicto yourself before migrating")
    expected = {os.path.normcase(str((code / part).resolve())) for part in ("main.py", "app/main.py", "odicto.py", "app/odicto.py", "app/setup_web.py")}
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid():
            continue
        try:
            name = (process.info.get("name") or "").lower()
            if not name.startswith(("python", "pypy")):
                continue
            command = process.cmdline()
            def script_path(argument):
                if not argument.lower().endswith(".py"):
                    return None
                if not os.path.isabs(argument):
                    argument = os.path.join(process.cwd(), argument)
                try:
                    return os.path.normcase(os.path.realpath(argument))
                except (OSError, ValueError):
                    raise RuntimeError("Python script ownership could not be resolved; migration is blocked") from None
            setup_scripts = {os.path.normcase(str((code / name).resolve())) for name in ("odicto.py", "app/odicto.py")}
            if (allow_setup and len(command) >= 3
                    and script_path(command[1]) in setup_scripts
                    and command[2] in {"setup", "_managed-setup"}):
                continue
            if any(script_path(arg) in expected for arg in command[1:] if not arg.startswith("-")):
                raise RuntimeError("A source or destination lifecycle process is active; close it before migration")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise RuntimeError("Python ownership is unreadable; migration is blocked") from None


def manager_lock(path):
    from tools.distribution.manager import file_lock
    return file_lock(path)


class MigrationAdapter:
    def inspect(self, source, manager):
        from tools.distribution.migration import StartupPreference
        directory = manager.release_path(manager.active())
        module = _autostart(directory)
        _, _, destination_startup = module.inspect(manager.home)
        if sys.platform == "win32":
            value = helper(manager, "Inspect", source=source)
            if enabled(manager.home) and not value["admin"]:
                raise RuntimeError("Destination administrator preference conflicts with the source")
            if destination_startup and not value["autostart"]:
                raise RuntimeError("Destination autostart preference conflicts with the source")
            return StartupPreference(bool(value["admin"]), bool(value["autostart"]), value)
        # POSIX startup retirement needs a platform-owned adapter. Never silently duplicate it.
        candidates = [Path.home() / "Library/LaunchAgents/com.odicto.plist",
                      Path.home() / ".config/autostart/odicto.desktop"]
        if any(path.exists() or path.is_symlink() for path in candidates):
            raise RuntimeError("Retire the existing source autostart entry before migration")
        return StartupPreference(False, bool(destination_startup), {})

    def assert_inactive(self, source, manager):
        assert_inactive(source, source)
        active = manager.active()
        assert_inactive(manager.data, manager.release_path(active))

    def apply(self, source, manager, preference, transaction_dir):
        if sys.platform == "win32":
            action = "MigrateAdmin" if preference.admin else "Migrate"
            helper(manager, action, source=source, transaction=transaction_dir)
        if preference.autostart:
            directory = manager.release_path(manager.active())
            module = _autostart(directory)
            module.configure(manager.home, True)

    def verify(self, source, manager, preference, transaction_dir):
        if sys.platform == "win32":
            value = helper(manager, "Inspect", source=source)
            if value.get("source_task") or value.get("source_shortcut"):
                raise RuntimeError("Source startup retirement did not finish")
            if enabled(manager.home) != preference.admin:
                raise RuntimeError("Administrator preference did not transfer")
        _, _, present = _autostart(manager.release_path(manager.active())).inspect(manager.home)
        if present != preference.autostart:
            raise RuntimeError("Autostart preference did not transfer")


def _autostart(directory):
    import importlib.util
    spec = importlib.util.spec_from_file_location("odicto_migration_autostart", directory / "app/managed_autostart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
