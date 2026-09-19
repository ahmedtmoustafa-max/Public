"""Thin wrapper over the Twilio REST API plus destination guardrails."""

from __future__ import annotations

import logging
import re

from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .settings import Settings

log = logging.getLogger(__name__)


class DestinationRefused(Exception):
    """Raised when a number is blocked or outside the allowlist."""


def normalise_number(number: str) -> str:
    """Loose E.164 tidy-up: strip formatting, keep a leading +."""
    cleaned = re.sub(r"[^\d+]", "", number or "")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


def check_destination(number: str, settings: Settings) -> str:
    number = normalise_number(number)
    if not number:
        raise DestinationRefused("No destination number given.")

    # Match the whole number, never a suffix: +1 800 555 9999 ends in "999"
    # but is not an emergency line. Emergency numbers are dialled as-is, at
    # most with a country code in front.
    bare = number.lstrip("+")
    candidates = {bare}
    if bare.startswith("1"):
        candidates.add(bare[1:])
    for blocked in settings.blocked_destinations:
        blocked_bare = normalise_number(blocked).lstrip("+")
        if blocked_bare and blocked_bare in candidates:
            raise DestinationRefused(
                f"{number} is on the blocked list (emergency services are never dialled)."
            )

    # Dialling your own phone means the agent waits on hold for you while your
    # phone rings; dialling the Twilio number points the service at itself.
    if settings.my_phone_number and number == normalise_number(settings.my_phone_number):
        raise DestinationRefused(
            f"{number} is your own number (MY_PHONE_NUMBER). The agent calls "
            "somewhere else and then rings you."
        )
    if settings.twilio_from_number and number == normalise_number(
        settings.twilio_from_number
    ):
        raise DestinationRefused(
            f"{number} is this service's own Twilio number."
        )

    if settings.allowed_destinations:
        allowed = {normalise_number(n) for n in settings.allowed_destinations}
        if number not in allowed:
            raise DestinationRefused(
                f"{number} is not in ALLOWED_DESTINATIONS. Add it there first."
            )
    return number


class Telephony:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Client | None = None
        self._validator: RequestValidator | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            if not (self.settings.twilio_account_sid and self.settings.twilio_auth_token):
                raise RuntimeError(
                    "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are not configured."
                )
            self._client = Client(
                self.settings.twilio_account_sid, self.settings.twilio_auth_token
            )
        return self._client

    @property
    def validator(self) -> RequestValidator:
        if self._validator is None:
            self._validator = RequestValidator(self.settings.twilio_auth_token)
        return self._validator

    def verify(self, url: str, params: dict, signature: str) -> bool:
        if not self.settings.validate_twilio_signatures:
            return True
        if not signature:
            return False
        return self.validator.validate(url, params, signature)

    # -- outbound ----------------------------------------------------------

    def originate(self, call_id: str, to: str, timeout_seconds: int) -> str:
        """Place the call. Returns the Twilio call SID."""
        to = check_destination(to, self.settings)
        call = self.client.calls.create(
            to=to,
            from_=self.settings.twilio_from_number,
            url=self.settings.url(f"/twiml/start?call_id={call_id}"),
            method="POST",
            status_callback=self.settings.url(f"/twilio/status?call_id={call_id}"),
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            time_limit=timeout_seconds,
            machine_detection="Enable",
            async_amd=True,
            async_amd_status_callback=self.settings.url(f"/twilio/amd?call_id={call_id}"),
            async_amd_status_callback_method="POST",
        )
        log.info("call %s -> %s sid=%s", call_id, to, call.sid)
        return call.sid

    def dial_user(self, call_id: str, to: str, conference_name: str) -> str:
        """Ring you and drop you into the conference with the human."""
        call = self.client.calls.create(
            to=normalise_number(to),
            from_=self.settings.twilio_from_number,
            url=self.settings.url(f"/twiml/bridge?call_id={call_id}"),
            method="POST",
            status_callback=self.settings.url(f"/twilio/bridge-status?call_id={call_id}"),
            status_callback_event=["answered", "completed"],
            status_callback_method="POST",
            timeout=45,
        )
        log.info("bridging %s: ringing %s sid=%s", call_id, to, call.sid)
        return call.sid

    def interrupt(self, call_sid: str, url: str) -> bool:
        """Cut the current TwiML short and run a new document instead.

        This is how the agent presses a key or speaks: the call is sitting in a
        `<Pause>`, and redirecting it jumps straight to the new instructions.
        """
        if not call_sid:
            return False
        try:
            self.client.calls(call_sid).update(url=url, method="POST")
            return True
        except TwilioRestException as exc:
            # 21220 just means the call already ended -- not worth shouting about.
            if exc.code == 21220:
                log.debug("interrupt on finished call %s", call_sid)
            else:
                log.warning("interrupt failed for %s: %s", call_sid, exc)
            return False

    def hangup(self, call_sid: str) -> None:
        if not call_sid:
            return
        try:
            self.client.calls(call_sid).update(status="completed")
        except TwilioRestException as exc:
            if exc.code != 21220:
                log.warning("hangup failed for %s: %s", call_sid, exc)

    def send_sms(self, to: str, body: str) -> None:
        self.client.messages.create(
            to=normalise_number(to), from_=self.settings.twilio_from_number, body=body
        )
