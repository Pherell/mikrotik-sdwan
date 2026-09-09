"""Cross-tenant isolation: a user in one tenant must never reach another
tenant's row by id, even though every list endpoint already filtered by
tenant_id correctly.

Every by-id fetch used to go through a bare ``session.get()`` -- a primary-key
lookup with no notion of tenant. This file seeds two tenants and, for each
entity, has tenant B reach for a UUID that belongs to tenant A: every one of
those calls must 404 (or, for the one list endpoint that was leaking, must not
appear at all), never 200.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import create_app
from app.models import Base, User
from app.models.enums import Role
from app.security import hash_password

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]]:
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

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        yield client, maker
    await engine.dispose()


async def _seed(
    maker: async_sessionmaker[AsyncSession], role: Role, email: str, tenant_id: str
) -> User:
    async with maker() as s:
        user = User(
            email=email,
            role=role,
            password_hash=hash_password("correct-horse"),
            tenant_id=tenant_id,
        )
        s.add(user)
        await s.commit()
        return user


async def _token(client: httpx.AsyncClient, email: str) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _as(client, maker, role: Role, email: str, tenant_id: str) -> dict[str, str]:
    """Seed a user in one tenant and return auth headers for them."""
    await _seed(maker, role, email, tenant_id)
    return _auth(await _token(client, email))


# -- sites --------------------------------------------------------------


async def test_a_site_is_invisible_to_another_tenant(api) -> None:
    client, maker = api
    a = await _as(client, maker, Role.operator, "op-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.admin, "admin-b@example.com", TENANT_B)

    created = await client.post(
        "/sites",
        headers=a,
        json={"name": "branch-1", "mgmt_host": "10.0.0.1", "username": "admin"},
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    # Every by-id route on this site, attempted with tenant B's token.
    assert (await client.get(f"/sites/{site_id}", headers=b)).status_code == 404
    assert (
        await client.patch(f"/sites/{site_id}", headers=b, json={"name": "renamed"})
    ).status_code == 404
    assert (await client.delete(f"/sites/{site_id}", headers=b)).status_code == 404

    # And it never shows up in tenant B's own list.
    listed = await client.get("/sites", headers=b)
    assert site_id not in {s["id"] for s in listed.json()}

    # Tenant A can still reach its own site, proving the 404s above were
    # about tenant, not a broken route.
    assert (await client.get(f"/sites/{site_id}", headers=a)).status_code == 200


# -- fabrics --------------------------------------------------------------


async def test_a_fabric_is_invisible_to_another_tenant(api) -> None:
    client, maker = api
    a = await _as(client, maker, Role.operator, "op-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.operator, "op-b@example.com", TENANT_B)

    fabric = await client.post(
        "/fabrics", headers=a, json={"name": "core", "transport": "ipsec_gre"}
    )
    assert fabric.status_code == 201, fabric.text
    fabric_id = fabric.json()["id"]

    assert (await client.get(f"/fabrics/{fabric_id}", headers=b)).status_code == 404
    assert (
        await client.patch(f"/fabrics/{fabric_id}", headers=b, json={"mtu": 1300})
    ).status_code == 404
    assert (await client.get(f"/fabrics/{fabric_id}/links", headers=b)).status_code == 404


async def test_a_fabric_cannot_adopt_another_tenants_site(api) -> None:
    """The actual IDOR: creating a fabric (or adding a member) with a
    site_id that exists, but belongs to someone else's tenant."""
    client, maker = api
    a = await _as(client, maker, Role.operator, "op-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.operator, "op-b@example.com", TENANT_B)

    site_a = await client.post(
        "/sites",
        headers=a,
        json={"name": "branch-1", "mgmt_host": "10.0.0.1", "username": "admin"},
    )
    site_a_id = site_a.json()["id"]

    # Tenant B tries to found a fabric with tenant A's site as a member.
    created = await client.post(
        "/fabrics",
        headers=b,
        json={
            "name": "core",
            "transport": "ipsec_gre",
            "member_site_ids": [site_a_id],
        },
    )
    assert created.status_code == 400, created.text

    # Tenant B tries again via the separate add-member endpoint, against a
    # fabric that is genuinely theirs.
    own_fabric = await client.post(
        "/fabrics", headers=b, json={"name": "core", "transport": "ipsec_gre"}
    )
    fabric_b_id = own_fabric.json()["id"]
    added = await client.post(
        f"/fabrics/{fabric_b_id}/members", headers=b, json={"site_id": site_a_id}
    )
    assert added.status_code == 404, added.text


async def test_removing_a_member_checks_the_fabric_belongs_to_the_caller(api) -> None:
    """remove_member took fabric_id and site_id with no ownership check on the
    fabric at all -- any fabric_id/site_id pair that existed anywhere would
    do, not just ones in the caller's own tenant."""
    client, maker = api
    a = await _as(client, maker, Role.operator, "op-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.operator, "op-b@example.com", TENANT_B)

    site_a = await client.post(
        "/sites",
        headers=a,
        json={"name": "branch-1", "mgmt_host": "10.0.0.1", "username": "admin"},
    )
    site_a_id = site_a.json()["id"]
    fabric_a = await client.post(
        "/fabrics", headers=a, json={"name": "core", "transport": "ipsec_gre"}
    )
    fabric_a_id = fabric_a.json()["id"]
    add = await client.post(
        f"/fabrics/{fabric_a_id}/members", headers=a, json={"site_id": site_a_id}
    )
    assert add.status_code == 201, add.text

    removed = await client.delete(
        f"/fabrics/{fabric_a_id}/members/{site_a_id}", headers=b
    )
    assert removed.status_code == 404, removed.text


# -- policy family --------------------------------------------------------


async def test_an_sla_profile_is_invisible_to_another_tenant(api) -> None:
    client, maker = api
    a = await _as(client, maker, Role.operator, "op-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.operator, "op-b@example.com", TENANT_B)

    created = await client.post("/sla-profiles", headers=a, json={"name": "gold"})
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    assert (await client.delete(f"/sla-profiles/{profile_id}", headers=b)).status_code == 404
    # A group in tenant B referencing tenant A's profile must be rejected as
    # "no such SLA profile", not silently accepted.
    rejected = await client.post(
        "/sdwan-groups",
        headers=b,
        json={
            "name": "g",
            "sla_profile_id": profile_id,
            "members": [{"uplink": "wan1"}],
        },
    )
    assert rejected.status_code == 400, rejected.text


# -- API tokens -------------------------------------------------------------


async def test_an_api_token_is_invisible_to_another_tenant(api) -> None:
    client, maker = api
    a = await _as(client, maker, Role.admin, "admin-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.admin, "admin-b@example.com", TENANT_B)

    minted = await client.post("/api-tokens", headers=a, json={"name": "ci"})
    assert minted.status_code == 201, minted.text
    token_id = minted.json()["id"]

    assert (
        await client.patch(f"/api-tokens/{token_id}", headers=b, json={"name": "stolen"})
    ).status_code == 404
    assert (await client.delete(f"/api-tokens/{token_id}", headers=b)).status_code == 404

    listed = await client.get("/api-tokens", headers=b)
    assert token_id not in {t["id"] for t in listed.json()}


# -- users --------------------------------------------------------------


async def test_the_user_list_does_not_cross_tenants(api) -> None:
    """list_users had no tenant filter at all: every admin, in any tenant,
    saw every user in the database."""
    client, maker = api
    # Only the side effect -- seeding this tenant's admin -- matters here.
    await _as(client, maker, Role.admin, "admin-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.admin, "admin-b@example.com", TENANT_B)

    listed = await client.get("/users", headers=b)
    assert listed.status_code == 200
    emails = {u["email"] for u in listed.json()}
    assert "admin-a@example.com" not in emails
    assert "admin-b@example.com" in emails


async def test_a_user_cannot_be_edited_from_another_tenant(api) -> None:
    client, maker = api
    a = await _as(client, maker, Role.admin, "admin-a@example.com", TENANT_A)
    b = await _as(client, maker, Role.admin, "admin-b@example.com", TENANT_B)

    created = await client.post(
        "/users",
        headers=a,
        json={"email": "viewer-a@example.com", "password": "correct-horse", "role": "viewer"},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    promoted = await client.patch(
        f"/users/{user_id}", headers=b, json={"role": "admin"}
    )
    assert promoted.status_code == 404, promoted.text
