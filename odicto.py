"""Launch Odicto from its application folder; retain the public entry point."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    app_dir = Path(__file__).resolve().parent / "app"
    sys.path.insert(0, str(app_dir))
    import migration_guard
    try:
        migrated_exit = migration_guard.forward(app_dir.parent, sys.argv[1:])
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    if migrated_exit is not None:
        sys.exit(migrated_exit)
    runpy.run_path(str(app_dir / "odicto.py"), run_name="__main__")
