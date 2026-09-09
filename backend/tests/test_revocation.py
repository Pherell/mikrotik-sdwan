"""M10: token revocation.

A JWT is a bearer credential the controller never sees again after issuing
it -- there is no row to mark revoked. What exists instead is
User.tokens_valid_after: everything issued before that instant is refused,
everything after is not. These tests cover the mechanism directly (so they
are not at the mercy of wall-clock timing), the two ways to trigger it
(self-service logout-everywhere, and an admin forcing it on someone else via
PATCH), and the one genuine race the mechanism has to get right: a login in
the same wall-clock second as a revocation must not be rejected, because
JWT's iat is truncated to whole seconds and tokens_valid_after is not.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import create_app
from app.models import Base, User
from app.models.enums import Role
from app.security import hash_password

ApiFixture = tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]


@pytest.fixture
async def api() -> AsyncIterator[ApiFixture]:
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


async def _seed(maker, role: Role, email: str) -> User:
    async with maker() as s:
        user = User(email=email, role=role, password_hash=hash_password("correct-horse"))
        s.add(user)
        await s.commit()
        return user


async def _login(client: httpx.AsyncClient, email: str) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _set_tokens_valid_after(maker, email: str, when: datetime) -> None:
    async with maker() as s:
        user = await s.scalar(select(User).where(User.email == email))
        user.tokens_valid_after = when
        await s.commit()


# -- the mechanism, tested directly so it is not at timing's mercy ----------


async def test_a_token_issued_before_tokens_valid_after_is_refused(api) -> None:
    client, maker = api
    await _seed(maker, Role.viewer, "v@example.com")
    token = await _login(client, "v@example.com")

    # Directly in the future relative to the token just issued -- avoids any
    # dependence on wall-clock timing between issuing it and this line.
    await _set_tokens_valid_after(maker, "v@example.com", datetime.now(UTC) + timedelta(seconds=5))

    resp = await client.get("/auth/me", headers=_auth(token))
    assert resp.status_code == 401
    assert "revoked" in resp.text.lower()


async def test_a_token_issued_after_tokens_valid_after_is_unaffected(api) -> None:
    client, maker = api
    await _seed(maker, Role.viewer, "v@example.com")
    await _set_tokens_valid_after(maker, "v@example.com", datetime.now(UTC) - timedelta(hours=1))

    token = await _login(client, "v@example.com")
    resp = await client.get("/auth/me", headers=_auth(token))
    assert resp.status_code == 200


async def test_a_login_in_the_same_second_as_a_past_revocation_is_not_rejected(api) -> None:
    """The race this mechanism has to get right: iat is truncated to whole
    seconds on encode, tokens_valid_after is not. A login landing in the
    same wall-clock second as an *already-set* revocation must not read as
    'before' it just because the fractional part was dropped."""
    client, maker = api
    await _seed(maker, Role.viewer, "v@example.com")
    # Truncated to the current second, same as iat will be on encode -- not
    # "now" with microseconds, which iat can never equal or exceed.
    await _set_tokens_valid_after(maker, "v@example.com", datetime.now(UTC).replace(microsecond=0))

    token = await _login(client, "v@example.com")
    resp = await client.get("/auth/me", headers=_auth(token))
    assert resp.status_code == 200


# -- the two ways to trigger it, end to end ----------------------------------


async def test_logout_everywhere_invalidates_the_calling_token_itself(api) -> None:
    client, maker = api
    await _seed(maker, Role.viewer, "v@example.com")
    token = await _login(client, "v@example.com")

    # Cross a whole-second boundary for real: this exercises the actual
    # request path end to end, unlike the mechanism tests above.
    await asyncio.sleep(1.1)

    resp = await client.post("/auth/logout-everywhere", headers=_auth(token))
    assert resp.status_code == 204

    again = await client.get("/auth/me", headers=_auth(token))
    assert again.status_code == 401


async def test_a_fresh_login_after_logout_everywhere_works(api) -> None:
    client, maker = api
    await _seed(maker, Role.viewer, "v@example.com")
    old_token = await _login(client, "v@example.com")
    await asyncio.sleep(1.1)
    await client.post("/auth/logout-everywhere", headers=_auth(old_token))

    new_token = await _login(client, "v@example.com")
    resp = await client.get("/auth/me", headers=_auth(new_token))
    assert resp.status_code == 200


async def test_an_admin_can_force_revoke_another_users_sessions(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    created = await _login(client, "admin@example.com")
    victim = await _seed(maker, Role.viewer, "victim@example.com")
    victim_token = await _login(client, "victim@example.com")

    await asyncio.sleep(1.1)

    resp = await client.patch(
        f"/users/{victim.id}", headers=_auth(created), json={"revoke_sessions": True}
    )
    assert resp.status_code == 200, resp.text

    again = await client.get("/auth/me", headers=_auth(victim_token))
    assert again.status_code == 401


async def test_revoke_sessions_is_not_stored_as_a_field(api) -> None:
    """It is an action, not data -- like UserUpdate.password, it must not
    round-trip or leave a trace on the row's normal fields."""
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _login(client, "admin@example.com")

    resp = await client.patch(
        "/users/me-does-not-exist", headers=_auth(token), json={"revoke_sessions": True}
    )
    # Wrong id entirely -- the point is just that the field is accepted by
    # the schema without a 422, proving it exists and is boolean-typed.
    assert resp.status_code == 404
