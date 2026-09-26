"""A fully offline British English voice, additive to AURA's normal pipeline.

This module owns exactly four things: a tiny on/off config, finding Piper
voice models on disk, picking a British one, and turning text into PCM16
mono audio at a caller-chosen sample rate. It knows nothing about sockets,
queues, visemes, or echo cancellation — `main.py` slices the PCM this
returns into the same ~50 ms chunks Gemini's own audio already uses and
drops them into the existing `audio_in_queue`, so every downstream system
(the mouth, the interrupt guard, the output-device picker) keeps working
completely unchanged, for either voice.

Backend: Piper (https://github.com/OHF-Voice/piper1-gpl), a small offline
neural TTS engine — an ONNX model file per voice, no OS speech engine, no
Windows language pack, no network call once the model is on disk. Voice
models are just files you place in `voices_dir` (see below); nothing here
downloads one automatically, so a missing model degrades to "unavailable"
rather than raising, and Gemini's own voice keeps working regardless.

── Getting a voice ───────────────────────────────────────────────────────────
  1. pip install piper-tts
  2. python -m piper.download_voices en_GB-alba-medium --data-dir <voices_dir>
     (browse other British options at https://rhasspy.github.io/piper-samples/
     — anything whose id starts with "en_GB-" is picked up automatically)
  3. Confirm the two files landed in `voices_dir`:
     en_GB-alba-medium.onnx
     en_GB-alba-medium.onnx.json
`voices_dir` defaults to <project root>/voices/piper — see `_VOICES_DIR`
below to change it. Run `python -m core.local_tts` any time to check what
this module can currently see.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import wave
from pathlib import Path

import numpy as np

# ── Paths ─────────────────────────────────────────────────────────────────────
# Both deliberately self-contained rather than reusing memory/config_manager.py
# or main.py's own path logic: this module has to stay a drop-in, independent
# addition that doesn't assume anything about those files' schema or format.

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # This file lives at <project root>/core/local_tts.py.
    return Path(__file__).resolve().parent.parent


_BASE_DIR    = _get_base_dir()
_CONFIG_PATH = _BASE_DIR / "config" / "local_tts.json"
_VOICES_DIR  = _BASE_DIR / "voices" / "piper"   # drop downloaded .onnx/.onnx.json pairs here

_DEFAULT_CONFIG = {"enabled": False, "voice_id": None}

_config_lock = threading.Lock()


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {**_DEFAULT_CONFIG, **data}
    except Exception:
        pass
    return dict(_DEFAULT_CONFIG)


def _save_config(cfg: dict) -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.replace(tmp, _CONFIG_PATH)
    except Exception:
        pass   # a config write failing must never take the app down


def is_enabled() -> bool:
    with _config_lock:
        return bool(_load_config().get("enabled"))


def enable() -> None:
    with _config_lock:
        cfg = _load_config()
        cfg["enabled"] = True
        _save_config(cfg)


def disable() -> None:
    with _config_lock:
        cfg = _load_config()
        cfg["enabled"] = False
        _save_config(cfg)


def active() -> bool:
    """True only when local speech should actually be used right now.

    Being switched on in config is not enough — if no Piper voice model was
    ever found in `voices_dir`, falling back to Gemini's own audio is what
    keeps AURA speaking at all. Callers in main.py should always gate on
    this, not on `is_enabled()`.
    """
    return is_enabled() and available_voice_id() is not None


# ── Finding voice models on disk ─────────────────────────────────────────────
# Piper voice ids are self-describing ("en_GB-alba-medium", "en_US-lessac-
# medium", ...), so — unlike an OS speech engine — "is this British" is just
# a filename prefix check, no name-matching heuristics needed.

def _is_british_id(voice_id: str) -> bool:
    return voice_id.lower().replace("-", "_").startswith("en_gb")


def list_voices() -> list[dict]:
    """Every Piper voice model found in `voices_dir`. Never raises.

    A "voice" here is any `<id>.onnx` with a matching `<id>.onnx.json` sitting
    next to it — exactly what `python -m piper.download_voices` produces.
    """
    out: list[dict] = []
    try:
        if not _VOICES_DIR.is_dir():
            return out
        for onnx_path in sorted(_VOICES_DIR.glob("*.onnx")):
            cfg_path = onnx_path.with_suffix(".onnx.json")
            if not cfg_path.exists():
                continue
            vid = onnx_path.stem
            out.append({
                "id": vid,
                "name": vid,
                "path": str(onnx_path),
                "british": _is_british_id(vid),
            })
    except Exception:
        return []
    return out


_voice_lock = threading.Lock()
_cached_voice: dict | None = None   # {"id":..., "path":...}
_scanned = False


def available_voice_id(force_refresh: bool = False) -> str | None:
    """The id of a usable British Male Piper voice, or None.

    Resolution order: a voice id saved in config (from a previous successful
    detection, or set by hand) if its model file still exists, otherwise the
    first British-looking (`en_GB-...`) voice found in `voices_dir`. Cached
    after the first scan.
    """
    global _cached_voice, _scanned
    with _voice_lock:
        if _scanned and not force_refresh:
            return _cached_voice["id"] if _cached_voice else None

        _scanned = True
        _cached_voice = None

        voices = list_voices()
        if not voices:
            return None

        cfg_id = _load_config().get("voice_id")
        by_id = {v["id"]: v for v in voices}
        if cfg_id and cfg_id in by_id:
            _cached_voice = by_id[cfg_id]
            return _cached_voice["id"]

        for v in voices:
            if v["british"]:
                _cached_voice = v
                with _config_lock:
                    cfg = _load_config()
                    cfg["voice_id"] = v["id"]
                    _save_config(cfg)
                return _cached_voice["id"]

        return None


def set_voice_id(voice_id: str | None) -> bool:
    """Manually pin the voice used for local speech, bypassing auto-detection.

    For machines with only non-British Piper voices downloaded, the UI offers
    the full list from `list_voices()` and lets the user pick one directly
    rather than being stuck with "none found". Returns False (and changes
    nothing) if `voice_id` doesn't match any model currently in `voices_dir`.
    """
    global _cached_voice, _scanned
    if not voice_id:
        return False
    by_id = {v["id"]: v for v in list_voices()}
    if voice_id not in by_id:
        return False
    with _config_lock:
        cfg = _load_config()
        cfg["voice_id"] = voice_id
        _save_config(cfg)
    with _voice_lock:
        _cached_voice = by_id[voice_id]
        _scanned = True
    return True


# ── Synthesis ────────────────────────────────────────────────────────────────
# The loaded PiperVoice (an ONNX Runtime session) is cached and reused rather
# than reloaded per sentence — unlike an OS engine, there is no "only works
# once per instance" bug to dodge here, and reloading the model from disk for
# every sentence would add real, unnecessary latency.

_load_lock  = threading.Lock()
_synth_lock = threading.Lock()
_loaded_voice = None          # the piper.PiperVoice instance
_loaded_voice_path: str | None = None


def _get_piper_voice(model_path: str):
    global _loaded_voice, _loaded_voice_path
    with _load_lock:
        if _loaded_voice is not None and _loaded_voice_path == model_path:
            return _loaded_voice
        from piper import PiperVoice   # raises ImportError if piper-tts isn't installed
        voice = PiperVoice.load(model_path)
        _loaded_voice, _loaded_voice_path = voice, model_path
        return voice


# ── PCM helpers (numpy, not `audioop`) ───────────────────────────────────────
# `audioop` was removed from the standard library in Python 3.13 (PEP 594),
# so — rather than adding a new dependency for three small operations —
# these do the same job with `numpy`, which this project already depends on
# for viseme formant analysis.

def _lin_to_int16(raw: bytes, sampwidth: int) -> np.ndarray:
    """Decode raw little-endian PCM of any common width to int16 samples."""
    if sampwidth == 2:
        return np.frombuffer(raw, dtype="<i2")
    if sampwidth == 1:
        # WAV's 8-bit PCM is unsigned; shift to signed 16-bit range.
        u8 = np.frombuffer(raw, dtype=np.uint8).astype(np.int16)
        return (u8 - 128) * 256
    if sampwidth == 4:
        i32 = np.frombuffer(raw, dtype="<i4")
        return (i32 >> 16).astype(np.int16)
    if sampwidth == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        i32 = (b[:, 0].astype(np.int32)
               | (b[:, 1].astype(np.int32) << 8)
               | (b[:, 2].astype(np.int32) << 16))
        i32 = np.where(i32 & 0x800000, i32 - 0x1000000, i32)   # sign-extend 24-bit
        return (i32 >> 8).astype(np.int16)
    raise ValueError(f"Unsupported WAV sample width: {sampwidth} bytes")


def _to_mono_int16(samples: np.ndarray, n_channels: int) -> np.ndarray:
    if n_channels <= 1:
        return samples
    frames = samples.reshape(-1, n_channels).astype(np.int32)
    mono = frames.mean(axis=1)
    return np.clip(mono, -32768, 32767).astype(np.int16)


def _resample_int16(samples: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    if in_rate == out_rate or samples.size == 0:
        return samples
    duration = samples.size / in_rate
    n_out = max(1, int(round(duration * out_rate)))
    x_old = np.linspace(0.0, duration, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
    resampled = np.interp(x_new, x_old, samples.astype(np.float64))
    return np.clip(resampled, -32768, 32767).astype(np.int16)


def synthesize_pcm16(text: str, sample_rate: int) -> bytes | None:
    """Render `text` with the local British Male voice as mono PCM16 at `sample_rate`.

    Blocking and CPU-bound — callers run it via `asyncio.to_thread`. Returns
    None only when nothing is configured yet (no voice model found) — that's
    a normal, expected state. Any other failure (bad install, corrupt model,
    a Piper API mismatch) is allowed to raise, so the caller's own error
    handling can log the real cause instead of the whole thing going quietly
    silent. `main.py`'s `_run_local_tts` already wraps this call in a
    try/except that prints exactly that.
    """
    text = (text or "").strip()
    if not text:
        return None

    voice_id = available_voice_id()
    if not voice_id or not _cached_voice:
        return None   # nothing configured yet — not an error

    # Only one synthesis at a time: `_run_local_tts` in main.py already calls
    # this from a single serial worker, so this lock is a second, cheap line
    # of defence rather than the only one.
    with _synth_lock:
        voice = _get_piper_voice(_cached_voice["path"])

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        buf.seek(0)

        with wave.open(buf, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth  = wf.getsampwidth()
            framerate  = wf.getframerate()
            raw        = wf.readframes(wf.getnframes())

        if not raw:
            raise RuntimeError(
                f"Piper returned an empty WAV for voice "
                f"'{_cached_voice['id']}' — the model file may be corrupt "
                f"or incompatible with the installed piper-tts version."
            )

        samples = _lin_to_int16(raw, sampwidth)
        samples = _to_mono_int16(samples, n_channels)
        samples = _resample_int16(samples, framerate, sample_rate)
        return samples.astype("<i2").tobytes()


if __name__ == "__main__":
    # Quick manual check: `python -m core.local_tts`
    print(f"Looking in: {_VOICES_DIR}")
    vs = list_voices()
    print(f"{len(vs)} voice(s) found:")
    for v in vs:
        flag = "🇬🇧" if v["british"] else "  "
        print(f"  {flag} {v['id']}")
    vid = available_voice_id()
    print(f"\nSelected: {vid or 'none'}")
    if vid:
        try:
            pcm = synthesize_pcm16(
                "Good afternoon. This is the local British Male voice.", 24000
            )
            print(f"Synthesized {len(pcm) if pcm else 0} bytes of PCM16 @ 24000 Hz.")
        except Exception:
            import traceback
            print("\nSynthesis FAILED — this is the real error hiding behind the silence:\n")
            traceback.print_exc()