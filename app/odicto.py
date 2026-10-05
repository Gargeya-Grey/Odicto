"""Cross-platform Odicto lifecycle CLI.

Usage:
    python odicto.py setup
    python odicto.py start
    python odicto.py stop
    python odicto.py status
    python odicto.py config
    python odicto.py autostart
    python odicto.py remove-autostart
"""

from __future__ import annotations

import argparse
import os
import json
import time
import subprocess
import sys

import platforms


def _repo_root() -> str:
    from platforms import base

    return base.install_root()


def _venv_python() -> str:
    if sys.platform == "win32":
        return os.path.join(_repo_root(), ".venv", "Scripts", "python.exe")
    return os.path.join(_repo_root(), ".venv", "bin", "python")


def _main_py() -> str:
    return os.path.join(_repo_root(), "main.py")


def cmd_setup(_args) -> int:
    import setup_web

    setup_web.run_server()
    return 0


def cmd_start(_args) -> int:
    proc = platforms.spawn_detached([_venv_python(), _main_py()])
    print(f"Launch requested (PID {proc.pid}); use odicto.py status to check readiness.")
    return 0


def cmd_stop(_args) -> int:
    pid_file = os.path.join(_repo_root(), "dictation.pid")
    try:
        killed = platforms.kill_other_odicto_processes(pid_file)
    except (OSError, RuntimeError) as error:
        print(f"Could not stop Odicto: {error}", file=sys.stderr)
        return 1
    platforms.release_lock()
    if killed:
        print(f"Stopped {len(killed)} Odicto runtime process(es), including any Python launcher.")
    else:
        print("No Odicto processes found.")
    return 0


def cmd_wait_ready(args) -> int:
    """Wait for the actual owner and microphone, not just an early PID file."""
    import psutil
    from platforms.base import is_odicto_command

    deadline = time.monotonic() + max(0.0, args.timeout)
    while True:
        try:
            with open(os.path.join(_repo_root(), "dictation.pid"), encoding="ascii") as f:
                pid = int(f.read().strip())
            with open(os.path.join(_repo_root(), "dictation-health.json"), encoding="utf-8") as f:
                health = json.load(f)
            mic = health.get("microphone") or {}
            age = time.time() - health["updated_at"]
            if (health["pid"] == pid and 0 <= age < 10 and health["ready"]
                    and mic and not mic.get("closed", True)
                    and mic.get("callback_count", 0) > 0
                    and 0 <= mic.get("callback_age_s", 10) + age < 3):
                owner = psutil.Process(pid)
                if (owner.create_time() <= health["updated_at"]
                        and is_odicto_command(owner.cmdline(), owner.cwd())):
                    print(f"Odicto ready (app PID {pid}); microphone receiving audio.")
                    return 0
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("Readiness not confirmed: startup may be in progress or the microphone has stopped. "
                  "Run odicto.py status; check dictation.log if this persists.", file=sys.stderr)
            return 1
        time.sleep(min(0.1, remaining))


def cmd_status(_args) -> int:
    pid_file = os.path.join(_repo_root(), "dictation.pid")
    pid = None
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = f.read().strip()
        except Exception:
            pid = None
    print(f"Backend:     {platforms.hotkey_backend_name()}")
    print(f"PID file:    {pid or '(none)'}")
    try:
        from platforms.preflight import environment_problems

        problems = environment_problems()
    except Exception:
        problems = []
    if problems:
        print("Environment problems:")
        for p in problems:
            print(f"  [{p.severity}] {p.code}: {p.message}")
    else:
        print("Environment: no known problems")
    health_path = os.path.join(_repo_root(), "dictation-health.json")
    try:
        with open(health_path, encoding="utf-8") as f:
            health = json.load(f)
        age = time.time() - health["updated_at"]
        if str(health["pid"]) != pid or age < 0 or age > 10:
            print("Health:      stale or unavailable (restart may be needed)")
        else:
            print(f"Health:      {health['state']} ready={health['ready']} (heartbeat {age:.1f}s ago)")
            mic = health.get("microphone")
            if mic:
                print(f"Microphone:  closed={mic.get('closed', True)} callback_age={mic['callback_age_s']}s")
                device = mic.get("device") or {}
                if device:
                    print(f"Input device: {device.get('name', '(unknown)')} [{device.get('host_api', '?')}] {device.get('sample_rate', '?')}Hz channels={device.get('channels', '?')}")
                if "input_rms" in mic:
                    print(f"Input level: peak={mic['input_peak']:.6f} RMS={mic['input_rms']:.6f} (latest heartbeat sample; silence is normal when not speaking)")
                captured = mic.get("last_capture")
                if captured:
                    print(f"Last capture: {captured['seconds']}s RMS={captured['rms']:.6f} (audio magnitude only)")
    except (OSError, ValueError, KeyError, TypeError):
        print("Health:      unavailable (this running version may predate health reporting)")
    return 0


