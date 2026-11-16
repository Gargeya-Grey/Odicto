"""WSL runs Odicto on the WSLg X server; a real Wayland desktop is still refused."""
import unittest
from unittest import mock

from platforms import _x11, preflight
from tests.test_preflight import codes, run_linux

# What a WSLg shell measured: Wayland and X are both set, there is no XDG_SESSION_TYPE.
WSLG = {
    "WSL_DISTRO_NAME": "Ubuntu-24.04",
    "WAYLAND_DISPLAY": "wayland-0",
    "DISPLAY": ":0",
    "PULSE_SERVER": "unix:/mnt/wslg/PulseServer",
    "XDG_RUNTIME_DIR": "/mnt/wslg/runtime-dir",
}
DESKTOP_WAYLAND = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0",
                   "XDG_RUNTIME_DIR": "/run/user/1000"}


def session(env):
    with mock.patch.dict(_x11.os.environ, env, clear=True):
        return _x11.require_session()


class PreflightInWslTests(unittest.TestCase):
    def test_wslg_passes_preflight_with_one_testing_warning(self):
        found = run_linux(euid=1000, env=WSLG)
        self.assertEqual([(p.severity, p.code) for p in found], [("warning", "wsl_testing_only")])

    def test_the_warning_says_what_works_and_what_does_not(self):
        (problem,) = run_linux(euid=1000, env=WSLG)
        for text in ("for testing", "while a Linux window has focus", "Linux X11 apps", "not into Windows apps"):
            self.assertIn(text, problem.message)

    def test_wsl_with_only_the_interop_variable_counts_as_wsl(self):
        env = {k: v for k, v in WSLG.items() if k != "WSL_DISTRO_NAME"}
        found = codes(run_linux(euid=1000, env=dict(env, WSL_INTEROP="/run/WSL/1_interop")))
        self.assertEqual(found, {"wsl_testing_only": "warning"})

    def test_session_type_wayland_in_wsl_still_uses_the_x_server(self):
        found = codes(run_linux(euid=1000, env=dict(WSLG, XDG_SESSION_TYPE="wayland")))
        self.assertEqual(found, {"wsl_testing_only": "warning"})

    def test_wsl_without_an_x_server_is_still_blocked(self):
        env = {k: v for k, v in WSLG.items() if k != "DISPLAY"}
        found = codes(run_linux(euid=1000, env=env))
        self.assertEqual(found.get("wayland_session"), "error")
        self.assertEqual(found.get("linux_missing_display"), "error")

    def test_a_wayland_desktop_without_wsl_is_still_refused(self):
        for env in (DESKTOP_WAYLAND,
                    {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
                    {"XDG_SESSION_TYPE": "Wayland", "DISPLAY": ":0"}):
            with self.subTest(env=env):
                found = codes(run_linux(euid=1000, env=env))
                self.assertEqual(found.get("wayland_session"), "error")
                self.assertNotIn("wsl_testing_only", found)

    def test_the_wayland_advice_does_not_promise_an_xorg_session(self):
        (problem,) = [p for p in run_linux(euid=1000, env=DESKTOP_WAYLAND) if p.code == "wayland_session"]
        self.assertIn("If your login screen offers an Xorg", problem.message)
        self.assertIn("Ubuntu 25.10 and newer have none", problem.message)
        self.assertNotIn("Choose an X11 desktop session", problem.message)

    def test_a_plain_x11_desktop_is_unchanged(self):
        self.assertEqual(run_linux(euid=1000, env={"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}), [])

    def test_the_warning_never_blocks_the_start(self):
        found = run_linux(euid=1000, env=WSLG)
        self.assertEqual([p for p in found if p.severity == "error"], [])

    def test_wsl_detection_reads_only_the_environment(self):
        self.assertTrue(preflight.running_in_wsl({"WSL_DISTRO_NAME": "x"}))
        self.assertTrue(preflight.running_in_wsl({"WSL_INTEROP": "/run/WSL/1"}))
        self.assertFalse(preflight.running_in_wsl({"DISPLAY": ":0"}))
        self.assertFalse(preflight.running_in_wsl({}))


class X11BackendInWslTests(unittest.TestCase):
    def test_wslg_environment_is_accepted_by_the_x11_backend(self):
        self.assertIsNone(session(WSLG))
        self.assertIsNone(session(dict(WSLG, XDG_SESSION_TYPE="wayland")))

    def test_a_wayland_desktop_is_refused_before_xlib_loads(self):
        with self.assertRaises(RuntimeError) as caught:
            session(DESKTOP_WAYLAND)
        self.assertIn("Wayland is not supported", str(caught.exception))
        self.assertIn("Ubuntu 25.10 and newer have none", str(caught.exception))

    def test_wsl_without_display_is_refused(self):
        with self.assertRaises(RuntimeError):
            session({k: v for k, v in WSLG.items() if k != "DISPLAY"})

    def test_a_plain_x11_session_is_accepted_and_no_display_is_refused(self):
        self.assertIsNone(session({"DISPLAY": ":99"}))
        with self.assertRaisesRegex(RuntimeError, "No X11 DISPLAY"):
            session({})


if __name__ == "__main__":
    unittest.main()
