"""
aura_clap.py — Double-Clap Wake Detection & Transient DSP for A.U.R.A. v2

Pure-numpy transient classifier that subscribes to existing audio streams
(no competing audio device streams).

Optimized for Clap-Only Wake & Anti-Doubling Rejection
-----------------------------------------------------
Mechanical clicks (keyboard, mouse), finger snaps, desk taps, and pen clicks
produce sharp acoustic transients. This detector uses multi-layer discrimination
to wake STRICTLY on deliberate human hand claps and reject any doubling-up of
frequent sharp sounds:

  1. OS input guard      - If keyboard/mouse input occurred near the transient
                           moment, it is typing or clicking, not a clap.
  2. Strict envelope     - Fast attack (<12 ms) with acoustic body duration
                           (3.5-38 ms) and exponential decay. Mechanical clicks
                           (<3.5 ms) and sustained sounds (>38 ms or slow decay)
                           are rejected.
  3. Body spectrum       - Human claps have significant acoustic body resonance
                           in 600-2600 Hz (Helmholtz cavity & palm resonance).
                           Snaps and pen clicks concentrate in 2600-5000 Hz;
                           mechanical switches and metal concentrate in >4000 Hz;
                           desk thumps concentrate in <600 Hz; whistles/tones are
                           rejected via spectral flatness.
  4. Isolation & quiet   - The first clap requires preceding quiet (_PRE_QUIET_S)
                           and a low recent transient density. Typing bursts,
                           frequent clicks, and clapping along never start a pair.
  5. Pattern integrity  - Any non-clap sound (or weak transient) between the two
                           claps immediately resets the detector.
  6. Pair consistency    - The two claps must have consistent loudness (ratio
                           0.35-2.8) and matching acoustic duration.
  7. Adaptive floor      - Dynamic noise tracking ensures thresholds scale with
                           room ambient noise.

Wake pattern
------------
  * Two deliberate claps 0.18-0.85 s apart.
  * Emits ``double_clap_detected`` and ``wake_detected("double_clap")``.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

_LOG = logging.getLogger("aura.clap")

# ── DSP constants ───────────────────────────────────────────────────────────
_BLOCK_MS = 2.0          # analysis block length
_EVENT_MS = 48.0         # observation window after an onset
_PRE_EMPH = 0.90         # pre-emphasis coefficient (suppresses desk rumble)
_WARMUP_BLOCKS = 250     # ~0.5 s of noise-floor calibration, no detections
_REFRACTORY_S = 0.09     # ignore re-triggers inside one clap's reverb
_PRE_QUIET_S = 0.40      # required silence before the first clap
_MAX_RECENT_TRANSIENTS = 2  # max allowed transients in the recent 2.0 s window before the 1st clap

_DEFAULTS = {
    "enabled": True,
    "allow_in_foreground": False,
    "snap_enabled": False,   # Strictly claps only; snaps disabled
    "input_guard": True,
    "input_guard_ms": 200,   # Keyboard/mouse input window
    "min_gap_s": 0.18,       # Min gap (180 ms) - excludes mouse double-clicks & key bounces
    "max_gap_s": 0.85,
    "cooldown_s": 2.0,
    "debug": False,
}


# ── Keyboard / mouse activity (OS level) ────────────────────────────────────
def _system_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard/mouse input, or None if unknown."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


@dataclass
class _Candidate:
    onset: int            # absolute sample index of the onset block
    wall: float           # estimated monotonic wall time of the onset
    floor: float          # noise floor when the onset was seen
    rms: List[float]
    peak: List[float]


@dataclass
class _Event:
    t: float              # onset time on the stream's sample clock (s)
    kind: str             # "clap" | "weak" | "rejected"
    peak: float
    snr: float
    dur_ms: float = 0.0
    reason: str = ""
    info: str = ""


class ClapDetector(QObject):
    """
    Subscribes to audio frames from the active microphone stream and emits
    double_clap_detected when a double clap is heard.
    """

    double_clap_detected = pyqtSignal()      # wake (double clap)
    single_clap_detected = pyqtSignal()      # first clap of a possible pair
    wake_detected = pyqtSignal(str)          # "double_clap"

    def __init__(
        self,
        sensitivity: float = 0.65,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sensitivity = float(np.clip(sensitivity, 0.1, 1.0))
        self._enabled = True
        self._is_away_cb: Callable[[], bool] | None = None
        self._is_foreground_cb: Callable[[], bool] | None = None
        self._input_idle_cb: Callable[[], Optional[float]] | None = None

        self._lock = threading.Lock()
        self._cfg: dict = dict(_DEFAULTS)
        self._cfg_ts = 0.0
        self._p: dict = {}

        self._sr = 0
        self._blk = 32
        self._event_blocks = 24
        self._cooldown_until = 0.0
        self._events: Deque[_Event] = deque()
        self._hist: Deque[float] = deque(maxlen=4)
        self._reset_stream()

    # ── properties / callbacks ──────────────────────────────────────────────
    @property
    def sensitivity(self) -> float:
        return self._sensitivity

    @sensitivity.setter
    def sensitivity(self, val: float) -> None:
        self._sensitivity = float(np.clip(val, 0.1, 1.0))

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, val: bool) -> None:
        val = bool(val)
        if val and not self._enabled:
            with self._lock:
                self._reset_stream()  # stale audio must not leak in
        self._enabled = val

    def set_away_callback(self, cb: Callable[[], bool]) -> None:
        """Callback returning True if user is in Away Mode / screen is locked."""
        self._is_away_cb = cb

    def set_foreground_callback(self, cb: Callable[[], bool]) -> None:
        """Callback returning True if main window is already foreground and active."""
        self._is_foreground_cb = cb

    def set_input_activity_callback(self, cb: Callable[[], Optional[float]]) -> None:
        """
        Callback returning seconds since the last keyboard/mouse input (or None).
        Only needed on non-Windows platforms; Windows uses GetLastInputInfo.
        """
        self._input_idle_cb = cb

    # ── public API ──────────────────────────────────────────────────────────
    def process_pcm_frame(self, pcm_data: bytes | np.ndarray, sample_rate: int = 16000) -> bool:
        """
        Process a short 16-bit PCM buffer (typically 20 ms - 100 ms).
        Returns True if a wake gesture (double clap / snap) fired on this frame.
        """
        if not self._enabled:
            return False
        cfg = self._config()
        if not cfg.get("enabled", True):
            return False
        samples = self._to_float(pcm_data)
        if samples is None or sample_rate < 8000:
            return False
        with self._lock:
            try:
                return self._process_locked(samples, int(sample_rate), cfg)
            except Exception:  # never let DSP errors kill the audio thread
                _LOG.exception("Clap detector error; resetting state")
                self._reset_stream()
                return False

    def stop(self) -> None:
        """Reset all state."""
        with self._lock:
            self._reset_stream()
            self._cooldown_until = 0.0

    # ── config ──────────────────────────────────────────────────────────────
    def _config(self) -> dict:
        now = time.monotonic()
        if now - self._cfg_ts > 1.0:
            cfg = dict(_DEFAULTS)
            try:
                from memory.config_manager import get_clap_config
                cfg.update(get_clap_config() or {})
            except Exception:
                pass
            self._cfg, self._cfg_ts = cfg, now
        return self._cfg

    def _build_params(self, cfg: dict) -> dict:
        sens = float(np.clip(cfg.get("sensitivity", self._sensitivity), 0.1, 1.0))
        return {
            # Robust thresholds: friendly to mid-setup hardware / CPU fan noise
            "snr": 1.8 + (1.0 - sens) * 3.0,
            "peak_min": 500.0 + (1.0 - sens) * 1600.0,
            "min_gap": float(cfg.get("min_gap_s", 0.18)),
            "max_gap": float(cfg.get("max_gap_s", 0.85)),
            "cooldown": float(cfg.get("cooldown_s", 2.0)),
            "snap": bool(cfg.get("snap_enabled", False)),
            "guard": bool(cfg.get("input_guard", True)),
            "guard_s": float(cfg.get("input_guard_ms", 200)) / 1000.0,
            "fg": bool(cfg.get("allow_in_foreground", False)),
            "debug": bool(cfg.get("debug", False)),
        }

    # ── state ───────────────────────────────────────────────────────────────
    def _reset_stream(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0
        self._next_block = 0
        self._prev_sample = 0.0
        self._hist.clear()
        self._cand: Optional[_Candidate] = None
        self._refractory_until = 0
        self._blocks_seen = 0
        self._floor = 30.0
        self._first: Optional[_Event] = None
        self._events.clear()

    @staticmethod
    def _to_float(pcm: bytes | np.ndarray) -> Optional[np.ndarray]:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            if len(pcm) < 64:
                return None
            n = len(pcm) // 2
            a = np.frombuffer(pcm, dtype=np.int16, count=n).astype(np.float32)
        elif isinstance(pcm, np.ndarray):
            a = pcm
            if a.ndim > 1:
                a = a.reshape(a.shape[0], -1).mean(axis=1)
            is_float = pcm.dtype.kind == "f"
            a = a.astype(np.float32, copy=False)
            if is_float and a.size and float(np.max(np.abs(a))) <= 1.5:
                a = a * 32768.0  # normalised float PCM -> int16 scale
        else:
            return None
        return a if a.size >= 32 else None

    # ── main pipeline ───────────────────────────────────────────────────────
    def _process_locked(self, samples: np.ndarray, sr: int, cfg: dict) -> bool:
        if sr != self._sr:
            self._reset_stream()
            self._sr = sr
            self._blk = max(16, int(round(sr * _BLOCK_MS / 1000.0)))
            self._event_blocks = max(8, int(round(_EVENT_MS / _BLOCK_MS)))
        self._p = self._build_params(cfg)

        now = time.monotonic()
        self._buf = np.concatenate((self._buf, samples))
        end = self._buf_start + self._buf.size
        blk = self._blk
        fired = False

        nb = (end - self._next_block) // blk
        if nb > 0:
            s = self._next_block - self._buf_start
            seg = self._buf[s:s + nb * blk]
            ext = np.concatenate((np.array([self._prev_sample], np.float32), seg))
            y = (ext[1:] - _PRE_EMPH * ext[:-1]).reshape(nb, blk)
            xb = seg.reshape(nb, blk)
            xb = xb - xb.mean(axis=1, keepdims=True)
            rms_f = np.sqrt(np.mean(y * y, axis=1))
            peaks = np.max(np.abs(xb), axis=1)
            self._prev_sample = float(seg[-1])
            for i in range(nb):
                start = self._next_block + i * blk
                wall = now - (end - start) / float(sr)
                if self._on_block(start, wall, float(rms_f[i]), float(peaks[i])):
                    fired = True
            self._next_block += nb * blk

        if self._tick(end / float(sr)):
            fired = True

        # keep ~0.5 s of raw audio for spectral analysis
        keep = int(sr * 0.5)
        drop = self._buf.size - keep
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf_start += drop
        return fired

    def _on_block(self, start: int, wall: float, rms: float, peak: float) -> bool:
        fired = False
        self._blocks_seen += 1
        cand = self._cand

        if cand is not None:
            cand.rms.append(rms)
            cand.peak.append(peak)
            if len(cand.rms) >= self._event_blocks:
                self._cand = None
                self._refractory_until = cand.onset + int(_REFRACTORY_S * self._sr)
                fired = self._handle_event(self._classify(cand))
            self._hist.append(rms)
            return fired

        floor = self._floor
        base = max(floor, (sum(self._hist) / len(self._hist)) if self._hist else rms)
        warm = self._blocks_seen <= _WARMUP_BLOCKS
        is_onset = (
            not warm
            and start >= self._refractory_until
            and peak >= 150.0
            and rms >= floor * 2.2
            and rms >= 2.2 * base
        )
        if is_onset:
            self._cand = _Candidate(start, wall, floor, [rms], [peak])
        else:
            if warm:
                floor = 0.98 * floor + 0.02 * rms
            elif rms < floor:
                floor = 0.90 * floor + 0.10 * rms          # drops fast
            elif rms < floor * 2.0:
                floor = 0.995 * floor + 0.005 * rms        # follows slowly
            else:
                floor += 0.0005 * (rms - floor)            # very slow rise
            self._floor = float(np.clip(floor, 6.0, 5000.0))
        self._hist.append(rms)
        return False

    # ── transient classification ────────────────────────────────────────────
    def _spectrum(self, c: _Candidate):
        sr, blk = self._sr, self._blk
        nfft = 1 << int(np.ceil(np.log2(sr * 0.032)))
        a = max(0, c.onset - 2 * blk - self._buf_start)
        seg = self._buf[a:a + nfft]
        if seg.size < nfft // 2:
            return None
        seg = (seg - seg.mean()) * np.hanning(seg.size).astype(np.float32)
        spec = np.abs(np.fft.rfft(seg, nfft)) ** 2
        freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
        cap = min(8000.0, sr / 2.0)

        def band(lo: float, hi: float) -> float:
            m = (freqs >= lo) & (freqs < min(hi, cap))
            return float(spec[m].sum())

        tot = band(150.0, cap) + 1e-9
        m_all = (freqs >= 150.0) & (freqs < cap)
        centroid = float((freqs[m_all] * spec[m_all]).sum() / (spec[m_all].sum() + 1e-9))
        m_fl = (freqs >= 300.0) & (freqs < cap)
        sp = spec[m_fl] + 1e-9
        flat = float(np.exp(np.mean(np.log(sp))) / np.mean(sp))
        return {
            "low": band(150, 600) / tot,
            "body": band(600, 2600) / tot,
            "sharp": band(2600, 5000) / tot,
            "high": band(4000, cap) / tot if cap > 4000 else 0.0,
            "centroid": centroid,
            "flat": flat,
        }

    def _classify(self, c: _Candidate) -> _Event:
        p, sr = self._p, self._sr
        rms = np.asarray(c.rms, dtype=np.float32)
        raw_peak = float(max(c.peak))
        peak_rms = float(rms.max())
        pi = int(rms.argmax())
        snr = peak_rms / max(c.floor, 1.0)
        blk_ms = self._blk * 1000.0 / sr

        # Baseline-relative excess: prevents CPU/fan ambient noise floor from inflating duration & masking decay
        peak_excess = max(1.0, peak_rms - c.floor)
        dur_thresh = c.floor + 0.25 * peak_excess
        dur_ms = float((rms >= dur_thresh).sum()) * blk_ms
        ev = _Event(t=c.onset / float(sr), kind="rejected", peak=raw_peak, snr=snr, dur_ms=dur_ms)

        if raw_peak < p["peak_min"] or snr < p["snr"]:
            ev.kind, ev.reason = "weak", "below loudness threshold"
            return ev
        if pi * blk_ms > 14.0:
            ev.reason = "slow attack (not impulsive)"
            return ev
        late = float(rms[(len(rms) * 2) // 3:].mean())
        late_excess = max(0.0, late - c.floor)
        if late_excess > 0.40 * peak_excess:
            ev.reason = "sustained sound (speech/music/noise)"
            return ev
        if dur_ms < 3.5:
            ev.reason = "too short (mechanical click)"
            return ev
        if dur_ms > 38.0:
            ev.reason = "too long (rub/scrape/voice)"
            return ev

        sp = self._spectrum(c)
        if sp is None:
            ev.kind, ev.reason = "weak", "not enough audio"
            return ev
        ev.info = (
            f"peak={raw_peak:.0f} snr={snr:.1f} dur={dur_ms:.0f}ms "
            f"low={sp['low']:.2f} body={sp['body']:.2f} sharp={sp['sharp']:.2f} "
            f"high={sp['high']:.2f} cen={sp['centroid']:.0f}Hz flat={sp['flat']:.2f}"
        )

        # Spectral classification for clap vs non-clap sounds:
        if sp["flat"] < 0.015:
            ev.reason = "tonal sound"
        elif sp["low"] > 0.50:
            ev.reason = "low-frequency thump"
        elif sp["high"] > 0.68:
            ev.reason = "high-frequency click / switch"
        elif sp["sharp"] > 0.60:
            ev.reason = "sharp snap / click / tap"
        elif sp["body"] < 0.10:
            ev.reason = "insufficient clap body resonance (600-2600 Hz)"
        elif not (700.0 <= sp["centroid"] <= 4800.0):
            ev.reason = f"centroid {sp['centroid']:.0f}Hz outside clap range"
        else:
            ev.kind = "clap"

        # OS input guard: reject if physical keyboard/mouse activity was detected near onset
        if ev.kind == "clap" and p["guard"] and self._input_near(c.wall):
            ev.kind, ev.reason = "rejected", "keyboard/mouse input at the same moment"
        return ev

    def _input_near(self, onset_wall: float) -> bool:
        try:
            idle = self._input_idle_cb() if self._input_idle_cb else _system_idle_seconds()
        except Exception:
            idle = None
        if idle is None:
            return False
        last_input = time.monotonic() - float(idle)
        guard = self._p["guard_s"]
        return (onset_wall - guard) <= last_input <= (onset_wall + guard + 0.1)

    # ── gesture logic ───────────────────────────────────────────────────────
    def _handle_event(self, ev: _Event) -> bool:
        p = self._p
        prior = [e for e in self._events if ev.t - _PRE_QUIET_S <= e.t < ev.t]
        recent = [e for e in self._events if ev.t - 2.0 <= e.t < ev.t]
        self._events.append(ev)
        while self._events and ev.t - self._events[0].t > 4.0:
            self._events.popleft()

        _LOG.log(
            logging.INFO if p["debug"] else logging.DEBUG,
            "transient t=%.3f -> %s%s | %s",
            ev.t, ev.kind, f" ({ev.reason})" if ev.reason else "", ev.info,
        )

        first = self._first
        if first is not None and ev.t - first.t > p["max_gap"]:
            first = self._first = None

        # WAKE ONLY ON CLAPS: Any non-clap transient immediately aborts an in-flight pair
        if ev.kind != "clap":
            if first is not None:
                self._first = None
            return False

        if time.monotonic() < self._cooldown_until:
            self._first = None
            return False

        if first is not None:
            gap = ev.t - first.t
            if gap < p["min_gap"]:
                self._first = None
                return False
            ratio = ev.peak / max(first.peak, 1.0)
            dur_diff = abs(ev.dur_ms - first.dur_ms)
            # Require clap pair consistency in both amplitude (ratio) and duration
            if 0.35 <= ratio <= 2.8 and dur_diff <= 22.0:
                _LOG.info("double_clap detected (gap=%.3fs, ratio=%.2f, dur_diff=%.1fms)", gap, ratio, dur_diff)
                return self._fire("double_clap")
            self._first = None  # loudness or duration mismatch -> not a deliberate pair
            return False

        # First clap validation:
        # 1. Pre-quiet silence: no transients within _PRE_QUIET_S
        # 2. Frequent sound rejection: no burst of recent transients in the last 2.0s
        if prior or len(recent) > _MAX_RECENT_TRANSIENTS:
            return False

        self._first = ev
        self.single_clap_detected.emit()
        return False

    def _tick(self, t_now: float) -> bool:
        f = self._first
        if f is None:
            return False
        age = t_now - f.t
        if age > self._p["max_gap"] + 0.05:
            self._first = None
        return False

    def _fire(self, kind: str) -> bool:
        p = self._p
        self._first = None
        now = time.monotonic()
        if now < self._cooldown_until:
            return False
        if self._is_away_cb:  # Away Mode / locked screen
            try:
                if self._is_away_cb():
                    return False
            except Exception:
                pass
        if not p["fg"] and self._is_foreground_cb:  # already in front
            try:
                if self._is_foreground_cb():
                    return False
            except Exception:
                pass
        self._cooldown_until = now + p["cooldown"]
        self.wake_detected.emit("double_clap")
        self.double_clap_detected.emit()
        return True