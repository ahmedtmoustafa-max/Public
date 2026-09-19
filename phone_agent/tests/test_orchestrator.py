"""The call state machine, with Twilio and Claude replaced by fakes."""

from __future__ import annotations

import time

import pytest

from fakes import FakeBrain, FakeNotifier, FakeTelephony
from phone_agent.detector import Verdict
from phone_agent.models import (
    Action,
    ActionType,
    CallMode,
    CallPhase,
    CallRequest,
    CallState,
)
from phone_agent.orchestrator import CallSession
from phone_agent.playbooks import PlaybookStore
from phone_agent.settings import Settings
from phone_agent.store import CallStore


@pytest.fixture
async def build(tmp_path):
    built: list[CallSession] = []

    def _build(request: CallRequest, *, brain=None, has_asr=False, **overrides):
        settings = Settings(
            public_base_url="https://agent.test",
            my_phone_number="+15551110000",
            database_path=str(tmp_path / "calls.db"),
            validate_twilio_signatures=False,
            **overrides,
        )
        state = CallState(request=request)
        state.conference_name = f"pa-{state.id}"
        state.call_sid = "CA-target"
        telephony = FakeTelephony()
        notifier = FakeNotifier()
        session = CallSession(
            state,
            settings=settings,
            telephony=telephony,
            notifier=notifier,
            brain=brain or FakeBrain(),
            store=CallStore(str(tmp_path / "calls.db")),
            playbooks=PlaybookStore(tmp_path / "playbooks"),
            has_asr=has_asr,
        )
        built.append(session)
        return session, telephony, notifier

    yield _build
    for session in built:  # let fire-and-forget tasks finish before teardown
        await session.drain()


# --- scripted mode ---------------------------------------------------------


async def test_script_mode_parses_and_sends_keys_in_order(build):
    session, telephony, _ = build(
        CallRequest(to="+18005551212", mode=CallMode.SCRIPT, keys="1,w3,0")
    )
    assert session.state.keys_remaining == ["1", "w3", "0"]
    session.answered()
    assert session.state.phase is CallPhase.NAVIGATING

    for expected in ["1", "w3", "0"]:
        session._last_key_at = time.time() - 20  # pretend the gap has elapsed
        session._quiet_since = time.time() - 5   # and the prompt has finished
        await session._advance_script()
        assert session.state.pending_action.digits == expected
        session.state.pending_action = None

    await session._advance_script()
    assert session.state.phase is CallPhase.ON_HOLD


async def test_script_mode_waits_for_the_minimum_gap(build):
    session, _, _ = build(
        CallRequest(to="+18005551212", mode=CallMode.SCRIPT, keys="1,2")
    )
    session.answered()
    session._last_key_at = time.time()  # just pressed something
    session._quiet_since = time.time() - 5
    await session._advance_script()
    assert session.state.pending_action is None
    assert session.state.keys_remaining == ["1", "2"]


async def test_watch_mode_goes_straight_to_hold_and_presses_nothing(build):
    session, telephony, _ = build(
        CallRequest(to="+18005551212", mode=CallMode.WATCH)
    )
    session.answered()
    assert session.state.phase is CallPhase.ON_HOLD
    await session._maybe_act()
    assert session.state.pending_action is None


# --- auto mode -------------------------------------------------------------


async def test_auto_mode_asks_the_brain_once_the_prompt_settles(build):
    brain = FakeBrain([Action(type=ActionType.DTMF, digits="2", reason="agent")])
    session, _, _ = build(
        CallRequest(to="+18005551212", mode=CallMode.AUTO),
        brain=brain,
        has_asr=True,
    )
    session.answered()
    await session.on_text("For reservations, press two.", True)

    session._buffer_at = time.time() - 5  # prompt has stopped changing
    await session._advance_auto()
    await session.drain()  # the model is consulted off the audio lock
    assert brain.prompts == ["For reservations, press two."]
    assert session.state.pending_action.digits == "2"


async def test_auto_mode_does_not_ask_while_the_prompt_is_still_arriving(build):
    brain = FakeBrain()
    session, _, _ = build(
        CallRequest(to="+18005551212", mode=CallMode.AUTO), brain=brain, has_asr=True
    )
    session.answered()
    await session.on_text("For flight status,", True)
    await session._advance_auto()
    await session.drain()
    assert brain.prompts == []


