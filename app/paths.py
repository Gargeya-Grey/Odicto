"""Install paths shared by application modules, independent of working directory."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
