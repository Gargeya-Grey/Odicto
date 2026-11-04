"""The installed `odicto` command: its help lists every command, and asking for help never acts."""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "distribution" / "manager.py"
SPEC = importlib.util.spec_from_file_location("odicto_cli_help_tested", MODULE_PATH)
dist = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dist
SPEC.loader.exec_module(dist)

HOME = "synthetic-home-that-does-not-exist"


def run_main(*argv):
    """(exit code, stdout, stderr) of the manager's main(), as the launcher calls it."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = dist.main(["--home", HOME, *argv])
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue(), err.getvalue()


class FakeManager:
    """Stands in for Manager. Building it, or running a command, is what help must never do."""
    built = 0
    ran: list = []

    def __init__(self, *args, **kwargs):
        type(self).built += 1

    def active(self):
        return {"directory": "synthetic"}

    def bind_dependencies(self):
        pass

    def run(self, command, *, automatic_updates=True):
        type(self).ran.append((list(command), automatic_updates))
        return 0


class TestCliHelp(unittest.TestCase):
    def setUp(self):
        FakeManager.built = 0
        FakeManager.ran = []
        patcher = patch.object(dist, "Manager", FakeManager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_guide_names_every_command(self):
        """A new command that is missing from the guide fails here."""
        code, out, _ = run_main("--help")
        self.assertEqual(code, 0)
        _, _, error = run_main("not-a-command")
        # Python 3.11 quotes the choices ('install', 'run'); 3.12.8+ does not (install, run).
        choices = error.split("choose from", 1)[1].split(")", 1)[0]
        manager_commands = set(re.findall(r"[a-z][a-z-]*", choices))
        app_commands = set(dist._app_parser()._subparsers._group_actions[0].choices)
        self.assertTrue({"install", "update", "rollback", "migrate", "run", "help"} <= manager_commands)
        self.assertTrue({"setup", "start", "stop", "restart", "status", "config"} <= app_commands)
        for name in (manager_commands | app_commands) - {"admin-task", "wait-ready"}:
            self.assertRegex(out, rf"odicto (run )?{re.escape(name)}\b", f"the guide omits {name}")
        for text in ("odicto setup", "odicto start", "odicto stop", "odicto help", "--help", "Ctrl+`"):
            self.assertIn(text, out)
        self.assertNotIn("admin-task", out)

    def test_the_guide_has_no_rows_for_advanced_flags(self):
        code, out, _ = run_main("--help")
        self.assertEqual(code, 0)
        self.assertNotIn("--home", out)
        self.assertNotIn("show this help message", out)
        self.assertIn("Appearance", out)

    def test_help_for_each_command_never_builds_or_runs_anything(self):
        app_commands = ("setup", "start", "restart", "stop", "status", "wait-ready", "config",
                        "version", "autostart", "remove-autostart")
        cases = [("run", name, flag) for name in app_commands for flag in ("--help", "-h")]
        cases += [("help", name) for name in app_commands]
        cases += [("help", name) for name in ("update", "rollback", "admin", "migrate", "run")]
        cases += [("help",), ("--help",), ("update", "--help"), ("migrate", "--help")]
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = run_main(*argv)
                self.assertEqual(code, 0, err)
                self.assertTrue(out.startswith("usage:"), out)
        self.assertEqual(FakeManager.built, 0, "help built a Manager")
        self.assertEqual(FakeManager.ran, [], "help ran a command")

    def test_every_abbreviation_of_help_shows_help_and_never_reaches_the_manager(self):
        """argparse accepts --h, --he, --hel. `odicto stop --h` must not stop anything."""
        app_commands = ("setup", "start", "restart", "stop", "status", "wait-ready", "config",
                        "version", "autostart", "remove-autostart")
        for name in app_commands:
            for flag in ("-h", "--h", "--he", "--hel", "--help"):
                with self.subTest(command=name, flag=flag):
                    code, out, err = run_main("run", name, flag)
                    self.assertEqual(code, 0, err)
                    self.assertTrue(out.startswith("usage:"), out)
        for argv in (("run", "stop", "--h", "extra"), ("run", "--no-auto-updates", "stop", "--he"),
                     ("run", "--", "stop", "--hel"), ("run", "wait-ready", "--timeout", "5", "--h")):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(*argv)[0], 0)
        self.assertEqual(FakeManager.built, 0, "an abbreviated help flag built a Manager")
        self.assertEqual(FakeManager.ran, [], "an abbreviated help flag ran a command")

    def test_the_guide_names_the_command_path_of_the_install_that_was_asked_about(self):
        for home in ("first-install-home", "second-install-home"):
            with self.subTest(home=home):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                    dist.main(["--home", home, "--help"])
                self.assertIn(str(Path(home) / "bin"), out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            dist.main(["--home=equals-form-home", "--help"])  # the --home=VALUE spelling
        self.assertIn("equals-form-home", out.getvalue())

    def test_help_makes_only_claims_the_code_keeps(self):
        """Wording the reviews found untrue; each line names the code that decides it."""
        _, out, _ = run_main("--help")
        _, start, _ = run_main("help", "start")
        _, restart, _ = run_main("help", "restart")
        _, stop, _ = run_main("help", "stop")
        _, config, _ = run_main("help", "config")
        _, admin, _ = run_main("help", "admin")
        self.assertNotIn("No Odicto processes found", stop)  # admin-mode stop prints nothing then
        self.assertNotIn("one Odicto runs at a time", start)  # the lock is per install
        self.assertIn("one Odicto install at a time", start)
        self.assertIn("does not check for", restart)  # Manager.run updates only before start/setup
        self.assertNotIn("same as 'odicto stop' followed", restart)
        self.assertNotIn("every setting", out + config)  # SETUP_PORT is not shown; vocabulary is a count
        self.assertIn("main settings", config)
        self.assertIn("If it already has that", admin)  # configure_admin returns early on a no-op
        self.assertIn("When the mode changes", out)

    def test_stop_help_is_about_stop(self):
        code, out, _ = run_main("run", "stop", "--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: odicto stop", out)
        self.assertIn("Stops every Odicto process", out)
        code, out, _ = run_main("help", "stop")
        self.assertIn("usage: odicto stop", out)

    def test_stray_words_and_unknown_commands_are_refused_before_anything_runs(self):
        for argv in (("run", "stop", "now"), ("run", "start", "--fast"), ("run", "bogus"),
                     ("run", "bogus", "--help"), ("help", "bogus"), ("bogus",), ()):
            with self.subTest(argv=argv):
                code, out, err = run_main(*argv)
                self.assertEqual(code, 2)
                self.assertIn("odicto --help", err)
        self.assertEqual(FakeManager.built, 0)
        self.assertEqual(FakeManager.ran, [])

    def test_real_commands_still_reach_the_manager_unchanged(self):
        cases = (
            (("run", "stop"), ["stop"], True),
            (("run", "start"), ["start"], True),
            (("run", "--no-auto-updates", "start"), ["start"], False),
            (("run", "--", "setup"), ["setup"], True),
            (("run", "wait-ready", "--timeout", "5", "--launch-token", "t", "--expected-root", "r"),
             ["wait-ready", "--timeout", "5", "--launch-token", "t", "--expected-root", "r"], True),
        )
        for argv, expected, automatic in cases:
            with self.subTest(argv=argv):
                FakeManager.ran = []
                self.assertEqual(run_main(*argv)[0], 0)
                self.assertEqual(FakeManager.ran, [(expected, automatic)])

    def test_help_text_matches_the_application_parser(self):
        """The commands the help describes are the ones the application accepts."""
        source = (MODULE_PATH.parents[2] / "app" / "odicto.py").read_text(encoding="utf-8")
        application = set(re.findall(r'sub\.add_parser\("([a-z-]+)"', source))
        self.assertEqual(application, set(dist._app_parser()._subparsers._group_actions[0].choices))


if __name__ == "__main__":
    unittest.main()
