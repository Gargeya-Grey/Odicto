"""Security tests for the local setup server: token, Host/Origin, body limits."""

from __future__ import annotations

import http.client
import os
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import setup_web


class SetupSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = os.path.join(self.tmp.name, ".env")
        self.prompt_path = os.path.join(self.tmp.name, "prompt.txt")
        self.patches = [
            mock.patch.object(setup_web, "ENV_PATH", self.env_path),
            mock.patch.object(setup_web, "prompt_live_path", lambda: self.prompt_path),
            mock.patch.object(setup_web, "restart_odicto", lambda: "restart stubbed"),
            mock.patch.object(setup_web, "start_ollama_pull", mock.Mock(return_value="")),
        ]
        for p in self.patches:
            p.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_web._Handler)
        self.server.odicto_token = "test-token-abc"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _req(self, method, path, body=b"", headers=None, host="default", content_length="auto"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host == "default":
                conn.putheader("Host", f"127.0.0.1:{self.port}")
            elif host is not None:
                conn.putheader("Host", host)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if content_length == "auto":
                if method == "POST":
                    conn.putheader("Content-Length", str(len(body)))
            elif content_length is not None:
                conn.putheader("Content-Length", content_length)
            conn.endheaders()
            if body:
                try:
                    conn.send(body)
                except OSError:
                    pass
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def _post(self, path="/save", body=b"HOTKEY_TOGGLE=false", token="test-token-abc", **kw):
        headers = dict(kw.pop("headers", {}))
        if token is not None:
            headers["X-Odicto-Token"] = token
        return self._req("POST", path, body, headers, **kw)

    def test_get_serves_token(self) -> None:
        status, body = self._req("GET", "/")
        self.assertEqual(status, 200)
        m = re.search(rb'<meta name="odicto-token" content="([^"]+)"', body)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), b"test-token-abc")

    def test_missing_or_wrong_token_forbidden_no_side_effect(self) -> None:
        for token in (None, "wrong", ""):
            for path in ("/save", "/reset", "/test", "/pull-ollama"):
                status, _ = self._post(path, token=token)
                self.assertEqual(status, 403, (path, token))
        self.assertFalse(os.path.exists(self.env_path))
        self.assertFalse(os.path.exists(self.prompt_path))
        setup_web.start_ollama_pull.assert_not_called()

    def test_valid_token_header_ok(self) -> None:
        status, _ = self._post("/save", b"HOTKEY_TOGGLE=false&SYSTEM_PROMPT=Hello+there")
        self.assertEqual(status, 200)
        self.assertTrue(os.path.exists(self.env_path))
        self.assertTrue(os.path.exists(self.prompt_path))

    def test_valid_token_form_field_ok(self) -> None:
        status, _ = self._post("/reset", b"_odicto_token=test-token-abc", token=None)
        self.assertEqual(status, 200)

    def test_pull_ollama_validates_name(self) -> None:
        status, body = self._post("/pull-ollama", b"OLLAMA_MODEL=qwen2.5%3A1.5b")
        self.assertEqual(status, 200)
        setup_web.start_ollama_pull.assert_called_once_with("qwen2.5:1.5b")
        setup_web.start_ollama_pull.reset_mock()
        status, body = self._post("/pull-ollama", b"OLLAMA_MODEL=x%3B+calc.exe")
        self.assertEqual(status, 200)
        self.assertIn(b'"ok": false', body)
        setup_web.start_ollama_pull.assert_not_called()

    def test_content_length_errors(self) -> None:
        self.assertEqual(self._post(body=b"", content_length="abc")[0], 400)
        self.assertEqual(self._post(body=b"", content_length="-5")[0], 400)
        self.assertEqual(self._post(body=b"", content_length=None)[0], 411)

    def test_oversize_body_413(self) -> None:
        # Declare 2 MiB but send a few bytes: the server must answer without reading.
        status, _ = self._post(body=b"AAAA", content_length=str(2 * 1024 * 1024))
        self.assertEqual(status, 413)
        self.assertFalse(os.path.exists(self.env_path))

    def test_bad_utf8_400(self) -> None:
        self.assertEqual(self._post(body=b"A=\xff\xfe\xfd")[0], 400)

    def test_host_checks(self) -> None:
        self.assertEqual(self._post(host="evil.example")[0], 403)
        self.assertEqual(self._post(host="evil.example:%d" % self.port)[0], 403)
        self.assertEqual(self._post(host="")[0], 403)
        self.assertEqual(self._post(host=None)[0], 403)
        self.assertEqual(self._post(host="127.0.0.1:1")[0], 403)
        self.assertEqual(self._post("/reset", host=f"localhost:{self.port}")[0], 200)

    def test_cross_site_origin_forbidden(self) -> None:
        self.assertEqual(self._post(headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self._post(headers={"Referer": "http://evil.example/x"})[0], 403)
        self.assertEqual(
            self._post("/reset", headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200
        )

    def test_handler_timeout(self) -> None:
        self.assertEqual(setup_web._Handler.timeout, 15)


if __name__ == "__main__":
    unittest.main()
