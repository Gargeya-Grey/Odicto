import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app_state import AppState
from config import Config, parse_hold_hotkey
from recorder import AudioRecorder, play_beep
from transcriber import WhisperTranscriber
from refiner import TextRefiner
from typer import paste_text, get_selected_text

import platforms

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


# Re-export hotkey helpers so existing callers/tests can import them from main.
side_exclusive_scan_codes = platforms.side_exclusive_scan_codes
is_pressed_exclusive = platforms.is_pressed_exclusive


# ---------------------------------------------------------------------------
# STRICT single-instance lock (layered per platform)
#
# Odicto installs a system-wide keyboard hook with suppression. A second Odicto
# process installing a second hook can double every keystroke system-wide.
# The platform backend owns the actual lock; this module owns the app-level flag.
# Rule: never bind hotkeys unless _INSTANCE_LOCK_HELD is True AND the platform
# backend reports its lock is held.
# ---------------------------------------------------------------------------
_INSTANCE_LOCK_HELD: bool = False


def _install_root() -> str:
    from platforms import base

    return base.install_root()


def _mutex_name_for_install() -> str:
    """Canonical install-scoped name (Windows mutex convention; kept for tests/logs)."""
    from platforms import base

    return f"Global\\Odicto_SingleInstance_{base.install_digest()}"


def acquire_single_instance_lock(timeout_ms: int = 8000) -> bool:
    """Take exclusive ownership of this install. False ⇒ must not bind keyboard hooks."""
    global _INSTANCE_LOCK_HELD

    if not platforms.acquire_lock(timeout_ms):
        print(
            "!!! FATAL: Could not acquire Odicto single-instance lock.\n"
            "    Two copies would stack system-wide keyboard hooks and double every\n"
            "    typed character (even when not dictating). Stop all instances,\n"
            "    then start only once.",
            flush=True,
        )
        _INSTANCE_LOCK_HELD = False
        return False

    _INSTANCE_LOCK_HELD = True
    print(
        f"Single-instance lock acquired (backend={platforms.hotkey_backend_name()}).",
        flush=True,
    )
    return True


def release_single_instance_lock() -> None:
    """Release the platform lock + drop all keyboard hooks."""
    global _INSTANCE_LOCK_HELD
    platforms.release_lock()
    _INSTANCE_LOCK_HELD = False


