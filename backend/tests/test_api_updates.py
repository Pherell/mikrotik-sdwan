"""Every PATCH endpoint, because none of them had a test.

`updated_at` was declared `onupdate=func.now()`, so the database generated the
value and SQLAlchemy expired the attribute after the UPDATE. Reading it back
while serialising the response needed a SELECT, and under asyncio a lazy read
raises MissingGreenlet rather than quietly fetching -- so every PATCH wrote its
row correctly and then returned 500 on the way out.

371 tests passed at the time. Not one of them issued a PATCH.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import create_app
from app.models import Base, User
from app.models.enums import Role
from app.security import hash_password


@pytest.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as s:
        s.add(
            User(
                email="admin@example.com",
                role=Role.admin,
                password_hash=hash_password("correct-horse"),
            )
        )
        await s.commit()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        resp = await client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "correct-horse"},
        )
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield client
    await engine.dispose()


async def _site(api: httpx.AsyncClient, name: str = "oslo") -> dict:
    resp = await api.post(
        "/sites",
        json={
            "name": name,
            "role": "spoke",
            "mgmt_host": "10.0.0.1",
            "username": "admin",
            "password": "device-password",
            "device_kind": "ros7",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def test_patch_site_returns_the_updated_row(api: httpx.AsyncClient) -> None:
    site = await _site(api)

    resp = await api.patch(f"/sites/{site['id']}", json={"role": "hub"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "hub"
    assert resp.json()["updated_at"], "the field that used to blow up on the way out"


async def test_patch_site_persists(api: httpx.AsyncClient) -> None:
    """A 500 on the response would still have written the row, so asserting the
    status alone would not prove the endpoint works."""
    site = await _site(api)
    await api.patch(f"/sites/{site['id']}", json={"drift_action": "auto-remediate"})

    resp = await api.get(f"/sites/{site['id']}")
    assert resp.json()["drift_action"] == "auto-remediate"


async def test_patch_wan_returns_the_updated_row(api: httpx.AsyncClient) -> None:
    site = await _site(api)
    created = await api.post(
        f"/sites/{site['id']}/wans",
        json={"name": "wan1", "interface": "ether1", "public_ip": "203.0.113.10"},
    )
    assert created.status_code in (200, 201), created.text

    resp = await api.patch(
        f"/sites/{site['id']}/wans/{created.json()['id']}", json={"cost": 4.0}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["cost"] == 4.0


async def test_patch_fabric_returns_the_updated_row(api: httpx.AsyncClient) -> None:
    created = await api.post(
        "/fabrics",
        json={
            "name": "core",
            "transport": "ipsec_gre",
            "topology": "hub_spoke",
            "ip_pool": "10.255.0.0/16",
            "loopback_pool": "10.254.0.0/24",
            "asn": 65000,
        },
    )
    assert created.status_code in (200, 201), created.text

    resp = await api.patch(f"/fabrics/{created.json()['id']}", json={"mtu": 1380})

    assert resp.status_code == 200, resp.text
    assert resp.json()["mtu"] == 1380
    assert resp.json()["updated_at"]


async def test_patch_policy_returns_the_updated_row(api: httpx.AsyncClient) -> None:
    site = await _site(api)
    await api.post(
        f"/sites/{site['id']}/wans",
        json={"name": "wan1", "interface": "ether1", "public_ip": "203.0.113.10"},
    )
    group = await api.post(
        "/sdwan-groups",
        json={"name": "voice-path", "members": [{"uplink": "wan1"}]},
    )
    assert group.status_code == 201, group.text
    created = await api.post(
        "/policies",
        json={
            "name": "voice",
            "priority": 10,
            "sdwan_group_id": group.json()["id"],
            "dst_prefixes": ["10.0.0.0/8"],
            "fallback": "any",
        },
    )
    assert created.status_code in (200, 201), created.text

    resp = await api.patch(f"/policies/{created.json()['id']}", json={"priority": 20})

    assert resp.status_code == 200, resp.text
    assert resp.json()["priority"] == 20
    assert resp.json()["updated_at"]


async def test_updated_at_actually_moves(api: httpx.AsyncClient) -> None:
    """Computing it in Python rather than SQL must not stop it being set."""
    site = await _site(api)
    before = site["updated_at"]

    resp = await api.patch(f"/sites/{site['id']}", json={"region": "nordics"})

    assert resp.status_code == 200, resp.text
    # Parsed, not compared as strings. Sub-second precision makes
    # "...08.307345Z" sort *before* "...08Z", because "." precedes "Z" --
    # so the string form of this assertion passed by accident of formatting.
    assert datetime.fromisoformat(resp.json()["updated_at"]) >= datetime.fromisoformat(
        before
    )


async def test_patch_site_refuses_a_drift_action_it_cannot_read_back(
    api: httpx.AsyncClient,
) -> None:
    """SiteUpdate did not inherit SiteBase's validation, so this was accepted,
    written, and then blew up serialising the response -- leaving the row
    holding a value nothing else would read."""
    site = await _site(api)

    resp = await api.patch(f"/sites/{site['id']}", json={"drift_action": "remediate"})

    assert resp.status_code == 422, resp.text
    assert (await api.get(f"/sites/{site['id']}")).json()["drift_action"] == "alert"


async def test_patch_site_keeps_the_rollback_timeout_in_range(
    api: httpx.AsyncClient,
) -> None:
    site = await _site(api)

    assert (
        await api.patch(f"/sites/{site['id']}", json={"rollback_timeout_seconds": 5})
    ).status_code == 422
    assert (
        await api.patch(f"/sites/{site['id']}", json={"rollback_timeout_seconds": 120})
    ).status_code == 200
