import os
import sys
import tempfile
import threading
import time
from typing import Optional, Tuple

import keyboard
from app_state import AppState
from config import Config, parse_hold_hotkey
from recorder import AudioRecorder, play_beep
from transcriber import WhisperTranscriber
from refiner import TextRefiner
from typer import paste_text, get_selected_text

# Redirect stdout/stderr to a log file if running under pythonw.exe (no console)
if sys.stdout is None:
    try:
        log_filepath = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "dictation.log"
        )
        sys.stdout = open(log_filepath, "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    except Exception:
        pass


# The `keyboard` library aliases side-specific modifiers onto both sides
# (e.g. "right ctrl" scan codes include left ctrl's 29). That makes
# is_pressed("right ctrl") true whenever *either* Ctrl is down — breaking AI mode.
_SIDE_COUNTERPARTS = {
    "right ctrl": "left ctrl",
    "right control": "left ctrl",
    "left ctrl": "right ctrl",
    "left control": "right ctrl",
    "right shift": "left shift",
    "left shift": "right shift",
    "right alt": "left alt",
    "left alt": "right alt",
}


def side_exclusive_scan_codes(key: str) -> Tuple[int, ...]:
    """Scan codes for a key with left/right modifier aliases stripped when possible."""
    key_n = key.strip().lower()
    codes = set(keyboard.key_to_scan_codes(key_n))
    other = _SIDE_COUNTERPARTS.get(key_n)
    if other is None:
        return tuple(codes)
    other_codes = set(keyboard.key_to_scan_codes(other))
    exclusive = codes - other_codes
    # Left-side keys are often a subset of the aliased right mapping; keep them as-is.
    return tuple(exclusive if exclusive else codes)


def is_pressed_exclusive(key: str) -> bool:
    """Like keyboard.is_pressed, but distinguishes left vs right Ctrl/Shift/Alt."""
    try:
        codes = side_exclusive_scan_codes(key)
        if not codes:
            return bool(keyboard.is_pressed(key))
        return any(keyboard.is_pressed(code) for code in codes)
    except Exception:
        try:
            return bool(keyboard.is_pressed(key))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# STRICT single-instance lock (layered)
#
# Odicto uses keyboard.hook_key(..., suppress=True). On Windows that installs a
# system-wide low-level keyboard hook (WH_KEYBOARD_LL). Every keystroke in every
# app is routed through that hook — including normal typing while Odicto is idle.
# A second Odicto process installs a second hook; keys are then delivered twice
# ("tthhiiss").
#
# Layers (all must succeed before hook_key):
#   1) Kill other main.py for this install (best-effort cleanup)
#   2) Named mutex (OS-enforced; survives races better than a PID file)
#   3) Exclusive lock file (second gate if mutex namespace is split by elevation)
# Rule: never bind hotkeys unless _INSTANCE_LOCK_HELD is True after both 2+3.
# ---------------------------------------------------------------------------
_INSTANCE_MUTEX_HANDLE: Optional[int] = None
_INSTANCE_LOCK_FILE = None  # open file object holding msvcrt byte lock
_INSTANCE_LOCK_HELD: bool = False
_INSTANCE_MUTEX_NAME: str = ""

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_ERROR_ALREADY_EXISTS = 183


def _install_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _install_digest() -> str:
    import hashlib

    return hashlib.sha256(
        os.path.normcase(os.path.normpath(_install_root())).encode("utf-8")
    ).hexdigest()[:16]


def _mutex_names_for_install() -> Tuple[str, ...]:
    """Prefer Global\\ (shared across integrity levels), fall back to Local\\."""
    digest = _install_digest()
    return (
        f"Global\\Odicto_SingleInstance_{digest}",
        f"Local\\Odicto_SingleInstance_{digest}",
    )


def _mutex_name_for_install() -> str:
    """Canonical name used in logs/tests (Global preference)."""
    return _mutex_names_for_install()[0]


def _lock_file_path() -> str:
    return os.path.join(_install_root(), "dictation.lock")


def _enumerate_odicto_pids(exclude_pid: Optional[int] = None) -> set:
    """PIDs of python/pythonw running this install's main.py (excludes self)."""
    import subprocess

    my_pid = os.getpid()
    if exclude_pid is None:
        exclude_pid = my_pid
    root_fwd = os.path.normcase(os.path.normpath(_install_root())).replace("\\", "/")
    found: set = set()

    if sys.platform != "win32":
        return found

    # Filter at WMI (python* only) — much faster than enumerating every process.
    # Format: ProcessId<TAB>CommandLine  (tab avoids comma-in-path issues)
    ps = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process -Filter "
        "\"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
        "ForEach-Object { "
        "  if ($_.CommandLine) { "
        "    Write-Output (($_.ProcessId).ToString() + \"`t\" + $_.CommandLine) "
        "  } "
        "}"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=0x08000000,
        )
        if result.returncode != 0 or not result.stdout:
            return found
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            pid_s, cmd = line.split("\t", 1)
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid == exclude_pid:
                continue
            cmd_n = os.path.normcase(cmd.replace('"', "").replace("\\", "/"))
            if "main.py" in cmd_n and root_fwd in cmd_n:
                found.add(pid)
    except Exception as e:
        print(f"Warning: process enum failed: {e}")

    return found


