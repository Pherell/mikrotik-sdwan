"""The web console.

A command console, not a shell. Almost every test here is about the boundary
of that: what it refuses, and whether the refusal can be talked around by
spelling something differently.
"""

from __future__ import annotations

import httpx
import pytest

from app.drivers.ros7_rest import Ros7RestDriver
from app.services.console import (
    ACTIONS,
    READABLE,
    REFUSED_EXACT,
    REFUSED_TREE,
    ConsoleRefused,
    parse,
    precheck,
    run_console,
)
from tests.fakeros.server import FakeRouterOS


async def _driver(fake: FakeRouterOS) -> Ros7RestDriver:
    d = Ros7RestDriver(
        "test-router", "admin", "secret", transport=httpx.ASGITransport(app=fake.app)
    )
    await d.connect()
    return d


# -- parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command,path,verb",
    [
        ("/ip/route/print", "ip/route", "print"),
        # RouterOS accepts both spellings, so refusing one would be the console
        # being pedantic about something the device is not.
        ("/ip route print", "ip/route", "print"),
        ("ip/route/print", "ip/route", "print"),
        # A bare menu means print, which is what RouterOS does too.
        ("/ip/route", "ip/route", "print"),
        ("/system/resource/print", "system/resource", "print"),
        ("  /IP/Route/Print  ", "ip/route", "print"),
    ],
)
def test_the_spellings_people_actually_type(command, path, verb) -> None:
    parsed = parse(command)
    assert parsed.path == path
    assert parsed.verb == verb


def test_parameters_are_read_as_parameters() -> None:
    parsed = parse("/ping address=8.8.8.8 count=3")
    assert parsed.path == "ping"
    assert parsed.params == {"address": "8.8.8.8", "count": "3"}


def test_a_quoted_value_survives() -> None:
    parsed = parse('/ip/route/print where="comment"')
    assert parsed.params["where"] == "comment"


# -- what it refuses --------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "/ip/route/print; /user/print",
        "/ip/route/print\n/user/print",
        "/system/script/run [find]",
        ":put $password",
        "/ip/route/print [/user/print]",
    ],
)
def test_one_checked_command_cannot_become_several_unchecked_ones(command) -> None:
    with pytest.raises(ConsoleRefused):
        precheck(command)


@pytest.mark.parametrize(
    "command",
    [
        "/ip/route/remove",
        "/ip/route/set distance=1",
        "/system/reboot",
        "/interface/disable",
        "/system/reset-configuration",
        "/ip/firewall/filter/add chain=input action=accept",
    ],
)
def test_anything_that_changes_the_device_is_refused(command) -> None:
    """Not only for safety. A change made here is drift the reconciler does not
    know about, and the next apply reverts it silently."""
    with pytest.raises(ConsoleRefused, match="changes configuration"):
        precheck(command)


@pytest.mark.parametrize(
    "command",
    [
        "/user/print",
        "/user/group/print",
        "/ip/ipsec/identity/print",
        "/ppp/secret/print",
        "/file/print",
        "/export",
        "/ip/route/export",
        "/interface/wireguard/print",
    ],
)
def test_menus_that_hold_secrets_are_refused_with_a_reason(command) -> None:
    with pytest.raises(ConsoleRefused) as caught:
        precheck(command)
    # "Not allowed" with no explanation trains people to stop reading.
    assert len(str(caught.value)) > 40


def test_a_refusal_cannot_be_talked_around_by_spelling() -> None:
    """The allowlist is on the parsed path, so every spelling of a refused
    menu lands on the same refusal."""
    for spelling in ["/user/print", "user print", "/USER/print", "/user"]:
        with pytest.raises(ConsoleRefused):
            precheck(spelling)


def test_an_unknown_menu_is_refused_rather_than_tried() -> None:
    with pytest.raises(ConsoleRefused, match="allowlist"):
        precheck("/some/menu/nobody/has/heard/of/print")


def test_monitor_is_refused_because_nothing_here_can_stop_it() -> None:
    with pytest.raises(ConsoleRefused, match="until it is stopped"):
        precheck("/interface/monitor")


def test_the_allow_and_refuse_lists_do_not_overlap() -> None:
    """An entry in both would be allowed or refused depending on the order the
    checks happen to run in, which is not a security boundary."""
    refused = set(REFUSED_TREE) | set(REFUSED_EXACT)
    assert not (READABLE & refused)
    assert not (ACTIONS & refused)


def test_a_submenu_of_a_refused_tree_is_refused_too() -> None:
    """A menu added by a later RouterOS must be refused by default rather than
    allowed by oversight."""
    with pytest.raises(ConsoleRefused):
        precheck("/user/something-new-in-a-later-version/print")


def test_wireguard_peers_stay_readable_while_the_interface_does_not() -> None:
    """The interface rows carry a private key next to the public one; the peer
    rows carry only public keys, and they answer the question people ask."""
    with pytest.raises(ConsoleRefused):
        precheck("/interface/wireguard/print")
    assert precheck("/interface/wireguard/peers/print").path == "interface/wireguard/peers"


# -- what it does -----------------------------------------------------------


async def test_a_read_returns_rows() -> None:
    fake = FakeRouterOS(
        password="secret",
        menus={
            "ip/route": [
                {"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1", "distance": 1}
            ]
        },
    )
    driver = await _driver(fake)
    try:
        result = await run_console(driver, "/ip/route/print")
    finally:
        await driver.close()

    assert result.error is None
    assert len(result.rows) == 1
    assert result.rows[0]["gateway"] == "203.0.113.1"


async def test_it_says_what_it_actually_ran() -> None:
    """A console that will not say what it ran is asking to be trusted for no
    reason -- and "/ip route" silently becoming a print should be visible."""
    fake = FakeRouterOS(password="secret", menus={"ip/route": []})
    driver = await _driver(fake)
    try:
        result = await run_console(driver, "/ip route")
    finally:
        await driver.close()

    assert result.command == "/ip route"
    assert result.resolved == "/ip/route/print"


async def test_ping_runs_as_an_action_not_a_read() -> None:
    fake = FakeRouterOS(password="secret")
    driver = await _driver(fake)
    try:
        result = await run_console(driver, "/ping address=8.8.8.8 count=2")
    finally:
        await driver.close()

    assert fake.commands[-1][0] == "ping"
    assert len(result.rows) == 2


async def test_secrets_are_stripped_even_from_an_allowed_menu() -> None:
    """Belt and braces. The allowlist is the real control; this catches a menu
    that grows a secret property in a later RouterOS."""
    fake = FakeRouterOS(
        password="secret",
        menus={
            "ip/ipsec/peer": [
                {"name": "peer1", "address": "198.51.100.1", "secret": "hunter2"}
            ]
        },
    )
    driver = await _driver(fake)
    try:
        result = await run_console(driver, "/ip/ipsec/peer/print")
    finally:
        await driver.close()

    assert result.rows[0]["name"] == "peer1"
    assert "secret" not in result.rows[0]
    assert "hunter2" not in str(result.rows)


async def test_a_device_error_is_reported_not_raised() -> None:
    """An allowed command that the device rejects is an answer, not a failure
    of the console."""
    fake = FakeRouterOS(password="secret")  # no ip/dns menu
    driver = await _driver(fake)
    try:
        result = await run_console(driver, "/ip/dns/print")
    finally:
        await driver.close()

    assert result.error is not None
    assert result.rows == []
