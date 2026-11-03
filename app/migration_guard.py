"""Source entrypoints cannot resurrect a migrated install."""
import os
from pathlib import Path
import subprocess
import sys


def check_source(root):
    if os.environ.get("ODICTO_MANAGED_HOME"):
        return None
    root = Path(root)
    if not (root / ".odicto-migration.json").exists() and not (root / ".odicto-migration.json").is_symlink():
        return None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.distribution.migration import validated_forward
    launcher = validated_forward(root)
    if launcher is not None:
        raise RuntimeError(f"This source install was migrated. Use the installed command: {launcher}")
    return None


def forward(root, arguments):
    if os.environ.get("ODICTO_MANAGED_HOME"):
        return None
    root = Path(root)
    if not (root / ".odicto-migration.json").exists() and not (root / ".odicto-migration.json").is_symlink():
        return None
    from tools.distribution.migration import validated_forward
    launcher = validated_forward(root)
    if launcher is None:
        raise RuntimeError("Migration receipt could not be validated")
    if sys.platform == "win32":
        command = [os.path.join(os.environ["SystemRoot"], "System32/WindowsPowerShell/v1.0/powershell.exe"),
                   "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher.with_suffix(".ps1")), *arguments]
    else:
        command = [str(launcher), *arguments]
    return subprocess.run(command, check=False).returncode
