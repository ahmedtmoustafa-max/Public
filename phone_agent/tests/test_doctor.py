"""The configuration check."""

from __future__ import annotations

import pytest

from phone_agent.doctor import Check, report, static_checks, worst
from phone_agent.settings import Settings

GOOD = dict(
    public_base_url="https://agent.example.com",
    twilio_account_sid="AC" + "0" * 32,
    twilio_auth_token="token",
    twilio_from_number="+15195559999",
    my_phone_number="+15195550123",
    validate_twilio_signatures=True,
)


def find(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


def test_a_complete_configuration_has_no_failures():
    checks = static_checks(Settings(**GOOD))
    assert worst(checks) != "fail", report(checks)
    assert find(checks, "Your phone").status == "ok"


@pytest.mark.parametrize(
    "number", ["5195550123", "+1 519 555 0123", "519-555-0123", "+1-519-555-0123"]
)
def test_a_number_that_is_not_e164_fails_with_the_fix(number):
    checks = static_checks(Settings(**{**GOOD, "my_phone_number": number}))
    check = find(checks, "Your phone")
    assert check.status == "fail"
    assert "E.164" in check.detail


def test_a_missing_phone_number_fails():
    checks = static_checks(Settings(**{**GOOD, "my_phone_number": ""}))
    assert find(checks, "Your phone").status == "fail"


def test_your_phone_and_the_twilio_number_must_differ():
    checks = static_checks(
        Settings(**{**GOOD, "my_phone_number": "+15195559999"})
    )
    assert find(checks, "Numbers differ").status == "fail"


def test_localhost_is_not_a_public_address():
    checks = static_checks(Settings(**{**GOOD, "public_base_url": "http://localhost:8080"}))
    check = find(checks, "Public address")
    assert check.status == "fail"
    assert "cloudflared" in check.fix


def test_plain_http_is_rejected():
    checks = static_checks(Settings(**{**GOOD, "public_base_url": "http://agent.example.com"}))
    assert find(checks, "Public address").status == "fail"


def test_an_api_key_sid_is_not_an_account_sid():
    checks = static_checks(Settings(**{**GOOD, "twilio_account_sid": "SK" + "0" * 32}))
    assert find(checks, "Twilio credentials").status == "fail"


def test_unsigned_webhooks_are_flagged():
    checks = static_checks(Settings(**{**GOOD, "validate_twilio_signatures": False}))
    assert find(checks, "Webhook signatures").status == "warn"


def test_auto_mode_is_reported_as_optional_not_broken():
    checks = static_checks(Settings(**GOOD))
    check = find(checks, "Auto mode")
    assert check.status == "skip"
    assert "script and watch modes still work" in check.detail


def test_auto_mode_is_ready_with_both_keys():
    checks = static_checks(
        Settings(
            **{
                **GOOD,
                "anthropic_api_key": "sk-ant-x",
                "asr_provider": "deepgram",
                "deepgram_api_key": "dg-x",
            }
        )
    )
    assert find(checks, "Auto mode").status == "ok"


def test_report_shows_fixes_only_for_problems():
    checks = [
        Check("Fine", "ok", "yes", fix="should not appear"),
        Check("Broken", "fail", "no", fix="do this instead"),
    ]
    text = report(checks)
    assert "do this instead" in text
    assert "should not appear" not in text


def test_worst_ranks_failures_above_warnings():
    assert worst([Check("a", "ok"), Check("b", "warn"), Check("c", "fail")]) == "fail"
    assert worst([Check("a", "ok"), Check("b", "warn")]) == "warn"
    assert worst([Check("a", "ok")]) == "ok"