def cmd_config(_args) -> int:
    """Print the fully-resolved configuration with the source of every value."""
    from config import Config, config_warnings

    rows = Config.explain()

    # Warnings first: typos and shadowed legacy names are what people come
    # here to hunt down.
    warnings = config_warnings()
    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  ! {w}")
        print()

    group = None
    width = max(len(r["label"]) for r in rows)
    for row in rows:
        if row["group"] != group:
            group = row["group"]
            print(f"\n[{group}]")
        value = str(row["value"])
        source = row["source"]
        marker = "" if source == "default" else f"   <- {source}"
        print(f"  {row['label']:<{width}}  {value}{marker}")

    print(
        "\nCascade: provider-specific override > generic LLM_* key > built-in "
        "default.\nEdit .env (start from .env.example) and restart Odicto to apply."
    )
    return 0


def cmd_autostart(_args) -> int:
    main_py = _main_py()
    venv_py = _venv_python()
    if sys.platform == "darwin":
        plist = os.path.expanduser("~/Library/LaunchAgents/com.odicto.plist")
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.odicto</string>
    <key>ProgramArguments</key>
    <array>
        <string>{venv_py}</string>
        <string>{main_py}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
</dict>
</plist>
"""
        with open(plist, "w") as f:
            f.write(body)
        subprocess.run(["launchctl", "load", plist], check=False)
        print(f"Installed launch agent: {plist}")
        return 0

    if sys.platform.startswith("linux"):
        desktop_dir = os.path.expanduser("~/.config/autostart")
        os.makedirs(desktop_dir, exist_ok=True)
        desktop = os.path.join(desktop_dir, "odicto.desktop")
        body = f"""[Desktop Entry]
Type=Application
Name=Odicto
Comment=Hold a hotkey, speak, and paste text anywhere
Exec={venv_py} {main_py}
Terminal=false
X-GNOME-Autostart-enabled=true
"""
        with open(desktop, "w") as f:
            f.write(body)
        print(f"Installed XDG autostart entry: {desktop}")
        return 0

    print("Windows autostart is managed by scripts/windows/make_startup_shortcut.ps1.")
    return 0


def cmd_remove_autostart(_args) -> int:
    if sys.platform == "darwin":
        plist = os.path.expanduser("~/Library/LaunchAgents/com.odicto.plist")
        subprocess.run(["launchctl", "unload", plist], check=False)
        try:
            os.remove(plist)
        except FileNotFoundError:
            pass
        print("Removed macOS launch agent.")
        return 0
    if sys.platform.startswith("linux"):
        desktop = os.path.expanduser("~/.config/autostart/odicto.desktop")
        try:
            os.remove(desktop)
        except FileNotFoundError:
            pass
        print("Removed Linux autostart entry.")
        return 0
    print("Windows autostart is managed by scripts/windows/make_startup_shortcut.ps1.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="odicto")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Open the local setup web page")
    sub.add_parser("start", help="Start Odicto in the background")
    sub.add_parser("stop", help="Stop all Odicto processes")
    sub.add_parser("status", help="Show runtime status")
    wait_ready = sub.add_parser("wait-ready", help="Wait for app and microphone readiness")
    wait_ready.add_argument("--timeout", type=float, default=30.0)
    sub.add_parser("config", help="Show resolved configuration and where each value came from")
    sub.add_parser("autostart", help="Install autostart entry")
    sub.add_parser("remove-autostart", help="Remove autostart entry")

    args = parser.parse_args()
    handlers = {
        "setup": cmd_setup,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "wait-ready": cmd_wait_ready,
        "config": cmd_config,
        "autostart": cmd_autostart,
        "remove-autostart": cmd_remove_autostart,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
