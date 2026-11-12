# Install Odicto

This guide takes you from nothing installed to your first dictation. You do not need
Python, Git or any other tool. You need an internet connection, a microphone and a few minutes.

| Your computer | How you install | State of this path |
| --- | --- | --- |
| [Windows 10 or 11](#windows) | Double-click one file. No terminal. | Used daily. Automated tests run on Windows Server 2025. |
| [macOS](#macos) | Paste one command in Terminal. | Automated tests install it on macOS 26 and 27 test machines. Those machines have no microphone and grant no permissions. |
| [Ubuntu or Debian](#ubuntu-or-debian) | Paste one command in a terminal. Needs an X11 session. | Automated tests install it on Ubuntu 26.04 and 26.10 test machines with a virtual X11 screen. |

Other Linux distributions and Wayland sessions are not supported yet.
The work to make macOS and Linux as easy as Windows is in
[the install plan](plans/zero-prerequisite-install-macos-linux.md).

## Windows

1. Download [Install-Odicto.cmd](https://github.com/Gargeya-Grey/Odicto/releases/latest/download/Install-Odicto.cmd).
   If the browser asks, choose **Keep**.
2. Double-click the file.
3. Windows can show a blue box, "Windows protected your PC". Click **More info**, then **Run anyway**.
   Windows shows this box because Odicto has no paid code-signing certificate.
4. A window shows three steps. Wait a few minutes. Do not type anything.
5. At the end, Odicto runs, it starts when you sign in, and Odicto Setup opens in your browser.

Go to [Your first dictation](#your-first-dictation).

The Start Menu has **Odicto**, **Odicto Setup** and **Stop Odicto**. You never need a terminal.

Did you use Odicto before from a source folder (a copy made with Git)? Read
[Windows, no terminal](../README.md#windows-no-terminal) in the README first. Two copies must never run together.

## macOS

A person has not yet done these steps on a real Mac and reported the result. If a step
does not work, please [open an issue](https://github.com/Gargeya-Grey/Odicto/issues/new).

Did you use Odicto before from a source folder (a copy made with Git)? Stop that copy and turn off
its start at login before you continue, then follow
[Moving from a source checkout](../README.md#moving-from-a-source-checkout) after step 5.
Two copies must never run together: each letter that you type would show twice.

1. Open the [latest release](https://github.com/Gargeya-Grey/Odicto/releases/latest). Under **Assets**,
   click `Odicto-v<version>.zip`.
2. Open your **Downloads** folder. If you see the zip file and no `Odicto` folder, double-click the zip file.
   You now have a folder named `Odicto`.
3. Open Terminal: press **Command + Space**, type `Terminal`, press **Return**.
4. Type `cd` and one space. Do not press Return yet. Drag the `Odicto` folder from Finder into the
   Terminal window. Now press **Return**.
5. Copy this line, paste it in Terminal and press **Return**:

   ```bash
   bash install-managed.sh
   ```

   Wait a few minutes. The last line says `Odicto v<version> is active.`
6. Give Odicto its permissions. Without them Odicto does not start, and macOS does not always ask
   by itself. Open **System Settings > Privacy & Security**.
   - Open **Accessibility**. Turn on **Terminal**. If Terminal is not in the list, click **+**,
     choose **Applications > Utilities > Terminal** and turn it on.
   - Open **Input Monitoring**. Do the same for **Terminal**.

   Accessibility lets Odicto put text at the cursor. Input Monitoring lets Odicto see its shortcut keys.
   Then quit Terminal (**Command + Q**) and open it again.
7. Open Setup. Paste this line, with its quotation marks, and press **Return**:

   ```bash
   "$HOME/Library/Application Support/Odicto/bin/odicto" setup
   ```

   Your browser shows Odicto Setup. Choose your microphone and click **Save**.
   Local Whisper works without a key or an account.
8. Start Odicto:

   ```bash
   "$HOME/Library/Application Support/Odicto/bin/odicto" start
   ```

   If the answer has `Cannot start Odicto [macos_accessibility]` or `[macos_input_monitoring]`,
   that permission is still off. Do step 6 again. If the list in System Settings now shows an entry
   named **Python** or **Odicto**, turn that entry on too. Then give the start command again.
   When macOS asks for the **Microphone**, click **Allow**.
9. Optional. To start Odicto each time you log in:

   ```bash
   "$HOME/Library/Application Support/Odicto/bin/odicto" autostart
   ```

Go to [Your first dictation](#your-first-dictation).

## Ubuntu or Debian

Odicto needs an **X11** session. It does not work in a Wayland session.

Did you use Odicto before from a source folder (a copy made with Git)? Stop that copy and turn off
its start at login before you continue, then follow
[Moving from a source checkout](../README.md#moving-from-a-source-checkout) after step 6.
Two copies must never run together: each letter that you type would show twice.

1. Open a terminal: press **Ctrl + Alt + T**.
2. Check your session. Paste this line and press **Enter**:

   ```bash
   echo $XDG_SESSION_TYPE
   ```

   - `x11`: continue with step 3.
   - `wayland`: log out. On the login screen, click your name, then click the gear icon at the
     bottom right and choose the entry with **Xorg** in its name. Log in and do this step again.
     If the login screen has no Xorg entry, Odicto cannot run on this system yet. Stop here.
3. Open the [latest release](https://github.com/Gargeya-Grey/Odicto/releases/latest). Under **Assets**,
   click `Odicto-v<version>.zip`.
4. Open the **Downloads** folder in Files. Right-click the zip file and choose **Extract Here**.
   You now have a folder named `Odicto`. With some versions of Files it is inside a folder
   named `Odicto-v<version>`.
5. Right-click the `Odicto` folder and choose **Open in Terminal**.
6. Paste this line and press **Enter**:

   ```bash
   bash install-managed.sh
   ```

   The installer can ask for your password. It uses the password only to install system
   packages for sound, the clipboard and the display. Wait a few minutes.
   The last line says `Odicto v<version> is active.`
7. Open Setup:

   ```bash
   ~/.local/share/odicto/bin/odicto setup
   ```

   Your browser shows Odicto Setup. Choose your microphone and click **Save**.
8. Start Odicto:

   ```bash
   ~/.local/share/odicto/bin/odicto start
   ```

9. Optional. To start Odicto each time you log in:

   ```bash
   ~/.local/share/odicto/bin/odicto autostart
   ```

Do not run the installer with `sudo`, and do not run Odicto as root.

## Your first dictation

1. Click in a text box, for example a new email.
2. Press **Ctrl** and **\`** together. On a US or UK keyboard the **\`** key is under **Esc**, left of **1**.
   On another keyboard layout, use the key that types **\`**, or choose another shortcut in Odicto Setup.
   A small indicator shows at the bottom of the screen.
3. Speak.
4. Press **Ctrl** and **\`** again. Your words appear at the cursor.

The first dictation can take longer, because Odicto downloads the Whisper speech model (about 75 MB).

The [README](../README.md#start-and-dictate) lists the other shortcuts and the everyday commands.

## If something does not work

First, ask Odicto. It lists the problems that it can see:

| System | Command |
| --- | --- |
| Windows | Press **Windows key + R**, type `cmd /k ""%LOCALAPPDATA%\Odicto\bin\odicto.cmd" run status"` and press **Enter** |
| macOS | `"$HOME/Library/Application Support/Odicto/bin/odicto" run status` |
| Linux | `~/.local/share/odicto/bin/odicto run status` |

| What you see | What to do |
| --- | --- |
| Windows: "Windows protected your PC" | Click **More info**, then **Run anyway**. |
| Windows: "Another copy of Odicto is running" | Right-click the Odicto icon near the clock and choose **Quit Odicto**. Then open **Odicto** from the Start Menu. |
| Every letter that you type shows twice | Two copies of Odicto run. Stop each copy: this copy with **Stop Odicto** or the `stop` command, a source copy with its own stop script. Turn off the source copy's start at login. Check that none is left: on Windows, Task Manager (Details) shows no `pythonw.exe`; on macOS and Linux, `pgrep -fl main.py` prints nothing. Then start one copy, one time. |
| macOS: `Cannot start Odicto [macos_accessibility]` or `[macos_input_monitoring]` | Do step 6 of the macOS list again, then give the start command again. |
| macOS: the indicator shows, but no words appear | Check **System Settings > Privacy & Security > Microphone**. |
| Linux: the installer says it supports Ubuntu and Debian | Your distribution is not supported by this installer yet. |
| Linux: the installer or Odicto says the desktop runs a Wayland session | Log in to an Xorg session (step 2 of the Linux list). |
| `odicto: command not found` | Use the full command from this guide. On macOS keep its quotation marks. |
| The installer stops with a download error | Check the internet connection and run the same installer again. A second run is safe. |

Still stuck? [Open an issue](https://github.com/Gargeya-Grey/Odicto/issues/new). Give your operating
system and its version, and the text of the error. Do not paste API keys.

## Updates

Odicto checks for a new version when it starts, at most one time in eight hours, and installs it
by itself. Your settings stay. See [Updates and settings](../README.md#updates-and-settings).