def ensure_can_bind_hotkeys() -> None:
    """Final gate immediately before installing hooks — raises if not exclusive owner."""
    if not _INSTANCE_LOCK_HELD or not platforms.lock_is_held():
        raise RuntimeError(
            "Refusing keyboard.hook_key: single-instance lock not held. "
            "Duplicate hooks double every keystroke system-wide."
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
        # Modifiers physically held at the moment the primary key went down, so the
        # mode decision is stable for the whole capture (releases mid-hold don't
        # flip it). Raw chord modifiers are stored so legacy AI_MODIFIER probing
        # can be re-checked against the held set.
        self._pressed_mods_at_press: tuple = ()
        # Per-capture override: True → force AI mode, False → force raw dictation,
        # None → decided by chord at press time.
        self._capture_mode_override: Optional[bool] = None
        # F6 (CTRL_KEEP_CONTEXT_KEYS): opt-in multi-turn memory for this capture.
        self._keep_history: bool = False

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
            platforms.kill_other_odicto_processes(self.pid_file)
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
                f"  - Hold '{Config.AI_HOTKEY}': RECORD and paste a fresh AI reply "
                "(no previous conversation)."
            )
        elif Config.AI_MODIFIER:
            print(
                f"  - Hold '{Config.HOTKEY}+{Config.AI_MODIFIER}': "
                "RECORD and paste a fresh AI reply (no previous conversation)."
            )
        keep_keys = ", ".join(k for k in Config.CTRL_KEEP_CONTEXT_KEYS if k)
        if keep_keys:
            print(
                f"  - Hold {keep_keys.upper()} + '{Config.HOTKEY}' (or the AI chord): "
                "same AI reply, but keep / continue conversation memory."
            )
        print("Press Ctrl+C in this terminal window to terminate.")
        print("==================================================")

    def _mods_held(self, mods: tuple) -> bool:
        """True if every listed modifier is currently down (empty mods → True)."""
        if not mods:
            return True
        try:
            return all(platforms.is_pressed(m) for m in mods)
        except Exception:
            return False

    def _mods_in_snapshot(self, mods: tuple) -> bool:
        """True if every modifier was physically down at primary-key press time."""
        if not mods:
            return True
        return all(m in self._pressed_mods_at_press for m in mods)

    def _match_active_chord(self) -> Optional[bool]:
        """Which hold-to-talk chord is active at primary-key press time.

        Uses the modifiers that were physically held when the key went down, so a
        mid-hold release can't flip the mode mid-capture.

        Returns:
            True  → AI chord (AI_HOTKEY or HOTKEY+AI_MODIFIER)
            False → dictation chord (HOTKEY)
            None  → no chord; let the key through for normal typing
        """
        # Prefer the more-specific AI chord when both could match
        # (e.g. ctrl+shift+grave vs ctrl+grave — shift+ctrl also satisfies ctrl).
        if self._ai_hotkey_modifiers:
            if self._mods_in_snapshot(self._ai_hotkey_modifiers):
                return True
            if self._mods_in_snapshot(self._hotkey_modifiers):
                return False
            return None

        # Legacy: HOTKEY + optional AI_MODIFIER extra key
        if not self._mods_in_snapshot(self._hotkey_modifiers):
            return None
        if Config.AI_MODIFIER and platforms.is_pressed_exclusive(Config.AI_MODIFIER):
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
            if event_type == platforms.KEY_DOWN:
                # Freeze the modifiers AND any override keys at the exact instant
                # the key went down so mid-hold releases don't flip the mode.
                snapshot = [
                    m
                    for m in ("ctrl", "shift", "alt", "cmd")
                    if platforms.is_pressed(m)
                ]
                for keep_key in Config.CTRL_KEEP_CONTEXT_KEYS:
                    try:
                        if keep_key and platforms.is_pressed(keep_key):
                            snapshot.append(keep_key)
                    except Exception:
                        pass
                self._pressed_mods_at_press = tuple(snapshot)
                match = self._match_active_chord()
                if match is None:
                    return True  # no chord — allow normal typing (e.g. bare `)
                if self._hotkey_physically_held:
                    return False  # key-repeat while held
                self._hotkey_physically_held = True
                self.on_press(use_llm=match)
                return False  # suppress so ` does not leak into the focused app
            if event_type == platforms.KEY_UP:
                if not self._hotkey_physically_held:
                    return True
                self._hotkey_physically_held = False
                self.on_release()
                return False
            return True

        # suppress=True installs a system-wide keyboard hook (all keys, all apps).
        # Only one process may do this — guarded by _INSTANCE_LOCK_HELD above.
        platforms.hook_key(primary, primary_handler, suppress=True)

        # Persistent reset: a direct key hook (not add_hotkey) that clears the AI
        # multi-turn memory immediately, no recording needed. add_hotkey fails to
        # fire for a plain single key in the keyboard library (0.13.5), so we hook
        # the scan code directly like the dictation chord.
        reset_key: str = (Config.RESET_CONTEXT_HOTKEY or "").strip().lower()
        if reset_key:
            def reset_handler(event: object) -> bool:
                if getattr(event, "event_type", None) == platforms.KEY_UP:
                    # Fire on release so a quick tap still registers exactly once.
                    self._reset_context_via_hotkey()
                return True  # never suppress; F5 keeps its normal app behavior

            platforms.hook_key(reset_key, reset_handler, suppress=False)
            print(
                f"Reset-context hotkey bound: '{reset_key}' "
                f"(clears AI multi-turn memory)",
                flush=True,
            )

        print(
            f"Hotkeys bound: primary='{primary}' "
            f"dictation_mods={list(dict_mods) or '(none)'} "
            f"ai_mods={list(self._ai_hotkey_modifiers) or Config.AI_MODIFIER or '(none)'} "
            f"(single-instance lock held)",
            flush=True,
        )

    def _kill_stale_instance(self) -> None:
        """Kill every other Odicto main.py for this install."""
        platforms.kill_other_odicto_processes(self.pid_file)

    def _ensure_ollama_running(self) -> None:
        """Starts a local Ollama server if port 11434 is not already listening."""
        import socket

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
            self.ollama_process = platforms.spawn_detached(["ollama", "serve"])
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
                platforms.wait()
        except KeyboardInterrupt:
            print("\nReceived termination signal. Shutting down dictation app...")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        """Release resources, keyboard hooks, PID file, and any Ollama we spawned."""
        self.ready = False
        try:
            if self.recorder is not None:
                self.recorder.close()
        except Exception:
            pass

        # Drop system-wide hooks ASAP so normal typing is not filtered by a dying process.
        platforms.unhook_all()

        self._cleanup_temp_file()

        if os.path.exists(self.pid_file):
            try:
                os.remove(self.pid_file)
            except Exception:
                pass

        if getattr(self, "ollama_process", None) is not None:
            print("Shutting down Ollama server to free system memory...")
            platforms.terminate_process_tree(self.ollama_process)

    def _cleanup_temp_file(self) -> None:
        """Removes the temporary WAV recording file if it exists."""
        if os.path.exists(self.audio_filepath):
            try:
                os.remove(self.audio_filepath)
            except Exception as e:
                print(f"Warning: Failed to clean up temporary audio file: {e}")

    def _reset_context_via_hotkey(self) -> None:
        """Hotkey handler: clear AI multi-turn memory without a recording."""
        if self.refiner is not None:
            self.refiner.reset_context()
        if self.indicator is not None:
            try:
                self.indicator.flash_reset_notice()
            except Exception:
                pass

    def _apply_chord_overrides(self) -> None:
        """Read hold-time modifier chords for one-shot mode overrides.

        F6 (or any key in Config.CTRL_KEEP_CONTEXT_KEYS) held while the primary
        key goes down forces AI mode AND keeps conversation memory for that
        capture. Plain F6 alone does nothing. Without F6, AI replies are fresh.

        The press-time snapshot (taken by the key-down handler) already includes
        the keep-context keys, so this is a pure set intersection.
        """
        self._capture_mode_override = None
        self._keep_history = False
        keep_keys = set(Config.CTRL_KEEP_CONTEXT_KEYS)
        keep_keys.discard("")
        if not keep_keys:
            return

        if any(k in keep_keys for k in self._pressed_mods_at_press):
            self._capture_mode_override = True
            self._keep_history = True

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

            # Per-capture override wins: e.g. holding F6 forces AI + keep memory.
            self._apply_chord_overrides()
            if self._capture_mode_override is not None:
                self.use_llm = self._capture_mode_override
                self._capture_mode_override = None

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

            # Snapshot mode flags for the worker so a future press can't flip them mid-flight.
            use_llm = self.use_llm
            keep_history = self._keep_history
            audio = self.recorder.last_audio_array

            # IMPORTANT: do NOT call get_selected_text() here on the keyboard-hook
            # thread. Synthetic copy input from inside a low-level hook is
            # unreliable, and AI mode still has modifiers physically held on
            # primary-key release — which pollutes the copy chord.
            # Selection is captured first thing in the pipeline worker instead.

            print(">>> Processing transcription and refinement...")
            threading.Thread(
                target=self.process_and_paste,
                args=(audio, use_llm, "", keep_history),
                daemon=True,
                name="dictation-pipeline",
            ).start()

    @staticmethod
    def _capture_selection(pre_context: str = "") -> str:
        """Copy highlighted text off the hook thread. Empty if nothing selected."""
        context = (pre_context or "").strip()
        if context:
            return context[:12000]
        # Brief settle so physical modifier key-ups finish after the chord.
        time.sleep(0.04)
        try:
            context = get_selected_text(timeout=0.35)
        except Exception as e:
            print(f"Warning: selection capture failed: {e}", flush=True)
            return ""
        if context and len(context) > 12000:
            context = context[:12000]
        return context

    def process_and_paste(
        self,
        audio,
        use_llm: bool,
        pre_context: str = "",
        keep_history: bool = False,
    ) -> None:
        """Worker: STT (and selection, in parallel for AI) → optional LLM → paste."""
        self.last_status = None
        sel_pool: Optional[ThreadPoolExecutor] = None
        try:
            if self.transcriber is None:
                raise RuntimeError("Transcriber not initialized")

            start_time: float = time.time()

            # Raw dictation never probes the clipboard. AI mode overlaps the
            # selection copy with Whisper so clipboard wait does not delay STT.
            context = (pre_context or "").strip()
            sel_future = None
            if use_llm and self.refiner is not None and not context:
                sel_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="odicto-sel")
                sel_future = sel_pool.submit(self._capture_selection, "")

            # Prefer the in-memory buffer; fall back to disk only if missing.
            audio_source = audio
            if audio_source is None:
                audio_source = self.audio_filepath

            raw_text: str = self.transcriber.transcribe(audio_source)
            print(f"Raw Transcript: \"{raw_text}\"")

            if sel_future is not None:
                try:
                    context = sel_future.result(timeout=0.8) or ""
                except Exception as e:
                    print(f"Warning: selection capture failed: {e}", flush=True)
                    context = ""
                if context:
                    print(
                        f'Context captured: {len(context)} chars — '
                        f'"{context[:80]}{"..." if len(context) > 80 else ""}"',
                        flush=True,
                    )
                else:
                    print(
                        "Context: (none — no text was selected, or copy failed)",
                        flush=True,
                    )

            if not raw_text.strip() or not any(c.isalnum() for c in raw_text):
                print(">>> Empty transcription. Paste cancelled.")
                self.last_status = "empty"
                return

            if use_llm and self.refiner is not None:
                refined_text = self.refiner.refine(
                    raw_text, context=context, keep_history=keep_history
                )
                print(f'Refined Text (AI):   "{refined_text}"')
            else:
                # Raw dictation: transcript only; no selection probe / no LLM.
                refined_text = raw_text
                print(f'Raw Text (Bypass):  "{refined_text}"')

            if not refined_text.strip():
                self.last_status = "empty"
                return

            # Paste chord: if text was selected, the target app replaces it with this payload.
            paste_text(refined_text)

            elapsed: float = time.time() - start_time
            print(f">>> Text pasted successfully in {elapsed:.2f} seconds!")
            self.last_status = "success"

        except Exception as e:
            print(f"!!! Pipeline Error: {e}", file=sys.stderr)
            self.last_status = "error"
        finally:
            if sel_pool is not None:
                sel_pool.shutdown(wait=False)
            self._finish_cycle()

    def _finish_cycle(self) -> None:
        """Idempotent cycle teardown shared by raw and AI pipeline workers."""
        try:
            if self.recorder is not None:
                self.recorder.clear()
            self._cleanup_temp_file()
        except Exception:
            pass
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

    # STRICT single-instance: kill orphans, take lock, only then construct the app
    # (which binds a system-wide keyboard hook). Never skip this gate.
    _pid_path = os.path.join(_install_root(), "dictation.pid")
    platforms.kill_other_odicto_processes(_pid_path)
    if not acquire_single_instance_lock():
        sys.exit(2)

    # Write PID as soon as we own the install so start scripts can confirm
    # launch without waiting for Whisper load / initialize_app.
    try:
        with open(_pid_path, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"Warning: Could not write PID file early: {e}", flush=True)

    try:
        Config.validate()
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr, flush=True)
        sys.exit(2)

    try:
        app = DictationApp()
        app.run()
    finally:
        release_single_instance_lock()