def kill_other_odicto_processes(pid_file: Optional[str] = None) -> list:
    """Force-kill every other python/pythonw running this install's main.py.

    Returns the list of PIDs we attempted to kill.
    """
    import subprocess

    my_pid = os.getpid()
    pids_to_kill: set = set()

    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            if old_pid != my_pid:
                pids_to_kill.add(old_pid)
        except Exception as e:
            print(f"Warning: Could not read PID file: {e}")
        try:
            os.remove(pid_file)
        except Exception:
            pass

    pids_to_kill |= _enumerate_odicto_pids()

    killed: list = []
    for pid in sorted(pids_to_kill):
        print(f"Killing stale Odicto instance (PID {pid})...")
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            killed.append(pid)
        except Exception as e:
            print(f"Warning: Could not kill PID {pid}: {e}")

    if killed:
        # Let Windows tear down WH_KEYBOARD_LL hooks before we re-bind.
        time.sleep(0.45)
    return killed


def _try_acquire_mutex(timeout_ms: int) -> bool:
    """Create/open named mutex and WaitForSingleObject. Sets globals on success.

    Uses the first namespace we can create (Global preferred, else Local). We do
    **not** fall through Global→Local after a busy wait — that would let two
    processes each own a different mutex and both bind hooks.
    """
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_NAME

    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = None
    name = ""
    for candidate in _mutex_names_for_install():
        h = kernel32.CreateMutexW(None, False, candidate)
        if h:
            handle = h
            name = candidate
            break

    if not handle:
        _INSTANCE_MUTEX_HANDLE = None
        _INSTANCE_MUTEX_NAME = ""
        return False

    last_err = kernel32.GetLastError()
    if last_err == _ERROR_ALREADY_EXISTS:
        print(
            f"Mutex busy ({name}); stopping orphans, then waiting...",
            flush=True,
        )
        kill_other_odicto_processes(os.path.join(_install_root(), "dictation.pid"))

    wait = kernel32.WaitForSingleObject(handle, int(timeout_ms))
    if wait in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        _INSTANCE_MUTEX_HANDLE = int(handle)
        _INSTANCE_MUTEX_NAME = name
        return True

    try:
        kernel32.CloseHandle(handle)
    except Exception:
        pass
    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_NAME = ""
    return False


def _try_acquire_lockfile() -> bool:
    """Exclusive byte-lock on dictation.lock (released automatically on process death)."""
    global _INSTANCE_LOCK_FILE

    if sys.platform != "win32":
        return True

    import msvcrt

    path = _lock_file_path()
    try:
        # 'a+b' creates if missing; lock 1 byte at start.
        fh = open(path, "a+b")
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        # Record owner for debugging.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n".encode("ascii", errors="ignore"))
            fh.flush()
        except Exception:
            pass
        _INSTANCE_LOCK_FILE = fh
        return True
    except Exception as e:
        print(f"Warning: lock file acquire failed: {e}")
        return False


