"""
Local wake-word + clap detection for AURA.

Wake triggers:
  • Say "Hey Aura"                    -> openwakeword model "hey_aura"
  • Say "Wake Up, Daddy's Home"       -> openwakeword model "wake_up_daddys_home"
                                          (speaks back "Welcome Home, Sir.")
  • Clap twice within ~1.5 seconds    -> model-free amplitude spike detector
                                          (also speaks back "Welcome Home, Sir.")

Design goals (unchanged from the original module):
  • ZERO cost when the feature is off — openwakeword is imported ONLY inside
    start()/install helpers, never at module load. If the user never enables
    wake word, none of this touches the app.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); the actual model inference AND
    the clap check run in this module's own background thread, so the
    real-time audio thread and the Gemini stream are never slowed.
  • Fully local & offline — audio fed here never leaves the machine; there is
    no network call except the one-time model download the user triggers
    from the UI.

A note on "Wake Up, Daddy's Home": openwakeword's pretrained/community
catalog only ships whatever phrases have actually been trained (that's how
"hey_aura" got there in the first place). A brand-new phrase like this one
needs its own tiny trained model (openwakeword's training notebook, ~1-2
hours of synthetic/recorded samples) before it can be downloaded here. This
module is written so that once that model file exists it just works — but
until then it will log that the phrase isn't available yet and quietly fall
back to "Hey Aura" + claps, rather than failing the whole feature.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

# Wake phrases this app knows about.
#   key   -> openwakeword model name (must match the .onnx/.tflite filename stem)
#   value -> (display phrase, spoken response when that phrase fires)
WAKE_MODEL = "hey_aura"  # kept for backward compatibility with existing callers
WAKE_PHRASES: dict[str, tuple[str, str]] = {
    "hey_aura": ("Hey Aura", "Yes, sir?"),
    "wake_up_daddys_home": ("Wake Up, Daddy's Home", "Welcome Home, Sir."),
}
# Score in [0,1]; above this counts as a detection. Tunable per environment.
DEFAULT_THRESHOLD = 0.5
# Mic frames arrive at 16 kHz int16; this is just the detector's input rate.
SAMPLE_RATE = 16000

# --- clap-to-wake tuning ----------------------------------------------------
CLAP_RESPONSE = "Welcome Home, Sir."
CLAP_COUNT_REQUIRED = 2          # how many claps make the gesture
CLAP_WINDOW_SECONDS = 1.5        # all claps must land inside this window
CLAP_REFRACTORY_SECONDS = 0.25   # ignore re-triggers off one clap's echo/tail
CLAP_AMPLITUDE_THRESHOLD = 9000  # int16 peak that can even count as a clap
CLAP_BACKGROUND_MULTIPLIER = 4.0  # ...and must also clear N x the noise floor


def is_installed() -> bool:
    """True if the openwakeword package is importable (no model check)."""
    try:
        import importlib.util
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def _models_dir() -> Path | None:
    try:
        import openwakeword
        d = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        return d if d.is_dir() else None
    except Exception:
        return None


def _shared_models_present(models_dir: Path) -> bool:
    has_mel = (any(models_dir.glob("melspectrogram*.onnx"))
               or any(models_dir.glob("melspectrogram*.tflite")))
    has_emb = (any(models_dir.glob("embedding_model*.onnx"))
               or any(models_dir.glob("embedding_model*.tflite")))
    return bool(has_mel and has_emb)


def is_ready(model_name: str = WAKE_MODEL) -> bool:
    """True if openwakeword is installed AND the given wake model's files are
    present on disk (plus the shared mel/embedding models it depends on).

    This is a cheap, DETERMINISTIC file-existence check. It deliberately does
    NOT construct a Model to probe readiness — doing that is slow and, worse,
    can clash with the detector's own Model when it's already running, which
    intermittently returned False and made the UI flicker to 'not
    downloaded'. Never raises.
    """
    if not is_installed():
        return False
    try:
        models_dir = _models_dir()
        if models_dir is None:
            return False
        has_wake = (any(models_dir.glob(f"{model_name}*.onnx"))
                    or any(models_dir.glob(f"{model_name}*.tflite")))
        return bool(has_wake and _shared_models_present(models_dir))
    except Exception:
        return False


def available_wake_words() -> list[str]:
    """Which entries in WAKE_PHRASES actually have their model file on disk
    right now. "hey_aura" should normally always be in this list once
    install_and_download() has succeeded; "wake_up_daddys_home" only appears
    once a custom-trained model for it has been placed/downloaded."""
    return [name for name in WAKE_PHRASES if is_ready(name)]


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install openwakeword if missing,
    then try to download every known wake model (WAKE_PHRASES). Returns
    (ok, message). Never raises — every failure is reported through the
    returned message and the logger.

    Success only requires the baseline "hey_aura" model to come through;
    any other phrase (e.g. "wake_up_daddys_home") that isn't available yet
    is reported as a heads-up, not a failure — claps + Hey Aura still work.
    """
    _tell = notify or (lambda _msg: None)
    try:
        if not is_installed():
            logger("Wake word: installing openwakeword (one-time)…")
            _tell("Wake word: installing openwakeword (one-time)…")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "openwakeword"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install failed: {tail[0][:160]}"

        logger("Wake word: downloading models…")
        _tell("Wake word: downloading models…")
        try:
            import openwakeword.utils as _u
            for name in WAKE_PHRASES:
                try:
                    _u.download_models([name])
                except TypeError:
                    _u.download_models()  # older signature downloads the default set
                    break
                except Exception:
                    # this particular phrase isn't in the downloadable catalog
                    # (most likely wake_up_daddys_home before it's trained) —
                    # skip it, other phrases still get their turn.
                    continue
        except Exception as e:
            return False, f"model download failed: {e}"

        if not is_ready(WAKE_MODEL):
            return False, "installed, but the wake model could not be loaded."

        missing = [name for name in WAKE_PHRASES if name != WAKE_MODEL and not is_ready(name)]
        if missing:
            phrases = ", ".join(f'"{WAKE_PHRASES[m][0]}"' for m in missing)
            msg = (f'Wake word ready ("Hey Aura", plus claps). {phrases} needs a '
                   f"custom-trained model before it can wake AURA by voice.")
            logger(f"Wake word: {msg}")
            return True, msg

        logger("Wake word: ready.")
        return True, "Wake word installed and ready."
    except Exception as e:
        return False, f"setup error: {e}"


