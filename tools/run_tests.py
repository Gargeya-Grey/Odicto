"""Single test gate for Odicto, shared by CI, tools/verify.ps1 and tools/verify_clean.py.

Discovers every tests/test_*.py (new files are picked up automatically), runs them
verbosely, then enforces:
  (a) no failures and no errors;
  (b) total tests run >= GATE["floor"];
  (c) every skipped test matches an allow-list pattern for the current platform.

Stdlib only. Usage (from anywhere):  python tools/run_tests.py
"""
import contextlib
import faulthandler
import fnmatch
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

# ---------------------------------------------------------------------------
# The ONE place for the gate numbers.
# FLOORS MAY ONLY RISE. Never lower "floor" to make a run pass: a lower count means
# tests were deleted or are no longer discovered. Raise it when tests are added.
# "allowed_skips" maps a platform to fnmatch patterns over full test ids
# (tests.module.Class.method). Windows allows only tests that skip themselves on a developer
# machine and always run on CI: the real-clipboard round trip and the 8.3 short-path installer tests.
# Off Windows only tests that are decorated skipUnless(win32) may skip. A skipped
# "node not available" JS gate is never allowed anywhere (dead gate).
# ---------------------------------------------------------------------------
GATE = {
    # Prior floor 518, plus the setup page and release tests, the root launchers, the
    # admin switch, resting resource use, the final-review fixes and the idle release of
    # the backup Whisper: 695, the tests that every platform discovers. Plus 6 installer tests
    # that start a process through another spelling of the install folder (3 Windows-only,
    # 3 macOS/Linux-only): 701. Plus 14 HUD tests for the Odicto Mobile look and the
    # per-second motion (brand font and tokens, easing, size spring, waveform), and 3 for
    # the disc cross-fade and the wake from the slow tick: 718. Plus 1 for the cached
    # shadow of a settled pill: 719. Plus 2 for the owner's process settings at lock time: 721.
    # Windows discovers 752 (37 Windows-only: 10 more for OS modifier state and the
    # process settings), macOS/Linux 728. Plus 12 for OS state of side modifiers, F keys,
    # AltGr and is_pressed_exclusive: 733. Windows discovers 764, macOS/Linux 740.
    # Plus 4 for the setup page keeping every API key on Save whatever the AI provider
    # is: 737.
    # Vocabulary, explicit polish, upgrade safety, rootless X11 and managed updates
    # add coverage on every platform; platform-specific definitions still differ.
    # Plus 5 for the private-directory access-list policy (managed homes and migration): 1035.
    # Plus 41 for the leaf HUD: the painter (28), voice bands (6) and the HUD_* keys (7): 1076.
    # Plus 32 for the HUD widget on the leaf painter (test_hud_motion, 54 to 86): 1108.
    # Plus 20 for the setup page Appearance view (15) and its parity with the painter (5): 1128.
    # Plus 3 for the Plain texture's faint grain (painter 2, preview parity 1): 1131.
    # Plus 10 for the F7 failure message on the caption card (9) and its service rule (1): 1141.
    # Plus 22 for HUD_SIZE: painter 6, widget 7, config 2, setup page 6, preview parity 1: 1163.
    # Plus 15 for the setup page look (text contrast from the tokens, no network, one leaf painter, the shell): 1178.
    # Plus 57 for THEME_COLOR: colour maths, palette and HUD painter, config key, page parity and contrast (43),
    # the setup page Colour panel and its save (14): 1235.
    # Plus 4 for the review fixes: a grey custom colour (3) and the page maths run in Node (1): 1239.
    # Plus 4 for the update screen: tool output in update.log, steps and result, up to date, failure: 1243.
    # Plus 3 for its review fixes: an update while Odicto runs, a failed check, the help rows: 1246.
    # Plus 24 for the no-terminal Windows path: the double-click installer (4 on every platform,
    # 14 Windows-only runs of the built file) and the Start Menu shortcuts (6 Windows-only): 1270.
    # Plus 8 for the session keyboard-hook lock: two installs (4), the Linux display key (2), the
    # busy exit code shared with the updater (1), and no rollback when another copy runs (1): 1278.
    # Plus 9 for its second review: the Linux X selection (3) replaces the display key (-2), the
    # macOS path (3), no hook on release (2), busy or error (2), a refused restart (1): 1287.
    # Third review: macOS refuses root, so its sudo path tests (2) go and one root refusal test
    # comes (-1); the lock path ignores the launch environment (1); the Windows mutex outlives
    # the native hook (2, Windows only): 1289.
    # Plus 4 for its fourth review: an owner drops an inherited test scope (2), lockfile busy or
    # error (2, Windows only): 1293.
    "floor": 1293,
    "allowed_skips": {
        # The real-clipboard round trip skips itself when the developer's clipboard
        # is busy or holds data it cannot save; CI's clean clipboard always runs it.
        # The 8.3 short-path installer tests skip themselves when the temp folder's volume has
        # no 8.3 names (a Dev Drive); CI's temp folder has them, and there they fail instead.
        "win32": ["tests.test_clipboard_safety.TestWindowsRoundTrip.*",
                  "tests.test_installer.TestWindowsInstaller.test_move_aside_refuses_a_venv_program_started_through_a_short_path",
                  "tests.test_installer.TestWindowsInstaller.test_move_aside_refuses_python_running_main_py_through_a_short_path",
                  "tests.test_installer.TestWindowsInstaller.test_other_spellings_in_the_process_list_are_recognised"],
        "other": [
            "tests.test_managed_admin.TestManagedNativeBoundary.test_powershell_helpers_parse_without_execution",
            "tests.test_managed_admin.TestManagedNativeBoundary.test_powershell_task_ownership_with_fake_objects",
            # Real Windows DACLs and the elevated helper's same-account grant.
            "tests.test_managed_distribution.TestWindowsPrivateDirectoryAcl.*",
            "tests.test_windows_admin.TestHelperDecisions.*",
            "tests.test_windows_admin.TestHelperParses.*",
            "tests.test_windows_admin.TestQuitEventRoundTrip.*",
            "tests.test_launchers.TestBatFilesRun.*",
            "tests.test_easy_install.TestWindowsEasyInstallerRuns.*",
            "tests.test_easy_install.TestWindowsStartMenuShortcuts.*",
            "tests.test_session_hook_lock.TestWindowsHookMutexOutlivesTheNativeHook.*",
            "tests.test_session_hook_lock.TestWindowsLockfileRefusal.*",
            # The Linux hook lock is an X selection; a headless checkout has no X server.
            # CI runs the suite under Xvfb, so these run there.
            "tests.test_session_hook_lock.TestSessionHookLock.*",
            "tests.test_resource_use.TestBlasThreads.test_importing_main_commits_little_memory_on_windows",
            "tests.test_reliability.*.test_win32_input_union_size_and_partial_input_never_retried",
            "tests.test_process_lifecycle.TestWindowsStop.*",
            "tests.test_units.*.test_side_exclusive_scan_codes_right_ctrl",
            "tests.test_units.*.test_is_pressed_exclusive_right_ctrl",
            "tests.test_clipboard_safety.*Win*",
            # GetAsyncKeyState modifiers and the owner's Win32 process settings.
            "tests.test_responsiveness.TestModifierTruth.*",
            "tests.test_responsiveness.TestStaleShiftKeepsRawChordRaw.*",
            "tests.test_responsiveness.TestExclusiveSideTruth.*",
            "tests.test_responsiveness.TestPasteWaitIgnoresStaleSides.*",
            "tests.test_responsiveness.TestKeepResponsive.*",
            # macOS forces CPU under WHISPER_DEVICE=auto, so the CUDA->CPU fallback
            # path does not exist there (runs on Windows and Linux).
            "tests.test_units.*.test_whisper_transcriber_loading_fallback",
        ],
    },
}

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def _elevation_probe_boundary():
    """Keep the gate off the real UAC prompt once an install has opted in to always-admin.

    With .odicto-admin-enabled present, windows_admin.run_if_needed() launches the elevated
    helper from an unelevated process, and the older tests that replace only the spawn/kill
    boundaries (start, stop, restart) would reach that launch. On Windows the whole run
    therefore sees "already elevated" from the native probe windows_admin._is_user_an_admin,
    the one native call that decides it. Nothing under test is disabled: run_if_needed and its
    callers still run, and the admin regressions (tests/test_windows_admin.py) patch the probe
    themselves to false / raising / true, which overrides this default for their duration.
    Windows tests always mock native boundaries; the real elevated lifecycle is verified
    separately at runtime, never by this gate. Other platforms are unchanged.
    """
    if sys.platform != "win32":
        yield
        return
    import windows_admin  # tests/__init__ put app/ on sys.path during discovery
    with unittest.mock.patch.object(windows_admin, "_is_user_an_admin", return_value=True):
        yield


