"""M8 provisioning: one-touch enrollment.

An operator mints a token; a factory-default router fetches a script and
calls back. These tests cover the token lifecycle (mint, fetch, confirm,
single-use, expiry, revocation, source restriction), that the resulting site
is created at the address the callback actually arrived from, and that one
tenant cannot touch another's tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import get_session
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Base, EnrollmentToken, Fabric, Site, User
from app.models.enums import Role, SiteRole, Topology, Transport
from app.security import SecretBox, hash_password
from app.services.enrollment import (
    EnrollmentTokenInvalid,
    confirm_enrollment,
    mint_enrollment_token,
    render_bootstrap_script,
)
from tests.fakeros.server import FakeRouterOS

PUBLIC_URL = "https://10.10.10.179"

MENUS = {
    "system/resource": [{"version": "7.16", "board-name": "CHR", "architecture-name": "x86"}],
    "ip/route": [],
    "ip/address": [],
    "ip/dhcp-client": [],
}


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _mint(db, **overrides) -> tuple[EnrollmentToken, str, str]:
    """Mint, and also return the plaintext device password: nothing else can
    predict it (it is generated inside mint_enrollment_token), and a fake
    device needs to be told the real value to authenticate a probe against."""
    defaults = dict(
        tenant_id="t", created_by=None, name="branch-42", site_name="branch-42",
        site_role=SiteRole.spoke, local_prefixes=["10.9.0.0/24"], fabric_id=None,
        source_cidr=None, expires_in_hours=24,
    )
    defaults.update(overrides)
    async with db() as s:
        token, credential = await mint_enrollment_token(s, **defaults)
        await s.commit()
        password = SecretBox().decrypt(token.device_password_enc)
        return token, credential, password


# -- the script ---------------------------------------------------------------


async def test_the_script_carries_the_generated_password_and_callback_url(
    db: async_sessionmaker[AsyncSession],
) -> None:
    token, credential, _ = await _mint(db)
    async with db() as s:
        script = await render_bootstrap_script(s, credential, "203.0.113.5", PUBLIC_URL)

    async with db() as s:
        row = await s.get(EnrollmentToken, token.id)
        password = SecretBox().decrypt(row.device_password_enc)

    assert password in script
    assert f"https://10.10.10.179/api/v1/enroll/{credential}/confirm" in script
    assert "/ip service set www-ssl" in script
    assert "/ip service set www disabled=yes" in script
    assert "/user add name=sdwan" in script


async def test_fetching_the_script_does_not_spend_the_token(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A network hiccup between fetch and import must be retryable."""
    _, credential, _ = await _mint(db)
    async with db() as s:
        await render_bootstrap_script(s, credential, "203.0.113.5", PUBLIC_URL)
    async with db() as s:
        # A second fetch, same credential: must not have been spent by the first.
        script = await render_bootstrap_script(s, credential, "203.0.113.5", PUBLIC_URL)
    assert script  # did not raise


@pytest.mark.parametrize(
    "credential", ["garbage", "sdwan_notreal_x", "enroll_wrongprefix_x", ""]
)
async def test_a_credential_that_is_not_this_tokens_is_refused(
    db: async_sessionmaker[AsyncSession], credential: str
) -> None:
    async with db() as s:
        with pytest.raises(EnrollmentTokenInvalid):
            await render_bootstrap_script(s, credential, "203.0.113.5", PUBLIC_URL)


async def test_an_expired_token_is_refused(db: async_sessionmaker[AsyncSession]) -> None:
    token, credential, _ = await _mint(db)
    async with db() as s:
        row = await s.get(EnrollmentToken, token.id)
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await s.commit()

    async with db() as s:
        with pytest.raises(EnrollmentTokenInvalid, match="expired"):
            await render_bootstrap_script(s, credential, "203.0.113.5", PUBLIC_URL)


async def test_a_revoked_token_is_refused(db: async_sessionmaker[AsyncSession]) -> None:
    token, credential, _ = await _mint(db)
    async with db() as s:
        row = await s.get(EnrollmentToken, token.id)
        row.revoked_at = datetime.now(UTC)
        await s.commit()

    async with db() as s:
        with pytest.raises(EnrollmentTokenInvalid, match="revoked"):
            await render_bootstrap_script(s, credential, "203.0.113.5", PUBLIC_URL)


