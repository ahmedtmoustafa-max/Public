"""Synthetic call audio, so the detector can be tested without a phone line."""

from __future__ import annotations

import math

import numpy as np

from phone_agent.media import FRAME_SAMPLES, SAMPLE_RATE

RNG = np.random.default_rng(20260919)


def _frames(signal: np.ndarray) -> list[np.ndarray]:
    usable = (signal.size // FRAME_SAMPLES) * FRAME_SAMPLES
    return list(signal[:usable].reshape(-1, FRAME_SAMPLES))


def speech_like(seconds: float, level: float = 0.25) -> list[np.ndarray]:
    """Noise shaped by a 4 Hz syllable envelope, with real gaps between words."""
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    envelope = np.abs(np.sin(2 * math.pi * 4.0 * t)) ** 1.5
    gate = (np.sin(2 * math.pi * 0.9 * t + 0.4) > -0.35).astype(float)
    carrier = RNG.normal(0, 1, n)
    return _frames((carrier * envelope * gate * level).astype(np.float32))


def music_like(seconds: float, level: float = 0.2) -> list[np.ndarray]:
    """A steady tonal pad: continuous, tonal, no syllable-rate modulation."""
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    signal = (
        np.sin(2 * math.pi * 220 * t)
        + 0.6 * np.sin(2 * math.pi * 330 * t)
        + 0.4 * np.sin(2 * math.pi * 440 * t)
    )
    signal *= 1.0 + 0.05 * np.sin(2 * math.pi * 0.4 * t)
    return _frames((signal / 2.0 * level).astype(np.float32))


def silence(seconds: float) -> list[np.ndarray]:
    n = int(seconds * SAMPLE_RATE)
    return _frames((RNG.normal(0, 1, n) * 0.0004).astype(np.float32))


def melodic_music(seconds: float, level: float = 0.22) -> list[np.ndarray]:
    """Closer to real hold music: a note sequence at ~2.5 notes/sec.

    Notes give it amplitude modulation in the same band as syllables, which is
    the honest hard case for any audio-only speech detector.
    """
    n = int(seconds * SAMPLE_RATE)
    notes = [262, 294, 330, 349, 392, 440, 392, 330]
    signal = np.zeros(n)
    duration = 0.4
    for index, freq in enumerate(notes * 40):
        start = int(index * duration * SAMPLE_RATE)
        if start >= n:
            break
        stop = min(n, start + int(duration * SAMPLE_RATE))
        t = np.arange(stop - start) / SAMPLE_RATE
        envelope = np.exp(-3.0 * t) + 0.25
        signal[start:stop] += (
            np.sin(2 * math.pi * freq * t) * envelope
            + 0.3 * np.sin(2 * math.pi * freq * 2 * t) * envelope
        )
    signal += RNG.normal(0, 0.02, n)
    return _frames((signal * level / 2).astype(np.float32))
