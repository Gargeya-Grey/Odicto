import time
import pyperclip
import keyboard
from config import Config


def paste_text(text: str) -> None:
    """Injects text at the current active cursor position using clipboard emulation.

    Backs up the user's current clipboard content, writes the target text,
    sends Ctrl+V, waits briefly for the target app to consume the paste,
    then restores the original clipboard.

    Args:
        text: The string content to paste.
    """
    if not text:
        return

    try:
        original_clipboard: str = pyperclip.paste()
    except Exception as e:
        print(f"Warning: Failed to read from clipboard for backup: {e}")
        original_clipboard = ""

    try:
        pyperclip.copy(text)

        # Brief settle so the OS clipboard has the new payload before paste.
        time.sleep(0.01)
        keyboard.send("ctrl+v")

        # Give the focused app time to read clipboard before we restore it.
        delay = max(0.02, float(Config.PASTE_DELAY_SECONDS))
        time.sleep(delay)
    except Exception as e:
        print(f"Error: Failed to perform paste simulation: {e}")
    finally:
        try:
            pyperclip.copy(original_clipboard)
        except Exception as e:
            print(f"Warning: Failed to restore original clipboard: {e}")