async def test_source_cidr_scopes_which_address_may_use_the_token(
    db: async_sessionmaker[AsyncSession],
) -> None:
    _, credential, _ = await _mint(db, source_cidr="203.0.113.0/24")

    async with db() as s:
        with pytest.raises(EnrollmentTokenInvalid, match="scoped"):
            await render_bootstrap_script(s, credential, "198.51.100.1", PUBLIC_URL)

    async with db() as s:
        # Inside the range: must succeed.
        script = await render_bootstrap_script(s, credential, "203.0.113.9", PUBLIC_URL)
    assert script


# -- confirm --------------------------------------------------------------


def _patch_probe_driver(monkeypatch, fake: FakeRouterOS) -> None:
    """Same credential derivation as the real drivers.factory.build_driver:
    the fake device must be told the site's *actual* stored password, or
    every probe fails auth and the reachable-only paths (WAN discovery,
    fabric join) never run."""

    @asynccontextmanager
    async def fake_open_driver(site, box=None):
        box = box or SecretBox()
        password = box.decrypt(site.password_enc) if site.password_enc else ""
        d = Ros7RestDriver(
            site.mgmt_host, site.username, password,
            transport=httpx.ASGITransport(app=fake.app),
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.services.probe.open_driver", fake_open_driver)


async def test_confirming_creates_a_site_at_the_callers_own_address(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    token, credential, password = await _mint(db)
    _patch_probe_driver(monkeypatch, FakeRouterOS(username="sdwan", password=password, menus=MENUS))

    async with db() as s:
        site = await confirm_enrollment(s, credential, "203.0.113.5")
        await s.commit()

    assert site.mgmt_host == "203.0.113.5"
    assert site.username == "sdwan"
    assert site.name == "branch-42"
    assert site.local_prefixes == ["10.9.0.0/24"]

    async with db() as s:
        row = await s.get(EnrollmentToken, token.id)
        assert row.used_at is not None
        assert row.used_from_ip == "203.0.113.5"
        assert row.enrolled_site_id == site.id


async def test_the_device_password_the_script_set_is_what_the_site_now_holds(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """The whole point: nobody types this password, and the controller must
    still be able to use it afterward."""
    token, credential, password = await _mint(db)
    async with db() as s:
        script = await render_bootstrap_script(s, credential, "203.0.113.5", PUBLIC_URL)
    assert password in script

    _patch_probe_driver(monkeypatch, FakeRouterOS(username="sdwan", password=password, menus=MENUS))
    async with db() as s:
        site = await confirm_enrollment(s, credential, "203.0.113.5")
        await s.commit()

    assert SecretBox().decrypt(site.password_enc) == password


async def test_confirming_twice_is_refused(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    _, credential, password = await _mint(db)
    _patch_probe_driver(monkeypatch, FakeRouterOS(username="sdwan", password=password, menus=MENUS))

    async with db() as s:
        await confirm_enrollment(s, credential, "203.0.113.5")
        await s.commit()

    async with db() as s:
        with pytest.raises(EnrollmentTokenInvalid, match="already been used"):
            await confirm_enrollment(s, credential, "203.0.113.5")


async def test_confirming_joins_the_named_fabric(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    async with db() as s:
        fabric = Fabric(
            id="fab-1", name="core", tenant_id="t", transport=Transport.ipsec_gre,
            topology=Topology.hub_spoke, asn=65000, mtu=1400,
        )
        s.add(fabric)
        await s.commit()

    _, credential, password = await _mint(db, fabric_id="fab-1")
    _patch_probe_driver(monkeypatch, FakeRouterOS(username="sdwan", password=password, menus=MENUS))

    async with db() as s:
        site = await confirm_enrollment(s, credential, "203.0.113.5")
        await s.commit()

    async with db() as s:
        refreshed = await s.get(Site, site.id)
        assert len(refreshed.memberships) == 1
        assert refreshed.memberships[0].fabric_id == "fab-1"


async def test_a_fabric_apply_failure_does_not_undo_the_enrollment(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """The device really did enroll; a fabric that no longer exists (or an
    apply that fails) is a smaller, separately-fixable problem."""
    _, credential, password = await _mint(db, fabric_id="does-not-exist")
    _patch_probe_driver(monkeypatch, FakeRouterOS(username="sdwan", password=password, menus=MENUS))

    async with db() as s:
        site = await confirm_enrollment(s, credential, "203.0.113.5")
        await s.commit()

    assert site.id  # the site still exists; nothing raised


# -- the HTTP surface ---------------------------------------------------------


ApiFixture = tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]


@pytest.fixture
async def api(monkeypatch) -> AsyncIterator[ApiFixture]:
    monkeypatch.setenv("SDWAN_PUBLIC_URL", PUBLIC_URL)
    get_settings.cache_clear()

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
    get_settings.cache_clear()


async def _seed(maker, role: Role, email: str, tenant_id: str = "default") -> User:
    async with maker() as s:
        user = User(email=email, role=role, password_hash=hash_password("correct-horse"),
                    tenant_id=tenant_id)
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


async def test_creating_a_token_returns_the_paste_line_once(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    admin_token = await _token(client, "admin@example.com")

    resp = await client.post(
        "/enrollment-tokens",
        headers=_auth(admin_token),
        json={"name": "branch-1", "site_name": "branch-1"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "/tool fetch" in body["enroll_command"]
    assert "/import enroll.rsc" in body["enroll_command"]

    listed = (await client.get("/enrollment-tokens", headers=_auth(admin_token))).json()
    assert "enroll_command" not in listed[0]


async def test_creating_a_token_requires_public_url(monkeypatch) -> None:
    monkeypatch.delenv("SDWAN_PUBLIC_URL", raising=False)
    get_settings.cache_clear()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with maker() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        await _seed(maker, Role.admin, "admin@example.com")
        admin_token = await _token(client, "admin@example.com")
        resp = await client.post(
            "/enrollment-tokens", headers=_auth(admin_token),
            json={"name": "x", "site_name": "x"},
        )
    await engine.dispose()
    get_settings.cache_clear()
    assert resp.status_code == 409


async def test_the_full_http_flow_fetch_then_confirm(api, monkeypatch) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    admin_token = await _token(client, "admin@example.com")

    created = await client.post(
        "/enrollment-tokens", headers=_auth(admin_token),
        json={"name": "branch-1", "site_name": "branch-1"},
    )
    fetch_url = created.json()["enroll_command"].split('"')[1]
    credential = fetch_url.rsplit("/", 1)[-1]

    script_resp = await client.get(f"/enroll/{credential}")
    assert script_resp.status_code == 200
    assert "/user add name=sdwan" in script_resp.text

    fake = FakeRouterOS(username="sdwan", password="irrelevant", menus=MENUS)
    _patch_probe_driver(monkeypatch, fake)
    confirm_resp = await client.post(f"/enroll/{credential}/confirm")
    assert confirm_resp.status_code == 200, confirm_resp.text
    site_id = confirm_resp.json()["site_id"]

    sites = (await client.get("/sites", headers=_auth(admin_token))).json()
    assert any(s["id"] == site_id and s["name"] == "branch-1" for s in sites)

    # Single-use: a second confirm on the same credential is refused.
    again = await client.post(f"/enroll/{credential}/confirm")
    assert again.status_code == 404


async def test_a_token_in_another_tenant_cannot_be_revoked(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin-a@example.com", tenant_id="a")
    await _seed(maker, Role.admin, "admin-b@example.com", tenant_id="b")
    token_a = await _token(client, "admin-a@example.com")
    token_b = await _token(client, "admin-b@example.com")

    created = await client.post(
        "/enrollment-tokens", headers=_auth(token_a),
        json={"name": "x", "site_name": "x"},
    )
    token_id = created.json()["id"]

    resp = await client.delete(f"/enrollment-tokens/{token_id}", headers=_auth(token_b))
    assert resp.status_code == 404
