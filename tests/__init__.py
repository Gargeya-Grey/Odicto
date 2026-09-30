"""Make application modules importable for the test suite without starting Odicto."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