class ClapDetector:
    """
    Lightweight, model-free double-clap detector. Runs on the same raw int16
    frames the wake-word model sees — no extra mic access, no ML, negligible
    CPU. A "clap" is a short, sharp amplitude spike well above the recent
    noise floor; two such spikes landing inside CLAP_WINDOW_SECONDS count as
    the "clap to wake" gesture.
    """

    def __init__(self,
                 required: int = CLAP_COUNT_REQUIRED,
                 window_seconds: float = CLAP_WINDOW_SECONDS,
                 refractory_seconds: float = CLAP_REFRACTORY_SECONDS,
                 amplitude_threshold: int = CLAP_AMPLITUDE_THRESHOLD,
                 background_multiplier: float = CLAP_BACKGROUND_MULTIPLIER):
        self._required = required
        self._window = window_seconds
        self._refractory = refractory_seconds
        self._threshold = amplitude_threshold
        self._bg_multiplier = background_multiplier
        self._hits: deque[float] = deque()
        self._last_hit_time = 0.0
        self._noise_floor = 200.0  # running estimate, adapts to ambient noise

    def feed(self, frame_int16) -> bool:
        """Feed one raw audio frame (numpy int16, already flattened to 1-D).
        Returns True the instant the required number of claps has landed
        inside the window, and clears state so the same gesture can't
        immediately re-fire."""
        try:
            import numpy as np
            peak = float(np.abs(frame_int16).max()) if frame_int16.size else 0.0
        except Exception:
            return False

        now = time.monotonic()
        # Adapt the noise floor slowly, and only from quiet frames, so a
        # clap itself never drags the floor up and desensitizes detection.
        if peak < self._threshold * 0.5:
            self._noise_floor = 0.98 * self._noise_floor + 0.02 * peak

        is_spike = (peak >= self._threshold
                    and peak >= self._noise_floor * self._bg_multiplier
                    and (now - self._last_hit_time) >= self._refractory)
        if not is_spike:
            return False

        self._last_hit_time = now
        self._hits.append(now)
        while self._hits and (now - self._hits[0]) > self._window:
            self._hits.popleft()

        if len(self._hits) >= self._required:
            self._hits.clear()
            return True
        return False


