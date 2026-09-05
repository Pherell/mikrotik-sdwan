"""Shared test fixtures."""

from __future__ import annotations

import os

os.environ.setdefault("SDWAN_SECRET_KEY", "test-secret-key-at-least-32-chars-long!!")
os.environ.setdefault("SDWAN_JWT_SECRET", "test-jwt-secret-at-least-32-chars-long!!")
os.environ.setdefault("SDWAN_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SDWAN_ENV", "test")
# Keep the unreachable-device tests fast.
os.environ.setdefault("SDWAN_DEVICE_CONNECT_TIMEOUT", "1")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.drivers.ros7_rest import Ros7RestDriver  # noqa: E402
from tests.fakeros.server import FakeRouterOS  # noqa: E402


@pytest.fixture
def fake_ros() -> FakeRouterOS:
    return FakeRouterOS(
        password="secret",
        menus={
            "ip/address": [
                {"address": "203.0.113.10/24", "interface": "ether1", "disabled": False},
                {"address": "192.168.88.1/24", "interface": "bridge", "disabled": False},
                {"address": "10.10.0.2/30", "interface": "ether2", "disabled": False},
            ],
            "ip/route": [
                {
                    "dst-address": "0.0.0.0/0",
                    "gateway": "203.0.113.1",
                    "distance": 1,
                    "disabled": False,
                    "inactive": False,
                },
                {
                    "dst-address": "0.0.0.0/0",
                    "gateway": "10.10.0.1",
                    "distance": 2,
                    "disabled": False,
                    "inactive": False,
                },
                {
                    "dst-address": "192.168.88.0/24",
                    "gateway": "bridge",
                    "distance": 0,
                    "disabled": False,
                },
            ],
            "ip/dhcp-client": [
                {"interface": "ether2", "gateway": "10.10.0.1", "disabled": False}
            ],
            "ip/ipsec/peer": [],
            "interface/gre": [],
        },
    )


@pytest.fixture
async def driver(fake_ros: FakeRouterOS):
    d = Ros7RestDriver(
        "test-router",
        "admin",
        "secret",
        transport=httpx.ASGITransport(app=fake_ros.app),
    )
    await d.connect()
    try:
        yield d
    finally:
        await d.close()
