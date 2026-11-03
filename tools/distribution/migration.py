"""One-time, resumable settings handoff from a stopped source installation."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Protocol
import uuid


SOURCE_RECEIPT = ".odicto-migration.json"
DESTINATION_RECEIPT = "migration.json"
KNOWN_FILES = (".env", "prompt.txt")
_TOKEN = re.compile(r"[a-f0-9]{32}\Z")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartupPreference:
    admin: bool
    autostart: bool
    ownership_token: dict


@dataclass(frozen=True)
class MigrationFile:
    name: str
    source_sha256: str | None
    destination_sha256: str | None


@dataclass(frozen=True)
class MigrationPlan:
    source: str
    home: str
    files: tuple[MigrationFile, ...]
    startup: StartupPreference
    state: str = "ready"
    advisory: bool = True

    def as_dict(self) -> dict:
        value = asdict(self)
        value["guaranteed_no_live_owner"] = False
        value["execution"] = "Rechecks owners and configuration under source and destination leases; never starts or stops Odicto."
        return value


class MigrationAdapter(Protocol):
    def inspect(self, source: Path, manager) -> StartupPreference: ...
    def assert_inactive(self, source: Path, manager) -> None: ...
    def apply(self, source: Path, manager, preference: StartupPreference, transaction_dir: Path) -> None: ...
    def verify(self, source: Path, manager, preference: StartupPreference, transaction_dir: Path) -> None: ...


def _plain(path: Path, *, directory: bool = False) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    if path.is_symlink() or reparse or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise MigrationError(f"Migration preserves a linked or special path: {path.name}")
    if not directory and info.st_nlink > 1:
        raise MigrationError(f"Migration preserves a multiply linked file: {path.name}")


def _json(path: Path) -> dict:
    _plain(path)
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError) as error:
        raise MigrationError(f"Invalid migration ownership metadata: {path.name}") from error


def _atomic(path: Path, value: dict) -> None:
    _plain(path)
    temporary = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _digest(path: Path) -> str | None:
    _plain(path)
    if not path.exists():
        return None
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            result.update(block)
    return result.hexdigest()


def _identity(manager, source: Path) -> tuple[Path, Path]:
    source = Path(source).expanduser().absolute()
    _plain(source, directory=True)
    source = source.resolve()
    home = Path(manager.home).resolve()
    data = Path(manager.data)
    _plain(home, directory=True)
    _plain(data, directory=True)
    if data.resolve() != home / "data":
        raise MigrationError("Destination private data does not match the managed installation")
    if source == home or source in home.parents or home in source.parents:
        raise MigrationError("Source and managed installation must be separate directories")
    if not all((source / name).is_file() for name in ("main.py", "odicto.py", ".env.example", "app/paths.py")):
        raise MigrationError("Source is not a recognized Odicto source installation")
    if (source / ".odicto-managed.json").exists():
        raise MigrationError("Migration accepts a source installation, not another managed home")
    source_capabilities = source / "tools/distribution/capabilities.json"
    if (not source_capabilities.exists()
            or _json(source_capabilities).get("source_migration_guard") is not True):
        raise MigrationError("Update the source checkout first; this source cannot safely guard legacy starts during migration")
    marker = _json(home / ".odicto-managed.json")
    if marker.get("schema") != 1 or marker.get("home") != str(home):
        raise MigrationError("Destination is not an owned managed installation")
    active = manager.active()
    if not active:
        raise MigrationError("Install a verified managed release before migrating settings")
    manager.release_path(active)
    return source, home


def _record_for(source: Path, home: Path) -> dict | None:
    marker_path = source / SOURCE_RECEIPT
    journal_path = home / DESTINATION_RECEIPT
    source_record = _json(marker_path) if marker_path.exists() or marker_path.is_symlink() else None
    journal = _json(journal_path) if journal_path.exists() or journal_path.is_symlink() else None
    for record in (source_record, journal):
        if record is not None and (record.get("schema") != 1 or record.get("source") != str(source)
                or record.get("home") != str(home) or not _TOKEN.fullmatch(str(record.get("token", "")))):
            raise MigrationError("An existing migration receipt has unknown ownership and was preserved")
    if source_record and not journal:
        raise MigrationError("The source migration receipt has no destination transaction; it was preserved")
    if source_record and journal and source_record["token"] != journal["token"]:
        raise MigrationError("Migration receipts disagree; both were preserved")
    if journal and journal.get("state") not in {"pending", "failed", "complete"}:
        raise MigrationError("The migration transaction has an unknown state and was preserved")
    if source_record and source_record.get("state") not in {"pending", "complete"}:
        raise MigrationError("The source migration receipt has an unknown state and was preserved")
    return journal


def _preference(value: dict) -> StartupPreference:
    if (not isinstance(value, dict) or type(value.get("admin")) is not bool
            or type(value.get("autostart")) is not bool or not isinstance(value.get("ownership_token"), dict)):
        raise MigrationError("Invalid migration startup preference")
    return StartupPreference(value["admin"], value["autostart"], value["ownership_token"])


def _files(value) -> tuple[MigrationFile, ...]:
    if not isinstance(value, list):
        raise MigrationError("Invalid migration file inventory")
    result = []
    for entry in value:
        if (not isinstance(entry, dict) or entry.get("name") not in KNOWN_FILES
                or "source_sha256" not in entry
                or entry["source_sha256"] is not None
                and not re.fullmatch(r"[a-f0-9]{64}", str(entry["source_sha256"]))
                or entry.get("destination_sha256") is not None
                and not re.fullmatch(r"[a-f0-9]{64}", str(entry["destination_sha256"]))):
            raise MigrationError("Invalid migration file inventory")
        result.append(MigrationFile(entry["name"], entry["source_sha256"], entry.get("destination_sha256")))
    if len({entry.name for entry in result}) != len(result):
        raise MigrationError("Duplicate migration file inventory")
    if {entry.name for entry in result} != set(KNOWN_FILES):
        raise MigrationError("Incomplete migration file inventory; absent settings must be recorded")
    return tuple(result)


def plan_migration(manager, source: Path, *, adapter: MigrationAdapter) -> MigrationPlan:
    """Read-only snapshot. Only execution can reserve the required owner leases."""
    source, home = _identity(manager, source)
    previous = _record_for(source, home)
    adapter.assert_inactive(source, manager)
    if previous:
        files = _files(previous.get("files"))
        if previous["state"] != "complete":
            for entry in files:
                if _digest(source / entry.name) != entry.source_sha256:
                    raise MigrationError(f"Source {entry.name} changed after migration began; recovery copies were preserved")
                accepted = {entry.destination_sha256}
                if entry.source_sha256 is not None:
                    accepted.add(entry.source_sha256)
                if _digest(manager.data / entry.name) not in accepted:
                    raise MigrationError(f"Destination {entry.name} changed during migration; it was preserved")
        return MigrationPlan(str(source), str(home), files,
                             _preference(previous.get("startup")), previous["state"])
    settings = source / ".env"
    _plain(settings)
    if settings.exists():
        from dotenv.parser import parse_stream
        snapshot = settings.read_bytes()
        validated_settings_hash = hashlib.sha256(snapshot).hexdigest()
        reference = ""
        with io.StringIO(snapshot.decode("utf-8", errors="surrogateescape")) as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise MigrationError("Save settings in source Setup before migration; the configuration syntax could not be verified")
                if binding.key == "SYSTEM_PROMPT_FILE":
                    reference = (binding.value or "").strip()
        if reference and reference not in {"prompt.txt", "./prompt.txt", ".\\prompt.txt"}:
            raise MigrationError("Save the prompt in source Setup before migration; legacy prompt-file references must be normalized to prompt.txt")
    else:
        validated_settings_hash = None
    preference = adapter.inspect(source, manager)
    if not isinstance(preference, StartupPreference):
        raise MigrationError("The startup adapter did not return a typed preference")
    release = manager.release_path(manager.active())
    files = []
    for name in KNOWN_FILES:
        source_hash = _digest(source / name)
        if name == ".env" and source_hash != validated_settings_hash:
            raise MigrationError("Source settings changed during validation; close the editor and retry migration")
        destination_hash = _digest(manager.data / name)
        default_hash = _digest(release / (".env.example" if name == ".env" else "prompt.txt.example"))
        if source_hash is None:
            if destination_hash is not None and (name == "prompt.txt" or destination_hash != default_hash):
                raise MigrationError(f"Destination {name} is already configured; no settings were overwritten")
            files.append(MigrationFile(name, None, destination_hash))
            continue
        if destination_hash is not None and destination_hash not in {default_hash, source_hash}:
            raise MigrationError(f"Destination {name} is already configured; no settings were overwritten")
        files.append(MigrationFile(name, source_hash, destination_hash))
    return MigrationPlan(str(source), str(home), tuple(files), preference)


def _copy_verified(source: Path, target: Path, expected: str) -> None:
    _plain(target)
    if target.exists():
        if _digest(target) != expected:
            raise MigrationError(f"A migration recovery file changed and was preserved: {target.name}")
        return
    _plain(source)
    temporary = target.with_name(target.name + ".new-" + uuid.uuid4().hex)
    with source.open("rb") as incoming, temporary.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    temporary.chmod(0o600)
    if _digest(temporary) != expected:
        raise MigrationError(f"A settings file changed during migration: {source.name}; retry after closing its editor")
    os.replace(temporary, target)


def _source_marker(record: dict, state: str) -> dict:
    return {key: record[key] for key in ("schema", "source", "home", "token")} | {"state": state}


def migrate(manager, source: Path, *, check: bool = False, adapter: MigrationAdapter,
            lock_factory=None) -> dict:
    if check:
        return plan_migration(manager, source, adapter=adapter).as_dict()
    if lock_factory is None:
        raise MigrationError("Migration requires the manager's compatible owner leases")
    source, home = _identity(manager, source)
    with ExitStack() as leases:
        for lock in (home / "manager.lock", home / "launch.lock"):
            _plain(lock)
            leases.enter_context(lock_factory(lock))
        manager._recover_launch_intent()
        for directory in (source, Path(manager.data)):
            for name in ("setup.lock", "dictation.lock"):
                _plain(directory / name)
                leases.enter_context(lock_factory(directory / name))
        plan = plan_migration(manager, source, adapter=adapter)
        record = _record_for(source, home)
        if record is None:
            record = {"schema": 1, "source": str(source), "home": str(home), "token": uuid.uuid4().hex,
                      "state": "pending", "files": [asdict(entry) for entry in plan.files],
                      "startup": asdict(plan.startup), "supervisor_pid": os.getpid()}
            _atomic(home / DESTINATION_RECEIPT, record)
        if record["state"] == "complete":
            _atomic(source / SOURCE_RECEIPT, _source_marker(record, "complete"))
            return {"state": "complete", "source": str(source), "home": str(home), "already_migrated": True}
        record["supervisor_pid"] = os.getpid()
        record["state"] = "pending"
        _atomic(home / DESTINATION_RECEIPT, record)
        transaction = home / "migrations" / record["token"]
        for directory in (home / "migrations", transaction, transaction / "source", transaction / "destination"):
            _plain(directory, directory=True)
            directory.mkdir(exist_ok=True, mode=0o700)
        _atomic(source / SOURCE_RECEIPT, _source_marker(record, "pending"))
        try:
            for entry in plan.files:
                source_backup = transaction / "source" / entry.name
                destination_backup = transaction / "destination" / entry.name
                if _digest(source / entry.name) != entry.source_sha256:
                    raise MigrationError(f"Source {entry.name} changed after migration began; recovery copies were preserved")
                if entry.source_sha256 is None:
                    if _digest(manager.data / entry.name) != entry.destination_sha256:
                        raise MigrationError(f"Destination {entry.name} changed during migration; it was preserved")
                    continue
                _copy_verified(source / entry.name, source_backup, entry.source_sha256)
                destination = manager.data / entry.name
                current_hash = _digest(destination)
                if current_hash not in {entry.destination_sha256, entry.source_sha256}:
                    raise MigrationError(f"Destination {entry.name} changed during migration; it was preserved")
                if entry.destination_sha256 is not None:
                    if current_hash == entry.destination_sha256:
                        _copy_verified(destination, destination_backup, entry.destination_sha256)
                    elif _digest(destination_backup) != entry.destination_sha256:
                        raise MigrationError(f"Destination {entry.name} has no verified recovery backup")
                if current_hash != entry.source_sha256:
                    staged = transaction / (entry.name + ".import")
                    _copy_verified(source_backup, staged, entry.source_sha256)
                    if _digest(destination) != current_hash:
                        raise MigrationError(f"Destination {entry.name} changed during migration; it was preserved")
                    os.replace(staged, destination)
            adapter.assert_inactive(source, manager)
            adapter.apply(source, manager, plan.startup, transaction)
            adapter.verify(source, manager, plan.startup, transaction)
            record["state"] = "complete"
            record.pop("failure", None)
            _atomic(home / DESTINATION_RECEIPT, record)
            _atomic(source / SOURCE_RECEIPT, _source_marker(record, "complete"))
            return {"state": "complete", "source": str(source), "home": str(home),
                    "receipt": str(home / DESTINATION_RECEIPT), "recovery": str(transaction), "already_migrated": False}
        except Exception as error:
            record["state"] = "failed"
            record["failure"] = type(error).__name__
            _atomic(home / DESTINATION_RECEIPT, record)
            raise


def validated_forward(source: Path) -> Path | None:
    """Return only a reciprocally owned stable launcher; partial migrations block source use."""
    source = Path(source).resolve()
    marker_path = source / SOURCE_RECEIPT
    if not marker_path.exists() and not marker_path.is_symlink():
        return None
    marker = _json(marker_path)
    home_value = marker.get("home")
    if not isinstance(home_value, str) or not Path(home_value).is_absolute():
        raise MigrationError("Unknown migration receipt; source startup was blocked")
    home = Path(home_value)
    _plain(home, directory=True)
    if home.resolve() != home or source == home or source in home.parents or home in source.parents:
        raise MigrationError("Invalid migration destination; source startup was blocked")
    journal = _record_for(source, home)
    if not journal or journal.get("state") != "complete" or marker.get("state") != "complete":
        raise MigrationError(f"Settings migration is incomplete; resume with the installed odicto migrate --source command for {source}")
    managed = _json(home / ".odicto-managed.json")
    if managed.get("schema") != 1 or managed.get("home") != str(home):
        raise MigrationError("The migration destination is no longer an owned managed installation")
    relative = "bin/odicto.ps1" if sys.platform == "win32" else "bin/odicto"
    launcher = home / relative
    _plain(launcher)
    launchers = managed.get("launchers")
    if not isinstance(launchers, dict):
        raise MigrationError("The installed stable command has no valid ownership inventory")
    accepted = launchers.get(relative)
    accepted = [accepted] if isinstance(accepted, str) else accepted
    if not isinstance(accepted, list) or _digest(launcher) not in accepted:
        raise MigrationError("The installed stable command has unknown ownership; source startup was blocked")
    return launcher