class WakeWordDetector:
    """
    Runs wake-word inference (and clap detection) in a dedicated thread. The
    mic thread calls feed() with raw int16 frames; detections invoke
    on_detect() (called from this thread — the callback must marshal to
    whatever loop/UI it needs).

    on_detect is called as on_detect(trigger, response) where trigger is one
    of "hey_aura", "wake_up_daddys_home", or "clap", and response is the
    phrase AURA should speak back (e.g. "Welcome Home, Sir." for the clap and
    "Daddy's Home" triggers). For backward compatibility, a callback that
    only accepts zero arguments still works — it just won't get the
    trigger/response info and will fire on every wake, same as before.
    """

    def __init__(self, on_detect: Callable[..., None],
                 threshold: float = DEFAULT_THRESHOLD,
                 enable_claps: bool = True,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._threshold = threshold
        self._enable_claps = enable_claps
        self._logger    = logger
        # See PluginRegistry: `logger` is the console and gets everything,
        # `notify` is the activity log and gets only what the user must act on.
        self._notify    = notify or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False
        self._model = None
        self._ready = False
        self._loaded_models: list[str] = []
        self._clap: ClapDetector | None = None

    def start(self) -> bool:
        """Load the model(s) and spawn the inference thread. Returns True on
        success. Safe to call again — a no-op if already running. Never
        raises. Falls back to clap-only detection if no voice model can be
        loaded at all."""
        if self._running:
            return True

        loaded_models: list[str] = []
        try:
            from openwakeword.model import Model
            loaded_models = available_wake_words() or [WAKE_MODEL]
            self._model = Model(wakeword_models=loaded_models, inference_framework="onnx")
            self._loaded_models = loaded_models
        except Exception as e:
            self._logger(f"Wake word: could not load voice model — {e}")
            if not self._enable_claps:
                self._notify("Wake word unavailable — use the WAKE NOW button.")
                self._model = None
                return False
            self._logger("Wake word: continuing with clap-to-wake only.")
            self._model = None
            self._loaded_models = []

        self._clap = ClapDetector() if self._enable_claps else None

        self._running = True
        self._ready = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()

        parts = [f'"{WAKE_PHRASES.get(n, (n, ""))[0]}"' for n in self._loaded_models]
        if self._clap is not None:
            parts.append(f"{CLAP_COUNT_REQUIRED} claps")
        self._logger("Wake word: listening for " + " or ".join(parts) + ".")
        return True

    def stop(self) -> None:
        self._running = False
        # unblock the thread if it's waiting on the queue
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        self._model = None
        self._clap = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Must stay cheap
        and never block — the frame is copied and dropped if the queue is
        backed up."""
        if not self._running:
            return
        try:
            # frame_int16 is a numpy int16 array (possibly 2-D mono) — flatten to 1-D
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _fire(self, trigger: str, response: str) -> None:
        try:
            self._on_detect(trigger, response)
        except TypeError:
            # backward compatibility: caller's callback takes no arguments
            try:
                self._on_detect()
            except Exception as e:
                self._logger(f"Wake word: on_detect error — {e}")
        except Exception as e:
            self._logger(f"Wake word: on_detect error — {e}")

    def _loop(self) -> None:
        import numpy as np
        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break

                frame_arr = np.asarray(frame, dtype=np.int16)

                # Clap check runs on every frame, independently of the ML
                # model, so it works even if no voice model loaded.
                if self._clap is not None and self._clap.feed(frame_arr):
                    self._drain()
                    self._fire("clap", CLAP_RESPONSE)
                    continue

                if self._model is None:
                    continue

                scores = self._model.predict(frame_arr)
                if isinstance(scores, dict) and scores:
                    for name in self._loaded_models:
                        score = max((float(v) for k, v in scores.items()
                                     if name in k.lower()), default=0.0)
                        if score >= self._threshold:
                            self._drain()
                            _, response = WAKE_PHRASES.get(name, (name, "Yes, sir?"))
                            self._fire(name, response)
                            break
            except Exception as e:
                self._logger(f"Wake word: inference error — {e}")

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass