"""The bootstrap says what went wrong in words a user can act on, before it changes anything."""
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION = ROOT / "tools" / "distribution"


def payload() -> str:
    shell = (DISTRIBUTION / "bootstrap.sh").read_text(encoding="utf-8")
    return shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def run_payload(error: Exception) -> str:
    def fail(request, timeout):
        raise error
    with patch.object(urllib.request, "build_opener", return_value=SimpleNamespace(open=fail)):
        try:
            exec(compile(payload(), "bootstrap-test", "exec"), {"__name__": "bootstrap"})
        except SystemExit as stopped:
            return str(stopped.code)
    raise AssertionError("the bootstrap did not stop")


def http_error(code: int, remaining=None) -> urllib.error.HTTPError:
    headers = {} if remaining is None else {"X-RateLimit-Remaining": remaining}
    return urllib.error.HTTPError("https://api.github.com/", code, "refused", headers, io.BytesIO())


class TestDownloadMessages(unittest.TestCase):
    def test_rate_limit_says_to_wait(self):
        for error in (http_error(403, "0"), http_error(429)):
            message = run_payload(error)
            self.assertIn("too many requests", message)
            self.assertIn("Nothing was installed", message)

    def test_other_http_error_names_the_code(self):
        message = run_payload(http_error(503))
        self.assertIn("HTTP 503", message)
        self.assertNotIn("too many requests", message)
        self.assertNotIn("too many requests", run_payload(http_error(403, "17")))

    def test_no_connection_says_to_check_the_internet(self):
        for error in (urllib.error.URLError("name resolution failed"), TimeoutError("timed out")):
            message = run_payload(error)
            self.assertIn("Check the internet connection", message)


class TestSetupCommand(unittest.TestCase):
    def test_a_home_with_a_space_pastes_as_one_path(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("odicto_setup_command_tested", DISTRIBUTION / "manager.py")
        manager = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {spec.name: manager}):
            spec.loader.exec_module(manager)
        home = Path("/Users/someone/Library/Application Support/Odicto")
        command = manager.setup_command(home)
        self.assertTrue(command.endswith(" setup"), command)
        if os.name == "nt":
            self.assertTrue(command.startswith('"'), command)
        else:
            import shlex
            self.assertEqual(shlex.split(command), [str(home / "bin" / "odicto"), "setup"])


if os.name != "nt":
    class TestWaylandStopsBeforeSudo(unittest.TestCase):
        def run_bootstrap(self, **session):
            with tempfile.TemporaryDirectory() as directory:
                stubs = Path(directory)
                marker = stubs / "touched"
                for name, body in {"uname": "printf 'Linux\\n'",
                                   "sudo": f"touch '{marker}'", "apt-get": f"touch '{marker}'",
                                   "dpkg-query": f"touch '{marker}'", "curl": f"touch '{marker}'"}.items():
                    (stubs / name).write_text("#!/bin/sh\n" + body + "\n")
                    (stubs / name).chmod(0o700)
                environment = {key: value for key, value in os.environ.items()
                               if key not in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY")}
                environment.update(session, PATH=str(stubs) + ":/usr/bin:/bin")
                result = subprocess.run(["/bin/bash", str(DISTRIBUTION / "bootstrap.sh")], env=environment,
                                        capture_output=True, text=True, timeout=15)
                return result, marker.exists()

        def test_a_wayland_session_stops_with_advice_and_touches_nothing(self):
            for session in ({"XDG_SESSION_TYPE": "wayland"}, {"XDG_SESSION_TYPE": "Wayland"},
                            {"WAYLAND_DISPLAY": "wayland-0"}):
                result, touched = self.run_bootstrap(**session)
                self.assertEqual(result.returncode, 1, session)
                self.assertIn("Xorg", result.stderr)
                self.assertIn("Nothing was installed", result.stderr)
                self.assertFalse(touched, session)

        def test_an_x11_session_goes_on_to_the_package_check(self):
            result, touched = self.run_bootstrap(XDG_SESSION_TYPE="x11")
            self.assertNotIn("Wayland session", result.stderr)
            self.assertTrue(touched)


if __name__ == "__main__":
    unittest.main()