async def test_auto_mode_nudges_with_zero_after_a_long_silence(build):
    session, _, _ = build(
        CallRequest(to="+18005551212", mode=CallMode.AUTO),
        brain=FakeBrain(),
        has_asr=True,
    )
    session.answered()
    session._last_speech_at = time.time() - 30
    await session._advance_auto()
    assert session.state.pending_action.digits == "0"

    # ...but only once.
    session.state.pending_action = None
    session._last_decision_at = 0
    await session._advance_auto()
    assert session.state.pending_action is None


# --- the handoff -----------------------------------------------------------


async def test_human_detection_rings_you_and_notifies(build):
    session, telephony, notifier = build(
        CallRequest(to="+18005551212", mode=CallMode.SCRIPT, keys="1,0", label="Clinic")
    )
    session.answered()
    session._enter_hold("queue")
    session._keys_sent = ["1", "0"]

    await session._human_detected(["agent-like phrasing"], 0.81)
    await session.drain()  # _ring_caller and the notification run as tasks

    assert session.state.phase is CallPhase.BRIDGING
    assert session.state.pending_action.type is ActionType.SPEAK
    assert telephony.dialled == [
        (session.state.id, "+15551110000", session.state.conference_name)
    ]
    assert notifier.human and notifier.human[0][0] == "Clinic"


async def test_human_detection_saves_a_playbook(build, tmp_path):
    session, _, _ = build(
        CallRequest(
            to="+18005551212", mode=CallMode.AUTO, keys="", label="Air Test"
        )
    )
    session.answered()
    session._keys_sent = ["1", "w3", "0"]
    await session._human_detected(["heard a person"], 0.9)
    await session.drain()

    books = session.playbooks.all()
    assert len(books) == 1
    assert books[0].keys == "1,w3,0"
    assert books[0].learned is True


async def test_human_detection_is_idempotent(build):
    session, telephony, notifier = build(
        CallRequest(to="+18005551212", mode=CallMode.WATCH)
    )
    session.answered()
    await session._human_detected(["first"], 0.9)
    await session._human_detected(["second"], 0.9)
    await session.drain()
    assert len(telephony.dialled) == 1
    assert len(notifier.human) == 1


async def test_verdicts_are_ignored_once_bridging(build):
    session, telephony, _ = build(CallRequest(to="+18005551212", mode=CallMode.WATCH))
    session.answered()
    session.state.phase = CallPhase.BRIDGED
    await session._apply_verdict(Verdict(human=True, score=1.0))
    await session.drain()
    assert telephony.dialled == []


# --- TwiML the webhooks will serve ----------------------------------------


async def test_next_twiml_idles_while_navigating(build):
    session, _, _ = build(CallRequest(to="+18005551212", mode=CallMode.WATCH))
    session.state.phase = CallPhase.NAVIGATING
    document = session.next_twiml()
    assert "<Pause length=\"2\"" in document
    assert "/twiml/next?call_id=" in document


async def test_next_twiml_idles_longer_on_hold(build):
    session, _, _ = build(CallRequest(to="+18005551212", mode=CallMode.WATCH))
    session.answered()
    document = session.next_twiml()
    assert "<Pause length=\"15\"" in document


async def test_next_twiml_plays_a_queued_keypress_once(build):
    session, _, _ = build(CallRequest(to="+18005551212", mode=CallMode.WATCH))
    session.state.phase = CallPhase.NAVIGATING
    session.state.pending_action = Action(type=ActionType.DTMF, digits="w1")
    assert '<Play digits="w1"' in session.next_twiml()
    assert "<Play" not in session.next_twiml()  # consumed


async def test_next_twiml_joins_the_conference_and_stops_listening(build):
    session, _, _ = build(CallRequest(to="+18005551212", mode=CallMode.WATCH))
    session.state.phase = CallPhase.BRIDGING
    document = session.next_twiml()
    assert "<Stop><Stream" in document
    assert session.state.conference_name in document


async def test_next_twiml_hangs_up_past_the_time_limit(build):
    session, _, _ = build(
        CallRequest(to="+18005551212", mode=CallMode.WATCH, max_seconds=60)
    )
    session.answered()
    session.state.started_at = time.time() - 120
    assert "<Hangup" in session.next_twiml()
