import contextlib
import os
import json
import queue
from paths import ROOT
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from typing import Optional, TextIO


_LOG_MAX_BYTES = 1_000_000
_LOG_KEEP_BYTES = 256_000


class _TimestampWriter:
    """Prefix each log line with a local timestamp (pythonw has no console)."""

    def __init__(self, raw: TextIO) -> None:
        self._raw = raw
        self._at_bol = True

    def write(self, s: str) -> int:
        if not s:
            return 0
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for line in s.splitlines(keepends=True):
            if self._at_bol and line not in ("\n", "\r\n"):
                out.append(f"{stamp} {line}")
            else:
                out.append(line)
            self._at_bol = line.endswith("\n")
        self._raw.write("".join(out))
        return len(s)

    def flush(self) -> None:
        try:
            self._raw.flush()
        except Exception:
            pass

    def fileno(self) -> int:
        return self._raw.fileno()

    def reconfigure(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        reconf = getattr(self._raw, "reconfigure", None)
        if reconf is not None:
            reconf(**kwargs)


def _session_log_path() -> str:
    return os.path.join(str(ROOT), "dictation.log")


def _trim_log(path: str) -> None:
    """Keep dictation.log from growing without bound across logins."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= _LOG_MAX_BYTES:
            return
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - _LOG_KEEP_BYTES))
            tail = f.read()
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1 :]
        with open(path, "wb") as f:
            f.write(tail)
    except Exception:
        pass


def _stdout_is_discarded() -> bool:
    """True when this process has no console to write to.

    pythonw starts with stdout is None. Setup's spawn_detached instead points
    stdout/stderr at NUL, which is not None — so a naive None-check would skip
    dictation.log and swallow every AI error after a setup-page restart.
    """
    if sys.stdout is None:
        return True
    try:
        name = str(getattr(sys.stdout, "name", "") or "").lower()
        if name in ("nul", "/dev/null"):
            return True
    except Exception:
        pass
    return False


def attach_pythonw_log() -> None:
    """pythonw has no console; append stdout/stderr to dictation.log.

    Must run before heavy imports so a PortAudio/Qt/CUDA failure at login is
    still on disk. Append (do not overwrite) so a later restart cannot erase
    the boot crash that just happened.
    """
    if not _stdout_is_discarded():
        return
    try:
        path = _session_log_path()
        _trim_log(path)
        raw = open(path, "a", encoding="utf-8", buffering=1)
        raw.write(
            f"----- session {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"pid={os.getpid()} -----\n"
        )
        raw.flush()
        wrapped = _TimestampWriter(raw)
        sys.stdout = wrapped  # type: ignore[assignment]
        sys.stderr = wrapped  # type: ignore[assignment]
        try:
            import faulthandler

            faulthandler.enable(file=raw, all_threads=True)
        except Exception:
            pass
    except Exception:
        pass


# Redirect before importing sounddevice / Qt / ctranslate2 so login crashes
# land in dictation.log instead of vanishing with pythonw.
attach_pythonw_log()

from app_state import AppState
from config import Config, parse_hold_hotkey
from recorder import AudioRecorder, play_beep
from transcriber import (
    CloudTranscriber,
    GeminiLiveSession,
    GeminiTranscriber,
    WhisperTranscriber,
)
from refiner import TextRefiner
from typer import (
    flush_pending_restore,
    get_selected_text,
    paste_text,
)

import platforms
from platforms.preflight import environment_problems


# Stage workers use the Thread class captured at import. A deadline is only a
# deadline if its worker really runs; a caller that patches threading.Thread
# (tests do, to intercept the pipeline thread) must not turn it into a hang.
_Thread = threading.Thread
# Serialises the "first caller sets _closing" step of _shutdown.
_SHUTDOWN_GUARD = threading.Lock()
# Backoff between failed initialisation attempts; the last value repeats.
_INIT_RETRY_DELAYS_S = (2.0, 5.0, 10.0, 30.0, 60.0)


class _Abort(Exception):
    """End the current pipeline cycle without insertion; ``status`` goes to the HUD."""

    def __init__(self, status: Optional[str]) -> None:
        super().__init__(status or "closing")
        self.status = status


class _Stage:
    """One bounded piece of pipeline work on its own daemon thread.

    The pipeline waits on it with a deadline and may abandon it. An abandoned
    stage only stores its late result here; it never inserts text.
    """

    def __init__(self, fn, name: str) -> None:
        self.done = threading.Event()
        self.value = None
        self.error: Optional[BaseException] = None
        _Thread(target=self._run, args=(fn,), daemon=True, name=name).start()

    def _run(self, fn) -> None:
        try:
            self.value = fn()
        except BaseException as e:  # reported by the waiting pipeline
            self.error = e
        finally:
            self.done.set()

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


class _Cycle:
    """Per-capture processing token: cancel flag, capture quality, early probe."""

    def __init__(self, token: int, gap: bool = False, probe: Optional[_Stage] = None) -> None:
        self.token = token
        self.cancel = threading.Event()
        self.gap = gap
        self.probe = probe
        self.claimed = False
        self.pasting = False


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


def claim_install(pid_file: str) -> bool:
    """Leave a live owner alone; sweep orphans only under exclusive ownership."""
    if not acquire_single_instance_lock():
        return False
    platforms.kill_other_odicto_processes(pid_file)
    return True


def ensure_can_bind_hotkeys() -> None:
    """Final gate immediately before installing hooks — raises if not exclusive owner."""
    if not _INSTANCE_LOCK_HELD or not platforms.lock_is_held():
        raise RuntimeError(
            "Refusing keyboard.hook_key: single-instance lock not held. "
            "Duplicate hooks double every keystroke system-wide."
        )


class DictationApp:
    # Longest wait for an in-flight insertion (and clipboard flush) at shutdown.
    _SHUTDOWN_LOCK_TIMEOUT_S: float = 3.0

    def __init__(self, *, runtime: bool = False) -> None:
        """Initializes the background dictation app, setting up state and loading model instances."""
        print("==================================================")
        print("              Initializing Odicto               ")
        print("==================================================")
        try:
            rev = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(ROOT),
            ).stdout.strip()
            if rev:
                print(f"Code version: git {rev}")
        except Exception:
            pass
        try:
            print(
                f"Resolved: provider={Config.LLM_PROVIDER} "
                f"model={Config.effective_llm_model()} "
                f"effort={Config.effective_reasoning_effort() or '(default)'} "
                f"prompt={Config.prompt_source_label()}"
            )
        except Exception as e:
            print(f"Warning: could not resolve config at boot: {e}")

        self.temp_dir: str = tempfile.gettempdir()
        self.audio_filepath: str = os.path.join(
            self.temp_dir, "dictation_recording.wav"
        )
        self.pid_file = os.path.join(
            str(ROOT), "dictation.pid"
        )

        self.state: AppState = AppState.IDLE
        self.state_lock: threading.Lock = threading.Lock()
        self.last_status: Optional[str] = None
        self._last_cycle_end: float = 0.0
        self._record_started_at: float = 0.0
        self.use_llm: bool = False
        self.ready: bool = False
        self._runtime_enabled = runtime
        self._closing = threading.Event()
        self._lifecycle_lock = threading.Lock()

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

        # F7 (LIVE_HOTKEY): tap-to-talk. Distinct from hold-to-talk chords.
        self.live_active: bool = False
        self._live_session: Optional[GeminiLiveSession] = None
        self._live_key_held: bool = False
        self._live_committed: str = ""
        self.live_preview: str = ""
        self._speech_backends = {}
        self._live_epoch: int = 0

        # Processing cycle (cancel / deadline token) and capture bookkeeping.
        self._cycle: Optional[_Cycle] = None
        self._cycle_seq: int = 0
        self._capture_seq: int = 0
        self._early_probe: Optional[_Stage] = None
        # Init/preflight error code shown by the HUD (see indicator.INIT_REASON_LABELS).
        self.status_detail: Optional[str] = None
        self._preflight_error: Optional[str] = None
        self._hooks_bound: bool = False

        self.ollama_process = None
        self.recorder: Optional[AudioRecorder] = None
        self.transcriber = None
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

        if runtime:
            self._start_hotkey_worker()
        threading.Thread(
            target=self.initialize_app, daemon=True, name="dictation-init"
        ).start()
        if runtime:
            threading.Thread(target=self._monitor_runtime, daemon=True,
                             name="odicto-runtime-health").start()

    def _monitor_runtime(self) -> None:
        """Publish only lifecycle metadata; never audio, transcripts or credentials."""
        if not getattr(self, "_runtime_enabled", False) or not platforms.lock_is_held():
            return  # test instances and nonowners must never overwrite runtime evidence
        path = os.path.join(str(ROOT), "dictation-health.json")
        temporary = path + f".{os.getpid()}.tmp"
        while not self._closing.is_set():
            try:
                microphone = self.recorder.health_snapshot() if self.recorder else None
                ready = (self.ready and microphone is not None and not microphone["closed"]
                         and microphone["callback_count"] > 0 and microphone["callback_age_s"] <= 3)
                snapshot = {"pid": os.getpid(), "updated_at": time.time(),
                            "ready": ready, "state": self.state.name,
                            "status": self.last_status,
                            "microphone": microphone}
                with open(temporary, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f)
                os.replace(temporary, path)
            except Exception:
                pass  # diagnostics must not break dictation
            if self._closing.wait(2.0):
                break
        try:
            os.remove(temporary)
        except OSError:
            pass

    # ------------------------------------------------------------------ UI push
    def _start_hotkey_worker(self) -> None:
        self._hotkey_actions = queue.SimpleQueue()
        self._hotkey_worker = threading.Thread(target=self._run_hotkey_actions,
                                               daemon=True, name="odicto-hotkey-actions")
        self._hotkey_worker.start()

    def _run_hotkey_actions(self) -> None:
        while True:
            action = self._hotkey_actions.get()
            if action is None or self._closing.is_set():
                return
            try:
                self._execute_hotkey_action(*action)
            except Exception as error:
                print(f"Hotkey action failed: {error}", file=sys.stderr)

    def _dispatch_hotkey(self, kind, snapshot=(), use_llm=None) -> None:
        if getattr(self, "_closing", None) is not None and self._closing.is_set():
            return
        action = (kind, snapshot, use_llm)
        if getattr(self, "_runtime_enabled", False):
            self._hotkey_actions.put(action)
        else:
            self._execute_hotkey_action(*action)

    def _execute_hotkey_action(self, kind, snapshot, use_llm) -> None:
        if kind == "primary":
            self._pressed_mods_at_press = snapshot
            if Config.HOTKEY_TOGGLE and self.state == AppState.RECORDING and not self.live_active:
                self.on_release()
            else:
                self.on_press(use_llm=use_llm)
        elif kind == "release":
            self.on_release()
        elif kind == "live":
            self.on_live_toggle()
        elif kind == "reset":
            self._reset_context_via_hotkey()
        elif kind == "cancel":
            self._cancel_processing()
        elif kind == "limit":
            # Same stop-and-process path as a second chord / F7 tap, but only
            # for the capture that reached the limit.
            if snapshot != self._capture_seq or self.state != AppState.RECORDING:
                return
            if self.live_active:
                self.on_live_toggle()
            else:
                self.on_release()

    def _on_recording_limit(self) -> None:
        """Recorder callback (own daemon thread): stop through the ordered worker."""
        print(f"Recording limit reached ({Config.MAX_RECORDING_SECONDS} s)", flush=True)
        self._dispatch_hotkey("limit", self._capture_seq)

    def _cancel_processing(self) -> None:
        """CANCEL_HOTKEY: abandon the current PROCESSING cycle; no insertion."""
        with self.state_lock:
            cycle = self._cycle
            if self.state != AppState.PROCESSING or cycle is None or cycle.pasting:
                return
            cycle.cancel.set()
        print(">>> Cancel requested; nothing will be inserted.", flush=True)

    def _notify_ui(self) -> None:
        """Push current state to the indicator on the Qt UI thread (non-blocking)."""
        indicator = self.indicator
        if indicator is None:
            return
        try:
            indicator.notify_state_changed()
        except Exception:
            pass

    def _on_live_interim(self, text: str, epoch: Optional[int] = None) -> None:
        with self.state_lock:
            if not self.live_active or (epoch is not None and epoch != self._live_epoch):
                return
            self.live_preview = f"{self._live_committed} {(text or '').strip()}".strip()
        self._notify_ui()

    def _on_live_final(self, text: str, epoch: Optional[int] = None) -> None:
        with self.state_lock:
            if not self.live_active or (epoch is not None and epoch != self._live_epoch):
                return
            piece = (text or "").strip()
            if not piece:
                return
            self._live_committed = f"{self._live_committed} {piece}".strip()
            self.live_preview = self._live_committed
        self._notify_ui()

    def _live_transcribe(self, audio, allow_local_fallback: bool = True) -> str:
        provider = Config.effective_live_stt_provider()
        if provider == Config.effective_stt_provider():
            return self.transcriber.transcribe(
                audio, allow_local_fallback=allow_local_fallback)
        backend = self._speech_backends.get(provider)
        if backend is None:
            backend = (GeminiTranscriber() if provider == "gemini" else
                       CloudTranscriber(provider) if provider in ("groq", "openrouter") else
                       WhisperTranscriber())
            self._speech_backends[provider] = backend
        return backend.transcribe(audio, allow_local_fallback=allow_local_fallback)

    def _speech_backend(self, live: bool):
        """The transcriber object that served this capture (for local fallback)."""
        if live:
            provider = Config.effective_live_stt_provider()
            if provider != Config.effective_stt_provider():
                return self._speech_backends.get(provider)
        return self.transcriber

    def _transcribe_for_pipeline(self, audio, use_llm: bool,
                                 allow_local_fallback: bool = True) -> str:
        """Raw dictation and AI mode share the selected speech provider and model.

        ``use_llm`` only decides whether the transcript is then sent to the
        assistant. It does not switch the speech engine.
        """
        del use_llm
        return self.transcriber.transcribe(audio, allow_local_fallback=allow_local_fallback)

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
            if not claim_install(self.pid_file):
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
            if self._runtime_enabled and platforms.lock_is_held():
                with open(self.pid_file, "w") as f:
                    f.write(str(os.getpid()))
        except Exception as e:
            print(f"Warning: Could not write PID file: {e}")

        self._report_environment()

        if Config.LLM_PROVIDER == "ollama":
            self._ensure_ollama_running()

        # Retry the failed steps with backoff until ready or shutdown. A failed
        # start must not leave a lock-holding process with no hooks and no retry.
        attempt = 0
        while True:
            reason = self._init_attempt()
            if reason is None:
                break
            if reason == "closing" or self._closing.is_set():
                return
            delay = _INIT_RETRY_DELAYS_S[min(attempt, len(_INIT_RETRY_DELAYS_S) - 1)]
            attempt += 1
            self.status_detail = self._preflight_error or reason
            self.last_status = "init_error"
            self._notify_ui()
            print(
                f"!!! Not ready ({reason}); retry {attempt} in {delay:.0f} s",
                file=sys.stderr,
                flush=True,
            )
            if self._closing.wait(delay):
                return

        self.status_detail = None
        self.last_status = None
        if self.indicator is not None:
            try:
                if self._preflight_error:
                    # Hotkeys bound, but the OS may still block them: say why once.
                    self.status_detail = self._preflight_error
                    self.last_status = "preflight"
                    self.indicator.notify_state_changed()
                else:
                    # Fade out the boot HUD; thread-safe via Qt signals inside hide_indicator path
                    self.indicator.notify_state_changed()
                    # Explicit hide once ready (idle, no last_status → hidden)
                    self.indicator.hide_indicator()
            except Exception:
                pass

        print("--------------------------------------------------")
        print(f"Application ready! Global Hotkey: '{Config.HOTKEY}'")
        stt_name = Config.stt_model_label()
        mode_name = Config.gemini_transcribe_mode()
        chord_verb = "Tap" if Config.HOTKEY_TOGGLE else "Hold"
        chord_how = (
            "tap again to stop"
            if Config.HOTKEY_TOGGLE
            else "release to stop"
        )
        print(
            f"  - {chord_verb} '{Config.HOTKEY}': RECORD and paste transcript "
            f"({stt_name}, {mode_name}; {chord_how})."
        )
        if Config.AI_HOTKEY:
            print(
                f"  - {chord_verb} '{Config.AI_HOTKEY}': RECORD and paste a fresh AI reply "
                f"({stt_name}, then LLM; no previous conversation; {chord_how})."
            )
        elif Config.AI_MODIFIER:
            print(
                f"  - {chord_verb} '{Config.HOTKEY}+{Config.AI_MODIFIER}': "
                "RECORD and paste a fresh AI reply "
                f"({stt_name}, then LLM; no previous conversation; {chord_how})."
            )
        keep_keys = ", ".join(k for k in Config.CTRL_KEEP_CONTEXT_KEYS if k)
        if keep_keys:
            print(
                f"  - Hold {keep_keys.upper()} + '{Config.HOTKEY}' (or the AI chord): "
                "same AI reply, but keep / continue conversation memory."
            )
        if Config.LIVE_HOTKEY:
            live_provider = Config.effective_live_stt_provider()
            live_stt_name = (
                "Gemini 3.5 Transcribe Live"
                if live_provider == "gemini"
                else Config.stt_model_label(live_provider)
            )
            print(
                f"  - Tap '{Config.LIVE_HOTKEY}': start live dictation; tap again to "
                f"stop and paste ({live_stt_name})."
            )
        print("Press Ctrl+C in this terminal window to terminate.")
        print("==================================================")

    def _report_environment(self) -> None:
        """Print preflight problems and config warnings once; remember the first error."""
        try:
            problems = list(environment_problems() or [])
        except Exception:
            problems = []
        for problem in problems:
            print(f"Preflight {problem.severity}: {problem.message}", flush=True)
            if problem.severity == "error" and self._preflight_error is None:
                self._preflight_error = problem.code
        for warning in list(getattr(Config, "CONFIG_WARNINGS", None) or []):
            print(f"Config warning: {warning}", flush=True)

    def _init_attempt(self) -> Optional[str]:
        """Build whatever is still missing and bind hooks. None means ready.

        Returns a short reason code (indicator.INIT_REASON_LABELS) on failure,
        or "closing" when shutdown began. Every step is idempotent so a retry
        only repeats the steps that failed.
        """
        if self.recorder is None:
            try:
                recorder = AudioRecorder(
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS,
                    max_seconds=Config.MAX_RECORDING_SECONDS,
                )
                recorder.set_limit_callback(self._on_recording_limit)
                self.recorder = recorder
            except Exception as e:
                print(f"!!! Microphone init error: {e}", file=sys.stderr)
                return "mic"
        if self.transcriber is None:
            try:
                provider = Config.effective_stt_provider()
                if provider == "gemini":
                    self.transcriber = GeminiTranscriber()
                elif provider in ("groq", "openrouter"):
                    self.transcriber = CloudTranscriber(provider)
                else:
                    self.transcriber = WhisperTranscriber()
            except Exception as e:
                print(f"!!! Speech engine init error: {e}", file=sys.stderr)
                return "speech"
        if self.refiner is None:
            try:
                refiner = TextRefiner()
                refiner.preload()
                self.refiner = refiner
            except Exception as e:
                print(f"!!! AI init error: {e}", file=sys.stderr)
                return "ai"

        # Bind global press/release hooks for hold-to-talk (ctrl+grave / ctrl+shift+grave).
        try:
            with self._lifecycle_lock:
                if self._closing.is_set():
                    self.recorder.close()
                    return "closing"
                if not self._hooks_bound:
                    self._bind_hotkeys()
                    self._hooks_bound = True
                self.ready = True
        except Exception as e:
            print(f"!!! Failed to bind hotkey '{Config.HOTKEY}': {e}", file=sys.stderr)
            # A partial bind must not survive into the retry (stacked hooks).
            try:
                platforms.unhook_all()
            except Exception:
                pass
            return "hotkeys"
        return None

    def _prewarm_for_capture(self, use_llm: bool) -> None:
        """Wake provider connections while the user speaks. Never blocks or raises."""
        targets = [self.transcriber]
        if use_llm or Config.POLISH_DICTATION:
            targets.append(self.refiner)
        for target in targets:
            prewarm = getattr(target, "prewarm", None)
            if target is None or not callable(prewarm):
                continue
            try:
                prewarm()
            except Exception:
                pass

    def _beep(self, frequency: float, name: str) -> None:
        """Play a short audio cue off the hot path."""
        threading.Thread(
            target=play_beep, args=(frequency, 0.08), daemon=True, name=name
        ).start()

    def _mods_in_snapshot(self, mods: tuple, snapshot=None) -> bool:
        """True if every modifier was physically down at primary-key press time."""
        if not mods:
            return True
        return all(m in (self._pressed_mods_at_press if snapshot is None else snapshot) for m in mods)

    def _match_active_chord(self, snapshot=None) -> Optional[bool]:
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
            if self._mods_in_snapshot(self._ai_hotkey_modifiers, snapshot):
                return True
            if self._mods_in_snapshot(self._hotkey_modifiers, snapshot):
                return False
            return None

        # Legacy: HOTKEY + optional AI_MODIFIER extra key
        if not self._mods_in_snapshot(self._hotkey_modifiers, snapshot):
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
                snapshot = tuple(snapshot)
                match = self._match_active_chord(snapshot)
                if match is None:
                    return True  # no chord — allow normal typing (e.g. bare `)
                if self._hotkey_physically_held:
                    return False  # key-repeat while held
                self._hotkey_physically_held = True
                self._dispatch_hotkey("primary", snapshot, match)
                return False  # suppress so ` does not leak into the focused app
            if event_type == platforms.KEY_UP:
                if not self._hotkey_physically_held:
                    return True
                self._hotkey_physically_held = False
                if not Config.HOTKEY_TOGGLE:
                    self._dispatch_hotkey("release")
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
                    self._dispatch_hotkey("reset")
                return True  # never suppress; F5 keeps its normal app behavior

            platforms.hook_key(reset_key, reset_handler, suppress=False)
            print(
                f"Reset-context hotkey bound: '{reset_key}' "
                f"(clears AI multi-turn memory)",
                flush=True,
            )

        cancel_key: str = (Config.CANCEL_HOTKEY or "").strip().lower()
        live_primary = (Config.LIVE_HOTKEY or "").split("+")[-1].strip().lower()
        if cancel_key and cancel_key in (primary, reset_key, live_primary):
            print(f"Warning: CANCEL_HOTKEY '{cancel_key}' is already bound; cancel disabled.",
                  flush=True)
        elif cancel_key:
            def cancel_handler(event: object) -> bool:
                if getattr(event, "event_type", None) == platforms.KEY_DOWN:
                    # Only enqueue; the worker ignores it outside PROCESSING.
                    self._dispatch_hotkey("cancel")
                return True  # never suppress; the key keeps its normal app behavior

            platforms.hook_key(cancel_key, cancel_handler, suppress=False)
            print(
                f"Cancel hotkey bound: '{cancel_key}' (while processing: insert nothing)",
                flush=True,
            )

        live_key: str = (Config.LIVE_HOTKEY or "").split("+")[-1].strip().lower()
        if live_key:
            def live_handler(event: object) -> bool:
                event_type = getattr(event, "event_type", None)
                if event_type == platforms.KEY_DOWN:
                    if self._live_key_held:
                        return False  # key-repeat while held
                    self._live_key_held = True
                    self._dispatch_hotkey("live")
                    return False  # suppress so F7 does not leak into the focused app
                if event_type == platforms.KEY_UP:
                    self._live_key_held = False
                    return False
                return True

            platforms.hook_key(live_key, live_handler, suppress=True)
            print(
                f"Live tap-to-talk hotkey bound: '{live_key}' "
                f"(press to start, press again to stop and paste)",
                flush=True,
            )

        print(
            f"Hotkeys bound: primary='{primary}' "
            f"dictation_mods={list(dict_mods) or '(none)'} "
            f"ai_mods={list(self._ai_hotkey_modifiers) or Config.AI_MODIFIER or '(none)'} "
            f"(single-instance lock held)",
            flush=True,
        )

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
        """Release resources, keyboard hooks, PID file, and any Ollama we spawned.

        Order matters: set _closing and drop the hooks FIRST, outside any lock.
        The pipeline holds _lifecycle_lock across paste_text (native clipboard,
        long typed input); Quit must never wait on that with hooks installed.
        Once _closing is set the pipeline re-checks it under the lock, so no new
        insertion can start.
        """
        with _SHUTDOWN_GUARD:
            if self._closing.is_set():
                return
            self._closing.set()
        actions = getattr(self, "_hotkey_actions", None)
        if actions is not None:
            actions.put(None)
        self.ready = False
        try:
            platforms.unhook_all()
        except Exception as e:
            print(f"Warning: unhook failed: {e}", file=sys.stderr)
        acquired = self._lifecycle_lock.acquire(timeout=self._SHUTDOWN_LOCK_TIMEOUT_S)
        if not acquired:
            print("Warning: an insertion is still running; continuing shutdown.",
                  file=sys.stderr, flush=True)
        try:
            self._live_epoch += 1
            self.live_active = False
        finally:
            if acquired:
                self._lifecycle_lock.release()
        try:
            if self._live_session is not None:
                try:
                    if self.recorder is not None:
                        self.recorder.remove_chunk_listener(self._live_session.push_audio)
                except Exception:
                    pass
                self._live_session.stop(timeout=1.0)
                self._live_session = None
        except Exception:
            pass
        try:
            if self.recorder is not None:
                self.recorder.close()
        except Exception:
            pass

        # Drop system-wide hooks ASAP so normal typing is not filtered by a dying process.
        try:
            platforms.unhook_all()
        except Exception:
            pass

        # Put the user's clipboard back now instead of after process exit.
        # Bounded: an insertion still typing holds the clipboard lock.
        try:
            flusher = _Thread(target=flush_pending_restore, daemon=True,
                              name="odicto-clipboard-flush")
            flusher.start()
            flusher.join(self._SHUTDOWN_LOCK_TIMEOUT_S)
        except Exception:
            pass

        self._cleanup_temp_file()

        if self._runtime_enabled and platforms.lock_is_held():
            try:
                with open(self.pid_file, encoding="ascii") as f:
                    owned = f.read().strip() == str(os.getpid())
                if owned:
                    os.remove(self.pid_file)
            except OSError:
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
            print("Context: F6 held — AI reply keeps conversation memory", flush=True)

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
            # Ordinary captures cannot inherit a previous F7 preview.
            self._live_committed = self.live_preview = ""
            self.live_active = False
            try:
                self.recorder.start()
            except Exception as e:
                print(f"!!! Failed to start recorder: {e}", file=sys.stderr)
                self.last_status = "error"
                self._set_state(AppState.IDLE)
                return

            if self._closing.is_set():
                return
            self._record_started_at = time.monotonic()
            self._capture_seq += 1
            self._early_probe = None
            self._set_state(AppState.RECORDING)
            if Config.PLAY_AUDIO_CUES:
                self._beep(880.0, "beep-start")

            # Toggle mode: the chord is released right after this tap, so read
            # the selection/image now (off the hook thread; the probe waits for
            # modifier release) instead of after STT. Hold mode keeps the chord
            # down while speaking, so it still probes at release. Only the
            # runtime owner touches the real clipboard here.
            if (self.use_llm and Config.HOTKEY_TOGGLE and self.refiner is not None
                    and getattr(self, "_runtime_enabled", False)):
                self._early_probe = _Stage(self._capture_selection, "odicto-sel-early")
            self._prewarm_for_capture(self.use_llm)

            mode_str = "AI refined" if self.use_llm else "raw dictation"
            hint = (
                "tap the same chord again when finished"
                if Config.HOTKEY_TOGGLE
                else "Hold key and speak"
            )
            print(f"\n>>> Recording ({mode_str})... ({hint})")

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
            if self.live_active:
                return  # F7 live tap owns the mic

            # The early selection probe belongs to this capture only.
            probe, self._early_probe = self._early_probe, None
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
                self._beep(440.0, "beep-stop")

            # Hot path: keep audio in memory only (no disk write).
            try:
                success: bool = self.recorder.stop(filepath=None)
            except Exception as e:
                print(f"!!! Failed to stop recorder: {e}", file=sys.stderr)
                self.last_status = "error"
                self.recorder.clear()
                self._live_committed = self.live_preview = ""
                self._last_cycle_end = time.monotonic()
                self._set_state(AppState.IDLE)
                return
            if not success:
                print("!!! Warning: No audio captured. Resetting to idle.")
                self.last_status = "empty"
                self._set_state(AppState.IDLE)
                return

            # Snapshot mode flags for the worker so a future press can't flip them mid-flight.
            use_llm = self.use_llm
            keep_history = self._keep_history
            audio = self.recorder.last_audio_array
            # A gap/overflow keeps the audio; the HUD says it may miss words.
            gap = getattr(self.recorder, "last_capture_gap", False) is True
            if gap:
                print("Warning: the microphone had a gap during this capture.", flush=True)
            if getattr(self.recorder, "last_capture_limited", False) is True:
                print("Capture stopped at the recording limit.", flush=True)
            self._cycle_seq += 1
            self._cycle = _Cycle(self._cycle_seq, gap=gap, probe=probe)

            # IMPORTANT: do NOT call get_selected_text() here on the keyboard-hook
            # thread. Synthetic copy input from inside a low-level hook is
            # unreliable, and AI mode still has modifiers physically held on
            # primary-key release — which pollutes the copy chord.
            # Selection is captured in the pipeline worker instead, or by the
            # toggle-mode probe that on_press started on its own thread.

            print(">>> Processing transcription and refinement...")
            threading.Thread(
                target=self.process_and_paste,
                args=(audio, use_llm, "", keep_history, ""),
                daemon=True,
                name="dictation-pipeline",
            ).start()

    def on_live_toggle(self, event: object = None) -> None:
        """Preview in the HUD; insert once after the second tap finalizes capture."""
        if not self.ready or self.recorder is None:
            return
        with self.state_lock:
            if self.state == AppState.PROCESSING:
                return
            if self.state == AppState.IDLE:
                now = time.monotonic()
                if now - self._last_cycle_end < Config.RETRIGGER_COOLDOWN_MS / 1000.0:
                    return
                self._live_epoch += 1
                epoch = self._live_epoch
                self.use_llm = False
                self._keep_history = False
                self._record_started_at = now
                self.last_status = None
                self._live_committed = self.live_preview = ""
                try:
                    if Config.effective_live_stt_provider() == "gemini":
                        session = GeminiLiveSession(
                            on_interim=lambda text: self._on_live_interim(text, epoch),
                            on_final=lambda text: self._on_live_final(text, epoch),
                            client=getattr(self.transcriber, "_client", None),
                        )
                        self._live_session = session
                        self.recorder.add_chunk_listener(session.push_audio)
                        session.start()
                    self.recorder.start()
                    if self._closing.is_set():
                        return
                    self._record_started_at = time.monotonic()
                    self._capture_seq += 1
                    self.live_active = True
                    self._set_state(AppState.RECORDING)
                    if Config.PLAY_AUDIO_CUES:
                        self._beep(880.0, "beep-start")
                    self._prewarm_for_capture(False)
                except Exception as e:
                    print(f"!!! Live start failed: {e}", file=sys.stderr)
                    session = self._live_session
                    self._live_session = None
                    if session is not None:
                        self.recorder.remove_chunk_listener(session.push_audio)
                        threading.Thread(target=session.stop, kwargs={"timeout": 0.4}, daemon=True).start()
                    self.live_active = False
                    self.last_status = "error"
                    self._set_state(AppState.IDLE)
                return
            if self.state != AppState.RECORDING or not self.live_active:
                return
            # Set PROCESSING before releasing the hook lock. No new capture can
            # race the final callback, recorder cleanup, polish or insertion.
            self.live_active = False
            self.last_status = "finalizing"
            self._set_state(AppState.PROCESSING)
            session = self._live_session
            self._live_session = None
            if session is not None:
                self.recorder.remove_chunk_listener(session.push_audio)
            try:
                captured = self.recorder.stop(filepath=None)
            except Exception as e:
                print(f"!!! Failed to stop live recorder: {e}", file=sys.stderr)
                self.last_status = "error"
                captured = False
            short = (time.monotonic() - self._record_started_at) * 1000 < Config.MIN_HOLD_MS
            audio = self.recorder.last_audio_array if captured and not short else None
            epoch = self._live_epoch
            gap = bool(captured) and getattr(self.recorder, "last_capture_gap", False) is True
            self._cycle_seq += 1
            self._cycle = _Cycle(self._cycle_seq, gap=gap)
            if Config.PLAY_AUDIO_CUES:
                self._beep(440.0, "beep-stop")
        threading.Thread(target=self._finish_live_session,
                         args=(session, audio, epoch, short or not captured),
                         daemon=True, name="odicto-live-stop").start()

    def _finish_live_session(self, session, audio, epoch: int, discard: bool = False) -> None:
        try:
            final = session.stop(timeout=4.0, final_wait_s=2.5) if session else ""
            if epoch != self._live_epoch:
                return
            if discard:
                if self.last_status != "error":
                    self.last_status = "empty"
                self._finish_cycle()
                return
            # A final/draft from this one Live call avoids a second STT request.
            incomplete = session is not None and getattr(session, "needs_batch_fallback", False) is True
            transcript = "" if incomplete else (final or self.live_preview or "").strip()
            if incomplete:
                print("Live audio delivery incomplete; transcribing the full recording.")
            self.process_and_paste(audio, False, "", False, transcript, live=True)
        except Exception as e:
            print(f"!!! Live finish failed: {e}", file=sys.stderr)
            if epoch == self._live_epoch:
                self.last_status = "error"
                self._finish_cycle()

    def _capture_selection(self) -> tuple[str, Optional[bytes]]:
        """Capture highlighted text and/or clipboard image off the hook thread."""
        # Brief settle so physical modifier key-ups finish after the chord.
        time.sleep(0.02)
        if self._closing.is_set():
            return "", None
        try:
            image_bytes = (self.indicator.capture_clipboard_image()
                           if Config.AI_CLIPBOARD_IMAGE and self.indicator is not None else None)
        except Exception as e:
            print(f"Warning: image capture failed: {e}", flush=True)
            image_bytes = None

        try:
            context = get_selected_text(timeout=0.35)
        except Exception as e:
            print(f"Warning: text selection capture failed: {e}", flush=True)
            context = ""
        if context and len(context) > 12000:
            context = context[:12000]
        return context, image_bytes

    def process_and_paste(
        self,
        audio,
        use_llm: bool,
        pre_context: str = "",
        keep_history: bool = False,
        pre_transcript: str = "",
        *, live: bool = False,
    ) -> None:
        """Worker: STT (and selection/image, in parallel for AI) → optional LLM → paste.

        STT and the LLM/polish step each run on a stage thread under a wall-clock
        deadline. The worker waits for done, deadline, cancel or shutdown, and
        only the worker that owns this cycle may insert text.
        """
        self.last_status = None
        cycle = self._claim_cycle()
        try:
            if self._closing.is_set():
                return
            if cycle.cancel.is_set():
                raise _Abort("cancelled")
            if self.transcriber is None:
                raise RuntimeError("Transcriber not initialized")

            start_time: float = time.time()

            # Raw dictation never probes the clipboard. AI mode overlaps the
            # selection/image probe with STT so clipboard wait does not delay it;
            # in toggle mode the probe already started with the capture.
            context = (pre_context or "").strip()
            image_bytes: Optional[bytes] = None
            sel_stage: Optional[_Stage] = None
            if use_llm and self.refiner is not None and not context:
                sel_stage = cycle.probe or _Stage(self._capture_selection, "odicto-sel")

            # Prefer the in-memory buffer; fall back to disk only if missing.
            audio_source = audio
            if audio_source is None:
                audio_source = self.audio_filepath

            stt_started = time.time()
            stt_notice = ""
            raw_text: str = (pre_transcript or "").strip()
            if raw_text:
                self._log_text("Live Transcript", raw_text,
                               f"committed in {time.time() - stt_started:.2f}s")
            else:
                raw_text, stt_notice = self._run_stt(audio_source, use_llm, live, cycle)
                raw_text = raw_text or ""
                self._log_text("Raw Transcript", raw_text, f"STT {time.time() - stt_started:.2f}s")

            if sel_stage is not None:
                sel_wait_started = time.time()
                outcome = self._await_stage(sel_stage, 1.5, cycle)
                self._raise_for(outcome)
                context, image_bytes = "", None
                if outcome == "timeout":
                    print("Warning: selection capture failed: TimeoutError", flush=True)
                elif sel_stage.error is not None:
                    e = sel_stage.error
                    err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                    print(f"Warning: selection capture failed: {err_msg}", flush=True)
                elif isinstance(sel_stage.value, tuple):
                    context, image_bytes = sel_stage.value
                elif isinstance(sel_stage.value, str):
                    context = sel_stage.value
                sel_elapsed = time.time() - sel_wait_started
                if context or image_bytes:
                    ctx_parts = []
                    if context:
                        preview = ""
                        if Config.LOG_TRANSCRIPTS:
                            preview = f' "{context[:80]}{"..." if len(context) > 80 else ""}"'
                        ctx_parts.append(f"text ({len(context)} chars){preview}")
                    if image_bytes:
                        ctx_parts.append(f'image ({len(image_bytes)} bytes PNG)')
                    print(
                        f"Context captured in {sel_elapsed:.2f}s — {' + '.join(ctx_parts)}",
                        flush=True,
                    )
                else:
                    print(
                        f"Context: (none after {sel_elapsed:.2f}s — no text/image selected)",
                        flush=True,
                    )

            if self._closing.is_set():
                return
            if cycle.cancel.is_set():
                raise _Abort("cancelled")
            if not raw_text.strip() or not any(c.isalnum() for c in raw_text):
                print(">>> Empty transcription. Paste cancelled.")
                self.last_status = "empty"
                return

            notice = "ai_fallback" if use_llm and self.refiner is None else ""
            if use_llm and self.refiner is not None:
                if not keep_history and getattr(self.refiner, "conversation_history", None):
                    print(
                        f"Context: fresh AI reply (history {len(self.refiner.conversation_history)} msgs ignored)",
                        flush=True,
                    )
                refiner = self.refiner
                # The memory token is fixed here, on the pipeline thread, before
                # the stage starts: an abandon that lands before the worker even
                # reaches refine() still makes that call stale.
                pin = self._pin_ai_generation(refiner)
                try:
                    def ask():
                        with pin():
                            return refiner.refine(raw_text, context=context,
                                                  image_bytes=image_bytes, keep_history=keep_history)

                    outcome, stage = self._run_stage(ask, Config.LLM_DEADLINE_SECONDS, cycle,
                                                     "odicto-llm")
                except _Abort:
                    self._abandon_ai(refiner)
                    raise
                if outcome == "timeout":
                    self._abandon_ai(refiner)
                    # Same as an AI failure: the user's words still arrive.
                    print(f"!!! AI reply timed out after {Config.LLM_DEADLINE_SECONDS:g}s; "
                          "inserting the raw transcript.", file=sys.stderr, flush=True)
                    refined_text, notice = raw_text, "ai_timeout"
                else:
                    refined_text = stage.result() or ""
                    notice = getattr(refiner, "last_notice", "")
                self._log_text("Refined Text (AI)", refined_text)
            else:
                # Raw dictation: transcript only; no selection probe / no LLM.
                refined_text = raw_text
                speech_provider = (Config.effective_live_stt_provider() if live else
                                   Config.effective_stt_provider())
                already_smart = speech_provider == "gemini" and Config.GEMINI_TRANSCRIBE_MODE == "smart"
                if Config.POLISH_DICTATION and not already_smart and self.refiner is not None:
                    self.last_status = "polishing"
                    self._notify_ui()
                    refiner = self.refiner
                    outcome, stage = self._run_stage(
                        lambda: refiner.polish(raw_text),
                        Config.LLM_DEADLINE_SECONDS, cycle, "odicto-polish-wait")
                    if outcome == "timeout":
                        refined_text, notice = raw_text, "polish_fallback"
                    else:
                        refined_text = stage.result() or raw_text
                        notice = refiner.last_notice
                self._log_text("Raw Text (Bypass)", refined_text)

            if not refined_text.strip():
                self.last_status = "empty"
                return

            # Shutdown and final insertion share a gate: once closing begins,
            # late STT/LLM results can never inject input into another app.
            # Only the worker that still owns this cycle may insert.
            with self._lifecycle_lock:
                if self._closing.is_set():
                    return
                with self.state_lock:
                    if cycle.cancel.is_set() or self._cycle is not cycle:
                        raise _Abort("cancelled")
                    cycle.pasting = True
                if live:
                    # Keep the final payload for asynchronous paste consumers.
                    paste_text(refined_text, restore_clipboard=False)
                else:
                    paste_text(refined_text)

            elapsed: float = time.time() - start_time
            print(f">>> Text pasted successfully in {elapsed:.2f} seconds!")
            if not (isinstance(notice, str) and notice):
                notice = stt_notice or ("mic_gap" if cycle.gap else "")
            self.last_status = notice or "success"

        except _Abort as stop:
            if stop.status == "cancelled":
                print(">>> Cancelled. Nothing inserted.", flush=True)
            if stop.status:
                self.last_status = stop.status
        except Exception as e:
            print(f"!!! Pipeline Error: {e}", file=sys.stderr)
            self.last_status = "error"
        finally:
            self._finish_cycle()

    # ------------------------------------------------------------ pipeline stages
    def _claim_cycle(self) -> _Cycle:
        """Take the cycle created when this capture entered PROCESSING (or a new one)."""
        with self.state_lock:
            cycle = self._cycle
            if cycle is None or cycle.claimed:
                self._cycle_seq += 1
                cycle = _Cycle(self._cycle_seq)
                self._cycle = cycle
            cycle.claimed = True
            return cycle

    def _await_stage(self, stage: _Stage, seconds: float, cycle: _Cycle) -> str:
        """Wait for done, deadline, cancel or shutdown, whichever comes first."""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if cycle.cancel.is_set():
                return "cancelled"
            if self._closing.is_set():
                return "closing"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "done" if stage.done.is_set() else "timeout"
            if stage.done.wait(min(0.05, remaining)):
                return "done"

    @staticmethod
    def _pin_ai_generation(refiner):
        """Return a context-manager factory that pins the refiner's memory
        generation read NOW (caller thread) for the stage thread's refine()."""
        read = getattr(refiner, "history_generation", None)
        pinned = getattr(refiner, "pinned_generation", None)
        if callable(read) and callable(pinned):
            try:
                generation = read()
                return lambda: pinned(generation)
            except Exception:
                pass
        return contextlib.nullcontext

    @staticmethod
    def _abandon_ai(refiner) -> None:
        """The AI stage was given up: its late reply or failure must not edit memory."""
        abandon = getattr(refiner, "abandon_inflight", None)
        if callable(abandon):
            try:
                abandon()
            except Exception as e:
                print(f"Warning: could not detach the abandoned AI call: {e}", flush=True)

    @staticmethod
    def _raise_for(outcome: str) -> None:
        if outcome == "cancelled":
            raise _Abort("cancelled")
        if outcome == "closing":
            raise _Abort(None)

    def _run_stage(self, fn, seconds: float, cycle: _Cycle, name: str):
        """Run fn on a stage thread; return ("done"|"timeout", stage) or abort."""
        stage = _Stage(fn, name)
        outcome = self._await_stage(stage, seconds, cycle)
        self._raise_for(outcome)
        return outcome, stage

    def _run_stt(self, audio_source, use_llm: bool, live: bool, cycle: _Cycle) -> tuple:
        """Transcribe under STT_DEADLINE_SECONDS. Returns (text, notice).

        The pipeline owns the one local-Whisper fallback: cloud transcribers are
        called with allow_local_fallback=False, so a cloud request that fails or
        is abandoned at the deadline never starts a second, competing Whisper
        run. A cloud failure or timeout gets one local retry under a second
        deadline.
        """
        seconds = Config.STT_DEADLINE_SECONDS
        if live:
            primary = lambda: self._live_transcribe(  # noqa: E731
                audio_source, allow_local_fallback=False)
        else:
            primary = lambda: self._transcribe_for_pipeline(  # noqa: E731
                audio_source, use_llm, allow_local_fallback=False)
        outcome, stage = self._run_stage(primary, seconds, cycle, "odicto-stt")
        if outcome == "done" and stage.error is None:
            return stage.value, ""
        provider = (Config.effective_live_stt_provider() if live else
                    Config.effective_stt_provider())
        fallback = getattr(self._speech_backend(live), "local_fallback", None)
        if provider == "whisper" or not callable(fallback):
            if outcome == "done":
                raise stage.error
            print(f"!!! Speech-to-text timed out after {seconds:g}s.", file=sys.stderr, flush=True)
            raise _Abort("stt_timeout")
        if outcome == "done":
            # Cloud error: same silent local fallback the transcriber used to run.
            error = stage.error
            print(f"{provider} STT fallback to Whisper ({str(error) or type(error).__name__})",
                  flush=True)
            notice = ""
        else:
            print(f"!!! {provider} speech timed out after {seconds:g}s; trying local Whisper.",
                  file=sys.stderr, flush=True)
            notice = "stt_fallback"
        outcome, stage = self._run_stage(lambda: fallback(audio_source), seconds, cycle,
                                         "odicto-stt-local")
        if outcome == "done":
            return stage.result(), notice
        print(f"!!! Local Whisper also timed out after {seconds:g}s.", file=sys.stderr, flush=True)
        raise _Abort("stt_timeout")

    @staticmethod
    def _log_text(label: str, text: str, timing: str = "") -> None:
        """Transcripts and replies reach dictation.log only with LOG_TRANSCRIPTS=true."""
        text = text or ""
        suffix = f" ({timing})" if timing else ""
        if Config.LOG_TRANSCRIPTS:
            print(f'{label}: "{text}"{suffix}')
        else:
            print(f"{label}: {len(text)} chars{suffix}")

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
            # Keep captions through finalization, then discard this capture's UI data.
            self._live_committed = self.live_preview = ""
            self._cycle = None
            if self._closing.is_set():
                return
            self._set_state(AppState.IDLE)
            print("System Idle. Ready.")

if __name__ == "__main__":
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(line_buffering=True)  # type: ignore
        except AttributeError:
            pass

    # STRICT single-instance: take lock, sweep orphans, then construct the app
    # (which binds a system-wide keyboard hook). Never skip this gate.
    _pid_path = os.path.join(_install_root(), "dictation.pid")
    if not claim_install(_pid_path):
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
        app = DictationApp(runtime=True)
        app.run()
    finally:
        release_single_instance_lock()
