import ctypes
import ctypes.util
import os
import shutil
import sys
import unittest
from unittest import mock

from platforms import preflight


def codes(problems):
    return {p.code: p.severity for p in problems}


def run_linux(euid=0, env=None, which=None, portaudio="libportaudio.so.2"):
    env = {"DISPLAY": ":0", "XAUTHORITY": "/x", "XDG_RUNTIME_DIR": "/run/user/1"} if env is None else env
    which = {"xclip", "xdotool"} if which is None else which
    with mock.patch.object(sys, "platform", "linux"), \
            mock.patch.object(os, "geteuid", lambda: euid, create=True), \
            mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(shutil, "which", lambda n: "/usr/bin/" + n if n in which else None), \
            mock.patch.object(ctypes.util, "find_library", lambda n: portaudio):
        return preflight.environment_problems()


class FakeFunc:
    def __init__(self, value):
        self.value = value

    def __call__(self, *args):
        return self.value


class FakeLib:
    def __init__(self, **funcs):
        for k, v in funcs.items():
            setattr(self, k, FakeFunc(v))


def run_macos(trusted=True, hid=0, iokit_has_symbol=True):
    def load(name):
        if "ApplicationServices" in str(name):
            return FakeLib(AXIsProcessTrusted=trusted)
        return FakeLib(IOHIDCheckAccess=hid) if iokit_has_symbol else FakeLib()

    with mock.patch.object(sys, "platform", "darwin"), \
            mock.patch.object(ctypes.cdll, "LoadLibrary", load):
        return preflight.environment_problems()


class LinuxTests(unittest.TestCase):
    def test_clean_root_session_has_no_problems(self):
        self.assertEqual(run_linux(), [])

    def test_not_root_is_error(self):
        self.assertEqual(codes(run_linux(euid=1000)).get("linux_not_root"), "error")

    def test_root_without_session_env_warns(self):
        found = codes(run_linux(env={}))
        self.assertEqual(found.get("linux_root_session_env"), "warning")

    def test_non_root_does_not_report_root_env(self):
        self.assertNotIn("linux_root_session_env", codes(run_linux(euid=1000, env={})))

    def test_wayland_by_session_type_and_display(self):
        base = {"DISPLAY": ":0", "XAUTHORITY": "/x", "XDG_RUNTIME_DIR": "/r"}
        self.assertEqual(codes(run_linux(env=dict(base, XDG_SESSION_TYPE="wayland"))).get("wayland_session"), "warning")
        self.assertIn("wayland_session", codes(run_linux(env=dict(base, WAYLAND_DISPLAY="wayland-0"))))
        self.assertNotIn("wayland_session", codes(run_linux(env=dict(base, XDG_SESSION_TYPE="x11"))))

    def test_missing_clipboard_tool_is_error(self):
        self.assertEqual(codes(run_linux(which={"xdotool"})).get("linux_missing_clipboard_tool"), "error")
        self.assertNotIn("linux_missing_clipboard_tool", codes(run_linux(which={"wl-copy", "xdotool"})))

    def test_missing_xdotool_is_warning(self):
        self.assertEqual(codes(run_linux(which={"xclip"})).get("linux_missing_xdotool"), "warning")

    def test_missing_portaudio_is_error(self):
        self.assertEqual(codes(run_linux(portaudio=None)).get("linux_missing_portaudio"), "error")


class MacTests(unittest.TestCase):
    def test_granted(self):
        self.assertEqual(run_macos(), [])

    def test_accessibility_missing(self):
        self.assertEqual(codes(run_macos(trusted=False)), {"macos_accessibility": "error"})

    def test_input_monitoring_denied_and_unknown(self):
        self.assertEqual(codes(run_macos(hid=1)), {"macos_input_monitoring": "error"})
        self.assertEqual(codes(run_macos(hid=2)), {"macos_input_monitoring": "error"})

    def test_missing_symbol_is_unknown_not_a_problem(self):
        self.assertEqual(run_macos(iokit_has_symbol=False), [])


class RobustnessTests(unittest.TestCase):
    def test_windows_returns_empty(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(preflight.environment_problems(), [])

    def test_probe_raising_does_not_propagate(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(os, "geteuid", mock.Mock(side_effect=OSError("boom")), create=True):
            self.assertIsInstance(preflight.environment_problems(), list)
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(ctypes.cdll, "LoadLibrary", mock.Mock(side_effect=OSError("boom"))):
            self.assertEqual(preflight.environment_problems(), [])

    def test_linux_probe_failure_is_isolated(self):
        def boom(name):
            raise OSError("boom")

        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(os, "geteuid", lambda: 1000, create=True), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(shutil, "which", lambda n: None), \
                mock.patch.object(ctypes.util, "find_library", boom):
            found = codes(preflight.environment_problems())
        self.assertIn("linux_not_root", found)
        self.assertIn("linux_missing_clipboard_tool", found)
        self.assertNotIn("linux_missing_portaudio", found)

    def test_macos_probe_failure_is_isolated(self):
        def load(name):
            if "ApplicationServices" in str(name):
                raise OSError("boom")
            return FakeLib(IOHIDCheckAccess=1)

        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(ctypes.cdll, "LoadLibrary", load):
            self.assertEqual(codes(preflight.environment_problems()), {"macos_input_monitoring": "error"})

    def test_problem_is_frozen(self):
        p = preflight.Problem("error", "x", "y")
        with self.assertRaises(Exception):
            p.code = "z"


if __name__ == "__main__":
    unittest.main()
