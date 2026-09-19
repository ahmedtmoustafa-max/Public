"""Core data types: call lifecycle, agent actions, transcript entries."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class CallPhase(str, Enum):
    """Where a call is in its life."""

    CREATED = "created"          # queued with Twilio, not answered yet
    NAVIGATING = "navigating"    # working through the phone tree
    ON_HOLD = "on_hold"          # in the queue, waiting for a human
    HUMAN_DETECTED = "human_detected"
    BRIDGING = "bridging"        # ringing you, agent is holding the line
    BRIDGED = "bridged"          # you and the human are talking
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_PHASES = {CallPhase.COMPLETED, CallPhase.FAILED, CallPhase.CANCELLED}


class ActionType(str, Enum):
    DTMF = "dtmf"          # press keypad digits
    SPEAK = "speak"        # say something (IVRs that want speech input)
    WAIT = "wait"          # keep listening, nothing to do yet
    ON_HOLD = "on_hold"    # we've reached the queue; switch to hold-watching
    HUMAN = "human"        # a live person is on the line; bridge now
    HANG_UP = "hang_up"    # dead end; give up


class Action(BaseModel):
    """One decision about what the agent should do next on the line."""

    type: ActionType
    digits: str = ""       # for DTMF, e.g. "2" or "w1ww3" ('w' = 0.5s pause)
    text: str = ""         # for SPEAK
    reason: str = ""       # short rationale, shown in the call log
    confidence: float = 0.5

    def describe(self) -> str:
        if self.type is ActionType.DTMF:
            return f"press {self.digits}"
        if self.type is ActionType.SPEAK:
            return f'say "{self.text}"'
        return self.type.value


class CallMode(str, Enum):
    #: Play a known key sequence, then watch the hold queue. No AI, no ASR.
    SCRIPT = "script"
    #: Let Claude listen to the menu and choose keys. Needs ASR + Anthropic.
    AUTO = "auto"
    #: Don't touch the menu at all -- just sit on the line and watch for a
    #: human. Useful when you've already been transferred.
    WATCH = "watch"


class CallRequest(BaseModel):
    """What you hand the API when you want a call placed."""

    to: str = Field(description="E.164 destination, e.g. +18005551212")
    goal: str = Field(
        default="Reach a live human agent.",
        description="Plain-English objective, used by the auto navigator.",
    )
    mode: CallMode = CallMode.AUTO
    keys: str = Field(
        default="",
        description="For SCRIPT mode: digits to send, 'w' for a 0.5s pause, "
        "',' to separate steps spaced a few seconds apart. e.g. '1,w3,0'",
    )
    label: str = Field(default="", description="Friendly name, e.g. 'Air Canada'")
    playbook: str = Field(default="", description="Playbook id to start from")
    callback_number: str = Field(
        default="", description="Override the number we ring; defaults to MY_PHONE_NUMBER"
    )
    max_seconds: Optional[int] = None


@dataclass
class TranscriptEntry:
    at: float
    speaker: Literal["them", "agent", "system"]
    text: str

    def as_dict(self) -> dict:
        return {"at": self.at, "speaker": self.speaker, "text": self.text}


@dataclass
class CallState:
    """Everything we know about one in-flight call."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    request: Optional[CallRequest] = None
    phase: CallPhase = CallPhase.CREATED
    call_sid: str = ""
    bridge_sid: str = ""
    conference_name: str = ""
    started_at: float = field(default_factory=time.time)
    answered_at: float = 0.0
    hold_started_at: float = 0.0
    human_at: float = 0.0
    ended_at: float = 0.0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    pending_action: Optional[Action] = None
    keys_remaining: list[str] = field(default_factory=list)
    error: str = ""
    notified: bool = False

    @property
    def label(self) -> str:
        if self.request and self.request.label:
            return self.request.label
        return self.request.to if self.request else self.id

    @property
    def elapsed(self) -> float:
        end = self.ended_at or time.time()
        return end - self.started_at

    @property
    def hold_seconds(self) -> float:
        if not self.hold_started_at:
            return 0.0
        end = self.human_at or self.ended_at or time.time()
        return max(0.0, end - self.hold_started_at)

    def log(self, speaker: Literal["them", "agent", "system"], text: str) -> None:
        text = text.strip()
        if text:
            self.transcript.append(TranscriptEntry(time.time(), speaker, text))

    def summary(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "to": self.request.to if self.request else "",
            "goal": self.request.goal if self.request else "",
            "mode": self.request.mode.value if self.request else "",
            "phase": self.phase.value,
            "call_sid": self.call_sid,
            "elapsed_seconds": round(self.elapsed, 1),
            "hold_seconds": round(self.hold_seconds, 1),
            "actions": [
                {"type": a.type.value, "detail": a.describe(), "reason": a.reason}
                for a in self.actions
            ],
            "transcript": [e.as_dict() for e in self.transcript],
            "error": self.error,
        }
