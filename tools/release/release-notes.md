## Install

Download the release ZIP below and extract it. The managed installer supplies
Python and creates an `odicto` command. No system Python, Git, Node or npm is required.

### Windows

Open PowerShell in the extracted `Odicto` directory and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\distribution\bootstrap.ps1
```

### macOS / Ubuntu / Debian

Open a terminal in the extracted `Odicto` directory and run:

```bash
bash tools/distribution/bootstrap.sh
```

Use the command path printed by the installer to run `odicto setup`, then
`odicto start`. On macOS, grant Accessibility and Input Monitoring when requested.
Linux requires an X11 desktop. Wayland is unsupported.

### Updates and rollback

The managed command checks for updates before launch. It stages verified versions
separately and activates them after Odicto and Setup release their locks. Settings
stay in a stable data directory. Run `odicto update` to check immediately, or
`odicto rollback` to restore the previous ready version. Automatic checks can be
disabled with `odicto auto-updates off`.

Quit an existing source installation and disable its startup entry before using
the managed install. Their settings are separate. Never run both together. See
`tools/distribution/README.md` for the complete command and permissions guide.

### Source installation on Windows

1. Download the `.zip` file below. Unzip it to a folder you keep, for example `Documents\Odicto`.
2. In that folder, right-click `install.ps1` and select **Run with PowerShell**. Or run `powershell -ExecutionPolicy Bypass -File .\install.ps1`. The installer sets up Python packages and the speech model.
3. Double-click `setup.bat`. The setup page opens in your browser. Add a key on **Connections** if you want cloud speech or AI answers, then select **Save**.
4. Double-click `start_dictation.bat`. Tap **Ctrl+`**, speak, then tap again. The text appears where your cursor is.

Stop Odicto with `stop_dictation.bat`. The setup page stops by itself when you close it.

### Source installation on macOS / Linux

Run `bash install.sh`, then `bash setup.sh` and `bash start_dictation.sh`. Stop with `bash stop_dictation.sh`. macOS asks for Accessibility and Input Monitoring permissions. Linux needs an X11 desktop session and runs as your normal user. Wayland is unsupported.

### Your data

Keys stay in `.env` on your computer. Audio and text go only to the speech and AI providers you select. With Whisper and Ollama, nothing leaves your computer.

Optional: check the download against the `.sha256` file (`certutil -hashfile Odicto-*.zip SHA256` on Windows, `shasum -a 256` on macOS, `sha256sum` on Linux).

## Feedback

Found a bug or have an idea? [Open an issue](https://github.com/Gargeya-Grey/Odicto/issues/new). Include your operating system and the last lines of `dictation.log`.