def acquire_single_instance_lock(timeout_ms: int = 8000) -> bool:
    """Take exclusive ownership of this install. False ⇒ must not bind keyboard hooks."""
    global _INSTANCE_LOCK_HELD

    if sys.platform != "win32":
        # Unsupported platform; still allow tests/import without a mutex.
        _INSTANCE_LOCK_HELD = True
        return True

    if not _try_acquire_mutex(timeout_ms):
        print(
            "!!! FATAL: Could not acquire Odicto single-instance mutex.\n"
            "    Two copies would stack system-wide keyboard hooks and double every\n"
            "    typed character (even when not dictating). Run stop_dictation.bat,\n"
            "    then start only once.",
            flush=True,
        )
        _INSTANCE_LOCK_HELD = False
        return False

    # Second gate: if Admin vs non-Admin split the mutex namespace, the lock file
    # in the install dir still serializes them (same path).
    if not _try_acquire_lockfile():
        print(
            "!!! FATAL: dictation.lock is held by another process.\n"
            "    Refusing to bind keyboard hooks. Run stop_dictation.bat first.",
            flush=True,
        )
        # Release mutex we just took so the other instance is not disturbed wrongfully
        # after we exit — actually other instance holds lockfile so we're the interloper.
        _release_mutex_only()
        _INSTANCE_LOCK_HELD = False
        return False

    _INSTANCE_LOCK_HELD = True
    print(
        f"Single-instance lock acquired (mutex={_INSTANCE_MUTEX_NAME}, lockfile=dictation.lock).",
        flush=True,
    )
    return True


def _release_mutex_only() -> None:
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_NAME

    if sys.platform == "win32" and _INSTANCE_MUTEX_HANDLE:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex(_INSTANCE_MUTEX_HANDLE)
            kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
        except Exception:
            pass
    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_NAME = ""


