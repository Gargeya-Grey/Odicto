"""Lifetime and status routes of the local setup server.

The server lives only while a setup page is open: the page polls /status with its own
client id and says goodbye on close. One setup server per computer: a second launch
shows the open page instead of starting another.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import setup_web

TOKEN = "test-token-lifecycle"


def _serve(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class _ServerCase(unittest.TestCase):
    """A real loopback _SetupServer with the lifetime attributes run_server sets."""

    def setUp(self) -> None:
        self.server = setup_web._SetupServer(("127.0.0.1", 0), setup_web._Handler)
        self.server.odicto_token = TOKEN
        self.server.odicto_last_request = time.monotonic()
        self.server.odicto_bye_at = None
        self.server.odicto_clients = {}
        self.port = self.server.server_address[1]
        self.thread = _serve(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _req(self, method, path, body=b"", headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            if method == "POST":
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def _post(self, path, body=b"", token=TOKEN):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if token is not None:
            body = (body + b"&" if body else b"") + b"_odicto_token=" + token.encode()
        return self._req("POST", path, body, headers)


class StatusRouteTests(_ServerCase):
    def test_status_is_json_names_the_service_and_is_not_cached(self) -> None:
        with mock.patch.object(setup_web, "app_version", lambda: "9.9.9"):
            status, headers, body = self._req("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        payload = json.loads(body)
        self.assertEqual(payload["service"], "odicto-setup")
        self.assertEqual(payload["version"], "9.9.9")
        self.assertEqual(
            set(payload), {"service", "version", "running", "ready", "state", "microphone", "admin"}
        )

    def test_reads_check_the_host_header(self) -> None:
        for path in ("/status", "/", "/assets/fonts/InstrumentSans-VF.ttf"):
            status, _, _ = self._req("GET", path, host="evil.example:80")
            self.assertEqual(status, 403, path)

    def test_status_with_a_client_id_registers_that_page(self) -> None:
        self._req("GET", "/status?c=abcdef0123456789")
        self.assertIn("abcdef0123456789", self.server.odicto_clients)

    def test_malformed_client_ids_are_ignored(self) -> None:
        for bad in ("short", "x" * 41, "%3Cscript%3E00000", ""):
            self._req("GET", "/status?c=" + bad)
        self.assertEqual(self.server.odicto_clients, {})

    def test_client_map_is_capped(self) -> None:
        for n in range(setup_web._MAX_CLIENTS + 5):
            self._req("GET", "/status?c=client%08d" % n)
        self.assertEqual(len(self.server.odicto_clients), setup_web._MAX_CLIENTS)
        self.assertIn("client%08d" % (setup_web._MAX_CLIENTS + 4), self.server.odicto_clients)

    def test_font_is_served_and_other_asset_paths_are_not(self) -> None:
        status, headers, body = self._req("GET", "/assets/fonts/InstrumentSans-VF.ttf")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "font/ttf")
        self.assertGreater(len(body), 10_000)
        for path in ("/assets/fonts/../../.env", "/assets/fonts/other.ttf", "/assets/odicto.ico"):
            status, _, _ = self._req("GET", path)
            self.assertEqual(status, 404, path)

    def test_page_has_no_leftover_template_tokens(self) -> None:
        status, _, body = self._req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/assets/fonts/InstrumentSans-VF.ttf", body)
        self.assertEqual(re.findall(rb"__[A-Z][A-Z0-9_]+__", body), [])


class GoodbyeRouteTests(_ServerCase):
    def test_goodbye_marks_the_time_and_forgets_that_page(self) -> None:
        self._req("GET", "/status?c=pageAAAA1111")
        self._req("GET", "/status?c=pageBBBB2222")
        status, _, _ = self._post("/bye", b"_odicto_client=pageAAAA1111")
        self.assertEqual(status, 204)
        self.assertIsNotNone(self.server.odicto_bye_at)
        self.assertNotIn("pageAAAA1111", self.server.odicto_clients)
        self.assertIn("pageBBBB2222", self.server.odicto_clients)

    def test_goodbye_does_not_count_as_a_request(self) -> None:
        before = self.server.odicto_last_request
        self._post("/bye", b"_odicto_client=pageAAAA1111")
        self.assertEqual(self.server.odicto_last_request, before)
        self.assertGreaterEqual(self.server.odicto_bye_at, before)

    def test_goodbye_needs_the_token(self) -> None:
        for token in (None, "wrong"):
            status, _, _ = self._post("/bye", token=token)
            self.assertEqual(status, 403)
        self.assertIsNone(self.server.odicto_bye_at)


class StartAndAdminRouteTests(_ServerCase):
    def test_start_and_admin_mode_return_json_with_the_token(self) -> None:
        start = mock.Mock(return_value={"ok": True, "message": "Odicto is starting."})
        admin = mock.Mock(return_value={"ok": True, "enabled": True, "message": "on"})
        with mock.patch.object(setup_web, "start_odicto", start), mock.patch.object(
            setup_web, "set_admin_mode", admin
        ):
            status, _, body = self._post("/start")
            self.assertEqual((status, json.loads(body)["ok"]), (200, True))
            status, _, body = self._post("/admin-mode", b"enabled=true")
            self.assertEqual((status, json.loads(body)["enabled"]), (200, True))
        start.assert_called_once_with()
        admin.assert_called_once_with(True)

    def test_start_and_admin_mode_refuse_without_the_token(self) -> None:
        start, admin = mock.Mock(), mock.Mock()
        with mock.patch.object(setup_web, "start_odicto", start), mock.patch.object(
            setup_web, "set_admin_mode", admin
        ):
            for path in ("/start", "/admin-mode"):
                status, _, _ = self._post(path, b"enabled=true", token="wrong")
                self.assertEqual(status, 403, path)
        start.assert_not_called()
        admin.assert_not_called()


class StartOdictoTests(unittest.TestCase):
    def setUp(self) -> None:
        import windows_admin

        patcher = mock.patch.object(windows_admin, "run_if_needed", return_value=None)
        self.admin = patcher.start()
        self.addCleanup(patcher.stop)

    def test_admin_mode_ensures_through_the_task_and_never_restarts(self) -> None:
        restart = mock.Mock()
        with mock.patch.object(setup_web, "restart_odicto", restart):
            for code, ok in ((0, True), (3, False)):
                self.admin.return_value = code
                result = setup_web.start_odicto()
                self.assertEqual(result["ok"], ok, code)
        self.admin.assert_called_with(str(setup_web.ROOT), "Ensure")
        restart.assert_not_called()

    def test_messages_drop_the_settings_prefix(self) -> None:
        cases = {
            "Settings saved. Odicto is restarting with the new settings.": (True, "Odicto is starting."),
            "Settings saved, but Odicto did not start (boom).": (False, "Odicto did not start (boom)."),
            "Settings saved. Start Odicto yourself to apply them.": (False, "Start Odicto yourself to apply them."),
        }
        for message, expected in cases.items():
            with mock.patch.object(setup_web, "restart_odicto", lambda m=message: m):
                result = setup_web.start_odicto()
            self.assertEqual((result["ok"], result["message"]), expected, message)

    def test_a_raised_restart_failure_becomes_a_message(self) -> None:
        def boom():
            raise RuntimeError("elevated Odicto restart failed (exit code 5)")

        with mock.patch.object(setup_web, "restart_odicto", boom):
            result = setup_web.start_odicto()
        self.assertFalse(result["ok"])
        self.assertIn("exit code 5", result["message"])


class AdminModeTests(unittest.TestCase):
    def test_unavailable_off_windows(self) -> None:
        with mock.patch.object(setup_web, "_admin_mode_state", lambda: (False, False)):
            result = setup_web.set_admin_mode(True)
        self.assertEqual((result["ok"], result["enabled"]), (False, False))

    def test_same_state_is_a_no_op(self) -> None:
        helper = types.SimpleNamespace(set_admin_mode=mock.Mock())
        with mock.patch.object(setup_web, "_admin_mode_state", lambda: (True, True)), mock.patch.dict(
            "sys.modules", {"windows_admin": helper}
        ):
            result = setup_web.set_admin_mode(True)
        self.assertTrue(result["ok"])
        helper.set_admin_mode.assert_not_called()

    def test_change_calls_the_helper_and_reports_the_new_state(self) -> None:
        helper = types.SimpleNamespace(set_admin_mode=mock.Mock(return_value=(True, "Admin windows mode is on.")))
        states = iter([(True, False), (True, True)])
        with mock.patch.object(setup_web, "_admin_mode_state", lambda: next(states)), mock.patch.dict(
            "sys.modules", {"windows_admin": helper}
        ):
            result = setup_web.set_admin_mode(True)
        self.assertEqual((result["ok"], result["enabled"]), (True, True))
        helper.set_admin_mode.assert_called_once_with(str(setup_web.ROOT), True)


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(setup_web, "ROOT", Path(self.tmp.name)),
            mock.patch.object(setup_web, "_admin_mode_state", lambda: (True, False)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _write(self, **fields) -> None:
        data = {"pid": os.getpid(), "updated_at": time.time(), "ready": True, "state": "idle"}
        data.update(fields)
        with open(os.path.join(self.tmp.name, "dictation-health.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_missing_health_file_reads_as_not_running(self) -> None:
        self.assertFalse(setup_web.runtime_status()["running"])

    def test_fresh_file_with_a_live_pid_reads_as_running(self) -> None:
        self._write(microphone={"device": {"name": "M" * 200}})
        status = setup_web.runtime_status()
        self.assertTrue(status["running"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["state"], "idle")
        self.assertEqual(len(status["microphone"]), 80)

    def test_stale_file_or_dead_pid_reads_as_not_running(self) -> None:
        self._write(updated_at=time.time() - setup_web._HEALTH_FRESH_S - 5)
        self.assertFalse(setup_web.runtime_status()["running"])
        self._write()
        with mock.patch.object(setup_web, "_pid_alive", lambda pid: False):
            self.assertFalse(setup_web.runtime_status()["running"])

    def test_malformed_files_never_raise(self) -> None:
        path = os.path.join(self.tmp.name, "dictation-health.json")
        for text in ("not json", "[]", '{"pid": "1", "updated_at": "x"}', '{"pid": true, "updated_at": true}'):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.assertFalse(setup_web.runtime_status()["running"], text)


class WatchLifetimeTests(unittest.TestCase):
    def _server(self, last_ago=0.0, bye_ago=None, clients_ago=()):
        now = time.monotonic()
        return types.SimpleNamespace(
            odicto_last_request=now - last_ago,
            odicto_bye_at=None if bye_ago is None else now - bye_ago,
            odicto_clients={"client%04d" % i: now - ago for i, ago in enumerate(clients_ago)},
            shutdown=mock.Mock(),
        )

    def _watch(self, server, wait_s=0.3) -> None:
        stop = threading.Event()
        thread = threading.Thread(
            target=setup_web._watch_lifetime,
            args=(server, stop, 60.0, 1.0),
            kwargs={"live_s": 30.0, "poll_s": 0.01},
            daemon=True,
        )
        thread.start()
        thread.join(timeout=wait_s)
        stop.set()
        thread.join(timeout=2)

    def test_goodbye_from_the_last_page_stops_after_the_grace(self) -> None:
        server = self._server(last_ago=5.0, bye_ago=2.0)
        self._watch(server)
        server.shutdown.assert_called_once_with()

    def test_goodbye_within_the_grace_does_not_stop(self) -> None:
        server = self._server(last_ago=5.0, bye_ago=0.0)
        self._watch(server, wait_s=0.2)
        server.shutdown.assert_not_called()

    def test_a_request_after_goodbye_keeps_the_server(self) -> None:
        server = self._server(last_ago=0.0, bye_ago=2.0)
        self._watch(server)
        server.shutdown.assert_not_called()

    def test_another_live_page_keeps_the_server(self) -> None:
        server = self._server(last_ago=5.0, bye_ago=2.0, clients_ago=(20.0,))
        self._watch(server)
        server.shutdown.assert_not_called()

    def test_a_page_silent_past_the_live_window_does_not_keep_it(self) -> None:
        server = self._server(last_ago=5.0, bye_ago=2.0, clients_ago=(45.0,))
        self._watch(server)
        server.shutdown.assert_called_once_with()

    def test_long_silence_stops_without_goodbye(self) -> None:
        server = self._server(last_ago=61.0)
        self._watch(server)
        server.shutdown.assert_called_once_with()


class OneServerPerComputerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = setup_web._SetupServer(("127.0.0.1", 0), setup_web._Handler)
        self.server.odicto_token = TOKEN
        self.port = self.server.server_address[1]
        self.thread = _serve(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_a_second_server_cannot_bind_the_same_port(self) -> None:
        with self.assertRaises(OSError):
            setup_web._SetupServer(("127.0.0.1", self.port), setup_web._Handler)

    def test_the_probe_finds_a_running_setup_page(self) -> None:
        self.assertEqual(setup_web._existing_setup_url(self.port), f"http://127.0.0.1:{self.port}")

    def test_the_probe_ignores_other_services_and_closed_ports(self) -> None:
        with mock.patch.object(setup_web, "runtime_status", lambda: {"service": "something-else"}):
            self.assertEqual(setup_web._existing_setup_url(self.port), "")
        closed = setup_web._SetupServer(("127.0.0.1", 0), setup_web._Handler)
        closed_port = closed.server_address[1]
        closed.server_close()
        self.assertEqual(setup_web._existing_setup_url(closed_port), "")

    def test_a_second_launch_shows_the_open_page(self) -> None:
        opened = mock.Mock()
        with mock.patch.dict(os.environ, {"SETUP_PORT": str(self.port)}), mock.patch.object(
            setup_web.webbrowser, "open", opened
        ), redirect_stdout(io.StringIO()):
            setup_web.run_server(port=self.port, open_browser=True)
        opened.assert_called_once_with(f"http://127.0.0.1:{self.port}")


if __name__ == "__main__":
    unittest.main()
