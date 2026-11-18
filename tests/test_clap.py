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