def release_single_instance_lock() -> None:
    """Release mutex + lockfile + drop all keyboard hooks so typing returns to normal."""
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_LOCK_FILE, _INSTANCE_LOCK_HELD
    global _INSTANCE_MUTEX_NAME

    try:
        keyboard.unhook_all()
    except Exception:
        pass

    if _INSTANCE_LOCK_FILE is not None:
        try:
            import msvcrt

            _INSTANCE_LOCK_FILE.seek(0)
            msvcrt.locking(_INSTANCE_LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            _INSTANCE_LOCK_FILE.close()
        except Exception:
            pass
        _INSTANCE_LOCK_FILE = None

    if sys.platform == "win32" and _INSTANCE_MUTEX_HANDLE:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            if _INSTANCE_LOCK_HELD:
                kernel32.ReleaseMutex(_INSTANCE_MUTEX_HANDLE)
            kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
        except Exception:
            pass

    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_NAME = ""
    _INSTANCE_LOCK_HELD = False


def ensure_can_bind_hotkeys() -> None:
    """Final gate immediately before keyboard.hook_key — raises if not exclusive owner."""
    if not _INSTANCE_LOCK_HELD or _INSTANCE_MUTEX_HANDLE is None:
        raise RuntimeError(
            "Refusing keyboard.hook_key: single-instance lock not held. "
            "Duplicate hooks double every keystroke system-wide."
        )
    # Confirm lockfile still ours (handle open).
    if sys.platform == "win32" and _INSTANCE_LOCK_FILE is None:
        raise RuntimeError(
            "Refusing keyboard.hook_key: dictation.lock not held."
        )


class DictationApp:
    def __init__(self) -> None:
        """Initializes the background dictation app, setting up state and loading model instances."""
        print("==================================================")
        print("              Initializing Odicto               ")
        print("==================================================")

        self.temp_dir: str = tempfile.gettempdir()
        self.audio_filepath: str = os.path.join(
            self.temp_dir, "dictation_recording.wav"
        )
        self.pid_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "dictation.pid"
        )

        self.state: AppState = AppState.IDLE
        self.state_lock: threading.Lock = threading.Lock()
        self.last_status: Optional[str] = None
        self._last_cycle_end: float = 0.0
        self._record_started_at: float = 0.0
        self.use_llm: bool = False
        self.ready: bool = False

        # Hold-to-talk chord bookkeeping (set during hotkey bind).
        # Dictation chord (HOTKEY) and optional AI chord (AI_HOTKEY) share one primary key.
        self._hotkey_modifiers: tuple = ()
        self._ai_hotkey_modifiers: tuple = ()
        self._hotkey_primary: str = ""
        self._hotkey_physically_held: bool = False

        self.ollama_process = None
        self.recorder: Optional[AudioRecorder] = None
        self.transcriber: Optional[WhisperTranscriber] = None
        self.refiner: Optional[TextRefiner] = None
        self.indicator = None

        # Instantiate indicator immediately so BOOTING UI appears while models load.
        if Config.SHOW_VISUAL_INDICATOR:
            try:
                from indicator import DictationIndicator

                self.indicator = DictationIndicator(self)
                print(
                    f"HUD enabled (python={sys.executable})",
                    flush=True,
                )
            except Exception as e:
                print(f"!!! Failed to start visual indicator: {e}", file=sys.stderr)
                self.indicator = None
        else:
            print("HUD disabled (SHOW_VISUAL_INDICATOR=false)")

        threading.Thread(
            target=self.initialize_app, daemon=True, name="dictation-init"
        ).start()

    # ------------------------------------------------------------------ UI push
    def _notify_ui(self) -> None:
        """Push current state to the indicator on the Qt UI thread (non-blocking)."""
        indicator = self.indicator
        if indicator is None:
            return
        try:
            indicator.notify_state_changed()
        except Exception:
            pass

    def _set_state(self, new_state: AppState) -> None:
        """Update app state and immediately notify the indicator."""
        self.state = new_state
        self._notify_ui()

    # ------------------------------------------------------------------ boot
    def initialize_app(self) -> None:
        """Runs the slow model loading and server initialization in a background thread."""
        # STRICT: never install keyboard hooks without exclusive single-instance ownership.
        # __main__ acquires first; unit tests call initialize_app() directly so we
        # acquire here only if the lock is not already held (never double-wait).
        if not _INSTANCE_LOCK_HELD:
            kill_other_odicto_processes(self.pid_file)
            if not acquire_single_instance_lock():
                print(
                    "!!! FATAL: single-instance lock not held — refusing init/hooks.",
                    file=sys.stderr,
                    flush=True,
                )
                self.last_status = "error"
                self._notify_ui()
                return
        else:
            # __main__ already killed orphans + acquired the lock before constructing
            # this app, so there is nothing left to clean up here.
            pass

        try:
            with open(self.pid_file, "w") as f:
                f.write(str(os.getpid()))
        except Exception as e:
            print(f"Warning: Could not write PID file: {e}")

        if Config.LLM_PROVIDER == "ollama":
            self._ensure_ollama_running()

        try:
            self.recorder = AudioRecorder(
                sample_rate=Config.SAMPLE_RATE,
                channels=Config.CHANNELS,
            )
            self.transcriber = WhisperTranscriber()
            self.refiner = TextRefiner()
            self.refiner.preload()
        except Exception as e:
            print(f"!!! Fatal init error: {e}", file=sys.stderr)
            self.last_status = "error"
            self._notify_ui()
            return

        # Bind global press/release hooks for hold-to-talk (ctrl+grave / ctrl+shift+grave).
        try:
            self._bind_hotkeys()
        except Exception as e:
            print(f"!!! Failed to bind hotkey '{Config.HOTKEY}': {e}", file=sys.stderr)
            self.last_status = "error"
            self._notify_ui()
            return

        self.ready = True

        if self.indicator is not None:
            try:
                # Fade out the boot HUD; thread-safe via Qt signals inside hide_indicator path
                self.indicator.notify_state_changed()
                # Explicit hide once ready (idle, no last_status → hidden)
                self.indicator.hide_indicator()
            except Exception:
                pass

        print("--------------------------------------------------")
        print(f"Application ready! Global Hotkey: '{Config.HOTKEY}'")
        print(
            f"  - Hold '{Config.HOTKEY}': RECORD and paste raw Whisper transcript."
        )
        if Config.AI_HOTKEY:
            print(
                f"  - Hold '{Config.AI_HOTKEY}': RECORD and paste AI-refined response."
            )
        elif Config.AI_MODIFIER:
            print(
                f"  - Hold '{Config.HOTKEY}+{Config.AI_MODIFIER}': "
                "RECORD and paste AI-refined response."
            )
        print("Press Ctrl+C in this terminal window to terminate.")
        print("==================================================")

    def _mods_held(self, mods: tuple) -> bool:
        """True if every listed modifier is currently down (empty mods → True)."""
        if not mods:
            return True
        try:
            return all(keyboard.is_pressed(m) for m in mods)
        except Exception:
            return False

    def _match_active_chord(self) -> Optional[bool]:
        """Which hold-to-talk chord is active at primary-key press time.

        Returns:
            True  → AI chord (AI_HOTKEY or HOTKEY+AI_MODIFIER)
            False → dictation chord (HOTKEY)
            None  → no chord; let the key through for normal typing
        """
        # Prefer the more-specific AI chord when both could match
        # (e.g. ctrl+shift+grave vs ctrl+grave — shift+ctrl also satisfies ctrl).
        if self._ai_hotkey_modifiers:
            if self._mods_held(self._ai_hotkey_modifiers):
                return True
            if self._mods_held(self._hotkey_modifiers):
                return False
            return None

        # Legacy: HOTKEY + optional AI_MODIFIER extra key
        if not self._mods_held(self._hotkey_modifiers):
            return None
        if Config.AI_MODIFIER and is_pressed_exclusive(Config.AI_MODIFIER):
            return True
        return False

    def _bind_hotkeys(self) -> None:
        """Hook the primary key; mode is chosen by which modifier chord is held.

        For chords like ``ctrl+grave`` / ``ctrl+shift+grave`` we hook ``grave``
        (the `` ` `` key) and require the matching modifiers. The key is only
        suppressed when a chord matches, so bare `` ` `` still types normally.
        """
        dict_mods, primary = parse_hold_hotkey(Config.HOTKEY)
        self._hotkey_modifiers = dict_mods
        self._hotkey_primary = primary
        self._hotkey_physically_held = False

        if Config.AI_HOTKEY:
            ai_mods, ai_primary = parse_hold_hotkey(Config.AI_HOTKEY)
            if ai_primary != primary:
                raise ValueError(
                    f"AI_HOTKEY primary '{ai_primary}' != HOTKEY primary '{primary}'"
                )
            self._ai_hotkey_modifiers = ai_mods
        else:
            self._ai_hotkey_modifiers = ()

        ensure_can_bind_hotkeys()

        def primary_handler(event: object) -> bool:
            event_type = getattr(event, "event_type", None)
            if event_type == keyboard.KEY_DOWN:
                match = self._match_active_chord()
                if match is None:
                    return True  # no chord — allow normal typing (e.g. bare `)
                if self._hotkey_physically_held:
                    return False  # key-repeat while held
                self._hotkey_physically_held = True
                self.on_press(use_llm=match)
                return False  # suppress so ` does not leak into the focused app
            if event_type == keyboard.KEY_UP:
                if not self._hotkey_physically_held:
                    return True
                self._hotkey_physically_held = False
                self.on_release()
                return False
            return True

        # suppress=True installs a system-wide LL keyboard hook (all keys, all apps).
        # Only one process may do this — guarded by _INSTANCE_LOCK_HELD above.
        keyboard.hook_key(primary, primary_handler, suppress=True)

        print(
            f"Hotkeys bound: primary='{primary}' "
            f"dictation_mods={list(dict_mods) or '(none)'} "
            f"ai_mods={list(self._ai_hotkey_modifiers) or Config.AI_MODIFIER or '(none)'} "
            f"(single-instance lock held)",
            flush=True,
        )

    def _kill_stale_instance(self) -> None:
        """Kill every other Odicto main.py for this install (see kill_other_odicto_processes)."""
        kill_other_odicto_processes(self.pid_file)

    def _ensure_ollama_running(self) -> None:
        """Starts a local Ollama server if port 11434 is not already listening."""
        import socket
        import subprocess

        port_open = False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", 11434))
                port_open = True
            except Exception:
                pass

        if port_open:
            print("Ollama server is already running on port 11434.")
            return

        print("Ollama server is offline. Spawning Ollama server process...")
        try:
            CREATE_NO_WINDOW = 0x08000000
            self.ollama_process = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            print("Waiting for Ollama server to boot...")
            boot_start = time.time()
            while time.time() - boot_start < 10.0:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    try:
                        s.connect(("127.0.0.1", 11434))
                        print("Ollama server is active and port 11434 is bound!")
                        return
                    except Exception:
                        time.sleep(0.2)
            print("Warning: Ollama did not become ready within 10s.")
        except Exception as e:
            print(f"Warning: Failed to launch Ollama server: {e}")

    def run(self) -> None:
        """Blocks the main thread running the indicator event loop or keyboard wait."""
        try:
            if self.indicator is not None:
                self.indicator.start()
            else:
                keyboard.wait()
        except KeyboardInterrupt:
            print("\nReceived termination signal. Shutting down dictation app...")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        """Release resources, keyboard hooks, PID file, and any Ollama we spawned."""
        self.ready = False
        try:
            if self.recorder is not None and self.recorder.recording:
                self.recorder.stop()
        except Exception:
            pass

        # Drop system-wide hooks ASAP so normal typing is not filtered by a dying process.
        try:
            keyboard.unhook_all()
        except Exception:
            pass

        self._cleanup_temp_file()

        if os.path.exists(self.pid_file):
            try:
                os.remove(self.pid_file)
            except Exception:
                pass

        if getattr(self, "ollama_process", None) is not None:
            print("Shutting down Ollama server to free system memory...")
            try:
                import subprocess

                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.ollama_process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
            except Exception as e:
                print(f"Warning: Failed to terminate Ollama process tree: {e}")

    def _cleanup_temp_file(self) -> None:
        """Removes the temporary WAV recording file if it exists."""
        if os.path.exists(self.audio_filepath):
            try:
                os.remove(self.audio_filepath)
            except Exception as e:
                print(f"Warning: Failed to clean up temporary audio file: {e}")

    # ----------------------------------------------------------- hotkey handlers
    def on_press(self, event: object = None, use_llm: Optional[bool] = None) -> None:
        """Handler triggered when the hold-to-talk primary key goes down.

        Args:
            use_llm: When provided by the chord matcher, selects AI vs raw mode.
                     When None (tests / legacy), falls back to live modifier checks.
        """
        if not self.ready or self.recorder is None:
            return

        with self.state_lock:
            if self.state != AppState.IDLE:
                return

            # Cooldown prevents accidental double-fires right after a cycle ends.
            now = time.monotonic()
            cooldown_s = Config.RETRIGGER_COOLDOWN_MS / 1000.0
            if now - self._last_cycle_end < cooldown_s:
                return

            if use_llm is None:
                matched = self._match_active_chord()
                self.use_llm = bool(matched) if matched is not None else False
            else:
                self.use_llm = bool(use_llm)

            self._record_started_at = now
            self.last_status = None
            self._set_state(AppState.RECORDING)

            if Config.PLAY_AUDIO_CUES:
                threading.Thread(
                    target=play_beep, args=(880.0, 0.08), daemon=True, name="beep-start"
                ).start()

            try:
                self.recorder.start()
            except Exception as e:
                print(f"!!! Failed to start recorder: {e}", file=sys.stderr)
                self.last_status = "error"
                self._set_state(AppState.IDLE)
                return

            mode_str = "AI refined" if self.use_llm else "raw dictation"
            print(f"\n>>> Recording ({mode_str})... (Hold key and speak)")

    def on_release(self, event: object = None) -> None:
        """Handler triggered when the hotkey is physically released."""
        with self.state_lock:
            if self.state == AppState.PROCESSING:
                print(
                    "!!! System busy. Still refining previous transcription. Please wait..."
                )
                return

            if self.state != AppState.RECORDING or self.recorder is None:
                return

            hold_ms = (time.monotonic() - self._record_started_at) * 1000.0
            if hold_ms < Config.MIN_HOLD_MS:
                # Accidental tap — discard without processing.
                try:
                    self.recorder.stop()
                except Exception:
                    pass
                if self.recorder is not None:
                    self.recorder.clear()
                self.last_status = None
                self._set_state(AppState.IDLE)
                # Debounce rapid accidental taps too: stamp the cycle end here so
                # RETRIGGER_COOLDOWN_MS applies even when no pipeline ever ran.
                self._last_cycle_end = time.monotonic()
                print(">>> Hold too short; ignored.")
                return

            self._set_state(AppState.PROCESSING)

            if Config.PLAY_AUDIO_CUES:
                threading.Thread(
                    target=play_beep, args=(440.0, 0.08), daemon=True, name="beep-stop"
                ).start()

            # Hot path: keep audio in memory only (no disk write).
            success: bool = self.recorder.stop(filepath=None)
            if not success:
                print("!!! Warning: No audio captured. Resetting to idle.")
                self.last_status = "empty"
                self._set_state(AppState.IDLE)
                return

            # Snapshot mode flag for the worker so a future press can't flip it mid-flight.
            use_llm = self.use_llm
            audio = self.recorder.last_audio_array

            print(">>> Processing transcription and refinement...")
            threading.Thread(
                target=self.process_and_paste,
                args=(audio, use_llm),
                daemon=True,
                name="dictation-pipeline",
            ).start()

    def process_and_paste(
        self, audio, use_llm: bool
    ) -> None:
        """Worker: STT → optional LLM → clipboard paste at the active cursor."""
        self.last_status = None
        try:
            if self.transcriber is None:
                raise RuntimeError("Transcriber not initialized")

            start_time: float = time.time()

            # Prefer the in-memory buffer; fall back to disk only if missing.
            audio_source = audio
            if audio_source is None:
                audio_source = self.audio_filepath

            raw_text: str = self.transcriber.transcribe(audio_source)
            print(f"Raw Transcript: \"{raw_text}\"")

            if not raw_text.strip() or not any(c.isalnum() for c in raw_text):
                print(">>> Empty transcription. Paste cancelled.")
                self.last_status = "empty"
                return

            if use_llm and self.refiner is not None:
                # AI mode only: selection is read as LLM context (Ctrl+C probe).
                # Raw dictation never calls this — paste below just replaces selection.
                context = get_selected_text()
                refined_text: str = self.refiner.refine(raw_text, context=context)
                print(f'Refined Text (AI):   "{refined_text}"')
            else:
                # Raw dictation: transcript only; no selection probe / no LLM.
                refined_text = raw_text
                print(f'Raw Text (Bypass):  "{refined_text}"')

            if not refined_text.strip():
                self.last_status = "empty"
                return

            # Ctrl+V: if text was selected, the target app replaces it with this payload.
            paste_text(refined_text)

            elapsed: float = time.time() - start_time
            print(f">>> Text pasted successfully in {elapsed:.2f} seconds!")
            self.last_status = "success"

        except Exception as e:
            print(f"!!! Pipeline Error: {e}", file=sys.stderr)
            self.last_status = "error"
        finally:
            if self.recorder is not None:
                self.recorder.clear()
            self._cleanup_temp_file()
            self._last_cycle_end = time.monotonic()
            with self.state_lock:
                self._set_state(AppState.IDLE)
                print("System Idle. Ready.")


if __name__ == "__main__":
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(line_buffering=True)  # type: ignore
        except AttributeError:
            pass

    # STRICT single-instance: kill orphans, take mutex, only then construct the app
    # (which binds a system-wide keyboard hook). Never skip this gate.
    kill_other_odicto_processes(os.path.join(_install_root(), "dictation.pid"))
    if not acquire_single_instance_lock():
        sys.exit(2)

    try:
        app = DictationApp()
        app.run()
    finally:
        release_single_instance_lock()
