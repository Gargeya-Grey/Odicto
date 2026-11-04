"""Fresh-install and native dependency evidence for disposable CI machines only."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from unittest.mock import patch
import zipfile


LIMITS = ["No physical microphone capture", "No desktop permission grant",
          "No global hook or real-editor insertion proof from the unit suite",
          "Linux X11 suppression is exercised separately; Wayland is not supported",
          "Exact runner builds are recorded; vendor-latest patch availability is not asserted"]


def run(command, *, cwd: Path, output: Path, timeout=900, env=None) -> str:
    with output.open("w", encoding="utf-8") as handle:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=handle,
                                stderr=subprocess.STDOUT, timeout=timeout)
    text = output.read_text(encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed with exit {result.returncode}; see {output.name}")
    return text


def os_report() -> dict:
    report = {"system": platform.system(), "release": platform.release(),
              "version": platform.version(), "machine": platform.machine(),
              "python": platform.python_version()}
    if sys.platform == "darwin":
        report["macos_version"] = platform.mac_ver()[0]
        report["macos_build"] = subprocess.check_output(["sw_vers", "-buildVersion"], text=True).strip()
    if sys.platform.startswith("linux"):
        report["os_release"] = platform.freedesktop_os_release()
    return report


def validate_platform(label: str, report: dict) -> None:
    if label in ("macos-26", "macos-26-intel", "xcode-27"):
        major = "27" if label == "xcode-27" else "26"
        if report.get("system") != "Darwin" or report.get("macos_version", "").split(".")[0] != major:
            raise RuntimeError("Runner does not provide the requested macOS major version")
        expected_arch = "x86_64" if label == "macos-26-intel" else "arm64"
        if report.get("machine") != expected_arch:
            raise RuntimeError("Runner does not provide the requested Mac architecture")
    elif label in ("ubuntu-26.04", "ubuntu-26.10-vm"):
        expected = "26.10" if label.endswith("-vm") else "26.04"
        release = report.get("os_release", {})
        if report.get("system") != "Linux" or release.get("ID") != "ubuntu" or release.get("VERSION_ID") != expected:
            raise RuntimeError("Runner does not provide the requested Ubuntu release")
    elif label != "windows-2025" or report.get("system") != "Windows":
        raise RuntimeError("Unexpected platform runner label or OS")


def setup_http(root: Path, evidence: Path) -> None:
    code = "import sys;sys.path.insert(0,'app');import setup_web;setup_web.run_server(port=0,open_browser=False)"
    process = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=root,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    def drain():
        for line in process.stdout:
            lines.put(line)
    threading.Thread(target=drain, daemon=True).start()
    seen = []
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None:
                    raise RuntimeError("Setup process exited before listening")
                continue
            seen.append(line)
            match = re.search(r"Odicto setup page: (http://127\.0\.0\.1:\d+)", line)
            if match:
                with urllib.request.urlopen(match[1], timeout=10) as response:
                    html = response.read(2_000_000).decode("utf-8")
                    if response.status != 200 or 'id="view-polish"' not in html:
                        raise RuntimeError("Installed Setup page is incomplete")
                return
        raise TimeoutError("Setup did not listen within 30 seconds")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        # Never persist the HTML, CSRF token or configuration values.
        (evidence / "setup-http.json").write_text(json.dumps({"listener": "127.0.0.1",
            "browser_opened": False, "process_stopped": process.poll() is not None}), encoding="utf-8")


def fetch_fixture(url: str, digest: str, attempts: int = 4, sleep=time.sleep, opener=None) -> bytes:
    """Download the pinned public fixture. A network error or timeout is retried (a CI VM
    timed out once on 4 Nov 2026); a wrong checksum is never retried."""
    opener = opener or urllib.request.urlopen
    for attempt in range(1, attempts + 1):
        try:
            with opener(url, timeout=30) as response:
                audio = response.read(2_000_001)
            break
        except OSError:  # URLError, TimeoutError and socket errors are all OSError
            if attempt == attempts:
                raise
            sleep(5 * attempt)
    if len(audio) > 2_000_000 or hashlib.sha256(audio).hexdigest() != digest:
        raise RuntimeError("Public speech fixture failed its pinned checksum")
    return audio


def speech_decode() -> dict:
    url = "https://raw.githubusercontent.com/openai/whisper/main/tests/jfk.flac"
    digest = "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715"
    fixture = Path(os.environ["RUNNER_TEMP"]) / "odicto-public-speech-fixture.flac"
    fixture.write_bytes(fetch_fixture(url, digest))
    from config import Config
    from transcriber import WhisperTranscriber
    with patch.object(Config, "WHISPER_DEVICE", "cpu"), patch.object(Config, "WHISPER_MODEL_SIZE", "tiny.en"):
        transcript = WhisperTranscriber().transcribe(str(fixture))
    normalized = " ".join(re.sub(r"[^a-z ]", " ", transcript.lower()).split())
    if "my fellow americans" not in normalized or "ask not what your country can do for you" not in normalized:
        raise RuntimeError("The actual local speech engine did not recognize the pinned fixture")
    return {"source": url, "sha256": digest, "model": "tiny.en", "device": "cpu",
            "expected_phrases_recognized": True, "owner_audio_used": False}


def installed(root: Path, evidence: Path, sha: str) -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true" or not (root / ".odicto-ci-fixture").is_file():
        raise RuntimeError("Native smoke checks require a disposable CI fixture")
    report = {"schema": 1, "source_sha": sha, "runner_label": os.environ.get("ODICTO_RUNNER_LABEL", ""),
              "os": os_report(), "limitations": LIMITS, "checks": {}, "result": "failed"}
    try:
        validate_platform(report["runner_label"], report["os"])
        sys.path.insert(0, str(root / "app"))
        for name in ("sounddevice", "soundfile", "faster_whisper", "PySide6.QtWidgets"):
            importlib.import_module(name)
        report["checks"]["native_imports"] = "pass"
        import numpy as np
        from faster_whisper.vad import get_speech_timestamps
        if get_speech_timestamps(np.zeros(16000, dtype=np.float32)):
            raise RuntimeError("Native VAD classified silence as speech")
        report["checks"]["native_onnx_inference"] = "pass"
        report["speech_fixture"] = speech_decode()
        report["checks"]["local_speech_decode"] = "pass"
        if sys.platform == "darwin":
            from platforms import macos
            from unittest.mock import patch
            virtual_key = macos._primary_virtual_key("p")
            if virtual_key is None:
                raise RuntimeError("Native macOS keyboard layout did not resolve the P key")
            with patch.dict(macos._hooks, {"p": {"virtual_key": virtual_key}}, clear=True):
                translated = macos._pynput.KeyCode.from_char("π", vk=virtual_key)
                if macos._key_token(translated) != "p":
                    raise RuntimeError("Translated macOS character lost its registered physical primary")
            report["checks"]["native_keyboard_layout_mapping"] = "pass"
        if sys.platform.startswith("linux"):
            import sounddevice as sd
            with sd.InputStream(samplerate=16000, channels=1, dtype="float32") as stream:
                samples, overflow = stream.read(3200)
            if samples.shape != (3200, 1) or not np.isfinite(samples).all() or overflow:
                raise RuntimeError("The emulated Linux audio source did not deliver valid PCM")
            report["checks"]["emulated_audio_capture"] = "pass"
            report["audio_source"] = "CI PulseAudio null-sink monitor; not a physical microphone"
        report["dependencies"] = {name: importlib.metadata.version(name)
                                  for name in ("PySide6", "sounddevice", "faster-whisper", "google-genai")}
        from PySide6.QtWidgets import QApplication, QLabel
        app = QApplication.instance() or QApplication([])
        widget = QLabel("Odicto installed native Qt smoke")
        widget.resize(400, 80)
        widget.show()
        app.processEvents()
        if not widget.grab().save(str(evidence / "qt-installed.png")):
            raise RuntimeError("Installed Qt widget did not render")
        widget.close()
        report["checks"]["qt_render"] = "pass"
        output = run([sys.executable, "odicto.py", "version"], cwd=root,
                     output=evidence / "version.txt", timeout=30)
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        if version not in output:
            raise RuntimeError("Installed version does not match the archive")
        report["version"] = version
        report["checks"]["version_cli"] = "pass"
        setup_http(root, evidence)
        report["checks"]["setup_http"] = "pass"
        preserved = root / ".ci-preserved-settings"
        preserved.mkdir()
        names = (".env", "prompt.txt")
        for name in names:
            (root / name).rename(preserved / name)
        try:
            text = run([sys.executable, "tools/run_tests.py"], cwd=root,
                       output=evidence / "tests.txt", timeout=600)
        finally:
            for name in names:
                (preserved / name).rename(root / name)
        report["test_configuration"] = "built-in defaults, fixture settings retained outside the test path"
        summary = re.search(r"GATE: ran (\d+), skipped (\d+) \(floor (\d+)\)", text)
        if not summary or not re.search(r"(?m)^GATE OK\s*$", text):
            raise RuntimeError("Installed test gate did not report success")
        report["checks"]["test_gate"] = "pass"
        report["tests"] = dict(zip(("ran", "skipped", "floor"), map(int, summary.groups())))
        report["result"] = "pass"
        return 0
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (evidence / "platform.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def fresh_install(source: Path, root: Path, evidence: Path, sha: str) -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Fresh installation is restricted to disposable CI machines")
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    root = root.resolve()
    if root == runner_temp or runner_temp not in root.parents or root.exists():
        raise RuntimeError("Fresh install target must be a new directory below RUNNER_TEMP")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if actual != sha:
        raise RuntimeError("Checkout SHA differs from the tested SHA")
    root.mkdir()
    archive = evidence / "committed-source.zip"
    (evidence / "installer-environment.json").write_text(json.dumps({"source_sha": sha,
        "os": os_report(), "runner_label": os.environ.get("ODICTO_RUNNER_LABEL", "")}, indent=2), encoding="utf-8")
    run(["git", "archive", "--format=zip", "--prefix=Odicto/", "-o", str(archive), sha], cwd=source,
        output=evidence / "archive.txt", timeout=30)
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            parts = member.filename.split("/")
            if parts[0] != "Odicto":
                raise RuntimeError("Release archive has an unexpected root")
            relative = "/".join(parts[1:])
            destination = (root / relative).resolve()
            if root not in destination.parents and destination != root:
                raise RuntimeError("Archive escapes the clean install root")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(package.read(member))
    (evidence / "archive-sha256.json").write_text(json.dumps({"source_sha": sha,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}), encoding="utf-8")
    (root / ".odicto-ci-fixture").write_text(sha, encoding="ascii")
    environment = dict(os.environ)
    for key in ("ODICTO_DATA_ROOT", "ODICTO_MANAGED_HOME"):
        environment.pop(key, None)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    if os.name == "nt":
        installer = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(root / "install.ps1"), "-Python", "3.12", "-SkipWhisper", "-NoShortcuts"]
        python = root / ".venv/Scripts/python.exe"
    else:
        installer = ["bash", str(root / "install.sh"), "-Python", "3.12", "-SkipWhisper"]
        python = root / ".venv/bin/python"
    run(installer, cwd=root, output=evidence / "install-first.txt", timeout=1200, env=environment)
    # These are fixture settings, not an owner's configuration.
    with (root / ".env").open("a", encoding="utf-8") as handle:
        handle.write("\n# CI preservation fixture\n")
    (root / "prompt.txt").write_text("CI prompt preservation fixture.\n", encoding="utf-8")
    before = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (".env", "prompt.txt")}
    run(installer, cwd=root, output=evidence / "install-repeat.txt", timeout=1200, env=environment)
    after = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in before}
    if before != after:
        raise RuntimeError("Installer changed existing settings or prompt")
    (evidence / "settings-preserved.json").write_text(json.dumps({"preserved": True}), encoding="utf-8")
    run([str(python), str(root / "tools/ci_platform_smoke.py"), "--installed", "--root", str(root),
         "--evidence", str(evidence), "--sha", sha], cwd=root,
        output=evidence / "smoke.txt", timeout=900, env=environment)
    run([str(python), str(root / "tools/ci_managed_smoke.py"), "--archive", str(archive),
         "--root", str(root.parent / "odicto-managed-smoke"), "--evidence", str(evidence)],
        cwd=root, output=evidence / "managed-update.txt", timeout=1200, env=environment)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("--sha must identify the exact source commit")
    args.evidence = args.evidence.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    if args.installed:
        return installed(args.root.resolve(), args.evidence, args.sha)
    return fresh_install(Path(__file__).resolve().parents[1], args.root, args.evidence, args.sha)


if __name__ == "__main__":
    raise SystemExit(main())
