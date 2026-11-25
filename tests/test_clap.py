"""
tests/test_clap.py — Unit Tests for Two-Clap Transient DSP and Fast Wake
"""

import time
import numpy as np
import pytest
from aura_clap import ClapDetector


def make_transient(peak_amplitude: float = 22000.0, length: int = 1600) -> np.ndarray:
    """Synthesize a sharp broadband impulse with rapid exponential decay (clap)."""
    t = np.linspace(0, 35, length)
    decay = np.exp(-t)
    noise = np.random.uniform(-1, 1, length)
    samples = (noise * decay * peak_amplitude).astype(np.int16)
    return samples


def feed_clap(cd: ClapDetector) -> bool:
    res = cd.process_pcm_frame(make_transient())
    # Feed trailing silence to let the ring buffer extract the completed block
    silence_res = cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
    return res or silence_res


def test_clap_detector_initialization():
    cd = ClapDetector(sensitivity=0.7)
    assert cd.sensitivity == pytest.approx(0.7)
    assert cd.enabled is True

    cd.sensitivity = 0.9
    assert cd.sensitivity == pytest.approx(0.9)

    cd.enabled = False
    assert cd.enabled is False


def test_silence_rejection():
    cd = ClapDetector(sensitivity=0.65)
    silence = np.zeros(1600, dtype=np.int16)
    assert not cd.process_pcm_frame(silence)
    assert not cd.process_pcm_frame(silence.tobytes())


def test_continuous_speech_rejection():
    cd = ClapDetector(sensitivity=0.65)
    # 440Hz sine wave has low crest factor (~1.41)
    t = np.linspace(0, 0.1, 1600, endpoint=False)
    sine = (np.sin(2 * np.pi * 440 * t) * 6000).astype(np.int16)
    assert not cd.process_pcm_frame(sine)


def test_single_and_double_clap_detection():
    cd = ClapDetector(sensitivity=0.75)
    cd.set_away_callback(lambda: False)
    cd.set_input_activity_callback(lambda: 999.0)
    # Establish pre-quiet period (0.5s)
    cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))

    single_events = []
    double_events = []
    cd.single_clap_detected.connect(lambda: single_events.append(1))
    cd.double_clap_detected.connect(lambda: double_events.append(1))

    # First clap: should register candidate but return False
    res1 = feed_clap(cd)
    assert not res1
    assert len(single_events) == 1
    assert len(double_events) == 0

    # Wait 300ms in audio samples (4800 samples at 16kHz)
    cd.process_pcm_frame(np.zeros(4800, dtype=np.int16))

    # Second clap: should trigger double-clap and return True
    res2 = feed_clap(cd)
    assert res2
    assert len(double_events) == 1


def test_double_clap_timing_window():
    cd = ClapDetector(sensitivity=0.75)
    cd.set_away_callback(lambda: False)
    cd.set_input_activity_callback(lambda: 999.0)

    # 1. Too fast (< 0.15s)
    feed_clap(cd)
    time.sleep(0.05)
    assert not feed_clap(cd)

    # 2. Too slow (> 0.85s)
    cd.stop()
    feed_clap(cd)
    time.sleep(0.95)
    assert not feed_clap(cd)


def test_away_mode_suppression():
    cd = ClapDetector(sensitivity=0.75)
    cd.set_away_callback(lambda: True)
    cd.set_input_activity_callback(lambda: 999.0)

    hits = []
    cd.double_clap_detected.connect(lambda: hits.append(1))

    cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))
    feed_clap(cd)
    cd.process_pcm_frame(np.zeros(4800, dtype=np.int16))
    res = feed_clap(cd)
    assert not res
    assert len(hits) == 0


def test_cooldown_enforcement():
    cd = ClapDetector(sensitivity=0.75)
    cd.set_away_callback(lambda: False)
    cd.set_input_activity_callback(lambda: 999.0)
    cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))

    # Valid double clap
    feed_clap(cd)
    cd.process_pcm_frame(np.zeros(4800, dtype=np.int16))
    assert feed_clap(cd)

    # Immediate next claps during cooldown (2.0s) should be rejected
    time.sleep(0.2)
    assert not feed_clap(cd)


def test_cupped_and_flat_claps_wake():
    sr = 16000
    t = np.arange(1600) / sr

    # Cupped palm clap (Helmholtz cavity resonance ~1050 Hz)
    cupped = ((0.75 * np.sin(2 * np.pi * 1050 * t) * np.exp(-t / 0.015) +
               0.5 * np.random.uniform(-1, 1, 1600) * np.exp(-t / 0.007)) / 1.25 * 22000).astype(np.int16)

    # Flat palm clap (resonance ~1800 Hz)
    flat = ((0.7 * np.sin(2 * np.pi * 1800 * t) * np.exp(-t / 0.012) +
             0.5 * np.random.uniform(-1, 1, 1600) * np.exp(-t / 0.007)) / 1.2 * 22000).astype(np.int16)

    for clap in [cupped, flat]:
        cd = ClapDetector(sensitivity=0.75)
        cd.set_away_callback(lambda: False)
        cd.set_input_activity_callback(lambda: 999.0)
        cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))

        events = []
        cd.double_clap_detected.connect(lambda: events.append(1))

        # First clap
        cd.process_pcm_frame(clap)
        cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
        assert len(events) == 0

        # Gap 300 ms
        cd.process_pcm_frame(np.zeros(4800, dtype=np.int16))

        # Second clap
        res = cd.process_pcm_frame(clap) or cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
        assert res is True
        assert len(events) == 1


