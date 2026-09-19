"""The hold-queue / live-human classifier."""

from __future__ import annotations

import pytest

from phone_agent.detector import HumanDetector
from phone_agent.media import AudioAnalyzer
from synth import melodic_music, music_like, silence, speech_like

AGENT_GREETING = "Thanks for holding, my name is Priya, how can I help you today?"
HOLD_MESSAGE = (
    "Your call is important to us. All of our agents are currently busy "
    "assisting other customers. Your estimated wait time is twelve minutes."
)
MENU_PROMPT = (
    "For flight status, press one. For existing reservations, press two. "
    "To hear these options again, press nine."
)


def feed(detector: HumanDetector, frames):
    """Push frames through an analyzer and into the detector, as a call would."""
    analyzer = AudioAnalyzer()
    verdict = detector.evaluate()
    for frame in frames:
        window = analyzer.push(frame)
        if window is not None:
            verdict = detector.observe_audio(window)
    return verdict


# --- with transcription ----------------------------------------------------


def test_agent_greeting_is_a_human():
    detector = HumanDetector(strictness=0.55, has_asr=True)
    feed(detector, speech_like(3.0) + silence(2.0))
    verdict = detector.observe_text(AGENT_GREETING)
    assert verdict.human
    assert verdict.score > 0.6


def test_hold_announcement_is_not_a_human():
    detector = HumanDetector(strictness=0.55, has_asr=True)
    feed(detector, speech_like(6.0))
    verdict = detector.observe_text(HOLD_MESSAGE)
    assert not verdict.human
    assert verdict.on_hold


def test_menu_prompt_is_not_a_human_and_is_flagged_as_a_menu():
    detector = HumanDetector(strictness=0.55, has_asr=True)
    feed(detector, speech_like(5.0))
    verdict = detector.observe_text(MENU_PROMPT)
    assert not verdict.human
    assert verdict.in_menu


def test_repeated_announcement_reads_as_a_hold_loop():
    detector = HumanDetector(strictness=0.55, has_asr=True)
    detector.observe_text("Did you know you can manage your booking online at our website")
    feed(detector, melodic_music(20.0))
    verdict = detector.observe_text(
        "Did you know you can manage your booking online at our website"
    )
    assert not verdict.human
    assert verdict.on_hold


def test_recorded_voice_that_sounds_agent_like_is_held_back_by_hold_phrasing():
    """A recording saying 'thank you for holding' must not trip the bridge."""
    detector = HumanDetector(strictness=0.55, has_asr=True)
    feed(detector, speech_like(4.0))
    verdict = detector.observe_text(
        "Thank you for holding. All of our representatives are currently busy. "
        "Please continue to hold and your call will be answered in the order received."
    )
    assert not verdict.human


# --- audio only (no transcription available) ------------------------------


def test_short_burst_then_expectant_silence_reads_as_a_human():
    detector = HumanDetector(strictness=0.55, has_asr=False)
    feed(detector, melodic_music(10.0))
    verdict = feed(detector, speech_like(3.0) + silence(3.0))
    assert verdict.human, verdict
    assert any("waited" in r for r in verdict.reasons)


def test_continuous_hold_music_never_fires():
    detector = HumanDetector(strictness=0.55, has_asr=False)
    verdict = feed(detector, melodic_music(45.0))
    assert not verdict.human
    assert verdict.on_hold


def test_steady_hold_music_never_fires():
    detector = HumanDetector(strictness=0.55, has_asr=False)
    verdict = feed(detector, music_like(45.0))
    assert not verdict.human


def test_long_recorded_announcement_does_not_fire():
    """A 20 second monologue is a recording, however speech-like it sounds."""
    detector = HumanDetector(strictness=0.55, has_asr=False)
    feed(detector, melodic_music(8.0))
    verdict = feed(detector, speech_like(20.0) + silence(3.0))
    assert not verdict.human, verdict


def test_detector_only_fires_once():
    detector = HumanDetector(strictness=0.55, has_asr=False)
    feed(detector, melodic_music(8.0))
    first = feed(detector, speech_like(3.0) + silence(3.0))
    assert first.human
    detector.mark_fired()
    second = feed(detector, speech_like(3.0) + silence(3.0))
    assert not second.human


@pytest.mark.parametrize("strictness,expected", [(0.0, 0.32), (1.0, 0.77)])
def test_strictness_moves_the_threshold(strictness, expected):
    detector = HumanDetector(strictness=strictness)
    assert detector.threshold == pytest.approx(expected)


def test_five_minutes_of_hold_music_stays_quiet():
    """Regression: a drifting silence threshold used to make long stretches of
    hold music read as silence, which then looked like a person pausing."""
    detector = HumanDetector(strictness=0.55, has_asr=False)
    analyzer = AudioAnalyzer()
    fired = []
    for chunk in range(10):  # 10 x 30 s
        for frame in melodic_music(30.0):
            window = analyzer.push(frame)
            if window is None:
                continue
            verdict = detector.observe_audio(window)
            if verdict.human:
                fired.append((chunk, verdict))
    assert not fired, fired[:3]


def test_realistic_sequence_fires_only_at_the_agent():
    """Menu, then a recorded queue message, then music, then a person."""
    detector = HumanDetector(strictness=0.55, has_asr=False)
    analyzer = AudioAnalyzer()
    script = (
        speech_like(8.0)    # menu prompt
        + silence(1.0)
        + speech_like(14.0)  # "all our agents are busy..." recording
        + melodic_music(60.0)
        + speech_like(11.0)  # another recorded announcement
        + melodic_music(40.0)
    )
    fired_early = []
    for frame in script:
        window = analyzer.push(frame)
        if window is not None and detector.observe_audio(window).human:
            fired_early.append(True)
    assert not fired_early, "bridged before a person was on the line"

    verdict = None
    for frame in speech_like(2.5) + silence(2.5):  # "Hi, this is Sam" ... waits
        window = analyzer.push(frame)
        if window is not None:
            verdict = detector.observe_audio(window)
    assert verdict is not None and verdict.human
