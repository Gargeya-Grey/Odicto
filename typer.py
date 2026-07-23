import time
import pyperclip
import keyboard
from config import Config


def get_selected_text() -> str:
    """Copies the currently selected text and returns it.

    Sends Ctrl+C to copy the selection to the clipboard, reads it, then
    restores the original clipboard content. Returns an empty string when
    nothing was selected (clipboard unchanged).

    Returns:
        str: The selected text, or "" if no selection was active.
    """
    try:
        original_clipboard: str = pyperclip.paste()
    except Exception as e:
        print(f"Warning: Failed to read clipboard for backup: {e}")
        original_clipboard = ""

    try:
        keyboard.send("ctrl+c")
        # Brief settle so the OS clipboard has the new payload before reading.
        time.sleep(0.05)
        selected = pyperclip.paste()
    except Exception as e:
        print(f"Error: Failed to copy selection: {e}")
        selected = ""
    finally:
        try:
            pyperclip.copy(original_clipboard)
        except Exception as e:
            print(f"Warning: Failed to restore original clipboard: {e}")

    # If the clipboard didn't change, nothing meaningful was selected.
    if selected == original_clipboard:
        return ""
    return selected


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
