"""Make application modules importable for the test suite without starting Odicto."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# Every test entry point (run_tests.py or plain unittest) imports this package first.
# An inherited managed launch token would make importing main attach the real data-root log.
import os

os.environ.pop("ODICTO_LAUNCH_TOKEN", None)
