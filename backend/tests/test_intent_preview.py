"""Intent preview (B5 layer 1): rendered intent, with no device contact.

There is no stored last-known device state in the controller, so this layer
cannot be a diff -- it is the site's configuration rendered on its own,
labelled as intent. These tests pin the properties that make it safe to
offer next to the live plan: it works for a site that cannot be reached at
all, it never opens a driver, and secrets are masked in its lines.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.base import ConfigItem, ConfigSection
from app.main import create_app
from app.models import Base, User
from app.models.enums import Role
from app.security import hash_password
from app.services.reconcile import _intent_preview


@pytest.fixture
async def api(monkeypatch) -> AsyncIterator[tuple]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    # The preview must never open a driver. Make any attempt fail loudly
    # rather than silently fall back to a real (unreachable) device.
    @asynccontextmanager
    async def exploding_open_driver(_site, _box=None):
        raise AssertionError("intent preview must not open a driver")
        yield  # pragma: no cover - the raise above always fires first

    monkeypatch.setattr("app.services.reconcile.open_driver", exploding_open_driver)

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with maker() as s:
        s.add(
            User(
                email="op@example.com",
                role=Role.operator,
                password_hash=hash_password("correct-horse"),
            )
        )
        s.add(
            User(
                email="viewer@example.com",
                role=Role.viewer,
                password_hash=hash_password("correct-horse"),
            )
        )
        s.add(
            User(
                email="other@example.com",
                role=Role.operator,
                password_hash=hash_password("correct-horse"),
                tenant_id="other",
            )
        )
        await s.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        yield client, maker
    await engine.dispose()


async def _auth(client: httpx.AsyncClient, email: str = "op@example.com") -> dict[str, str]:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _make_site(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides
) -> str:
    body = {
        "name": "branch-1",
        "mgmt_host": "203.0.113.10",
        "username": "admin",
        "password": "secret",
        "loopback_ip": "10.254.0.7",
        "local_prefixes": ["192.168.10.0/24"],
    }
    body.update(overrides)
    resp = await client.post("/sites", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_preview_renders_intent_sections_and_totals(api) -> None:
    client, _ = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    resp = await client.post(f"/sites/{site_id}/preview", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Labelled as intent, never as a diff against a device.
    assert body["source"] == "intent"
    assert body["diffed_against_device"] is False
    # Bridge + loopback address + one local prefix, as in the plan tests.
    assert body["total_items"] == 3
    assert body["section_count"] == len(body["sections"])
    assert sum(s["item_count"] for s in body["sections"]) == body["total_items"]

    paths = [s["path"] for s in body["sections"]]
    assert "/interface/bridge" in paths
    assert "/ip/address" in paths
    assert "/ip/firewall/address-list" in paths
    orders = [s["order"] for s in body["sections"]]
    assert orders == sorted(orders)

    for section in body["sections"]:
        assert section["item_count"] == len(section["lines"])
        for line in section["lines"]:
            assert line.startswith(f"+ {section['path']} ")
    bridge = next(s for s in body["sections"] if s["path"] == "/interface/bridge")
    assert "name=lo-sdwan" in bridge["lines"][0]


async def test_viewer_may_preview(api) -> None:
    client, _ = api
    op_headers = await _auth(client)
    site_id = await _make_site(client, op_headers)

    viewer = await _auth(client, "viewer@example.com")
    resp = await client.post(f"/sites/{site_id}/preview", headers=viewer)
    assert resp.status_code == 200, resp.text


async def test_preview_works_for_an_unreachable_site_without_credentials(api) -> None:
    """No password, a management host that routes nowhere -- still 200.

    The fixture's driver factory raises if anything tries to open one, so a
    200 here proves the preview rendered from the database alone.
    """
    client, _ = api
    headers = await _auth(client)
    site_id = await _make_site(
        client, headers, name="branch-offline", mgmt_host="192.0.2.1", password=None
    )

    resp = await client.post(f"/sites/{site_id}/preview", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["total_items"] == 3


def test_preview_lines_mask_secrets() -> None:
    sections = [
        ConfigSection(
            path="/ip/ipsec/identity",
            order=20,
            key=("peer",),
            items=[
                ConfigItem(
                    props={
                        "peer": "hub",
                        "auth-method": "pre-shared-key",
                        "secret": "super-secret-value-123",
                    },
                    tag="sdwan:test:identity",
                )
            ],
        ),
        ConfigSection(
            path="/interface/bridge",
            order=10,
            key=("name",),
            items=[ConfigItem(props={"name": "lo-sdwan"}, tag="sdwan:test:loopback")],
        ),
    ]

    result = _intent_preview(sections)

    assert result["source"] == "intent"
    assert result["diffed_against_device"] is False
    assert result["section_count"] == 2
    assert result["total_items"] == 2
    # Sorted by section order, not by input order.
    assert [s["path"] for s in result["sections"]] == [
        "/interface/bridge",
        "/ip/ipsec/identity",
    ]

    line = result["sections"][1]["lines"][0]
    assert line.startswith("+ /ip/ipsec/identity hub  ")
    assert "comment=sdwan:test:identity" in line
    # The secret is masked the way redact_props masks it: never in the clear.
    assert "super-secret-value-123" not in line
    assert "secret=supe" in line
    assert "*" in line


async def test_preview_of_another_tenants_site_is_not_found(api) -> None:
    client, _ = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    other = await _auth(client, "other@example.com")
    resp = await client.post(f"/sites/{site_id}/preview", headers=other)

    assert resp.status_code == 404, resp.text
