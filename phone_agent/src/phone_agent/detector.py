"""Decide when a live human has picked up the line.

This is the part that earns its keep. The hard case is not telling speech from
music -- it is telling a *recorded* hold announcement (which is speech) from an
actual agent. Three things separate them:

1. What they say. Agents greet and ask questions; recordings apologise about
   wait times and tell you your call is important.
2. Repetition. Hold loops come around again; people do not.
3. Expectant silence. A person stops talking and waits for an answer. A
   recording is followed by more recording, or by music.

Signal 3 works with no transcription at all, which is why the whole system
still functions without a speech-to-text key.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

from .media import AudioWindow

# --- Phrase banks ----------------------------------------------------------
# Matched case-insensitively against transcribed audio.

LIVE_AGENT_PATTERNS: list[tuple[str, float]] = [
    (r"\bmy name is\b", 0.85),
    (r"\bthis is \w+ speaking\b", 0.9),
    (r"\bhow (?:can|may) i (?:help|assist)\b", 0.95),
    (r"\bwhat can i (?:help|do) (?:you )?(?:with|for)\b", 0.9),
    (r"\bwho am i speaking (?:with|to)\b", 0.9),
    (r"\bmay i (?:have|get) your name\b", 0.8),
    (r"\bthank(?:s| you) for (?:holding|waiting)\b", 0.75),
    (r"\bsorry (?:to keep you|for the) wait(?:ing)?\b", 0.8),
    (r"\bare you (?:still )?there\b", 0.8),
    (r"\bgo ahead\b", 0.5),
    (r"\bspeaking\b", 0.4),
    (r"\bhello\?", 0.5),
    (r"\bhi,? (?:this is|my name)\b", 0.85),
]

HOLD_PATTERNS: list[tuple[str, float]] = [
    (r"\byour call is (?:very )?important\b", 0.95),
    (r"\ball (?:of )?our (?:agents|representatives|operators) are (?:currently )?busy\b", 0.95),
    (r"\bestimated wait time\b", 0.95),
    (r"\bplease (?:continue to )?(?:hold|stay on the line)\b", 0.9),
    (r"\bthe next available (?:agent|representative)\b", 0.9),
    (r"\bfor quality (?:assurance|and training)\b", 0.6),
    (r"\bdid you know (?:that )?you can\b", 0.8),
    (r"\bvisit (?:us|our website) at\b", 0.7),
    (r"\byour (?:call|place) in (?:the )?queue\b", 0.9),
    (r"\bcalls? (?:may|will) be (?:monitored|recorded)\b", 0.5),
    (r"\bwe (?:are )?experienc(?:e|ing) (?:higher|unusually high)\b", 0.9),
]

MENU_PATTERNS: list[tuple[str, float]] = [
    (r"\bpress \w+\b", 0.9),
    (r"\bfor .{2,40}, press\b", 0.95),
    (r"\bto .{2,40}, press\b", 0.9),
    (r"\bmain menu\b", 0.8),
    (r"\bmarque\b|\boprima\b|\bpara espa", 0.8),
    (r"\bsay (?:or press|the|one of)\b", 0.8),
    (r"\bplease (?:enter|key in)\b", 0.85),
    (r"\bin a few words,? tell (?:me|us)\b", 0.9),
]


def _match_score(text: str, patterns: list[tuple[str, float]]) -> tuple[float, list[str]]:
    """Highest-weighted matching pattern wins; extras nudge it up a little."""
    hits: list[tuple[float, str]] = []
    for pattern, weight in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            hits.append((weight, pattern))
    if not hits:
        return 0.0, []
    hits.sort(reverse=True)
    score = hits[0][0]
    for weight, _ in hits[1:]:
        score += (1.0 - score) * weight * 0.3
    return min(1.0, score), [p for _, p in hits]


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


@dataclass
class Verdict:
    """Detector output. ``score`` is 0..1 confidence that a human is on."""

    human: bool = False
    score: float = 0.0
    on_hold: bool = False
    in_menu: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Utterance:
    at: float
    text: str


class HumanDetector:
    """Rolling classifier over audio windows and transcript fragments."""

    #: A person's turn is short. Anything longer is a recording.
    MAX_HUMAN_BURST = 9.0
    MIN_HUMAN_BURST = 0.8
    #: How long the line must go quiet after a burst to count as "waiting".
    EXPECTANT_SILENCE = 1.4

    def __init__(self, strictness: float = 0.55, has_asr: bool = False):
        self.strictness = max(0.0, min(1.0, strictness))
        self.has_asr = has_asr
        self.threshold = 0.32 + 0.45 * self.strictness

        self._windows: deque[AudioWindow] = deque(maxlen=8)
        self._utterances: list[_Utterance] = []
        self._speech_run = 0.0        # seconds of continuous speech-like audio
        self._silence_run = 0.0       # seconds of continuous near-silence
        self._last_burst_length = 0.0
        self._music_seconds = 0.0
        self._audio_seconds = 0.0
        self._text_human = 0.0
        self._text_hold = 0.0
        self._text_menu = 0.0
        self._text_at = 0.0
        self._repeated = False
        self._latched_hold = False
        #: Set once we fire, so we never bridge the same call twice.
        self.fired = False

    # -- inputs ------------------------------------------------------------

    def observe_audio(self, window: AudioWindow) -> Verdict:
        self._windows.append(window)
        self._audio_seconds += 1.0
        speechy = window.speech_likeness >= 0.45

        if window.is_silent:
            self._silence_run += 1.0
            if self._speech_run:
                self._last_burst_length = self._speech_run
            self._speech_run = 0.0
        elif speechy:
            self._speech_run += 1.0
            self._silence_run = 0.0
        else:
            # Audible but not speech-like: hold music, ringback, line noise.
            self._music_seconds += 1.0
            if self._speech_run:
                self._last_burst_length = self._speech_run
            self._speech_run = 0.0
            self._silence_run = 0.0

        return self.evaluate()

    def observe_text(self, text: str) -> Verdict:
        text = text.strip()
        if not text:
            return self.evaluate()

        now = time.time()
        human, _ = _match_score(text, LIVE_AGENT_PATTERNS)
        hold, _ = _match_score(text, HOLD_PATTERNS)
        menu, _ = _match_score(text, MENU_PATTERNS)

        # Decay whatever we thought a moment ago, then fold in the new line.
        age = now - self._text_at if self._text_at else 999.0
        decay = 0.5 ** (age / 12.0)
        self._text_human = max(human, self._text_human * decay)
        self._text_hold = max(hold, self._text_hold * decay)
        self._text_menu = max(menu, self._text_menu * decay)
        self._text_at = now

        self._repeated = self._note_repetition(now, text)
        return self.evaluate()

    def _note_repetition(self, now: float, text: str) -> bool:
        """True when this line already went by earlier -- i.e. a hold loop."""
        key = normalise(text)
        if len(key) < 25:
            return False
        for previous in self._utterances:
            if now - previous.at < 15.0:
                continue
            if _similar(key, previous.text):
                return True
        self._utterances.append(_Utterance(now, key))
        if len(self._utterances) > 60:
            self._utterances.pop(0)
        return False

    # -- output ------------------------------------------------------------

    def evaluate(self) -> Verdict:
        reasons: list[str] = []

        audio_speech = 0.0
        if self._windows:
            audio_speech = sum(w.speech_likeness for w in self._windows) / len(self._windows)

        # The strongest signal available without transcription: a short spoken
        # turn that stops and leaves the line quiet, as though awaiting a reply.
        expectant = 0.0
        if (
            self.MIN_HUMAN_BURST <= self._last_burst_length <= self.MAX_HUMAN_BURST
            and self._silence_run >= self.EXPECTANT_SILENCE
        ):
            expectant = 1.0
            reasons.append(
                f"spoke for {self._last_burst_length:.0f}s then waited "
                f"{self._silence_run:.0f}s"
            )
        elif self._last_burst_length > self.MAX_HUMAN_BURST:
            reasons.append(f"{self._last_burst_length:.0f}s monologue (recorded)")

        if self.has_asr and self._text_at:
            score = (
                0.55 * self._text_human
                + 0.25 * audio_speech
                + 0.25 * expectant
                - 0.70 * self._text_hold
                - 0.45 * self._text_menu
                - (0.50 if self._repeated else 0.0)
            )
            if self._text_human > 0.4:
                reasons.append(f"agent-like phrasing ({self._text_human:.2f})")
            if self._text_hold > 0.4:
                reasons.append(f"hold-queue phrasing ({self._text_hold:.2f})")
            if self._text_menu > 0.4:
                reasons.append(f"menu phrasing ({self._text_menu:.2f})")
            if self._repeated:
                reasons.append("announcement repeated -- hold loop")
        else:
            # Audio only. Lean almost entirely on burst-then-silence, because
            # loudness alone cannot tell a person from a recording.
            score = 0.72 * expectant + 0.38 * audio_speech - 0.25 * _music_share(
                self._music_seconds, self._audio_seconds
            )
            if audio_speech > 0.5:
                reasons.append(f"speech-like audio ({audio_speech:.2f})")

        score = max(0.0, min(1.0, score))
        human = (not self.fired) and score >= self.threshold

        on_hold = self._latched_hold or self._looks_like_hold()
        if on_hold:
            self._latched_hold = True

        return Verdict(
            human=human,
            score=round(score, 3),
            on_hold=on_hold and not human,
            in_menu=self._text_menu > 0.5,
            reasons=reasons,
        )

    def _looks_like_hold(self) -> bool:
        if self._text_hold > 0.6 or self._repeated:
            return True
        # Six seconds of continuous non-speech audio is hold music, not a menu.
        return _music_share(self._music_seconds, self._audio_seconds) > 0.6 and (
            self._music_seconds >= 6.0
        )

    def mark_fired(self) -> None:
        self.fired = True


def _music_share(music_seconds: float, total_seconds: float) -> float:
    if total_seconds <= 0:
        return 0.0
    return min(1.0, music_seconds / total_seconds)


def _similar(a: str, b: str) -> bool:
    """Cheap bag-of-words overlap; good enough to spot a repeating loop."""
    aw, bw = set(a.split()), set(b.split())
    if not aw or not bw:
        return False
    overlap = len(aw & bw) / max(len(aw), len(bw))
    return overlap >= 0.75
