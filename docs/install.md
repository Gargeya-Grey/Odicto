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
6. Open Setup. Paste this line and press **Return**:

   ```bash
   ~/.local/bin/odicto setup
   ```

   Your browser shows Odicto Setup. Choose your microphone and click **Save**.
   Local Whisper works without a key or an account.
7. Start Odicto:

   ```bash
   ~/.local/bin/odicto start
   ```

8. macOS asks for permissions. Odicto needs three, all in **System Settings > Privacy & Security**:
   - **Accessibility**, so that Odicto can put text at the cursor.
   - **Input Monitoring**, so that Odicto can see its shortcut keys.
   - **Microphone**, so that Odicto can hear you.

   Turn on the entry that macOS shows for Odicto. Its name can be **Terminal** or **Python**.
   Then restart Odicto:

   ```bash
   ~/.local/bin/odicto restart
   ```

9. Optional. To start Odicto each time you log in:

   ```bash
   ~/.local/bin/odicto autostart
   ```

Go to [Your first dictation](#your-first-dictation).

## Ubuntu or Debian

Odicto needs an **X11** session. It does not work in a Wayland session.

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
   ~/.local/bin/odicto setup
   ```

   Your browser shows Odicto Setup. Choose your microphone and click **Save**.
8. Start Odicto:

   ```bash
   ~/.local/bin/odicto start
   ```

9. Optional. To start Odicto each time you log in:

   ```bash
   ~/.local/bin/odicto autostart
   ```

Do not run the installer with `sudo`, and do not run Odicto as root.

## Your first dictation

1. Click in a text box, for example a new email.
2. Press **Ctrl** and **\`** together. The **\`** key is under **Esc**, left of **1**.
   A small indicator shows at the bottom of the screen.
3. Speak.
4. Press **Ctrl** and **\`** again. Your words appear at the cursor.

The first dictation can take longer, because Odicto downloads the Whisper speech model (about 75 MB).

The [README](../README.md#start-and-dictate) lists the other shortcuts and the everyday commands.

## If something does not work

First, ask Odicto. It lists the problems that it can see:

| System | Command |
| --- | --- |
| Windows | Press **Windows key + R**, type `cmd /k %LOCALAPPDATA%\Odicto\bin\odicto.cmd run status` and press **Enter** |
| macOS, Linux | `~/.local/bin/odicto run status` |

| What you see | What to do |
| --- | --- |
| Windows: "Windows protected your PC" | Click **More info**, then **Run anyway**. |
| Windows: "Another copy of Odicto is running" | Right-click the Odicto icon near the clock and choose **Quit Odicto**. Then open **Odicto** from the Start Menu. |
| Every letter that you type shows twice | Two copies of Odicto run. Stop Odicto, check that no Odicto icon stays, then start it one time. |
| macOS: the indicator says **Allow Accessibility** or **Allow Input Monitoring** | Do step 8 of the macOS list again, then restart Odicto. |
| macOS: the indicator shows, but no words appear | Check **System Settings > Privacy & Security > Microphone**. |
| Linux: the installer says it supports Ubuntu and Debian | Your distribution is not supported by this installer yet. |
| Linux: Odicto says Wayland is not supported | Log in to an Xorg session (step 2 of the Linux list). |
| `odicto: command not found` | Use the full path: `~/.local/bin/odicto`. |
| The installer stops with a download error | Check the internet connection and run the same installer again. A second run is safe. |

Still stuck? [Open an issue](https://github.com/Gargeya-Grey/Odicto/issues/new). Give your operating
system and its version, and the text of the error. Do not paste API keys.

## Updates

Odicto checks for a new version when it starts, at most one time in eight hours, and installs it
by itself. Your settings stay. See [Updates and settings](../README.md#updates-and-settings).
