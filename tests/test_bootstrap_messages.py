"""The bootstrap says what went wrong in words a user can act on, before it changes anything."""
import io
import json
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
            self.assertIn("Odicto was not installed", message)

    def test_other_http_error_names_the_code(self):
        message = run_payload(http_error(503))
        self.assertIn("HTTP 503", message)
        self.assertNotIn("too many requests", message)
        self.assertNotIn("too many requests", run_payload(http_error(403, "17")))

    def test_no_connection_says_to_check_the_internet(self):
        for error in (urllib.error.URLError("name resolution failed"), TimeoutError("timed out")):
            message = run_payload(error)
            self.assertIn("Check the internet connection", message)


MANIFEST = "https://github.com/Gargeya-Grey/Odicto/releases/latest/download/odicto-release.json"
API = "https://api.github.com/repos/Gargeya-Grey/Odicto/releases/latest"
CHECKSUM = "https://github.com/Gargeya-Grey/Odicto/releases/download/v1.2.0/Odicto-v1.2.0.zip.sha256"


def release_json(**changes) -> bytes:
    prefix = "https://github.com/Gargeya-Grey/Odicto/releases/download/v1.2.0/"
    value = {"tag_name": "v1.2.0", "draft": False, "prerelease": False,
             "assets": [{"name": "Odicto-v1.2.0.zip", "browser_download_url": prefix + "Odicto-v1.2.0.zip"},
                        {"name": "Odicto-v1.2.0.zip.sha256",
                         "browser_download_url": prefix + "Odicto-v1.2.0.zip.sha256"}]}
    return json.dumps({**value, **changes}).encode()


def run_answers(answers: dict):
    """Run the payload with one answer per URL: bytes are served, an exception is raised.
    Returns (what the bootstrap stopped with, the URLs it asked for, in order)."""
    asked = []

    def answer(request, timeout):
        asked.append(request.full_url)
        value = answers[request.full_url]
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value)

    with patch.object(urllib.request, "build_opener", return_value=SimpleNamespace(open=answer)):
        try:
            exec(compile(payload(), "bootstrap-test", "exec"), {"__name__": "bootstrap"})
        except SystemExit as stopped:
            return str(stopped.code), asked
    raise AssertionError("the bootstrap did not stop")


class TestReleaseManifest(unittest.TestCase):
    """The manifest comes first. The rate-limited API answers only for an HTTP error or bad content."""

    # The checksum answer is wrong on purpose: the run stops right after the release is chosen.
    BAD_CHECKSUM = {CHECKSUM: b"not a checksum"}

    def test_a_served_manifest_is_used_and_the_api_is_not_called(self):
        message, asked = run_answers({MANIFEST: release_json(), **self.BAD_CHECKSUM})
        self.assertEqual(message, "Invalid release checksum")
        self.assertEqual(asked, [MANIFEST, CHECKSUM])

    def test_a_missing_manifest_falls_back_to_the_api(self):
        message, asked = run_answers({MANIFEST: http_error(404), API: release_json(), **self.BAD_CHECKSUM})
        self.assertEqual(message, "Invalid release checksum")
        self.assertEqual(asked, [MANIFEST, API, CHECKSUM])

    def test_an_invalid_manifest_falls_back_to_the_api(self):
        for bad in (b"not json", b"[]", release_json(draft=True), release_json(assets=[]), release_json(tag_name="1.2")):
            with self.subTest(manifest=bad):
                message, asked = run_answers({MANIFEST: bad, API: release_json(), **self.BAD_CHECKSUM})
                self.assertEqual(asked, [MANIFEST, API, CHECKSUM])
                self.assertEqual(message, "Invalid release checksum")

    def test_an_invalid_manifest_and_an_invalid_api_answer_stop_with_a_message(self):
        message, asked = run_answers({MANIFEST: b"not json", API: release_json(assets=[])})
        self.assertEqual(message, "Release archive or checksum is missing")
        self.assertEqual(asked, [MANIFEST, API])

    def test_a_rate_limited_api_after_a_missing_manifest_says_to_wait(self):
        message, asked = run_answers({MANIFEST: http_error(404), API: http_error(403, "0")})
        self.assertIn("too many requests", message)
        self.assertEqual(asked, [MANIFEST, API])

    def test_a_failing_api_after_a_missing_manifest_names_the_code(self):
        message, asked = run_answers({MANIFEST: http_error(404), API: http_error(503)})
        self.assertIn("HTTP 503", message)
        self.assertEqual(asked, [MANIFEST, API])

    def test_a_connection_error_on_the_manifest_does_not_call_the_api(self):
        for error in (urllib.error.URLError("name resolution failed"), TimeoutError("timed out")):
            message, asked = run_answers({MANIFEST: error})
            self.assertIn("Check the internet connection", message)
            self.assertEqual(asked, [MANIFEST])


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
            # Every program that could change the machine is a stub, uv included, and HOME and
            # the install folders are temporary: the real uv and managed home are never reached.
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stubs = root / "stubs"
                stubs.mkdir()
                marker = root / "touched"
                for name, body in {"uname": "printf 'Linux\\n'",
                                   "sudo": f"touch '{marker}'", "apt-get": f"touch '{marker}'",
                                   "dpkg-query": f"touch '{marker}'", "curl": f"touch '{marker}'",
                                   "uv": f"touch '{marker}'"}.items():
                    (stubs / name).write_text("#!/bin/sh\n" + body + "\n")
                    (stubs / name).chmod(0o700)
                environment = {key: value for key, value in os.environ.items()
                               if key not in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "XDG_DATA_HOME",
                                              "UV_INSTALL_DIR", "ODICTO_DATA_ROOT", "ODICTO_MANAGED_HOME")}
                environment.update(session, PATH=str(stubs) + ":/usr/bin:/bin", HOME=str(root / "home"),
                                   XDG_DATA_HOME=str(root / "data"), UV_INSTALL_DIR=str(root / "uv"))
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
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Wayland session", result.stderr)
            self.assertTrue(touched)


if __name__ == "__main__":
    unittest.main()
