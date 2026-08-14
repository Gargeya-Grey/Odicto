"""Cross-platform Odicto lifecycle CLI.

Usage:
    python odicto.py setup
    python odicto.py start
    python odicto.py stop
    python odicto.py status
    python odicto.py autostart
    python odicto.py remove-autostart
"""

from __future__ import annotations

import argparse
import os
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
    print(f"Started Odicto (PID {proc.pid})")
    return 0


def cmd_stop(_args) -> int:
    pid_file = os.path.join(_repo_root(), "dictation.pid")
    killed = platforms.kill_other_odicto_processes(pid_file)
    platforms.release_lock()
    if killed:
        print(f"Stopped {len(killed)} Odicto process(es).")
    else:
        print("No Odicto processes found.")
    return 0


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
    print(f"Lock held:   {platforms.lock_is_held()}")
    print(f"PID file:    {pid or '(none)'}")
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

    print("Windows autostart is managed by make_startup_shortcut.ps1.")
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
    print("Windows autostart is managed by make_startup_shortcut.ps1.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="odicto")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Open the local setup web page")
    sub.add_parser("start", help="Start Odicto in the background")
    sub.add_parser("stop", help="Stop all Odicto processes")
    sub.add_parser("status", help="Show runtime status")
    sub.add_parser("autostart", help="Install autostart entry")
    sub.add_parser("remove-autostart", help="Remove autostart entry")

    args = parser.parse_args()
    handlers = {
        "setup": cmd_setup,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "autostart": cmd_autostart,
        "remove-autostart": cmd_remove_autostart,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
