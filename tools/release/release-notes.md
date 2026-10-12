## Install

### Windows 10 / 11

1. Download the `.zip` file below. Unzip it to a folder you keep, for example `Documents\Odicto`.
2. In that folder, right-click `install.ps1` and select **Run with PowerShell**. Or run `powershell -ExecutionPolicy Bypass -File .\install.ps1`. The installer sets up Python packages and the speech model.
3. Double-click `setup.bat`. The setup page opens in your browser. Add a key on **Connections** if you want cloud speech or AI answers, then select **Save**.
4. Double-click `start_dictation.bat`. Tap **Ctrl+`**, speak, then tap again. The text appears where your cursor is.

Stop Odicto with `stop_dictation.bat`. The setup page stops by itself when you close it.

### macOS / Linux

Run `bash install.sh`, then `bash setup.sh` and `bash start_dictation.sh`. Stop with `bash stop_dictation.sh`. macOS asks for Accessibility and Input Monitoring permissions. Linux needs X11 and root for the global hotkey.

### Your data

Keys stay in `.env` on your computer. Audio and text go only to the speech and AI providers you select. With Whisper and Ollama, nothing leaves your computer.

Optional: check the download against the `.sha256` file (`certutil -hashfile Odicto-*.zip SHA256` on Windows, `shasum -a 256` on macOS, `sha256sum` on Linux).

## Feedback

Found a bug or have an idea? [Open an issue](https://github.com/Gargeya-Grey/Odicto/issues/new). Include your operating system and the last lines of `dictation.log`.
