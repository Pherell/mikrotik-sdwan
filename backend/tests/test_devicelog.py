"""RouterOS's own log.

The only place that says *why* the device did something. The parsing that
matters is topics: RouterOS returns them comma-separated in one string, and
the severity of a line is the worst topic on it, not the first.
"""

from __future__ import annotations

import httpx
import pytest

from app.drivers.ros7_rest import Ros7RestDriver
from app.services.devicelog import read_device_log, severity_of
from tests.fakeros.server import FakeRouterOS

LOG = [
    {"time": "sep/07 09:00:01", "topics": "system,info", "message": "router rebooted"},
    {"time": "sep/07 09:00:12", "topics": "dhcp,info",
     "message": "dhcp-client on ether2 got 10.10.10.29"},
    {"time": "sep/07 09:03:44", "topics": "ipsec,error",
     "message": "phase1 negotiation failed due to time up"},
    {"time": "sep/07 09:04:01", "topics": "ipsec,info", "message": "peer is established"},
    {"time": "sep/07 09:11:00", "topics": "script,warning",
     "message": "scheduler script did not finish"},
]


async def _driver(fake: FakeRouterOS) -> Ros7RestDriver:
    d = Ros7RestDriver(
        "test-router", "admin", "secret", transport=httpx.ASGITransport(app=fake.app)
    )
    await d.connect()
    return d


@pytest.mark.parametrize(
    "topics,expected",
    [
        (["ipsec", "error"], "error"),
        (["error", "ipsec"], "error"),
        (["script", "warning"], "warning"),
        (["system", "info"], "info"),
        ([], "info"),
        (["IPSEC", "ERROR"], "error"),
    ],
)
def test_the_worst_topic_decides_the_severity(topics, expected) -> None:
    """"ipsec,error" is an error that happens to be about ipsec, not an ipsec
    line that happens to be tagged error."""
    assert severity_of(topics) == expected


async def test_the_log_comes_back_newest_first() -> None:
    """RouterOS returns the buffer oldest-first. Anyone reading a log after an
    incident wants the other end of it."""
    fake = FakeRouterOS(password="secret", menus={"log": LOG})
    driver = await _driver(fake)
    try:
        entries = await read_device_log(driver)
    finally:
        await driver.close()

    assert [e.message for e in entries][0] == "scheduler script did not finish"
    assert len(entries) == 5


async def test_truncation_keeps_the_newest_lines_not_the_oldest() -> None:
    fake = FakeRouterOS(password="secret", menus={"log": LOG})
    driver = await _driver(fake)
    try:
        entries = await read_device_log(driver, limit=2)
    finally:
        await driver.close()

    assert len(entries) == 2
    assert entries[0].message == "scheduler script did not finish"
    assert entries[1].message == "peer is established"


async def test_topics_are_split_once_here() -> None:
    fake = FakeRouterOS(password="secret", menus={"log": LOG})
    driver = await _driver(fake)
    try:
        entries = await read_device_log(driver, topic="ipsec")
    finally:
        await driver.close()

    assert len(entries) == 2
    assert all("ipsec" in e.topics for e in entries)
    assert entries[-1].severity == "error"


async def test_filtering_on_a_topic_finds_lines_that_carry_several() -> None:
    """The reason filtering is not pushed to the device: RouterOS's query
    matches the topic string exactly, so asking it for "error" misses every
    line tagged "ipsec,error" -- which is every line anyone wants."""
    fake = FakeRouterOS(password="secret", menus={"log": LOG})
    driver = await _driver(fake)
    try:
        entries = await read_device_log(driver, topic="error")
    finally:
        await driver.close()

    assert len(entries) == 1
    assert "phase1" in entries[0].message


async def test_a_text_search_is_case_insensitive() -> None:
    fake = FakeRouterOS(password="secret", menus={"log": LOG})
    driver = await _driver(fake)
    try:
        entries = await read_device_log(driver, contains="PHASE1")
    finally:
        await driver.close()

    assert len(entries) == 1


async def test_a_freshly_booted_router_has_an_empty_log_not_an_error() -> None:
    fake = FakeRouterOS(password="secret")
    driver = await _driver(fake)
    try:
        entries = await read_device_log(driver)
    finally:
        await driver.close()

    assert entries == []
