## Install

### Windows, no terminal

Stable releases only: a pre-release does not have this file, and the file always installs the
latest stable release. Download `Install-Odicto.cmd` below and double-click it. Windows can show
"Windows protected your PC", because the file is not signed: click **More info**, then **Run
anyway**. The window installs Odicto in a few minutes. Then Odicto starts at sign-in, it is
running, and Setup opens in your browser. The Start Menu has Odicto, Odicto Setup and Stop Odicto.
An Odicto that this installer or `install-managed.ps1` installed in its default folder is
updated in place. A copy in your own folder (`-HomePath`) is not: update it with its
`odicto update` instead, and do not run this file.

Do you already use Odicto from a source folder? Quit it first and turn off its start at sign-in: its Startup shortcut, and the `Odicto` task in Task
Scheduler if you used Admin mode.
The installer does not see that task. A source copy older than 1.5.0
does not check for other copies, so never run it beside the new copy. When the installer finds
the source copy's Start Menu or Startup shortcut, it installs the new copy but does not start
it, turn on start at sign-in or open Setup. Then follow "Existing source installations" below.

### Windows, from a terminal

Download the release ZIP below and extract it. The managed installer supplies
Python and creates an `odicto` command. No system Python, Git, Node or npm is required.
Open PowerShell in the extracted `Odicto` directory and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-managed.ps1
```

### macOS, Ubuntu, Debian and Linux Mint: one line

Stable releases only: the file always installs the latest stable release. Open a terminal
(on a Mac: press Command + Space, type `Terminal`, press Return; on Linux: press Ctrl + Alt + T).
On a Mac, paste this line and press Return:

```bash
curl -fsSL https://github.com/Gargeya-Grey/Odicto/releases/latest/download/install-odicto.sh | bash
```

On Ubuntu, Debian or Linux Mint, paste this line and press Enter (Ubuntu has `wget`, not `curl`):

```bash
wget -qO- https://github.com/Gargeya-Grey/Odicto/releases/latest/download/install-odicto.sh | bash
```

It needs no Python, Homebrew or Git. Linux asks for your password once, only to install system
packages. In a few minutes Odicto starts at sign-in, it is running, and Setup opens in your browser.
On a Mac, macOS asks for Accessibility and Input Monitoring: turn both on, then paste the line again.
When the installer finds a source copy of Odicto (its start-at-sign-in entry, or a copy that runs
now), it installs the new copy but does not start it. Then follow "Existing source installations" below.

Linux needs an X11 (Xorg) desktop. Ubuntu 25.10 and newer have no Xorg session, so they are not
supported yet. Odicto in WSL is for testing only: its keys work only while a Linux window has focus.

### macOS and Linux, from the release ZIP

Download the release ZIP below, extract it, open a terminal in the extracted `Odicto`
directory and run:

```bash
bash install-managed.sh
```

Use the command path printed by the installer to run `odicto setup`, then
`odicto start`. On macOS, grant Accessibility and Input Monitoring when requested.

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
