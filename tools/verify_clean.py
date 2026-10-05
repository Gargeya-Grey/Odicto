"""Run clean-config unit tests without moving the running install's private files."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


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
    with tempfile.TemporaryDirectory(prefix="odicto-clean-") as directory:
        clean = Path(directory)
        for folder in ("app", "tests", "assets"):
            shutil.copytree(root / folder, clean / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("main.py", "odicto.py", ".env.example", "prompt.txt.example"):
            shutil.copy2(root / name, clean / name)
        result = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_units"],
                                cwd=clean, env=environment, timeout=150)
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
