"""Parsing and guarding of the model's chosen action."""

from __future__ import annotations

import pytest

from phone_agent.brain import Brain, _parse_action
from phone_agent.models import ActionType, CallRequest, CallState
from phone_agent.settings import Settings


def test_parses_a_keypress():
    action = _parse_action({"type": "dtmf", "digits": "2", "reason": "reservations"})
    assert action.type is ActionType.DTMF
    assert action.digits == "2"
    assert action.describe() == "press 2"


def test_strips_anything_that_is_not_a_dialable_character():
    action = _parse_action({"type": "dtmf", "digits": "press 1 then #", "reason": "x"})
    assert action.digits == "1#"


def test_dtmf_without_usable_digits_degrades_to_wait():
    action = _parse_action({"type": "dtmf", "digits": "abc", "reason": "x"})
    assert action.type is ActionType.WAIT


def test_speak_without_text_degrades_to_wait():
    assert _parse_action({"type": "speak", "text": "", "reason": "x"}).type is ActionType.WAIT


def test_unknown_action_type_degrades_to_wait():
    assert _parse_action({"type": "teleport", "reason": "x"}).type is ActionType.WAIT


@pytest.mark.parametrize("value", ["on_hold", "human", "hang_up", "wait"])
def test_control_actions_round_trip(value):
    assert _parse_action({"type": value, "reason": "x"}).type.value == value


def test_long_text_is_truncated():
    action = _parse_action({"type": "speak", "text": "x" * 500, "reason": "y" * 500})
    assert len(action.text) == 200
    assert len(action.reason) == 300


# --- prompt construction ---------------------------------------------------


def _state() -> CallState:
    return CallState(
        request=CallRequest(to="+18005551212", goal="reschedule surgery follow-up",
                            label="Clinic")
    )


def test_prompt_refuses_disclosure_when_no_profile_is_set():
    brain = Brain(Settings(), profile={})
    context = brain._context(_state(), "Please say your date of birth.")
    assert "do not answer it" in context


def test_prompt_lists_only_permitted_facts():
    brain = Brain(Settings(), profile={"may_disclose": {"first_name": "Ahmed"}})
    context = brain._context(_state(), "Who is calling?")
    assert "first_name: Ahmed" in context
    assert "ONLY these details" in context


def test_prompt_carries_the_goal_and_what_was_already_tried():
    brain = Brain(Settings())
    state = _state()
    state.actions.append(_parse_action({"type": "dtmf", "digits": "1", "reason": "English"}))
    context = brain._context(state, "Main menu.")
    assert "reschedule surgery follow-up" in context
    assert "press 1 (English)" in context


async def test_missing_api_key_yields_a_wait_rather_than_an_error():
    brain = Brain(Settings(anthropic_api_key=""))
    action = await brain.decide(_state(), "For agents, press zero.")
    assert action.type is ActionType.WAIT
    assert not brain.available
