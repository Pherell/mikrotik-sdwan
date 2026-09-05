"""API surface: auth, RBAC, site CRUD, and credential handling."""

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
from app.security import SecretBox, hash_password


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]]:
    # StaticPool keeps every session on the one in-memory connection.
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
    # The lifespan seeds a bootstrap admin against the real engine; tests seed
    # their own users, so skip it by not running lifespan.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        yield client, maker
    await engine.dispose()


async def _seed(maker: async_sessionmaker[AsyncSession], role: Role, email: str) -> User:
    async with maker() as s:
        user = User(email=email, role=role, password_hash=hash_password("correct-horse"))
        s.add(user)
        await s.commit()
        return user


async def _token(client: httpx.AsyncClient, email: str, password: str = "correct-horse") -> str:
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- authentication ---------------------------------------------------------


async def test_login_and_me(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")

    token = await _token(client, "admin@example.com")
    resp = await client.get("/auth/me", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@example.com"
    assert "password_hash" not in resp.json()


async def test_login_rejects_wrong_password(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")

    resp = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "nope"}
    )
    assert resp.status_code == 401


async def test_login_does_not_reveal_whether_an_account_exists(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")

    missing = await client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": "nope"}
    )
    wrong = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "nope"}
    )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["detail"] == wrong.json()["detail"]


async def test_unauthenticated_request_is_401(api) -> None:
    client, _ = api
    assert (await client.get("/sites")).status_code == 401


# -- RBAC -------------------------------------------------------------------


async def test_viewer_cannot_create_a_site(api) -> None:
    client, maker = api
    await _seed(maker, Role.viewer, "viewer@example.com")
    token = await _token(client, "viewer@example.com")

    resp = await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "branch-1", "mgmt_host": "10.0.0.1", "username": "admin"},
    )
    assert resp.status_code == 403
    assert "operator" in resp.json()["detail"]


async def test_viewer_can_list_sites(api) -> None:
    client, maker = api
    await _seed(maker, Role.viewer, "viewer@example.com")
    token = await _token(client, "viewer@example.com")

    assert (await client.get("/sites", headers=_auth(token))).status_code == 200


async def test_operator_cannot_delete_a_site(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    created = await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "branch-1", "mgmt_host": "10.0.0.1", "username": "admin"},
    )
    site_id = created.json()["id"]

    resp = await client.delete(f"/sites/{site_id}", headers=_auth(token))
    assert resp.status_code == 403


# -- sites ------------------------------------------------------------------


async def test_create_site_with_wans(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    resp = await client.post(
        "/sites",
        headers=_auth(token),
        json={
            "name": "branch-1",
            "mgmt_host": "203.0.113.10",
            "username": "sdwan",
            "password": "device-secret",
            "role": "spoke",
            "local_prefixes": ["192.168.10.0/24"],
            "wans": [
                {"name": "wan1", "interface": "ether1", "public_ip": "203.0.113.10"},
                {"name": "wan2", "interface": "ether2", "nat_behind": True, "dynamic": True},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["name"] == "branch-1"
    assert body["status"] == "unprovisioned"
    assert len(body["wans"]) == 2
    # The NAT'd uplink is flagged so the fabric planner will not make it a responder.
    assert {w["name"]: w["dial_out_only"] for w in body["wans"]} == {
        "wan1": False,
        "wan2": True,
    }


async def test_device_password_is_never_returned_but_is_stored_encrypted(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    resp = await client.post(
        "/sites",
        headers=_auth(token),
        json={
            "name": "branch-1",
            "mgmt_host": "10.0.0.1",
            "username": "sdwan",
            "password": "device-secret",
        },
    )
    body = resp.json()

    assert "password" not in body
    assert "password_enc" not in body
    assert body["has_credentials"] is True
    assert "device-secret" not in resp.text

    from sqlalchemy import select

    from app.models import Site

    async with maker() as s:
        site = await s.scalar(select(Site).where(Site.name == "branch-1"))
        assert site is not None
        assert site.password_enc is not None
        assert site.password_enc != "device-secret"
        assert SecretBox().decrypt(site.password_enc) == "device-secret"


async def test_duplicate_site_name_is_a_conflict(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")
    payload = {"name": "branch-1", "mgmt_host": "10.0.0.1", "username": "admin"}

    assert (await client.post("/sites", headers=_auth(token), json=payload)).status_code == 201
    second = await client.post("/sites", headers=_auth(token), json=payload)
    assert second.status_code == 409


async def test_invalid_prefix_is_rejected(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    resp = await client.post(
        "/sites",
        headers=_auth(token),
        json={
            "name": "bad",
            "mgmt_host": "10.0.0.1",
            "username": "admin",
            "local_prefixes": ["not-a-prefix"],
        },
    )
    assert resp.status_code == 422


async def test_audit_row_written_for_site_creation(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "branch-1", "mgmt_host": "10.0.0.1", "username": "admin"},
    )

    from sqlalchemy import select

    from app.models import AuditEvent

    async with maker() as s:
        actions = [e.action for e in await s.scalars(select(AuditEvent))]
    assert "site.create" in actions
    assert "auth.login" in actions


# -- login throttling, end to end -------------------------------------------


@pytest.fixture(autouse=True)
def _clean_throttle():
    """The throttle is process-wide; without this, tests leak lockouts into
    each other and fail in an order-dependent way."""
    from app.services.throttle import get_throttle

    get_throttle().reset()
    yield
    get_throttle().reset()


async def test_repeated_failures_are_locked_out(api) -> None:
    """Unlimited guessing against the service that holds every router
    credential is not acceptable."""
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")

    from app.services.throttle import get_throttle

    limit = get_throttle().max_attempts

    for _ in range(limit):
        resp = await client.post(
            "/auth/login", json={"email": "admin@example.com", "password": "wrong"}
        )
        assert resp.status_code == 401

    blocked = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "wrong"}
    )
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers

    # Even the correct password is refused while locked out -- otherwise the
    # lockout would only slow down an attacker who is already wrong.
    correct = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "correct-horse"}
    )
    assert correct.status_code == 429


async def test_a_success_before_the_limit_clears_the_count(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")

    await client.post("/auth/login", json={"email": "admin@example.com", "password": "no"})
    await client.post("/auth/login", json={"email": "admin@example.com", "password": "no"})
    ok = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "correct-horse"}
    )
    assert ok.status_code == 200

    # The counter reset, so a fresh mistake is still just a 401.
    again = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "no"}
    )
    assert again.status_code == 401


async def test_lockout_is_recorded_in_the_audit_trail(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")

    from app.services.throttle import get_throttle

    for _ in range(get_throttle().max_attempts + 1):
        await client.post(
            "/auth/login", json={"email": "admin@example.com", "password": "wrong"}
        )

    from sqlalchemy import select

    from app.models import AuditEvent

    async with maker() as s:
        actions = [e.action for e in await s.scalars(select(AuditEvent))]

    assert "auth.login.failed" in actions
    assert "auth.login.throttled" in actions
