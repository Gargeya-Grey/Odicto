"""Launch Odicto from its application folder; retain the public entry point."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    app_dir = root / "app"
    sys.path.insert(0, str(app_dir))
    # An install in admin mode runs elevated. A normal process starts the elevated logon task
    # instead (no prompt) and exits; this decides before the app's single-instance gate.
    import windows_admin
    try:
        admin_exit = windows_admin.run_if_needed(root, "Ensure")
    except RuntimeError as error:
        # pythonw has no stderr; the failure must still exit 1 instead of raising here.
        if sys.stderr is not None:
            try:
                print("Odicto admin mode: %s" % error, file=sys.stderr)
            except (OSError, ValueError):
                pass
        sys.exit(1)
    if admin_exit is not None:
        sys.exit(admin_exit)
    runpy.run_path(str(app_dir / "main.py"), run_name="__main__")
