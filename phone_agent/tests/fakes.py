"""Stand-ins for Twilio, Claude and the notifier, so calls can be tested dry."""

from __future__ import annotations

from phone_agent.models import Action, ActionType


class FakeTelephony:
    def __init__(self):
        self.interrupts: list[tuple[str, str]] = []
        self.dialled: list[tuple[str, str, str]] = []
        self.hangups: list[str] = []
        self.sms: list[tuple[str, str]] = []

    def interrupt(self, call_sid: str, url: str) -> bool:
        self.interrupts.append((call_sid, url))
        return True

    def dial_user(self, call_id: str, to: str, conference: str) -> str:
        self.dialled.append((call_id, to, conference))
        return "CA-bridge"

    def hangup(self, call_sid: str) -> None:
        self.hangups.append(call_sid)

    def send_sms(self, to: str, body: str) -> None:
        self.sms.append((to, body))


class FakeNotifier:
    def __init__(self):
        self.human: list[tuple[str, float, str]] = []
        self.failures: list[tuple[str, str]] = []

    async def human_answered(self, label: str, hold_seconds: float, call_id: str) -> None:
        self.human.append((label, hold_seconds, call_id))

    async def call_failed(self, label: str, reason: str) -> None:
        self.failures.append((label, reason))


class FakeBrain:
    """Returns a queued list of actions, then waits forever."""

    def __init__(self, actions: list[Action] | None = None):
        self.queue = list(actions or [])
        self.prompts: list[str] = []
        self.available = True

    async def decide(self, state, heard: str) -> Action:
        self.prompts.append(heard)
        if self.queue:
            return self.queue.pop(0)
        return Action(type=ActionType.WAIT, reason="nothing queued")
