"""Single test gate for Odicto, shared by CI, tools/verify.ps1 and tools/verify_clean.py.

Discovers every tests/test_*.py (new files are picked up automatically), runs them
verbosely, then enforces:
  (a) no failures and no errors;
  (b) total tests run >= GATE["floor"];
  (c) every skipped test matches an allow-list pattern for the current platform.

Stdlib only. Usage (from anywhere):  python tools/run_tests.py
"""
import fnmatch
import os
import sys
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# The ONE place for the gate numbers.
# FLOORS MAY ONLY RISE. Never lower "floor" to make a run pass: a lower count means
# tests were deleted or are no longer discovered. Raise it when tests are added.
# "allowed_skips" maps a platform to fnmatch patterns over full test ids
# (tests.module.Class.method). Windows allows only the self-skipping real-clipboard round trip.
# Off Windows only tests that are decorated skipUnless(win32) may skip. A skipped
# "node not available" JS gate is never allowed anywhere (dead gate).
# ---------------------------------------------------------------------------
GATE = {
    "floor": 438,  # raised after the October review fixes (315 when the gate was introduced)
    "allowed_skips": {
        # The real-clipboard round trip skips itself when the developer's clipboard
        # is busy or holds data it cannot save; CI's clean clipboard always runs it.
        "win32": ["tests.test_clipboard_safety.TestWindowsRoundTrip.*"],
        "other": [
            "tests.test_reliability.*.test_win32_input_union_size_and_partial_input_never_retried",
            "tests.test_process_lifecycle.TestWindowsStop.*",
            "tests.test_units.*.test_side_exclusive_scan_codes_right_ctrl",
            "tests.test_units.*.test_is_pressed_exclusive_right_ctrl",
            "tests.test_clipboard_safety.*Win*",
            # macOS forces CPU under WHISPER_DEVICE=auto, so the CUDA->CPU fallback
            # path does not exist there (runs on Windows and Linux).
            "tests.test_units.*.test_whisper_transcriber_loading_fallback",
        ],
    },
}

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    for stream in (sys.stdout, sys.stderr):
        try:  # a cp1252 console must not crash on non-ASCII text in a traceback
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

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
