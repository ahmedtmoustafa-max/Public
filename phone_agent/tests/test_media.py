"""Audio decoding and the speech-vs-music features."""

from __future__ import annotations

import numpy as np

from phone_agent.media import AudioAnalyzer, ULAW_TABLE, ulaw_to_pcm

from synth import music_like, silence, speech_like


def test_ulaw_table_matches_reference_codec():
    """Cross-check our lookup table against the stdlib G.711 implementation."""
    audioop = __import__("audioop")
    payload = bytes(range(256))
    expected = np.frombuffer(audioop.ulaw2lin(payload, 2), dtype="<i2")
    assert np.array_equal(ULAW_TABLE, expected)


def test_ulaw_decode_scales_to_unit_range():
    decoded = ulaw_to_pcm(bytes(range(256)))
    assert decoded.dtype == np.float32
    assert decoded.size == 256
    assert np.all(np.abs(decoded) <= 1.0)
    assert ulaw_to_pcm(b"").size == 0


def _windows(frames):
    analyzer = AudioAnalyzer()
    return [w for w in (analyzer.push(f) for f in frames) if w is not None]


def test_analyzer_emits_one_window_per_second():
    windows = _windows(speech_like(3.0))
    assert len(windows) == 3


def test_speech_scores_higher_than_hold_music():
    speech = _windows(speech_like(4.0))
    music = _windows(music_like(4.0))
    speech_score = np.mean([w.speech_likeness for w in speech])
    music_score = np.mean([w.speech_likeness for w in music])
    assert speech_score > 0.5, speech_score
    assert music_score < 0.3, music_score
    assert speech_score > music_score + 0.3


def test_silence_is_flagged_and_scores_zero():
    windows = _windows(silence(2.0))
    assert all(w.is_silent for w in windows)
    assert all(w.speech_likeness == 0.0 for w in windows)


def test_music_has_low_syllable_rate_modulation():
    speech = _windows(speech_like(4.0))
    music = _windows(music_like(4.0))
    # Skip the first window: the envelope buffer is still filling.
    assert np.mean([w.modulation for w in speech[1:]]) > np.mean(
        [w.modulation for w in music[1:]]
    )
