"""Two installs in one user session must never both own the keyboard hooks.

The install lock is scoped to one data root, so it cannot see another install. These tests
drive the real platform lock from two processes with different data roots.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]

OWNER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    import platforms
    ok = platforms.acquire_lock(500)
    print("held" if ok and platforms.lock_is_held() else "refused", flush=True)
    if ok:
        sys.stdin.readline()
        platforms.release_lock()
        print("released", flush=True)
""")


class TestSessionHookLock(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.scope = "test-" + uuid.uuid4().hex
        self.children = []

    def owner(self, name):
        root = Path(self.temporary.name) / name
        root.mkdir()
        environment = dict(os.environ, ODICTO_DATA_ROOT=str(root), ODICTO_HOOK_LOCK_SCOPE=self.scope)
        child = subprocess.Popen([sys.executable, "-c", OWNER, str(ROOT / "app")], cwd=root, env=environment,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.addCleanup(self.stop, child)
        return child, child.stdout.readline().strip()

    @staticmethod
    def stop(child):
        if child.poll() is None:
            try:
                child.stdin.write("\n")
                child.stdin.flush()
                child.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()
        child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()

    def test_a_second_install_cannot_take_the_lock_while_the_first_holds_it(self):
        first, state = self.owner("install-a")
        self.assertEqual(state, "held", first.stderr.read() if state != "held" else "")
        second, state = self.owner("install-b")
        self.assertEqual(state, "refused")

    def test_the_second_install_can_take_it_after_the_first_releases_it(self):
        first, state = self.owner("install-a")
        self.assertEqual(state, "held")
        first.stdin.write("\n")
        first.stdin.flush()
        self.assertEqual(first.stdout.readline().strip(), "released")
        second, state = self.owner("install-b")
        self.assertEqual(state, "held")

    def test_the_lock_is_free_again_when_the_first_owner_dies(self):
        first, state = self.owner("install-a")
        self.assertEqual(state, "held")
        first.kill()
        first.wait(timeout=10)
        time.sleep(0.2)
        second, state = self.owner("install-b")
        self.assertEqual(state, "held")

    def tearDown(self):
        if sys.platform == "darwin":  # macOS keeps a lock file; Linux uses an abstract socket
            from unittest.mock import patch
            import platforms.base as base
            with patch.dict(os.environ, {"ODICTO_HOOK_LOCK_SCOPE": self.scope}):
                Path(base.hook_lock_path()).unlink(missing_ok=True)

    def test_another_scope_is_not_blocked(self):
        first, state = self.owner("install-a")
        self.assertEqual(state, "held")
        self.scope = "test-" + uuid.uuid4().hex
        second, state = self.owner("install-b")
        self.assertEqual(state, "held")


class TestRefusalExitCode(unittest.TestCase):
    def test_the_owner_and_the_updater_agree_on_the_busy_exit_code(self):
        import importlib.util
        import main
        spec = importlib.util.spec_from_file_location("odicto_manager_exit_code", ROOT / "tools" / "distribution" / "manager.py")
        manager = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = manager
        spec.loader.exec_module(manager)
        self.assertEqual(main.EXIT_ANOTHER_OWNER, 75)
        self.assertEqual(manager.EXIT_ANOTHER_OWNER, 75)


class TestLinuxDisplayKey(unittest.TestCase):
    """On Linux the hooks belong to one X server, so the lock is keyed to the display."""

    def test_spellings_of_one_local_display_share_a_key(self):
        import platforms.base as base
        keys = {base.hook_lock_display_key(value) for value in (":0", ":0.0", "unix:0", "unix:0.1")}
        self.assertEqual(keys, {"local-0"})

    def test_other_displays_have_other_keys(self):
        import platforms.base as base
        self.assertEqual(base.hook_lock_display_key(":1"), "local-1")
        self.assertEqual(base.hook_lock_display_key("localhost:10.0"), "localhost-10")
        self.assertEqual(base.hook_lock_display_key(""), "none")
        self.assertEqual(base.hook_lock_display_key("bad name:x/1"), "badname-x1")


if __name__ == "__main__":
    unittest.main()
