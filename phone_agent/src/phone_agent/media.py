"""Decoding and feature extraction for Twilio Media Stream audio.

Twilio hands us 8 kHz G.711 mu-law in 20 ms frames (160 bytes). Everything in
here works on that assumption. The features exist to answer one question:
*does this sound like a live person, or like a hold queue?*
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAMES_PER_SECOND = 1000 // FRAME_MS            # 50


def _build_ulaw_table() -> np.ndarray:
    """256-entry mu-law -> int16 lookup table (G.711 mu-law, ITU-T G.711)."""
    codes = np.arange(256, dtype=np.int32)
    u = ~codes & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa.astype(np.int32) << 3) + 0x84) << exponent
    magnitude -= 0x84
    samples = np.where(sign != 0, -magnitude, magnitude)
    return samples.astype(np.int16)


ULAW_TABLE = _build_ulaw_table()


def ulaw_to_pcm(payload: bytes) -> np.ndarray:
    """Decode mu-law bytes to float32 samples in roughly [-1, 1]."""
    if not payload:
        return np.zeros(0, dtype=np.float32)
    codes = np.frombuffer(payload, dtype=np.uint8)
    return ULAW_TABLE[codes].astype(np.float32) / 32768.0


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def _spectral_flatness(x: np.ndarray) -> float:
    """Geometric mean / arithmetic mean of the power spectrum.

    Near 1.0 for noise-like signals, near 0.0 for tonal ones (music, ringback).
    """
    if x.size < 64:
        return 0.0
    windowed = x * np.hanning(x.size)
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    spectrum = spectrum[1:]  # drop DC
    if spectrum.size == 0:
        return 0.0
    spectrum = spectrum + 1e-12
    geometric = float(np.exp(np.mean(np.log(spectrum))))
    arithmetic = float(np.mean(spectrum))
    if arithmetic <= 0:
        return 0.0
    return min(1.0, geometric / arithmetic)


def _modulation_index(envelope: np.ndarray) -> float:
    """Depth of 2-10 Hz amplitude modulation, relative to the mean level.

    Running speech modulates its own loudness at roughly 4 Hz as syllables come
    and go, swinging most of the way to silence between them; the index lands
    near 1. Steady hold music keeps a near-constant level, so however its
    energy is distributed across the band, the *depth* stays near 0.

    Measuring depth rather than the band's share of envelope energy matters:
    a flat envelope with a little jitter still has a third of that jitter
    inside 2-10 Hz, which made the share-based version read almost as high
    for music as for speech.
    """
    if envelope.size < 16:
        return 0.0
    mean_level = float(np.mean(envelope))
    if mean_level <= 1e-9:
        return 0.0
    centred = envelope - mean_level
    if not np.any(centred):
        return 0.0
    spectrum = np.fft.rfft(centred)
    freqs = np.fft.rfftfreq(centred.size, d=1.0 / FRAMES_PER_SECOND)
    spectrum[(freqs < 2.0) | (freqs > 10.0)] = 0.0
    band = np.fft.irfft(spectrum, n=centred.size)
    return float(np.std(band) / mean_level)


@dataclass
class AudioWindow:
    """Aggregate description of about one second of audio."""

    energy: float          # mean RMS
    peak: float            # loudest frame RMS
    pause_ratio: float     # fraction of frames that were near-silent
    flatness: float
    modulation: float
    is_silent: bool

    @property
    def speech_likeness(self) -> float:
        """0..1 -- how much this window behaves like someone talking.

        Deliberately cheap and explainable. Three independent votes:
        syllable-rate modulation, a healthy amount of pausing, and a
        noise-like (rather than tonal) spectrum.
        """
        if self.is_silent:
            return 0.0
        modulation_vote = min(1.0, self.modulation / 0.55)
        # Speech breathes: 10-55% of frames should be quiet. Music rarely stops,
        # and pure silence is not speech either.
        if self.pause_ratio < 0.05 or self.pause_ratio > 0.75:
            pause_vote = 0.0
        else:
            pause_vote = 1.0 - abs(self.pause_ratio - 0.30) / 0.45
            pause_vote = max(0.0, min(1.0, pause_vote))
        flatness_vote = min(1.0, self.flatness / 0.25)
        return round(
            0.5 * modulation_vote + 0.3 * pause_vote + 0.2 * flatness_vote, 4
        )


class AudioAnalyzer:
    """Feed it 20 ms frames; it emits an :class:`AudioWindow` once a second."""

    #: Below this RMS the line is quiet whatever else is going on. Roughly
    #: -46 dBFS, comfortably under PSTN line noise.
    ABSOLUTE_SILENCE = 0.005
    #: A frame counts as a pause at this fraction of the recent loud level...
    PAUSE_FRACTION = 0.08
    #: ...and a whole window counts as silent at this fraction.
    SILENCE_FRACTION = 0.12

    def __init__(self, window_seconds: float = 1.0, envelope_seconds: float = 2.0):
        self._frames_per_window = max(1, int(window_seconds * FRAMES_PER_SECOND))
        self._envelope = deque(maxlen=int(envelope_seconds * FRAMES_PER_SECOND))
        self._pending: list[np.ndarray] = []
        self._frame_rms: list[float] = []
        #: Rolling 30 s of frame loudness. Silence is judged against the LOUD
        #: end of this, not the quiet end: a quiet-end reference drifts up to
        #: meet sustained hold music and eventually declares it silence, which
        #: then looks exactly like a person pausing for an answer.
        self._recent_rms: deque[float] = deque(maxlen=30 * FRAMES_PER_SECOND)
        self.frames_seen = 0

    def push(self, pcm: np.ndarray) -> AudioWindow | None:
        if pcm.size == 0:
            return None
        self.frames_seen += 1
        rms = _rms(pcm)
        self._pending.append(pcm)
        self._frame_rms.append(rms)
        self._envelope.append(rms)
        self._recent_rms.append(rms)

        if len(self._pending) < self._frames_per_window:
            return None
        return self._flush()

    @property
    def reference_level(self) -> float:
        """Loudness of the recent loud parts of the line."""
        if not self._recent_rms:
            return 0.0
        return float(np.percentile(np.asarray(self._recent_rms, dtype=np.float64), 90))

    def _flush(self) -> AudioWindow:
        samples = np.concatenate(self._pending)
        rms_values = np.asarray(self._frame_rms, dtype=np.float64)
        energy = float(np.mean(rms_values))
        peak = float(np.max(rms_values))
        reference = self.reference_level

        if reference < self.ABSOLUTE_SILENCE * 1.5:
            # Nothing loud has happened recently: the line is simply quiet.
            gate = self.ABSOLUTE_SILENCE
            silent = True
        else:
            gate = max(self.ABSOLUTE_SILENCE, reference * self.PAUSE_FRACTION)
            silent = peak < max(self.ABSOLUTE_SILENCE, reference * self.SILENCE_FRACTION)

        window = AudioWindow(
            energy=energy,
            peak=peak,
            pause_ratio=float(np.mean(rms_values < gate)),
            flatness=_spectral_flatness(samples),
            modulation=_modulation_index(np.asarray(self._envelope, dtype=np.float64)),
            is_silent=silent,
        )
        self._pending.clear()
        self._frame_rms.clear()
        return window
