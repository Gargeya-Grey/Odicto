"""Launch Odicto from its application folder; retain the public entry point."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    app_dir = Path(__file__).resolve().parent / "app"
    sys.path.insert(0, str(app_dir))
    runpy.run_path(str(app_dir / "main.py"), run_name="__main__")
