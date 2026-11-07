"""Run clean-config unit tests without moving the running install's private files."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def main():
    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "app" / "config.py").read_text(encoding="utf-8"))
    defaults = next(node for node in tree.body if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name) and node.target.id == "ENV_DEFAULTS")
    keys = ast.literal_eval(defaults.value)
    environment = {key: value for key, value in os.environ.items() if key not in keys}
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Only shipped source/fixtures enter the temporary install. Neither private
    # configuration nor live runtime evidence is opened or copied.
    clean = Path(tempfile.mkdtemp(prefix="odicto-clean-"))
    try:
        for folder in ("app", "tests", "assets", "tools", "docs", "scripts", ".github"):
            shutil.copytree(root / folder, clean / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("main.py", "odicto.py", ".env.example", "prompt.txt.example", "install.ps1", "install.sh", "install-managed.ps1", "install-managed.sh",
                     "start_dictation.bat", "setup.bat", "stop_dictation.bat",
                     "start_dictation.sh", "setup.sh", "stop_dictation.sh", ".gitignore",
                     "VERSION", "CHANGELOG.md"):
            shutil.copy2(root / name, clean / name)
        # The same suite gets 600 s in Gate 2 and verify.ps1 waits 600 s for this script;
        # stop just before that, so this run reports its own timeout and cleans up.
        result = subprocess.run([sys.executable, "-B", "tools/run_tests.py"],
                                cwd=clean, env=environment, timeout=570)
        # Windows reports a crash status (0xC0000409) unsigned; sys.exit() needs it
        # signed, or the parent sees 0xFFFFFFFF and the crash type is lost.
        code = result.returncode
        return code - (1 << 32) if code > 0x7FFFFFFF else code
    finally:
        _remove(clean)


def _remove(folder):
    """Delete the copy. Windows can hold a folder for a moment after its files are gone (an
    antivirus or indexer handle); the suite's result decides the gate, not that moment."""
    for _ in range(20):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(0.5)
    print(f"Warning: could not remove the clean copy {folder}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
