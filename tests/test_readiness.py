"""Readiness is a live owner plus microphone, never just a PID file."""
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import odicto


class TestReadiness(unittest.TestCase):
    def check(self, health, verified=True, created=None):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "dictation.pid"), "w") as f:
                f.write("12345")
            with open(os.path.join(directory, "dictation-health.json"), "w") as f:
                json.dump(health, f)
            owner = MagicMock()
            owner.create_time.return_value = time.time() - 20 if created is None else created
            with patch.object(odicto, "_repo_root", return_value=directory), patch(
                "psutil.Process", return_value=owner
            ), patch("platforms.base.is_odicto_command", return_value=verified):
                return odicto.cmd_wait_ready(SimpleNamespace(timeout=0))

    def health(self):
        return {"pid": 12345, "updated_at": time.time(), "ready": True,
                "microphone": {"closed": False, "callback_age_s": 0.05,
                               "callback_count": 1}}

    def test_fresh_ready_owner_and_microphone_pass(self):
        self.assertEqual(self.check(self.health()), 0)

    def test_stale_pid_or_heartbeat_cannot_pass(self):
        for update in ({"pid": 12346}, {"updated_at": time.time()-11},
                       {"updated_at": time.time()-9}, {"ready": False}):
            with self.subTest(update=update):
                self.assertEqual(self.check(self.health() | update), 1)

    def test_unavailable_microphone_cannot_pass(self):
        for update in ({"closed": True}, {"callback_age_s": 4},
                       {"callback_count": 0}):
            with self.subTest(update=update):
                health = self.health()
                health["microphone"].update(update)
                self.assertEqual(self.check(health), 1)

    def test_reused_or_unrelated_pid_cannot_pass(self):
        self.assertEqual(self.check(self.health(), verified=False), 1)
        self.assertEqual(self.check(self.health(), created=time.time()+1), 1)