def test_snaps_do_not_wake():
    sr = 16000
    t = np.arange(1600) / sr
    snap = ((np.sin(2 * np.pi * 3200 * t) * np.exp(-t / 0.005) +
             0.3 * np.random.uniform(-1, 1, 1600) * np.exp(-t / 0.004)) / 1.3 * 22000).astype(np.int16)

    cd = ClapDetector(sensitivity=0.75)
    cd.set_away_callback(lambda: False)
    cd.set_input_activity_callback(lambda: 999.0)
    cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))

    events = []
    cd.double_clap_detected.connect(lambda: events.append(1))

    # Single snap with trailing silence must not trigger wake
    cd.process_pcm_frame(snap)
    cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
    cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))
    assert len(events) == 0

    # Double snap must also not trigger wake
    cd.process_pcm_frame(snap)
    cd.process_pcm_frame(np.zeros(4800, dtype=np.int16))
    cd.process_pcm_frame(snap)
    cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
    assert len(events) == 0


def test_pen_and_mouse_clicks_do_not_wake():
    sr = 16000
    t = np.arange(1600) / sr

    # Pen click (narrow 3800 Hz transient, 3 ms decay)
    pen = ((np.sin(2 * np.pi * 3800 * t) * np.exp(-t / 0.003) +
            0.4 * np.random.uniform(-1, 1, 1600) * np.exp(-t / 0.002)) / 1.4 * 20000).astype(np.int16)

    # Mouse click (sharp 1.5 ms switch impulse)
    mouse = np.zeros(1600, dtype=np.float32)
    dur_m = int(sr * 0.0015)
    mouse[:dur_m] = np.sin(2 * np.pi * 4800 * t[:dur_m]) * np.exp(-t[:dur_m] * 2000)
    mouse = (mouse / (np.max(np.abs(mouse)) + 1e-5) * 18000).astype(np.int16)

    for click in [pen, mouse]:
        cd = ClapDetector(sensitivity=0.75)
        cd.set_away_callback(lambda: False)
        cd.set_input_activity_callback(lambda: 999.0)
        cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))

        events = []
        cd.double_clap_detected.connect(lambda: events.append(1))

        # Two clicks separated by 250 ms
        cd.process_pcm_frame(click)
        cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
        cd.process_pcm_frame(np.zeros(4000, dtype=np.int16))
        res = cd.process_pcm_frame(click) or cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
        assert not res
        assert len(events) == 0


def test_frequent_sharp_sounds_burst_rejected():
    sr = 16000
    t = np.arange(1600) / sr
    click = ((np.sin(2 * np.pi * 3800 * t) * np.exp(-t / 0.003) +
              0.4 * np.random.uniform(-1, 1, 1600) * np.exp(-t / 0.002)) / 1.4 * 20000).astype(np.int16)

    cd = ClapDetector(sensitivity=0.75)
    cd.set_away_callback(lambda: False)
    cd.set_input_activity_callback(lambda: 999.0)
    cd.process_pcm_frame(np.zeros(8000, dtype=np.int16))

    events = []
    cd.double_clap_detected.connect(lambda: events.append(1))

    # Burst of 6 sharp clicks 180 ms apart (e.g. typing or clicking burst)
    for _ in range(6):
        cd.process_pcm_frame(click)
        cd.process_pcm_frame(np.zeros(1600, dtype=np.int16))
        cd.process_pcm_frame(np.zeros(2880, dtype=np.int16))
    assert len(events) == 0


def test_claps_wake_under_cpu_fan_noise():
    sr = 16000
    t = np.arange(8000) / sr

    # Heavy CPU fan / motor noise (RMS ~800)
    fan_noise = (
        800 * np.sin(2 * np.pi * 150 * t) +
        600 * np.sin(2 * np.pi * 300 * t) +
        400 * np.sin(2 * np.pi * 900 * t) +
        1200 * np.random.uniform(-1, 1, 8000)
    ).astype(np.int16)

    cd = ClapDetector(sensitivity=0.65)
    cd.set_away_callback(lambda: False)
    cd.set_input_activity_callback(lambda: 999.0)

    # Establish elevated fan noise floor
    for _ in range(6):
        cd.process_pcm_frame(fan_noise)
    assert cd._floor > 400.0

    events = []
    cd.double_clap_detected.connect(lambda: events.append(1))

    # Moderate hand clap (1050 Hz cavity resonance) mixed into fan noise
    t_c = np.arange(1600) / sr
    clap_clean = ((0.75 * np.sin(2 * np.pi * 1050 * t_c) * np.exp(-t_c / 0.015) +
                   0.5 * np.random.uniform(-1, 1, 1600) * np.exp(-t_c / 0.007)) / 1.25 * 10000).astype(np.int16)
    clap_mixed = clap_clean + fan_noise[:1600]

    # Clap 1
    cd.process_pcm_frame(clap_mixed)
    cd.process_pcm_frame(fan_noise[:1600])
    assert len(events) == 0

    # Gap (300 ms of fan noise)
    cd.process_pcm_frame(fan_noise[:4800])

    # Clap 2
    res = cd.process_pcm_frame(clap_mixed) or cd.process_pcm_frame(fan_noise[:1600])
    assert res is True
    assert len(events) == 1


