from __future__ import annotations

import pytest

from phone_agent.settings import Settings
from phone_agent.telephony import DestinationRefused, check_destination, normalise_number


def test_normalise_number_strips_formatting():
    assert normalise_number("+1 (800) 555-1212") == "+18005551212"
    assert normalise_number("00448005551212") == "+448005551212"
    assert normalise_number("") == ""


def test_emergency_numbers_are_always_refused():
    settings = Settings(allowed_destinations=["911"])
    with pytest.raises(DestinationRefused, match="blocked"):
        check_destination("911", settings)


def test_allowlist_is_enforced_when_set():
    settings = Settings(allowed_destinations=["+18005551212"])
    assert check_destination("+1 800 555 1212", settings) == "+18005551212"
    with pytest.raises(DestinationRefused, match="ALLOWED_DESTINATIONS"):
        check_destination("+18005559999", settings)


def test_empty_allowlist_permits_anything():
    settings = Settings()
    assert check_destination("+18005559999", settings) == "+18005559999"


def test_blank_destination_is_refused():
    with pytest.raises(DestinationRefused):
        check_destination("", Settings())


@pytest.mark.parametrize("number", ["911", "+1911", "112", "988", "+1988"])
def test_emergency_variants_are_refused(number):
    with pytest.raises(DestinationRefused, match="blocked"):
        check_destination(number, Settings())


@pytest.mark.parametrize("number", ["+18005559999", "+18885550911", "+15195559112"])
def test_ordinary_numbers_ending_in_emergency_digits_are_allowed(number):
    """Regression: suffix matching used to block any number ending in 911."""
    assert check_destination(number, Settings()) == number
