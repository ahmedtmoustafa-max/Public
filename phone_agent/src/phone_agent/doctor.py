"""Configuration check, so the first real call isn't the first thing you test.

``phone-agent doctor`` runs these. The static checks need no network; passing
``--live`` also asks Twilio whether the credentials work and whether the
outbound number can actually place voice calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .settings import Settings

E164 = re.compile(r"^\+[1-9]\d{7,14}$")

Status = Literal["ok", "warn", "fail", "skip"]
MARKS: dict[str, str] = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "----"}


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


def _number_check(name: str, value: str, env_var: str, required: bool) -> Check:
    if not value:
        if not required:
            return Check(name, "skip", "not set")
        return Check(name, "fail", "not set", f"Set {env_var} in .env")
    if not E164.match(value):
        return Check(
            name,
            "fail",
            f"{value} is not E.164",
            f"Write it as +<country><number>, e.g. +15195551234 (no spaces or dashes)",
        )
    return Check(name, "ok", value)


def static_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []

    checks.append(
        _number_check("Your phone", settings.my_phone_number, "MY_PHONE_NUMBER", True)
    )
    checks.append(
        _number_check(
            "Outbound number", settings.twilio_from_number, "TWILIO_FROM_NUMBER", True
        )
    )

    if settings.my_phone_number and (
        settings.my_phone_number == settings.twilio_from_number
    ):
        checks.append(
            Check(
                "Numbers differ",
                "fail",
                "your phone and the Twilio number are the same",
                "The agent dials out from one and rings you on the other.",
            )
        )

    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        checks.append(
            Check(
                "Twilio credentials",
                "fail",
                "missing",
                "Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN from console.twilio.com",
            )
        )
    elif not settings.twilio_account_sid.startswith("AC"):
        checks.append(
            Check(
                "Twilio credentials",
                "fail",
                "account SID should start with 'AC'",
                "Copy the Account SID, not an API key SID.",
            )
        )
    else:
        checks.append(Check("Twilio credentials", "ok", "present"))

    url = settings.public_base_url
    if not url or "localhost" in url or "127.0.0.1" in url:
        checks.append(
            Check(
                "Public address",
                "fail",
                f"{url or 'not set'} is not reachable from Twilio",
                "Run `cloudflared tunnel --url http://localhost:8080` and put the "
                "https URL in PUBLIC_BASE_URL, or deploy the service.",
            )
        )
    elif not url.startswith("https://"):
        checks.append(
            Check("Public address", "fail", f"{url} is not HTTPS", "Twilio requires HTTPS.")
        )
    else:
        checks.append(Check("Public address", "ok", url))

    if not settings.validate_twilio_signatures:
        checks.append(
            Check(
                "Webhook signatures",
                "warn",
                "validation is off",
                "Set VALIDATE_TWILIO_SIGNATURES=true before real use -- these URLs "
                "can place and steer calls.",
            )
        )
    else:
        checks.append(Check("Webhook signatures", "ok", "validated"))

    auto_ready = bool(settings.anthropic_api_key) and (
        settings.asr_provider == "deepgram" and bool(settings.deepgram_api_key)
    )
    if auto_ready:
        checks.append(Check("Auto mode", "ok", f"Claude + {settings.asr_provider}"))
    else:
        missing = []
        if not settings.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if settings.asr_provider != "deepgram" or not settings.deepgram_api_key:
            missing.append("ASR_PROVIDER=deepgram + DEEPGRAM_API_KEY")
        checks.append(
            Check(
                "Auto mode",
                "skip",
                "unavailable (script and watch modes still work)",
                "Add " + " and ".join(missing) + " to read unfamiliar menus.",
            )
        )

    channels = []
    if settings.notify_ntfy_topic:
        channels.append("ntfy")
    if settings.notify_pushover_token and settings.notify_pushover_user:
        channels.append("Pushover")
    if settings.notify_sms and settings.my_phone_number:
        channels.append("SMS")
    if channels:
        checks.append(Check("Notifications", "ok", ", ".join(channels)))
    else:
        checks.append(
            Check(
                "Notifications",
                "warn",
                "none configured",
                "Your phone will still ring, but set NOTIFY_NTFY_TOPIC for a push "
                "you can see before you pick up.",
            )
        )

    if settings.allowed_destinations:
        checks.append(
            Check(
                "Destination allowlist",
                "ok",
                f"{len(settings.allowed_destinations)} number(s)",
            )
        )
    else:
        checks.append(
            Check(
                "Destination allowlist",
                "warn",
                "any number may be dialled",
                "Fine on your own machine. Set ALLOWED_DESTINATIONS if this is hosted.",
            )
        )

    return checks


def live_checks(settings: Settings) -> list[Check]:
    """Ask Twilio whether the credentials and the outbound number really work."""
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        return [Check("Twilio account", "skip", "no credentials to check")]

    checks: list[Check] = []
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    try:
        account = client.api.accounts(settings.twilio_account_sid).fetch()
        checks.append(Check("Twilio account", "ok", f"{account.friendly_name} ({account.status})"))
    except TwilioRestException as exc:
        return [
            Check(
                "Twilio account",
                "fail",
                str(exc.msg or exc),
                "Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
            )
        ]

    if not settings.twilio_from_number:
        checks.append(Check("Outbound number is yours", "skip", "not set"))
        return checks

    try:
        owned = client.incoming_phone_numbers.list(
            phone_number=settings.twilio_from_number, limit=1
        )
    except TwilioRestException as exc:
        checks.append(Check("Outbound number is yours", "fail", str(exc.msg or exc)))
        return checks

    if not owned:
        checks.append(
            Check(
                "Outbound number is yours",
                "fail",
                f"{settings.twilio_from_number} is not on this account",
                "Buy a voice-capable number in the Twilio console.",
            )
        )
    elif not owned[0].capabilities.get("voice"):
        checks.append(
            Check(
                "Outbound number is yours",
                "fail",
                "that number cannot make voice calls",
                "Buy a number with Voice capability.",
            )
        )
    else:
        checks.append(Check("Outbound number is yours", "ok", "voice-capable"))

    # Trial accounts can only call numbers that have been verified.
    if account.type and account.type.lower() == "trial":
        verified = {
            caller.phone_number
            for caller in client.outgoing_caller_ids.list(limit=50)
        }
        if settings.my_phone_number in verified:
            checks.append(Check("Trial account", "warn", "your number is verified"))
        else:
            checks.append(
                Check(
                    "Trial account",
                    "fail",
                    "a trial account can only call verified numbers",
                    "Verify the numbers you want to reach, or upgrade the account. "
                    "Trial calls also carry a spoken Twilio preamble.",
                )
            )

    return checks


def report(checks: list[Check]) -> str:
    width = max((len(c.name) for c in checks), default=0)
    lines = []
    for check in checks:
        lines.append(f"  [{MARKS[check.status]}]  {check.name.ljust(width)}   {check.detail}")
        if check.fix and check.status in {"fail", "warn"}:
            lines.append(f"           {' ' * width}   -> {check.fix}")
    return "\n".join(lines)


def worst(checks: list[Check]) -> Status:
    for status in ("fail", "warn", "ok"):
        if any(c.status == status for c in checks):
            return status  # type: ignore[return-value]
    return "skip"