@contextlib.contextmanager
def _source_migration_boundary():
    """A migrated checkout must never forward a test into the real installed owner."""
    import migration_guard
    original_forward = migration_guard.forward
    original_check = migration_guard.check_source
    def bounded(operation):
        def invoke(root, *args, **kwargs):
            if Path(root).resolve() == ROOT.resolve():
                raise AssertionError("Test reached the real source migration boundary; isolate the fixture's routing")
            return operation(root, *args, **kwargs)
        return invoke
    with unittest.mock.patch.object(migration_guard, "forward", side_effect=bounded(original_forward)), \
            unittest.mock.patch.object(migration_guard, "check_source", side_effect=bounded(original_check)):
        yield


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # An inherited managed launch token would make importing main attach the real data-root log.
    os.environ.pop("ODICTO_LAUNCH_TOKEN", None)
    # The keyboard-hook lock is per user session. A gate run must never meet the owner's real
    # one (it would refuse) or hold it (the owner could not start); child processes inherit this.
    os.environ["ODICTO_HOOK_LOCK_SCOPE"] = "gate-%d" % os.getpid()
    for stream in (sys.stdout, sys.stderr):
        try:  # a cp1252 console must not crash on non-ASCII text in a traceback
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    # A native crash, also after GATE OK during most of interpreter teardown, prints every
    # thread's stack. A crash in late DLL teardown prints nothing; the exit code still shows it.
    try:
        faulthandler.enable(all_threads=True)
    except (AttributeError, ValueError, RuntimeError, OSError):
        pass
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    # Capture thread stacks from a long-running suite before the parent deadline.
    faulthandler.dump_traceback_later(160, repeat=True)
    try:
        with _elevation_probe_boundary(), _source_migration_boundary():
            result = runner.run(suite)
    finally:
        faulthandler.cancel_dump_traceback_later()
        if sys.platform == "darwin":  # the run's own hook-lock file (Linux uses an X selection)
            try:
                from platforms import base
                os.remove(base.hook_lock_path())
            except (ImportError, OSError):
                pass

    problems = []
    if result.failures or result.errors:
        problems.append("%d failure(s), %d error(s):" % (len(result.failures), len(result.errors)))
        problems += ["  - " + test.id() for test, _ in result.failures + result.errors]

    if result.testsRun < GATE["floor"]:
        problems.append("only %d tests ran, floor is %d (tests deleted or not discovered?)"
                        % (result.testsRun, GATE["floor"]))

    key = "win32" if sys.platform == "win32" else "other"
    allowed = GATE["allowed_skips"][key]
    bad_skips = [(test.id(), reason) for test, reason in result.skipped
                 if not any(fnmatch.fnmatchcase(test.id(), pattern) for pattern in allowed)]
    if bad_skips:
        problems.append("%d unexpected skip(s) on %s (a skipped gate is not a passing gate):"
                        % (len(bad_skips), sys.platform))
        problems += ["  - %s (%s)" % item for item in bad_skips]

    print("\nGATE: ran %d, skipped %d (floor %d)" % (result.testsRun, len(result.skipped), GATE["floor"]))
    if problems:
        print("GATE FAILED:")
        print("\n".join(problems))
        return 1
    print("GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
