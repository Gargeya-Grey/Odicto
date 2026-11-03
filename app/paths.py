"""Install paths shared by application modules, independent of working directory."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_state_root = os.environ.get("ODICTO_DATA_ROOT", "")
DATA_ROOT = Path(_state_root).expanduser() if _state_root else ROOT
if not DATA_ROOT.is_absolute():
    raise RuntimeError("ODICTO_DATA_ROOT must be an absolute directory")
