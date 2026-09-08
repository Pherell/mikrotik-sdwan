"""API tokens: a credential that is not a person.

The properties worth holding onto are the ones that stop a token becoming a
way around the permission system: it never exceeds its owner's role, it stops
working the moment it is revoked or expires, it cannot mint another token, and
the usable value exists in exactly one HTTP response and nowhere else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import create_app
from app.models import ApiToken, AuditEvent, Base, User
from app.models.enums import Role
from app.security import hash_password


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


async def _user(maker, role: Role, email: str) -> User:
    async with maker() as s:
        user = User(email=email, role=role, password_hash=hash_password("correct-horse"))
        s.add(user)
        await s.commit()
        return user


async def _login(client, email: str) -> dict[str, str]:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mint(client, headers, **body) -> dict:
    payload = {"name": "ci", "role": "operator", **body}
    resp = await client.post("/api-tokens", headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# -- shape ------------------------------------------------------------------


async def test_the_credential_is_returned_once_and_never_again(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")

    created = await _mint(client, headers, name="ci-deploy")
    assert created["token"].startswith("sdwan_")
    assert created["prefix"] in created["token"]

    listed = (await client.get("/api-tokens", headers=headers)).json()
    assert len(listed) == 1
    # The list carries the public half and nothing else.
    assert "token" not in listed[0]
    assert listed[0]["prefix"] == created["prefix"]


async def test_the_secret_half_is_never_stored(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers)

    async with maker() as s:
        row = await s.scalar(select(ApiToken))
    assert row is not None
    secret = created["token"].split("_", 2)[2]
    assert secret not in row.token_hash
    assert row.token_hash != secret
    # SHA-256 hex.
    assert len(row.token_hash) == 64


async def test_minting_is_audited_without_the_credential(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, name="ci-deploy")

    async with maker() as s:
        events = list(await s.scalars(select(AuditEvent)))
    minted = [e for e in events if e.action == "token.create"]
    assert len(minted) == 1
    assert minted[0].detail["name"] == "ci-deploy"
    assert created["token"] not in str(minted[0].detail)


# -- authenticating with one ------------------------------------------------


async def test_a_token_authenticates_like_a_login(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, role="viewer")

    resp = await client.get("/sites", headers=_bearer(created["token"]))

    assert resp.status_code == 200


async def test_a_token_cannot_exceed_the_role_it_was_given(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, role="viewer")

    resp = await client.post(
        "/sites",
        headers=_bearer(created["token"]),
        json={"name": "one", "mgmt_host": "10.0.0.1", "username": "admin",
              "password": "pw", "local_prefixes": ["10.1.0.0/24"]},
    )

    assert resp.status_code == 403
    # The message says which of the two limits bit, because "you are viewer"
    # is baffling when you signed in as an admin.
    assert "API token" in resp.json()["detail"]


async def test_a_token_weakens_when_its_owner_is_demoted(api) -> None:
    """A token must never be a way to keep rights you have lost. The effective
    permission is the lesser of the two, so demoting the person is enough --
    nobody has to remember to go and revoke anything."""
    client, maker = api
    owner = await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, role="admin")

    body = {"name": "one", "mgmt_host": "10.0.0.1", "username": "admin",
            "password": "pw", "local_prefixes": ["10.1.0.0/24"]}
    assert (
        await client.post("/sites", headers=_bearer(created["token"]), json=body)
    ).status_code == 201

    async with maker() as s:
        user = await s.get(User, owner.id)
        user.role = Role.viewer
        await s.commit()

    body["name"] = "two"
    body["mgmt_host"] = "10.0.0.2"
    body["local_prefixes"] = ["10.2.0.0/24"]
    resp = await client.post("/sites", headers=_bearer(created["token"]), json=body)
    assert resp.status_code == 403


async def test_a_revoked_token_stops_working(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers)

    assert (
        await client.get("/sites", headers=_bearer(created["token"]))
    ).status_code == 200

    resp = await client.delete(f"/api-tokens/{created['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["revoked_at"] is not None

    assert (
        await client.get("/sites", headers=_bearer(created["token"]))
    ).status_code == 401


async def test_a_revoked_token_stays_in_the_list(api) -> None:
    """It stays in the audit trail, so it has to stay nameable."""
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, name="old-ci")
    await client.delete(f"/api-tokens/{created['id']}", headers=headers)

    listed = (await client.get("/api-tokens", headers=headers)).json()

    assert [t["name"] for t in listed] == ["old-ci"]
    assert listed[0]["revoked_at"] is not None


async def test_an_expired_token_is_refused(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, expires_in_days=1)

    async with maker() as s:
        token = await s.get(ApiToken, created["id"])
        token.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await s.commit()

    resp = await client.get("/sites", headers=_bearer(created["token"]))
    assert resp.status_code == 401


async def test_a_wrong_credential_says_only_that_it_is_wrong(api) -> None:
    """Not whether the token exists, expired, or was revoked -- each of those
    is a fact about this installation."""
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers)
    await client.delete(f"/api-tokens/{created['id']}", headers=headers)

    revoked = await client.get("/sites", headers=_bearer(created["token"]))
    unknown = await client.get(
        "/sites", headers=_bearer("sdwan_deadbeefcafe_notarealsecretatall")
    )
    wrong_secret = await client.get(
        "/sites", headers=_bearer(f"sdwan_{created['prefix']}_wrong")
    )

    details = {r.json()["detail"] for r in (revoked, unknown, wrong_secret)}
    assert details == {"Invalid API token"}


async def test_a_login_jwt_still_works(api) -> None:
    """Tokens and JWTs share one header, told apart by shape. A value that is
    not token-shaped must fall through rather than failing at the split."""
    client, maker = api
    await _user(maker, Role.viewer, "viewer@example.com")
    headers = await _login(client, "viewer@example.com")

    assert (await client.get("/sites", headers=headers)).status_code == 200


async def test_use_is_recorded_so_a_token_can_be_retired_safely(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers)
    assert created["last_used_at"] is None

    await client.get("/sites", headers=_bearer(created["token"]))

    listed = (await client.get("/api-tokens", headers=headers)).json()
    assert listed[0]["last_used_at"] is not None


async def test_the_audit_trail_says_which_credential_acted(api) -> None:
    """"admin@example.com did this" is not the whole answer once automation
    exists."""
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, name="ci-deploy", role="operator")

    await client.post(
        "/sites",
        headers=_bearer(created["token"]),
        json={"name": "one", "mgmt_host": "10.0.0.1", "username": "admin",
              "password": "pw", "local_prefixes": ["10.1.0.0/24"]},
    )

    async with maker() as s:
        events = list(await s.scalars(select(AuditEvent)))
    created_site = [e for e in events if e.action == "site.create"]
    assert len(created_site) == 1
    assert created_site[0].detail["via_token"] == "ci-deploy"
    assert created_site[0].actor_email == "admin@example.com"


# -- managing them ----------------------------------------------------------


async def test_a_token_cannot_mint_another_token(api) -> None:
    """A token that mints tokens is a token that outlives its own revocation."""
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, role="admin")

    resp = await client.post(
        "/api-tokens",
        headers=_bearer(created["token"]),
        json={"name": "second", "role": "admin"},
    )

    assert resp.status_code == 403
    assert "cannot create" in resp.json()["detail"]


async def test_managing_tokens_is_admin_only(api) -> None:
    client, maker = api
    await _user(maker, Role.operator, "op@example.com")
    headers = await _login(client, "op@example.com")

    assert (await client.get("/api-tokens", headers=headers)).status_code == 403
    assert (
        await client.post("/api-tokens", headers=headers, json={"name": "x"})
    ).status_code == 403


async def test_only_the_name_can_be_changed(api) -> None:
    """Changing a role in place would silently widen a credential already
    sitting in somebody's CI configuration."""
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers, name="old", role="viewer")

    resp = await client.patch(
        f"/api-tokens/{created['id']}",
        headers=headers,
        json={"name": "new", "role": "admin"},
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "new"
    assert resp.json()["role"] == "viewer"


async def test_revoking_twice_keeps_the_first_timestamp(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")
    created = await _mint(client, headers)

    first = (await client.delete(f"/api-tokens/{created['id']}", headers=headers)).json()
    second = (await client.delete(f"/api-tokens/{created['id']}", headers=headers)).json()

    assert first["revoked_at"] == second["revoked_at"]


async def test_revoking_a_token_that_does_not_exist_is_a_404(api) -> None:
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")

    resp = await client.delete("/api-tokens/does-not-exist", headers=headers)

    assert resp.status_code == 404


async def test_every_timestamp_carries_a_timezone(api) -> None:
    """SQLite has no timezone storage, so a row read back is naive while the
    same row still in the session is aware. Serialised, that is the difference
    between a UTC instant and one a client will read as local time -- and two
    responses in the same second disagreed about it."""
    client, maker = api
    await _user(maker, Role.admin, "admin@example.com")
    headers = await _login(client, "admin@example.com")

    fresh = await _mint(client, headers)  # straight out of the session
    listed = (await client.get("/api-tokens", headers=headers)).json()[0]  # read back

    assert fresh["created_at"].endswith("Z")
    assert listed["created_at"].endswith("Z")
    assert fresh["expires_at"].endswith("Z")
    assert listed["expires_at"].endswith("Z")
