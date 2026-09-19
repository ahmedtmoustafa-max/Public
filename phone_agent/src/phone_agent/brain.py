"""Claude decides which key to press next.

Only used in ``auto`` mode. The model sees the menu as transcribed, the goal
you gave, and what has already been tried; it returns one structured action.
It is deliberately kept on a short leash -- it can press keys, say a short
phrase, wait, or declare that we've reached a queue or a person. It cannot
invent information about you: anything it is permitted to disclose has to be
listed explicitly in the profile.
"""

from __future__ import annotations

import logging
import re

from anthropic import AsyncAnthropic

from .models import Action, ActionType, CallState
from .settings import Settings

log = logging.getLogger(__name__)

ACTION_TOOL = {
    "name": "choose_action",
    "description": "Choose the single next thing to do on the phone line.",
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["dtmf", "speak", "wait", "on_hold", "human", "hang_up"],
                "description": (
                    "dtmf: press keypad digits. speak: say a short phrase to a "
                    "speech-driven menu. wait: the prompt is still playing or "
                    "incomplete. on_hold: we have reached a hold queue and "
                    "should stop navigating. human: a live person is speaking "
                    "to us right now. hang_up: dead end, nothing more to try."
                ),
            },
            "digits": {
                "type": "string",
                "description": (
                    "Digits for dtmf, e.g. '2' or '0'. Use 'w' for a half-second "
                    "pause between tones and '#'/'*' as needed."
                ),
            },
            "text": {
                "type": "string",
                "description": "Short phrase for speak, e.g. 'representative'.",
            },
            "reason": {
                "type": "string",
                "description": "One short sentence of justification for the log.",
            },
            "confidence": {
                "type": "number",
                "description": "0..1 confidence in this choice.",
            },
        },
        "required": ["type", "reason"],
    },
}

SYSTEM_PROMPT = """\
You are navigating an automated phone menu on behalf of a person who is not \
on the line yet. Your only job is to get the call to a live human being who \
can help with their goal, then stop.

Rules you must follow:
- Prefer the option that reaches a person: "speak to a representative", \
"agent", "operator", "customer service". Pressing 0 often works; so does \
saying "representative" or "agent" on speech-driven menus.
- If the prompt is still mid-sentence or you only have a fragment, choose \
"wait". Guessing at a half-heard menu wastes a whole cycle.
- If you hear hold music, "all our agents are busy", or an estimated wait \
time, choose "on_hold". Stop pressing keys once you are in the queue.
- If someone is clearly talking TO you and waiting for an answer, choose \
"human".
- Never select options that spend money, cancel or change a booking, accept \
terms, or make any commitment. Those are the caller's to make, not yours.
- Never provide personal information unless it appears in the DISCLOSURE \
section below. If a menu demands information you do not have, choose "wait" \
or "human" so the caller can take over -- do not invent it.
- Never choose the callback/"we'll call you back" option unless the goal \
explicitly asks for it, because the caller is not on this line to take it.
- Do not repeat an action that has already been tried and did not help.
"""


class Brain:
    def __init__(self, settings: Settings, profile: dict | None = None):
        self.settings = settings
        self.profile = profile or {}
        self._client: AsyncAnthropic | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    @property
    def client(self) -> AsyncAnthropic:
        if self._client is None:
            self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def _disclosure_block(self) -> str:
        allowed = self.profile.get("may_disclose") or {}
        if not allowed:
            return (
                "DISCLOSURE: You have no information about the caller. If the "
                "menu asks for any personal detail, do not answer it."
            )
        lines = [f"- {key}: {value}" for key, value in allowed.items()]
        return (
            "DISCLOSURE: You may provide ONLY these details if a menu asks for "
            "them, and nothing else:\n" + "\n".join(lines)
        )

    def _context(self, state: CallState, heard: str) -> str:
        request = state.request
        tried = "\n".join(
            f"- {a.describe()} ({a.reason})" for a in state.actions[-8:]
        ) or "- nothing yet"
        return (
            f"CALLING: {state.label} ({request.to if request else 'unknown'})\n"
            f"GOAL: {request.goal if request else 'Reach a live human agent.'}\n"
            f"TIME ON CALL: {state.elapsed:.0f} seconds\n\n"
            f"{self._disclosure_block()}\n\n"
            f"ALREADY TRIED:\n{tried}\n\n"
            f"WHAT THE LINE IS SAYING RIGHT NOW (transcribed, may contain errors):\n"
            f'"""{heard}"""\n\n'
            "Choose the next action."
        )

    async def decide(self, state: CallState, heard: str) -> Action:
        if not self.available:
            return Action(type=ActionType.WAIT, reason="No Anthropic API key configured.")
        try:
            message = await self.client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=[ACTION_TOOL],
                tool_choice={"type": "tool", "name": "choose_action"},
                messages=[{"role": "user", "content": self._context(state, heard)}],
            )
        except Exception as exc:  # noqa: BLE001 - never let the model kill a call
            log.warning("brain call failed: %s", exc)
            return Action(type=ActionType.WAIT, reason=f"Model unavailable: {exc}")

        for block in message.content:
            if getattr(block, "type", None) == "tool_use":
                return _parse_action(block.input)
        return Action(type=ActionType.WAIT, reason="Model returned no action.")


def _parse_action(payload: dict) -> Action:
    try:
        action_type = ActionType(str(payload.get("type", "wait")).lower())
    except ValueError:
        action_type = ActionType.WAIT

    digits = re.sub(r"[^0-9*#w]", "", str(payload.get("digits") or ""))
    action = Action(
        type=action_type,
        digits=digits,
        text=str(payload.get("text") or "")[:200],
        reason=str(payload.get("reason") or "")[:300],
        confidence=float(payload.get("confidence") or 0.5),
    )
    # A DTMF action with no usable digits is just a wait.
    if action.type is ActionType.DTMF and not action.digits:
        return Action(type=ActionType.WAIT, reason="Model asked for DTMF but gave no digits.")
    if action.type is ActionType.SPEAK and not action.text:
        return Action(type=ActionType.WAIT, reason="Model asked to speak but gave no text.")
    return action

