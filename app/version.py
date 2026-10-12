"""Odicto's own version: the single line in VERSION at the install root (MAJOR.MINOR.PATCH)."""
import re

from paths import ROOT

VERSION_PATH = ROOT / "VERSION"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def app_version() -> str:
    """The installed version, or "0.0.0" when VERSION is missing or malformed."""
    try:
        text = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return text if VERSION_RE.match(text) else "0.0.0"
