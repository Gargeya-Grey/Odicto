import os
import sys
import tempfile
import threading
import time
from typing import Optional

import keyboard
from app_state import AppState
from config import Config, parse_hold_hotkey
from recorder import AudioRecorder, play_beep
from transcriber import WhisperTranscriber
from refiner import TextRefiner
from typer import paste_text

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
        self._hotkey_modifiers: tuple = ()
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

        # Bind global press/release hooks for hold-to-talk (supports chords like alt+x).
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

        ai_chord = self._ai_chord_label()
        print("--------------------------------------------------")
        print(f"Application ready! Global Hotkey: '{Config.HOTKEY}'")
        print(
            f"  - Hold '{Config.HOTKEY}': RECORD and paste raw Whisper transcript."
        )
        if ai_chord:
            print(
                f"  - Hold '{ai_chord}': RECORD and paste AI-refined response."
            )
        print("Press Ctrl+C in this terminal window to terminate.")
        print("==================================================")

    def _ai_chord_label(self) -> str:
        """Human-readable AI chord, e.g. 'alt+x+z'."""
        if not Config.AI_MODIFIER:
            return ""
        return f"{Config.HOTKEY}+{Config.AI_MODIFIER}"

    def _modifiers_held(self) -> bool:
        """True if every HOTKEY modifier is currently down (or no modifiers required)."""
        if not self._hotkey_modifiers:
            return True
        try:
            return all(keyboard.is_pressed(m) for m in self._hotkey_modifiers)
        except Exception:
            return False

    def _bind_hotkeys(self) -> None:
        """Hook the primary key with selective suppress for multi-key chords.

        ``keyboard.on_press_key`` only accepts a single key name. For chords like
        ``alt+x`` we hook ``x`` and require the modifiers to be held. The primary
        key is suppressed only when the full chord matches, so normal typing of
        ``x`` still works.
        """
        mods, primary = parse_hold_hotkey(Config.HOTKEY)
        self._hotkey_modifiers = mods
        self._hotkey_primary = primary
        self._hotkey_physically_held = False

        def primary_handler(event: object) -> bool:
            event_type = getattr(event, "event_type", None)
            if event_type == keyboard.KEY_DOWN:
                if not self._modifiers_held():
                    return True  # modifiers not held — allow normal typing
                if self._hotkey_physically_held:
                    return False  # key-repeat while held: keep suppressing
                self._hotkey_physically_held = True
                self.on_press()
                return False  # suppress so the key does not leak into the focused app
            if event_type == keyboard.KEY_UP:
                if not self._hotkey_physically_held:
                    return True
                self._hotkey_physically_held = False
                self.on_release()
                return False
            return True

        keyboard.hook_key(primary, primary_handler, suppress=True)

        # Suppress the AI modifier while HOTKEY modifiers are held so e.g. 'z'
        # does not type into the active text field during Alt+X+Z.
        ai_mod = Config.AI_MODIFIER
        if ai_mod:

            def ai_modifier_handler(event: object) -> bool:
                if self._modifiers_held():
                    return False  # suppress
                # Also suppress while the dictation primary is already held (no-modifier hotkeys).
                if not self._hotkey_modifiers and self._hotkey_physically_held:
                    return False
                return True

            keyboard.hook_key(ai_mod, ai_modifier_handler, suppress=True)

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
        """Release resources, PID file, and any Ollama process we spawned."""
        self.ready = False
        try:
            if self.recorder is not None and self.recorder.recording:
                self.recorder.stop()
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
    def on_press(self, event: object = None) -> None:
        """Handler triggered when the hotkey is physically pressed down."""
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

            self.use_llm = False
            if Config.AI_MODIFIER:
                try:
                    self.use_llm = keyboard.is_pressed(Config.AI_MODIFIER)
                except Exception:
                    pass

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
                refined_text: str = self.refiner.refine(raw_text)
                print(f'Refined Text (AI):   "{refined_text}"')
            else:
                refined_text = raw_text
                print(f'Raw Text (Bypass):  "{refined_text}"')

            if not refined_text.strip():
                self.last_status = "empty"
                return

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

    app = DictationApp()
    app.run()
