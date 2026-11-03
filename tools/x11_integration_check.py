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


def typing_editor():
    from PySide6.QtCore import QSocketNotifier, QTimer
    from PySide6.QtWidgets import QApplication, QPlainTextEdit
    app = QApplication([])
    editor = QPlainTextEdit()
    editor.show()
    app.processEvents()
    print(int(editor.winId()), flush=True)
    notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Type.Read)

    def finish():
        sys.stdin.readline()
        print(json.dumps(editor.toPlainText(), ensure_ascii=False), flush=True)
        app.quit()

    notifier.activated.connect(finish)
    QTimer.singleShot(10000, lambda: app.exit(2))
    return app.exec()


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
        fields = [b"", b"", b"MIT-MAGIC-COOKIE-1", cookie]
        authorization.write_bytes(struct.pack(">H", 65535) + b"".join(
            struct.pack(">H", len(field)) + field for field in fields))
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
    desktop = None
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
        if authority is not None:
            from Xlib import display as xdisplay, error as xerror
            empty_authorization = Path(authority.name) / "empty-authority"
            empty_authorization.write_bytes(b"")
            os.environ["XAUTHORITY"] = str(empty_authorization)
            try:
                try:
                    untrusted = xdisplay.Display()
                except xerror.DisplayConnectionError:
                    pass
                else:
                    untrusted.close()
                    raise AssertionError("The owned TCP display accepted an unauthenticated client")
            finally:
                os.environ["XAUTHORITY"] = str(authorization)
        os.environ["XDG_SESSION_TYPE"] = "x11"
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QPlainTextEdit
        from platforms import _x11
        from Xlib import X, XK, display
        from Xlib.ext import xtest
        backend = _x11
        desktop = display.Display()
        desktop_root = desktop.screen().root
        desktop_keys = {desktop.keysym_to_keycode(XK.string_to_keysym(name)) for name in ("F7", "F8")}
        for code in desktop_keys:
            desktop_root.grab_key(code, X.Mod1Mask, False, X.GrabModeAsync, X.GrabModeAsync)
        desktop.sync()
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
            # Older xdotool adds Alt to function keys with XKB 2.46+ (upstream #493).
            if arguments[0] in ("key", "keydown", "keyup"):
                action = None
                for argument in arguments:
                    if argument in ("key", "keydown", "keyup"):
                        action = argument
                        continue
                    names = argument.split("+")
                    aliases = {"alt": "Alt_L", "ctrl": "Control_L", "shift": "Shift_L"}
                    codes = [desktop.keysym_to_keycode(XK.string_to_keysym(aliases.get(name, name)))
                             for name in names]
                    if not all(codes):
                        raise RuntimeError(f"Cannot inject physical keys for {argument!r}")
                    if action != "keyup":
                        for code in codes:
                            xtest.fake_input(desktop, X.KeyPress, code)
                    if action != "keydown":
                        for code in reversed(codes):
                            xtest.fake_input(desktop, X.KeyRelease, code)
                    desktop.sync()
            else:
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

        _x11.hook_key("f8", polish, suppress=True, modifier_sets=((),))
        _x11.hook_key("f7", lambda event: captured.append(("live", event.event_type)) or False,
                      suppress=True, modifier_sets=((),))
        _x11.hook_key("grave", dictate, suppress=True, modifier_sets=(("ctrl",), ("ctrl", "shift")))
        inject("key", "alt+F7")
        inject("key", "alt+F8")
        desktop_events = []
        while desktop.pending_events():
            event = desktop.next_event()
            if event.type == X.KeyPress and event.detail in desktop_keys:
                desktop_events.append(event.detail)
        assert set(desktop_events) == desktop_keys, "Desktop window shortcuts were captured by Odicto"
        assert captured == [], captured
        observed.clear()
        inject("key", "F7")
        assert captured == [("live", "down"), ("live", "up")], captured
        assert not any(key == Qt.Key.Key_F7 for _, key in observed), observed
        inject("type", "--delay", "15", "abc")
        assert editor.toPlainText() == "abc", "Ordinary typing was altered"
        observed.clear()
        inject("key", "F8")
        assert captured.count(("polish", "down")) == 1, captured
        assert ("polish", "up") in captured, captured
        assert not any(key == Qt.Key.Key_F8 for _, key in observed), observed
        inject("key", "Caps_Lock")
        inject("key", "F8")
        inject("key", "Caps_Lock")
        assert captured.count(("polish", "down")) == 2, captured
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
        payload = "नमस्ते\n--not-a-flag"
        target = subprocess.Popen([sys.executable, __file__, "--typing-editor"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert select.select([target.stdout], [], [], 10)[0], "Separate editor did not start"
            target_window = target.stdout.readline().strip()
            assert target_window.isdigit(), target_window
            inject("windowfocus", "--sync", target_window)
            from platforms.base import TextDeliveryUnavailable
            try:
                _x11.send_text_bulk(payload)
            except TextDeliveryUnavailable:
                pass
            else:
                raise AssertionError("Unsupported Unicode must require explicit copying")
            safe_payload = "line one\n--not-a-flag"
            assert _x11.send_text_bulk(safe_payload)
            from polish_result import PolishResultDialog
            recovery = PolishResultDialog(None, payload, title="Odicto · Text ready",
                                           explanation="Manual paste needed")
            try:
                recovery.copy_button.click()
                assert app.clipboard().text() == payload
                _x11.send_paste()
                pump()
            finally:
                recovery.close()
            target.stdin.write("done\n")
            target.stdin.flush()
            assert select.select([target.stdout], [], [], 10)[0], "Separate editor did not return text"
            typed = json.loads(target.stdout.readline())
            assert typed == safe_payload + payload, "Recovery lost or duplicated text: " + repr(typed)
            assert target.wait(timeout=5) == 0
        finally:
            if target.poll() is None:
                target.terminate()
                target.wait(timeout=5)
            target.stdin.close()
            target.stdout.close()
        inject("windowfocus", "--sync", str(int(editor.winId())))
        _x11.unhook_all()
        observed.clear()
        inject("key", "grave")
        inject("key", "F8")
        assert editor.toPlainText() == "abc`z01234567`", "Typing did not recover after teardown"
        assert ("down", Qt.Key.Key_F8) in observed, "F8 grab survived teardown"
        editor.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"pass": True, "display": os.environ["DISPLAY"],
            "key_injector": "XTEST physical keycodes; xdotool retained for text and window operations",
            "owned_display": True, "cases": ["normal typing", "bare shortcut suppression", "modifier passthrough",
            "bare primary replay", "typing during held chord", "primary release suppression", "eight repeated held chords",
            "desktop Alt+F7/F8 coexistence", "private-pipe ASCII and multiline typing",
            "unsupported Unicode refuses before any input", "explicit Unicode Copy and manual paste",
            "teardown recovery"],
            "limits": ["Xvfb X11 only", "No physical microphone or Wayland coverage"]}, indent=2), encoding="utf-8")
        return 0
    except Exception as error:
        report = {"pass": False, "error": f"{type(error).__name__}: {error}",
                  "display": os.environ.get("DISPLAY"), "owned_display": True,
                  "captured": locals().get("captured", []), "observed": locals().get("observed", [])}
        active = getattr(backend, "_backend", None) if backend is not None else None
        if active is not None:
            try:
                with active.lock:
                    report["input_state"] = {"pointer_mask": active.root.query_pointer().mask,
                        "pressed": active.pressed, "suppressed": sorted(active.suppressed),
                        "grab_masks": {code: sorted(masks) for code, masks in active.modifier_masks.items()},
                        "keymap": {code: list(active.display.get_keyboard_mapping(code, 1)[0])
                                   for code in active.handlers},
                        "worker_alive": active.thread.is_alive()}
            except Exception as snapshot_error:
                report["diagnostic_error"] = str(snapshot_error)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        raise
    finally:
        if backend is not None:
            backend.unhook_all()
        if desktop is not None:
            desktop.close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        if authority is not None:
            authority.cleanup()


if __name__ == "__main__":
    raise SystemExit(typing_editor() if sys.argv[1:] == ["--typing-editor"] else main())
