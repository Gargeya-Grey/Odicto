"""Two installs in one user session must never both own the keyboard hooks.

The install lock is scoped to one data root, so it cannot see another install. These tests
drive the real platform lock from two processes with different data roots.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest.mock import MagicMock, patch
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
        self.scopes = [self.scope]
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

    def stop(self, child):
        """Stop a child once (tearDown and the cleanup both call this)."""
        if child not in self.children:
            return
        self.children.remove(child)
        if child.poll() is None:
            try:
                child.stdin.write("\n")
                child.stdin.flush()
                child.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()
        child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            try:  # a newline still buffered for a child that already exited cannot be flushed
                stream.close()
            except OSError:
                pass

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
        for child in list(self.children):  # the files are removed only after their owners exit
            self.stop(child)
        if sys.platform == "darwin":  # macOS keeps a lock file; Linux uses an X selection
            import platforms.base as base
            for scope in self.scopes:
                with patch.dict(os.environ, {"ODICTO_HOOK_LOCK_SCOPE": scope}):
                    Path(base.hook_lock_path()).unlink(missing_ok=True)

    def test_another_scope_is_not_blocked(self):
        first, state = self.owner("install-a")
        self.assertEqual(state, "held")
        self.scope = "test-" + uuid.uuid4().hex
        self.scopes.append(self.scope)
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


class TestReleaseInstallsNoHook(unittest.TestCase):
    """keyboard.unhook_all() starts the listener, and that installs a system-wide hook."""

    def load(self, listening):
        fake = types.SimpleNamespace(KEY_DOWN="down", KEY_UP="up", unhook_all=MagicMock(),
                                     _listener=types.SimpleNamespace(listening=listening))
        spec = importlib.util.spec_from_file_location("odicto_keyboard_wrapper_tested",
                                                      ROOT / "app" / "platforms" / "_keyboard.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"keyboard": fake}):
            spec.loader.exec_module(module)
        return module, fake

    def test_a_process_that_never_hooked_does_not_start_the_listener(self):
        module, fake = self.load(listening=False)
        module.unhook_all()
        fake.unhook_all.assert_not_called()

    def test_a_process_with_hooks_removes_them(self):
        module, fake = self.load(listening=True)
        module.unhook_all()
        fake.unhook_all.assert_called_once_with()


class FakeXServer:
    """The selection table of one X server, shared by every connection to it."""

    def __init__(self):
        self.owners, self.next_id, self.reachable = {}, 1, True

    def modules(self):
        server = self
        X = types.SimpleNamespace(NONE=0, CopyFromParent=0, CurrentTime=0)

        class Window:
            def __init__(self, connection):
                server.next_id += 1
                self.id, self.connection = server.next_id, connection

            def create_window(self, *args):
                return Window(self.connection)

            def set_selection_owner(self, atom, time):
                server.owners[atom] = self

        class Display:
            def __init__(self):
                if not server.reachable:
                    raise OSError("cannot connect to the X server")
                self.root = Window(self)

            def intern_atom(self, name):
                return name

            def screen(self):
                return types.SimpleNamespace(root=self.root)

            def get_selection_owner(self, atom):
                return server.owners.get(atom, X.NONE)

            def grab_server(self):
                pass

            ungrab_server = sync = grab_server

            def close(self):  # the server frees what a closed connection owned
                for atom, owner in list(server.owners.items()):
                    if owner.connection is self:
                        del server.owners[atom]

        package = types.ModuleType("Xlib")
        package.X, package.display = X, types.SimpleNamespace(Display=Display)
        return {"Xlib": package, "Xlib.X": X, "Xlib.display": package.display}


class TestLinuxXSelectionLock(unittest.TestCase):
    """On Linux the hooks grab keys on one X server, so the lock is a selection on that server."""

    def setUp(self):
        import platforms._posix as posix
        self.posix, self.server = posix, FakeXServer()
        modules = patch.dict(sys.modules, self.server.modules())
        modules.start()
        self.addCleanup(modules.stop)
        self.addCleanup(setattr, posix, "_hook_lock_display", None)

    def take(self):
        """One more copy: a fresh connection, as a second process would make."""
        self.posix._hook_lock_display = None
        return self.posix._try_acquire_x_selection(), self.posix.lock_refusal()

    def test_a_second_connection_to_the_same_server_is_busy(self):
        self.assertTrue(self.take()[0])
        self.assertEqual(self.take(), (False, "busy"))

    def test_the_selection_is_free_once_the_owner_disconnects(self):
        self.assertTrue(self.take()[0])
        self.posix._hook_lock_display.close()
        self.assertTrue(self.take()[0])

    def test_no_x_server_is_an_error_not_another_copy(self):
        self.server.reachable = False
        with patch("builtins.print"):
            self.assertEqual(self.take(), (False, "error"))


class TestMacLockPath(unittest.TestCase):
    """macOS: one file per desktop user, beside the managed home, never inside it."""

    def path(self, uid, environment):
        homes = {501: "/Users/ada", 0: "/var/root"}
        pwd = types.SimpleNamespace(getpwuid=lambda value: types.SimpleNamespace(pw_dir=homes[value]))
        import platforms.base as base
        with patch.dict(sys.modules, {"pwd": pwd}), \
                patch.object(base.os, "getuid", create=True, return_value=uid), \
                patch.dict(os.environ, environment, clear=True):
            return base.hook_lock_path()

    def test_the_lock_is_not_inside_the_managed_home(self):
        path = self.path(501, {"ODICTO_HOOK_LOCK_SCOPE": "session"})
        support = os.path.join("/Users/ada", "Library", "Application Support")
        self.assertEqual(path, os.path.join(support, ".odicto-keyboard-hooks-session.lock"))
        self.assertFalse(path.startswith(os.path.join(support, "Odicto") + os.sep))

    def test_a_sudo_copy_uses_the_desktop_users_file(self):
        self.assertEqual(self.path(0, {"SUDO_UID": "501"}), self.path(501, {}))

    def test_sudo_uid_counts_only_for_root(self):
        self.assertEqual(self.path(501, {"SUDO_UID": "0"}), self.path(501, {}))
        self.assertTrue(self.path(0, {}).startswith("/var/root"))


class TestLockErrorIsNotAnotherOwner(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        for name in ("_LOCK_REFUSED", "_INSTANCE_LOCK_HELD"):
            self.addCleanup(setattr, main, name, getattr(main, name))

    def refuse(self, reason):
        with patch.object(self.main.platforms, "acquire_lock", return_value=False), \
                patch.object(self.main.platforms, "lock_refusal", return_value=reason), \
                patch("builtins.print"):
            self.assertFalse(self.main.acquire_single_instance_lock(10))
        return self.main._LOCK_REFUSED

    def test_a_lock_another_copy_holds_is_busy(self):
        self.assertTrue(self.refuse("busy"))

    def test_a_lock_that_cannot_be_made_is_a_failed_start(self):
        self.assertFalse(self.refuse("error"))


if __name__ == "__main__":
    unittest.main()
