## Install

### Windows, no terminal

Stable releases only: a pre-release does not have this file, and the file always installs the
latest stable release. Do you already use Odicto from a source folder, or a version older than
1.5.0? Then quit it and turn off its start at sign-in first: an older copy does not check for
other copies, so both could type every letter twice. Download `Install-Odicto.cmd` below and double-click it. Windows can show "Windows protected
your PC", because the file is not signed: click **More info**, then **Run anyway**. The window
installs Odicto in a few minutes. Then Odicto starts at sign-in, it is running, and Setup opens
in your browser. The Start Menu has Odicto, Odicto Setup and Stop Odicto.

### Windows, from a terminal

Download the release ZIP below and extract it. The managed installer supplies
Python and creates an `odicto` command. No system Python, Git, Node or npm is required.
Open PowerShell in the extracted `Odicto` directory and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-managed.ps1
```

### macOS / Ubuntu / Debian

Download the release ZIP below, extract it, open a terminal in the extracted `Odicto`
directory and run:

```bash
bash install-managed.sh
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

### Existing source installations

After installing, inspect the migration with:

```text
odicto migrate --source "PATH-TO-YOUR-SOURCE-CHECKOUT" --check
```

Quit that source instance and close its Setup page, then run the same command
without `--check` to import its known settings and retire verified source startup
entries. Windows admin/startup changes may prompt through UAC. Migration does not
launch dictation. Afterwards use `odicto setup` and `odicto start`, like a new user.

On macOS/Linux, retire an existing source login-startup entry before migration;
use `odicto autostart` afterward to enable the managed entry.

Never run the source and installed applications together. When both copies are 1.5.0 or
later, the second one refuses to start and says "Another copy of Odicto is running". A copy
older than 1.5.0 does not take that lock: stop it and retire its startup entries first. See
`tools/distribution/README.md` for migration, command and permissions details.
Development-only source setup is documented in `docs/development.md`.

### Your data

Keys stay in `.env` on your computer. Audio and text go only to the speech and AI providers you select. With Whisper and Ollama, nothing leaves your computer.

Optional: check the download against the `.sha256` file (`certutil -hashfile Odicto-*.zip SHA256` on Windows, `shasum -a 256` on macOS, `sha256sum` on Linux).

## Feedback

Found a bug or have an idea? [Open an issue](https://github.com/Gargeya-Grey/Odicto/issues/new). Include your operating system and the last lines of `dictation.log`.
