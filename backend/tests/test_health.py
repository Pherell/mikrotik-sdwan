"""Device health, read from /system/resource."""

from __future__ import annotations

import httpx
import pytest

from app.drivers.ros7_rest import Ros7RestDriver
from app.services.health import read_health
from tests.fakeros.server import FakeRouterOS

RESOURCE = {
    "uptime": "3w2d4h11m",
    "version": "7.14.3 (stable)",
    "board-name": "RB5009UG+S+",
    "architecture-name": "arm64",
    "cpu-load": 7,
    "cpu-count": 4,
    "cpu-frequency": 1400,
    "free-memory": 812900352,
    "total-memory": 1073741824,
    "free-hdd-space": 91234304,
    "total-hdd-space": 134217728,
}


async def _health(menus: dict) -> object:
    ros = FakeRouterOS(password="secret", menus=menus)
    d = Ros7RestDriver(
        "test", "admin", "secret", transport=httpx.ASGITransport(app=ros.app)
    )
    await d.connect()
    try:
        return await read_health(d)
    finally:
        await d.close()


async def test_reads_cpu_memory_and_uptime() -> None:
    health = await _health({"system/resource": [RESOURCE]})

    assert health.cpu_load_percent == 7
    assert health.cpu_count == 4
    assert health.total_memory_bytes == 1073741824
    assert health.free_memory_bytes == 812900352
    assert health.uptime == "3w2d4h11m"
    assert health.board_name == "RB5009UG+S+"


async def test_a_field_the_device_omits_is_none_not_the_word_none() -> None:
    """The exact bug that labelled every port "None": str(None) is truthy."""
    health = await _health({"system/resource": [{"uptime": "1d"}]})

    assert health.cpu_load_percent is None
    assert health.board_name is None
    assert health.version is None
    assert health.uptime == "1d"


async def test_an_empty_menu_does_not_raise() -> None:
    health = await _health({"system/resource": []})

    assert health.cpu_load_percent is None
    assert health.uptime is None


@pytest.mark.parametrize("bad", ["", "n/a", None])
async def test_unparseable_numbers_become_none(bad) -> None:
    health = await _health({"system/resource": [{**RESOURCE, "cpu-load": bad}]})

    assert health.cpu_load_percent is None
