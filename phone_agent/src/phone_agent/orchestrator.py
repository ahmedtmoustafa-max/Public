"""The per-call state machine.

One :class:`CallSession` per live call. It consumes audio windows and
transcript fragments, decides what the agent should do, and owns the
transition from "working the menu" to "sitting in the queue" to "a person is
here, go get the caller".

The TwiML webhooks never make decisions of their own -- they ask the session
what the next document should be. That keeps all the logic in one place and
makes it testable without Twilio in the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import twiml
from .brain import Brain
from .detector import HumanDetector
from .media import AudioAnalyzer
from .models import (
    TERMINAL_PHASES,
    Action,
    ActionType,
    CallMode,
    CallPhase,
    CallState,
)
from .notify import Notifier
from .playbooks import PlaybookStore, parse_key_script
from .settings import Settings
from .store import CallStore
from .telephony import Telephony

log = logging.getLogger(__name__)

#: How long a menu prompt must stop changing before we ask Claude about it.
PROMPT_SETTLE_SECONDS = 1.3
#: Never ask the model more than this often -- menus are slower than we are.
MIN_DECISION_INTERVAL = 2.5
#: In script mode, the shortest and longest we'll wait between keypresses.
MIN_KEY_GAP = 2.5
MAX_KEY_GAP = 11.0
#: If a menu says nothing at all for this long, try the universal escape hatch.
SILENCE_NUDGE_SECONDS = 12.0


class CallSession:
    def __init__(
        self,
        state: CallState,
        *,
        settings: Settings,
        telephony: Telephony,
        notifier: Notifier,
        brain: Brain,
        store: CallStore,
        playbooks: PlaybookStore,
        has_asr: bool,
    ):
        self.state = state
        self.settings = settings
        self.telephony = telephony
        self.notifier = notifier
        self.brain = brain
        self.store = store
        self.playbooks = playbooks

        self.analyzer = AudioAnalyzer()
        self.detector = HumanDetector(
            strictness=settings.detector_strictness, has_asr=has_asr
        )
        self.has_asr = has_asr

        self._lock = asyncio.Lock()
        self._buffer: list[str] = []
        self._buffer_at = 0.0
        self._last_decision_at = 0.0
        self._last_key_at = 0.0
        self._last_speech_at = time.time()
        self._quiet_since = 0.0
        self._nudged = False
        self._deciding = False
        self._keys_sent: list[str] = []
        self._closing = False
        #: asyncio only holds weak references to tasks, so a fire-and-forget
        #: task can be collected mid-flight. Keep them until they finish.
        self._tasks: set[asyncio.Task] = set()

        if state.request and state.request.mode is CallMode.SCRIPT:
            state.keys_remaining = parse_key_script(state.request.keys)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for outstanding background work. Used on shutdown and in tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- lifecycle ---------------------------------------------------------

    def answered(self) -> None:
        if self.state.answered_at:
            return
        self.state.answered_at = time.time()
        if self.state.request and self.state.request.mode is CallMode.WATCH:
            self._enter_hold("watch mode -- listening only")
        else:
            self.state.phase = CallPhase.NAVIGATING
        self.state.log("system", f"Answered by {self.state.label}.")

    async def finish(self, phase: CallPhase, reason: str = "") -> None:
        if self.state.phase in TERMINAL_PHASES:
            return
        self._closing = True
        self.state.phase = phase
        self.state.ended_at = time.time()
        if reason:
            self.state.error = reason if phase is CallPhase.FAILED else ""
            self.state.log("system", reason)
        self.store.persist(self.state)
        log.info("call %s finished: %s (%s)", self.state.id, phase.value, reason)

    async def cancel(self) -> None:
        await asyncio.to_thread(self.telephony.hangup, self.state.call_sid)
        await self.finish(CallPhase.CANCELLED, "Cancelled by request.")

    # -- inputs ------------------------------------------------------------

    async def on_audio(self, payload: bytes) -> None:
        from .media import ulaw_to_pcm

        window = self.analyzer.push(ulaw_to_pcm(payload))
        if window is None:
            return
        async with self._lock:
            if not window.is_silent:
                self._last_speech_at = time.time()
                self._quiet_since = 0.0
            elif not self._quiet_since:
                self._quiet_since = time.time()

            verdict = self.detector.observe_audio(window)
            await self._apply_verdict(verdict)
            await self._maybe_act()

    async def on_text(self, text: str, is_final: bool) -> None:
        if not is_final:
            return
        async with self._lock:
            self.state.log("them", text)
            self._buffer.append(text)
            self._buffer_at = time.time()
            verdict = self.detector.observe_text(text)
            await self._apply_verdict(verdict)

    # -- decisions ---------------------------------------------------------

    async def _apply_verdict(self, verdict) -> None:
        if self.state.phase in TERMINAL_PHASES or self.state.phase in (
            CallPhase.HUMAN_DETECTED,
            CallPhase.BRIDGING,
            CallPhase.BRIDGED,
        ):
            return

        if verdict.human:
            await self._human_detected(verdict.reasons, verdict.score)
            return

        if verdict.on_hold and self.state.phase is CallPhase.NAVIGATING:
            if not self.state.keys_remaining:
                self._enter_hold("; ".join(verdict.reasons) or "hold queue detected")

    async def _maybe_act(self) -> None:
        if self.state.phase is not CallPhase.NAVIGATING or self._closing:
            return
        if self.state.pending_action is not None:
            return

        request = self.state.request
        if request and request.mode is CallMode.SCRIPT:
            await self._advance_script()
            return
        if request and request.mode is CallMode.AUTO:
            await self._advance_auto()

    async def _advance_script(self) -> None:
        if not self.state.keys_remaining:
            self._enter_hold("key sequence finished")
            return

        now = time.time()
        since_key = now - (self._last_key_at or self.state.answered_at or now)
        if since_key < MIN_KEY_GAP:
            return
        prompt_finished = self._quiet_since and (now - self._quiet_since) >= 1.2
        if not prompt_finished and since_key < MAX_KEY_GAP:
            return

        digits = self.state.keys_remaining.pop(0)
        self._queue(
            Action(
                type=ActionType.DTMF,
                digits=digits,
                reason="scripted step",
                confidence=1.0,
            )
        )

    async def _advance_auto(self) -> None:
        now = time.time()
        if self._deciding or now - self._last_decision_at < MIN_DECISION_INTERVAL:
            return

        heard = " ".join(self._buffer).strip()
        settled = self._buffer_at and (now - self._buffer_at) >= PROMPT_SETTLE_SECONDS

        if heard and settled:
            self._last_decision_at = now
            self._buffer.clear()
            self._deciding = True
            # Off the lock: the model takes a second or two, and audio frames
            # arrive every 20 ms. Blocking here would stall both the detector
            # and the transcription feed behind a network round trip.
            self._spawn(self._decide(heard))
            return

        # Nothing transcribed and nothing happening. Some trees just wait for
        # input; 0 is the near-universal "give me a person" key.
        quiet_for = now - self._last_speech_at
        if (
            not heard
            and not self._nudged
            and quiet_for >= SILENCE_NUDGE_SECONDS
            and self.state.answered_at
        ):
            self._nudged = True
            self._last_decision_at = now
            self._queue(
                Action(
                    type=ActionType.DTMF,
                    digits="0",
                    reason="line went quiet; trying the operator key",
                    confidence=0.4,
                )
            )

    async def _decide(self, heard: str) -> None:
        try:
            action = await self.brain.decide(self.state, heard)
        finally:
            self._deciding = False
        async with self._lock:
            if self.state.phase is CallPhase.NAVIGATING:
                await self._handle_brain_action(action)

    async def _handle_brain_action(self, action: Action) -> None:
        if action.type in (ActionType.DTMF, ActionType.SPEAK):
            self._queue(action)
        elif action.type is ActionType.ON_HOLD:
            self.state.actions.append(action)
            self._enter_hold(action.reason)
        elif action.type is ActionType.HUMAN:
            self.state.actions.append(action)
            await self._human_detected([action.reason or "model heard a person"], 1.0)
        elif action.type is ActionType.HANG_UP:
            self.state.actions.append(action)
            self.state.pending_action = action
            await self._interrupt()
        # WAIT needs no action at all -- the pause loop already does that.

    def _queue(self, action: Action) -> None:
        self.state.pending_action = action
        self.state.actions.append(action)
        if action.type is ActionType.DTMF:
            self._keys_sent.append(action.digits)
            self._last_key_at = time.time()
        self.state.log("agent", action.describe())
        self._spawn(self._interrupt())

    async def _interrupt(self) -> None:
        """Cut the `<Pause>` short so the queued action happens now."""
        url = self.settings.url(f"/twiml/next?call_id={self.state.id}")
        await asyncio.to_thread(self.telephony.interrupt, self.state.call_sid, url)

    def _enter_hold(self, reason: str) -> None:
        if self.state.phase is CallPhase.ON_HOLD:
            return
        self.state.phase = CallPhase.ON_HOLD
        self.state.hold_started_at = time.time()
        self.state.keys_remaining.clear()
        self.state.log("system", f"In the hold queue ({reason}). Watching for a human.")
        log.info("call %s -> on hold (%s)", self.state.id, reason)

    # -- the payoff --------------------------------------------------------

    async def _human_detected(self, reasons: list[str], score: float) -> None:
        if self.state.phase in (
            CallPhase.HUMAN_DETECTED,
            CallPhase.BRIDGING,
            CallPhase.BRIDGED,
        ):
            return

        self.detector.mark_fired()
        self.state.phase = CallPhase.HUMAN_DETECTED
        self.state.human_at = time.time()
        why = "; ".join(r for r in reasons if r) or "live speech detected"
        self.state.log("system", f"Human detected ({score:.2f}): {why}")
        log.info("call %s: human detected (%.2f) %s", self.state.id, score, why)

        # Say something immediately so the agent doesn't hang up on dead air,
        # and so they know what they're talking to.
        self._queue(
            Action(
                type=ActionType.SPEAK,
                text=self.settings.human_greeting,
                reason="holding the line while the caller picks up",
                confidence=score,
            )
        )
        self.state.phase = CallPhase.BRIDGING

        target = (
            self.state.request.callback_number
            if self.state.request and self.state.request.callback_number
            else self.settings.my_phone_number
        )
        if not target:
            self.state.log(
                "system", "No number to ring -- set MY_PHONE_NUMBER or callback_number."
            )
        else:
            self._spawn(self._ring_caller(target))

        self._spawn(
            self.notifier.human_answered(
                self.state.label, self.state.hold_seconds, self.state.id
            )
        )
        self._remember_playbook()

    async def _ring_caller(self, target: str) -> None:
        try:
            sid = await asyncio.to_thread(
                self.telephony.dial_user,
                self.state.id,
                target,
                self.state.conference_name,
            )
            self.state.bridge_sid = sid
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to ring %s", target)
            self.state.log("system", f"Could not ring you: {exc}")

    def _remember_playbook(self) -> None:
        if not self._keys_sent or not self.state.request:
            return
        try:
            self.playbooks.learn(
                label=self.state.label,
                number=self.state.request.to,
                keys=",".join(self._keys_sent),
                goal=self.state.request.goal,
                call_id=self.state.id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save playbook: %s", exc)

    def bridged(self) -> None:
        if self.state.phase is CallPhase.BRIDGING:
            self.state.phase = CallPhase.BRIDGED
            self.state.log("system", "You joined the call. Handing over.")

    # -- what the webhooks ask for -----------------------------------------

    def next_twiml(self) -> str:
        """The TwiML document to serve for ``/twiml/next`` right now."""
        state = self.state
        next_url = self.settings.url(f"/twiml/next?call_id={state.id}")

        if state.phase in TERMINAL_PHASES:
            return twiml.hangup(voice=self.settings.tts_voice)

        if state.elapsed > self._deadline():
            self._spawn(self.finish(CallPhase.COMPLETED, "Hit the time limit."))
            return twiml.hangup(
                "Ending the call, the time limit was reached. Goodbye.",
                voice=self.settings.tts_voice,
            )

        action = state.pending_action
        if action is not None:
            state.pending_action = None
            if action.type is ActionType.DTMF:
                return twiml.press(action.digits, next_url)
            if action.type is ActionType.SPEAK:
                return twiml.speak(action.text, self.settings.tts_voice, next_url)
            if action.type is ActionType.HANG_UP:
                self._spawn(
                    self.finish(CallPhase.COMPLETED, action.reason or "Dead end.")
                )
                return twiml.hangup(voice=self.settings.tts_voice)

        if state.phase in (CallPhase.BRIDGING, CallPhase.BRIDGED, CallPhase.HUMAN_DETECTED):
            # Park the human in the conference and stop listening -- the rest of
            # this conversation is none of our business.
            return twiml.join_conference(
                state.conference_name,
                stop_stream=True,
                status_callback=self.settings.url(
                    f"/twilio/conference?call_id={state.id}"
                ),
            )

        tick = (
            twiml.HOLD_TICK_SECONDS
            if state.phase is CallPhase.ON_HOLD
            else twiml.NAV_TICK_SECONDS
        )
        return twiml.idle(next_url, tick)

    def _deadline(self) -> float:
        request = self.state.request
        if request and request.max_seconds:
            return float(request.max_seconds)
        return float(self.settings.max_call_seconds)
