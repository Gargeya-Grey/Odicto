<p align="center">
  <img src="https://img.shields.io/badge/Windows-voice%20typing-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Windows voice typing" />
  <img src="https://img.shields.io/badge/macOS-native%20CI-41CD52?style=for-the-badge&logo=apple&logoColor=white" alt="macOS native CI" />
  <img src="https://img.shields.io/badge/Linux-X11%20CI-41CD52?style=for-the-badge&logo=linux&logoColor=white" alt="Linux X11 native CI" />
</p>

<h1 align="center">Odicto</h1>

<p align="center"><b>Tap a hotkey. Speak. Tap again. Your words appear at the cursor.</b></p>

<p align="center"><a href="https://github.com/Gargeya-Grey/Odicto/releases/latest/download/Install-Odicto.cmd"><b>Download for Windows</b></a> · free · no account · <a href="#windows-no-terminal">how to install</a></p>

Odicto is free, open-source voice typing for Windows, macOS and Linux X11. It runs
quietly in the background and works with the editor, browser or chat app you are
already using. Local Whisper is the default; cloud speech and AI features are optional.

## Install

### Windows, no terminal

1. Download [Install-Odicto.cmd](https://github.com/Gargeya-Grey/Odicto/releases/latest/download/Install-Odicto.cmd).
   Your browser can ask if you want to keep the file. Choose **Keep**.
2. Double-click the file.
3. Windows can show a blue box, "Windows protected your PC". Click **More info**, then **Run anyway**.
   Windows shows this box for apps from a publisher that has not bought a code-signing certificate.
   Odicto is free and has no certificate yet. Its code is public in this repository.
4. A window shows three steps and takes a few minutes. You do not need to type anything.
   At the end, Odicto is running, it starts when you sign in, and Setup opens in your browser.
5. Try it: click in a text box, press **Ctrl** and **\`** (the key under Esc), speak, and press them again.

If a copy of Odicto that you installed from its folder is on the computer, the installer installs
the new copy but does not start it, because two copies must never run together. Follow
[Moving from a source checkout](#moving-from-a-source-checkout). The installer finds that copy by
its Start Menu and Startup shortcuts only. If you made it without shortcuts, stop it before you run
the installer. If you run the installer again on a computer that has the new copy, it repairs that
copy and keeps your start-at-sign-in choice.

The Start Menu then has **Odicto**, **Odicto Setup** and **Stop Odicto**. You never need a terminal.
Updates install by themselves when Odicto starts. To stop Odicto starting at sign-in, turn it off
in Task Manager, **Startup apps** ("Odicto managed").

If Odicto says **Another copy of Odicto is running**, a different Odicto is already on (for
example a copy from a source folder). Right-click the Odicto icon near the clock and choose
**Quit Odicto**. Then open **Odicto** from the Start Menu. Odicto never starts a second copy,
because two copies would type every letter twice.

### Any platform, from a terminal

Download `Odicto-v<version>.zip` from the [latest release](https://github.com/Gargeya-Grey/Odicto/releases/latest),
extract it, and open a terminal in the extracted `Odicto` folder.

**Windows PowerShell**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-managed.ps1
```

**macOS / Ubuntu / Debian**

```bash
bash install-managed.sh
```

The installer supplies Python, verifies the release and installs the `odicto`
command. You do not need Git, Node, npm or a preinstalled Python. It does not start
dictation. If the command is not on PATH, use the full command path printed by the
installer. Existing unrelated commands are preserved.

The application is installed in your user account, outside the extracted folder
and outside any development checkout. Updates retain the same command and settings.

## Setup

```text
odicto setup
```

This opens the settings UI in your browser and starts its local web server.
The usual address is **http://127.0.0.1:8765**. If that port is occupied, Odicto
opens and prints the selected address. No separate server command is needed.

Choose the speech backend and microphone. Local Whisper works without an API key;
its first use may download model weights. Add a provider key only for the cloud
features you want. Configure vocabulary, Polish and hotkeys, then save.

Closing Setup shuts down its web server. Dictation is a separate background app.

## Start and dictate

```text
odicto start
```

Click a text field, tap **Ctrl+\`**, speak, then tap the same chord again. Odicto
shows its status in the small bottom-center indicator and inserts the transcript.
There is no separate activation command. You can change the shortcuts in Setup.

| Action | Default shortcut |
| --- | --- |
| Dictate | Tap **Ctrl+\`**, speak, tap again |
| Ask AI for a fresh reply | Tap **Ctrl+Shift+\`**, speak, tap again |
| Live dictation | Tap **F7** to start and again to finish |
| Polish selected text | Select text, then press **F8** |
| Continue AI memory | Hold **F6** while using the AI or dictation chord |
| Clear AI memory | **F5** |
| Cancel processing | **Esc** |

Raw and live dictation never polish automatically. On the **Polish** page, select
its model independently and record its shortcut by pressing the keys. Select text
and press the shortcut: the polished text replaces your selection. Press **Ctrl+Z**
to get the original back. If the selection changed while polish ran, your text stays
as it was.

Add names, brands and technical terms to your personal vocabulary on **Speech**
to supply spelling hints to transcription and Polish.

On **Appearance**, choose how the indicator looks: its size (1x, 1.3x or 1.5x),
its theme colour (five presets or your own colour, which also colours Setup), and
its waveform, texture, edge and shape. A live preview shows each choice. The
indicator changes when Save restarts Odicto.

## Everyday commands

| Command | What it does |
| --- | --- |
| `odicto setup` | Open settings / localhost UI |
| `odicto start` | Start background dictation |
| `odicto stop` | Stop dictation |
| `odicto restart` | Stop, then start again (it does not check for updates) |
| `odicto config` | Show the main settings and where each value came from (keys show only as `<set>`) |
| `odicto version` | Show the Odicto version |
| `odicto run status` | Show the application's runtime status |
| `odicto status` | Show the installed and previous release records |
| `odicto help` | List every command. `odicto help stop` or `odicto stop --help` shows one command. Help never runs the command. |
| `odicto update` | Check for and activate a ready update when the app is inactive |
| `odicto rollback` | Restore the previous compatible ready version |
| `odicto autostart` | Enable startup at login |
| `odicto remove-autostart` | Disable this installation's login startup |

## Updates and settings

Automatic update checks run before `start` and `setup`, at most once every eight
hours. New versions are downloaded and verified separately. A running app or open
Setup defers activation. Offline checks keep the installed version available.

Settings, provider keys and your private prompt live in a stable data directory,
separate from versioned application files. Updates preserve them. Use
`odicto auto-updates off` or `odicto auto-updates on` to control automatic checks.

See the [managed installation reference](tools/distribution/README.md) for paths,
update controls and recovery details.

## Moving from a source checkout

Install the managed application using the command above. Then inspect the migration:

```powershell
odicto migrate --source "W:\AI-Projects\Odicto" --check
```

Replace that example path with the source checkout you currently use. The checkout
must be updated to a version that includes migration guards. Quit its running
Odicto instance and close its Setup page before applying the migration:

```powershell
odicto migrate --source "W:\AI-Projects\Odicto"
```

Migration preserves the source settings and imports the known configuration files
without displaying keys. A configured destination or unknown startup entry blocks
the operation instead of being overwritten. Windows admin/startup changes may
require an owner-approved UAC prompt.

An older or unrecognized Windows login task blocks migration. Inspect its action
in Task Scheduler before retiring it, then use `odicto autostart` to enable the
managed startup entry if wanted.

If the check reports a legacy custom prompt-file reference, save the prompt in
the source Setup first. This normalizes it to `prompt.txt` before migration.

On macOS/Linux, retire the source installation's existing login-startup entry
before migrating. The check reports it when present. Enable the managed entry
afterward with `odicto autostart`.

Keep the recovery path printed by migration. After an interruption, run the same
migration command again to resume the recorded transaction.

After migration, use the same `odicto setup`, `odicto start` and `odicto update`
commands as a new installation. Keep the source checkout for development. Run one
installation at a time.

The old checkout's supported launch commands now forward to the installed app.
You can edit code and run tests there. A deliberate source runtime needs a separate
unmigrated checkout, after stopping the installed app.

## Supported platforms

| Platform | Requirements and verification |
| --- | --- |
| Windows | A working microphone. Enable Admin mode in Setup if you dictate into elevated windows. |
| macOS | Grant Accessibility and Input Monitoring when requested. Whisper runs on CPU. Native CI covers macOS 26 ARM/Intel and macOS 27.0.1. |
| Linux | Use a desktop X11 session as your normal user. Ubuntu/Debian native dependencies are handled by the installer; other distributions receive prerequisite instructions. Native CI covers Ubuntu 26.04 and a real Ubuntu 26.10 QEMU guest. |

Wayland is unsupported. Native CI checks installation, local speech decoding, Qt,
Setup HTTP, automated behavior and updates/rollback. It does not establish physical
microphone, permission-grant or genuine-editor acceptance on macOS/Linux, or testing
on every vendor-latest patch.

Changing Windows Admin mode preserves your login-startup setting and leaves
dictation stopped. Run `odicto start` after the change. You can also use
`odicto admin on` or `odicto admin off` from a terminal; Windows requests approval
when required.

Terminal detection is best-effort. Odicto types into recognized terminals rather
than assuming a paste shortcut. Multi-line shell text can execute commands. Linux
terminal text outside printable ASCII is offered in a Copy/Dismiss card before any
input is sent; normal browser/editor clipboard insertion preserves Unicode.

## Privacy

Odicto has no telemetry. Local Whisper runs on your computer; its weights download
from Hugging Face. Cloud speech and AI requests go to the providers you select.
Keys stay in your local settings. Raw dictation does not require a cloud key.

## Development

The repository holds application code in `app/`, UI assets in `assets/`, tests in
`tests/`, OS helpers in `scripts/`, distribution tooling in `tools/distribution/`,
and documentation in `docs/`. Daily use goes through the installed `odicto` command.

- [Development and advanced configuration](docs/development.md)
- [Architecture and invariants](docs/architecture.md)
- [Engineering notes](docs/engineering-notes.md)
- [Agent setup and safety rules](AGENTS.md)
- [Changelog](CHANGELOG.md)

## License

Odicto is licensed under [Apache-2.0](LICENSE). Preserve the [LICENSE](LICENSE) and
[NOTICE](NOTICE) when distributing copies or derivative work. See the license for
the complete terms.
