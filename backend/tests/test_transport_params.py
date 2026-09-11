"""Transport parameters: what a fabric lets you override, and what it refuses.

The whole point is that both ways free-form configuration fails are silent. A
misspelled key is ignored, so the fabric builds with the default exactly as if
nothing had been set. A bad value renders, applies cleanly, and the tunnel
never establishes -- IKE mismatches do not report themselves as configuration
errors. Either way it looks like "the VPN is broken" days later.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.fabric import FabricCreate
from app.transports import available, get_transport
from app.transports.params import OPTIONS, describe, options_for, validate


def make(params: dict, transport: str = "ipsec_gre") -> FabricCreate:
    return FabricCreate(name="core", transport=transport, transport_params=params)


# -- the failure this exists to prevent -------------------------------------


def test_a_misspelled_setting_is_refused_rather_than_ignored() -> None:
    """Ignoring it means the fabric builds with the default and nothing
    anywhere says the setting did not take."""
    problems = validate("ipsec_gre", {"dh_grup": "ecp384"})
    assert len(problems) == 1
    assert "not a setting" in problems[0].message
    # The message names what it does accept, so the fix does not need docs.
    assert "dh_group" in problems[0].message


def test_a_value_the_far_end_could_never_agree_to_is_refused() -> None:
    problems = validate("ipsec_gre", {"dh_group": "modp999"})
    assert len(problems) == 1
    assert "ecp256" in problems[0].message


def test_every_problem_is_reported_not_only_the_first() -> None:
    """Somebody pasting a security standard in is likely to have several, and
    fixing them one round trip at a time is miserable."""
    problems = validate(
        "ipsec_gre",
        {"dh_group": "nope", "enc_algorithm": "rot13", "lifetime": "soon"},
    )
    assert {p.key for p in problems} == {"dh_group", "enc_algorithm", "lifetime"}


# -- the kinds --------------------------------------------------------------


@pytest.mark.parametrize("value", ["8h", "30m", "1d", "1d12h", "90s", "2w"])
def test_durations_routeros_accepts(value) -> None:
    assert validate("ipsec_gre", {"lifetime": value}) == []


@pytest.mark.parametrize("value", ["8", "soon", "h8", "8x", "", "8h30"])
def test_durations_routeros_does_not(value) -> None:
    """A bare number is not a duration RouterOS accepts, and it is the mistake
    everyone makes."""
    assert validate("ipsec_gre", {"lifetime": value})


@pytest.mark.parametrize("value,ok", [(1, True), (100, True), (0, False), (101, False)])
def test_numbers_are_bounded(value, ok) -> None:
    problems = validate("ipsec_gre", {"dpd_maximum_failures": value})
    assert (problems == []) is ok


def test_a_bridge_name_cannot_be_empty() -> None:
    assert validate("vxlan", {"bridge": "   "})
    assert validate("vxlan", {"bridge": "br-lan"}) == []


# -- transports with nothing to negotiate -----------------------------------


@pytest.mark.parametrize("transport", ["gre", "ipip"])
def test_a_transport_with_nothing_to_negotiate_says_so(transport) -> None:
    """GRE and IPIP have no ciphers to agree on and no port. An empty list is
    the honest answer; the UI draws "nothing to configure" rather than an empty
    section that looks broken."""
    assert options_for(transport) == ()
    assert validate(transport, {"dh_group": "ecp256"})  # and refuses overrides


def test_every_transport_with_options_actually_exists() -> None:
    """OPTIONS described "ipsec_policy" -- a transport nothing registers and
    create_fabric refuses. Harmless on its own, but it is how a UI ends up
    offering settings for something that cannot be built, and how someone
    concludes a transport is available when it is not."""
    assert set(OPTIONS) <= set(available()), "options for a transport that does not exist"


def test_wireguard_offers_the_port_but_no_ciphers() -> None:
    """WireGuard's ciphers are not selectable by design, so there is nothing to
    negotiate -- but the port is worth exposing, because something in the path
    dropping it is the one way a WireGuard fabric fails to come up."""
    keys = {option.key for option in options_for("wireguard")}
    assert "listen_port" in keys
    assert not keys & {"dh_group", "enc_algorithm", "auth_algorithm"}
    assert validate("wireguard", {"dh_group": "ecp256"})  # still refuses ciphers
    assert validate("wireguard", {"listen_port": 51820}) == []


# -- the schema -------------------------------------------------------------


def test_a_fabric_with_good_overrides_is_accepted() -> None:
    fabric = make({"dh_group": "ecp384", "lifetime": "1d", "exchange_mode": "ike2"})
    assert fabric.transport_params["dh_group"] == "ecp384"


def test_a_fabric_with_a_bad_override_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a setting"):
        make({"dh_grup": "ecp384"})


def test_no_overrides_is_still_valid() -> None:
    """The defaults are chosen; not touching them must stay the easy path."""
    assert make({}).transport_params == {}


def test_params_are_checked_against_the_right_transport() -> None:
    """vxlan's bridge is meaningless to ipsec_gre and the other way round."""
    assert validate("vxlan", {"bridge": "br-lan"}) == []
    assert validate("ipsec_gre", {"bridge": "br-lan"})
    assert validate("ipsec_gre", {"dh_group": "ecp256"}) == []
    assert validate("vxlan", {"dh_group": "ecp256"})


# -- the description the UI draws from --------------------------------------


def test_every_transport_this_build_has_is_described() -> None:
    """A transport missing from OPTIONS would silently accept anything, since
    an unknown transport has no allowlist to check against."""
    assert set(available()) <= set(OPTIONS)


def test_defaults_come_from_the_transport_not_a_second_copy() -> None:
    """Two lists of defaults is one list of defaults and one lie."""
    driver = get_transport("ipsec_gre")
    described = describe("ipsec_gre", driver.defaults())
    for option in described.options:
        assert option["default"] == driver.defaults()[option["key"]]


def test_every_default_would_pass_its_own_validation() -> None:
    """A default that the validator rejects means nobody can save a fabric
    after opening the Advanced section and saving it unchanged."""
    for transport in available():
        defaults = get_transport(transport).defaults()
        assert validate(transport, defaults) == [], transport


def test_every_option_explains_itself() -> None:
    """These are settings people change because a security standard says so,
    and a label with no explanation makes that a guess."""
    for transport in available():
        for option in options_for(transport):
            assert option.label
            assert len(option.why) > 30
