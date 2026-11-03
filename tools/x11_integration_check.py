"""Real input/suppression checks on an owned Xvfb server, never the user's desktop."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import secrets
import select
import signal
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tcp-display", action="store_true",
                        help="Use an authenticated TCP Xvfb display when WSLg owns /tmp/.X11-unix")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        raise RuntimeError("This check needs a disposable Linux environment")
    for tool in ("Xvfb", "xdotool"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Missing integration-test dependency: {tool}")
    authority = tempfile.TemporaryDirectory(prefix="odicto-x11-auth-") if args.tcp_display else None
    server_args = ["Xvfb", "-displayfd", "1", "-screen", "0", "1024x768x24"]
    cookie = None
    if authority is not None:
        cookie = secrets.token_bytes(16)
        authorization = Path(authority.name) / "Xauthority"
        authorization.write_bytes(b"")
        authorization.chmod(0o600)
        server_args += ["-nolisten", "local", "-nolisten", "unix", "-listen", "tcp", "-auth", str(authorization)]
    else:
        server_args += ["-nolisten", "tcp"]
    libc = ctypes.CDLL(None, use_errno=True)
    parent_pid = os.getpid()

    def stop_with_parent():
        if libc.prctl(1, signal.SIGTERM) != 0:
            raise OSError(ctypes.get_errno(), "Could not guard the owned Xvfb lifetime")
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)

    server = subprocess.Popen(server_args, preexec_fn=stop_with_parent,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    backend = None
    try:
        readable, _, _ = select.select([server.stdout], [], [], 10)
        if not readable:
            raise RuntimeError("The isolated X server did not become ready")
        display_number = server.stdout.readline().strip()
        if not display_number.isdigit():
            raise RuntimeError("The isolated X server did not return a display")
        if authority is not None:
            entries = []
            for family, address in ((0, socket.inet_aton("127.0.0.1")),
                                    (256, socket.gethostname().encode())):
                fields = [address, display_number.encode(), b"MIT-MAGIC-COOKIE-1", cookie]
                entries.append(struct.pack(">H", family) + b"".join(
                    struct.pack(">H", len(field)) + field for field in fields))
            authorization.write_bytes(b"".join(entries))
            os.environ["XAUTHORITY"] = str(authorization)
        os.environ["DISPLAY"] = ("127.0.0.1:" if args.tcp_display else ":") + display_number
        os.environ["XDG_SESSION_TYPE"] = "x11"
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QPlainTextEdit
        from platforms import _x11
        backend = _x11
        app = QApplication([])
        observed = []

        class Editor(QPlainTextEdit):
            def keyPressEvent(self, event):
                observed.append(("down", event.key()))
                super().keyPressEvent(event)

            def keyReleaseEvent(self, event):
                observed.append(("up", event.key()))
                super().keyReleaseEvent(event)

        editor = Editor()
        editor.resize(500, 300)
        editor.show()
        editor.setFocus()

        def pump():
            until = time.monotonic() + 0.15
            while time.monotonic() < until:
                app.processEvents()
                time.sleep(0.005)

        def inject(*arguments):
            subprocess.run(["xdotool", *arguments], check=True, timeout=10)
            pump()

        pump()
        inject("windowfocus", "--sync", str(int(editor.winId())))
        captured = []

        def polish(event):
            if event.event_type == "down" and any(_x11.is_pressed(key) for key in ("ctrl", "shift", "alt", "cmd")):
                return True
            captured.append(("polish", event.event_type))
            return False

        def dictate(event):
            if event.event_type == "down" and not _x11.is_pressed("ctrl"):
                return True
            captured.append(("dictate", event.event_type))
            return False

        _x11.hook_key("f8", polish, suppress=True)
        _x11.hook_key("grave", dictate, suppress=True)
        inject("type", "--delay", "15", "abc")
        assert editor.toPlainText() == "abc", "Ordinary typing was altered"
        observed.clear()
        inject("key", "F8")
        assert captured.count(("polish", "down")) == 1, captured
        assert ("polish", "up") in captured, captured
        assert not any(key == Qt.Key.Key_F8 for _, key in observed), observed
        inject("key", "shift+F8")
        assert ("down", Qt.Key.Key_F8) in observed, "Unrelated modified F8 was suppressed"
        inject("key", "grave")
        assert editor.toPlainText() == "abc`", editor.toPlainText()
        observed.clear()
        inject("keydown", "Control_L", "keydown", "grave", "keyup", "Control_L")
        inject("type", "--clearmodifiers", "--delay", "15", "z")
        inject("keyup", "grave")
        assert editor.toPlainText() == "abc`z", editor.toPlainText()
        assert not any(key == Qt.Key.Key_QuoteLeft for _, key in observed), observed
        assert ("dictate", "down") in captured and ("dictate", "up") in captured, captured
        for index in range(8):
            before = len(captured)
            observed.clear()
            inject("keydown", "Control_L", "keydown", "grave", "keyup", "Control_L")
            inject("type", "--clearmodifiers", str(index))
            inject("keyup", "grave")
            assert captured[before:] == [("dictate", "down"), ("dictate", "up")], captured[before:]
            assert not any(key == Qt.Key.Key_QuoteLeft for _, key in observed), observed
        assert editor.toPlainText() == "abc`z01234567", editor.toPlainText()
        _x11.unhook_all()
        observed.clear()
        inject("key", "grave")
        inject("key", "F8")
        assert editor.toPlainText() == "abc`z01234567`", "Typing did not recover after teardown"
        assert ("down", Qt.Key.Key_F8) in observed, "F8 grab survived teardown"
        editor.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"pass": True, "display": os.environ["DISPLAY"],
            "owned_display": True, "cases": ["normal typing", "bare shortcut suppression", "modifier passthrough",
            "bare primary replay", "typing during held chord", "primary release suppression", "eight repeated held chords", "teardown recovery"],
            "limits": ["Xvfb X11 only", "No physical microphone or Wayland coverage"]}, indent=2), encoding="utf-8")
        return 0
    finally:
        if backend is not None:
            backend.unhook_all()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        if authority is not None:
            authority.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
