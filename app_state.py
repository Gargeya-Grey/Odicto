"""Shared application state enum.

Must live in its own module (not main.py) so indicator and app always
compare against the *same* Enum class. Running `python main.py` loads the
entry file as `__main__`; `from main import AppState` would otherwise load a
second copy of the enum and every `==` check would silently fail.
"""

from enum import Enum, auto


class AppState(Enum):
    IDLE = auto()
    RECORDING = auto()
    PROCESSING = auto()
