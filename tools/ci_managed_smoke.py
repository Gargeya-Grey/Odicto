"""Real staged-venv/update checks using a committed archive on disposable OS runners."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Managed installation smoke runs only on disposable CI machines")
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    if args.root.exists() or runner_temp not in args.root.resolve().parents:
        raise RuntimeError("Managed smoke needs a new directory inside RUNNER_TEMP")
    sys.path.insert(0, str(Path(__file__).resolve().parent / "distribution"))
    from manager import Manager, Release, BusyError
    source = args.archive.read_bytes()
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        version = archive.read("Odicto/VERSION").decode().strip()
        major, minor, patch = map(int, version.split("."))
        future = f"{major}.{minor}.{patch + 1}"
        destination = io.BytesIO()
        with zipfile.ZipFile(destination, "w") as package:
            for item in archive.infolist():
                contents = (future + "\n").encode() if item.filename == "Odicto/VERSION" else archive.read(item)
                package.writestr(item, contents)
    fixtures = {}
    releases = []
    for current, contents in ((version, source), (future, destination.getvalue())):
        tag = "v" + current
        name = f"Odicto-{tag}.zip"
        base = f"https://github.com/Gargeya-Grey/Odicto/releases/download/{tag}/{name}"
        fixtures[base] = contents
        fixtures[base + ".sha256"] = (hashlib.sha256(contents).hexdigest() + "  " + name + "\n").encode()
        releases.append(Release(tag, base, base + ".sha256"))

    def fetch(url, maximum):
        contents = fixtures[url]
        if len(contents) > maximum:
            raise RuntimeError("Fixture exceeded download limits")
        return contents

    manager = Manager(args.root, fetch=fetch)
    manager.initialize()
    original = manager.stage(releases[0])
    manager.activate(original)
    manager.initialize_settings(original)
    settings = {".env": b"POLISH_HOTKEY=\n", "prompt.txt": b"synthetic retained prompt\n",
                "user-note.txt": b"unknown user file stays untouched\n"}
    for name, contents in settings.items():
        (manager.data / name).write_bytes(contents)
    assert manager.run(["version"], automatic_updates=False) == 0
    update = manager.stage(releases[1])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    from state_lock import StateLease
    for name in ("dictation.lock", "setup.lock"):
        with StateLease(manager.data / name):
            try:
                manager.activate(update)
            except BusyError:
                pass
            else:
                raise AssertionError(f"Activation ignored the actual {name} writer lease")
        assert manager.active()["directory"] == original["directory"]
    manager.activate(update)
    assert manager.active()["directory"] == update["directory"]
    assert manager.run(["version"], automatic_updates=False) == 0
    manager.rollback()
    assert manager.active()["directory"] == original["directory"]
    for name, contents in settings.items():
        assert (manager.data / name).read_bytes() == contents, f"Update changed {name}"
    args.evidence.mkdir(parents=True, exist_ok=True)
    (args.evidence / "managed-update.json").write_text(json.dumps({"pass": True,
        "source_sha": os.environ.get("GITHUB_SHA"), "actual_dependency_install": True,
        "versioned_venv_locations": [original["directory"], update["directory"]],
        "checks": ["initial activation", "installed version CLI", "runtime lease blocks update",
                   "Setup lease blocks update", "activation", "rollback", "settings and unknown-file preservation"],
        "limits": ["Future-version fixture changes VERSION only in the committed archive",
                   "No physical microphone, permissions or global-hook start"]}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
