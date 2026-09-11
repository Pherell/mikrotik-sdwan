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


async def _site_with_uplink(client, token, name, host, public_ip):
    return await client.post(
        "/sites",
        headers=_auth(token),
        json={
            "name": name,
            "mgmt_host": host,
            "username": "sdwan",
            "password": "device-secret",
            "wans": [{"name": "isp", "interface": "ether1", "public_ip": public_ip}],
        },
    )


async def test_two_sites_cannot_claim_the_same_public_ip(api) -> None:
    """A tunnel endpoint is one address on one router. Accepting the same one
    twice produced a fabric that dialled an address answering as somebody else,
    and only surfaced much later as "neither end is publicly reachable"."""
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    first = await _site_with_uplink(client, token, "router-a", "203.0.113.1", "198.51.100.7")
    assert first.status_code == 201, first.text

    clash = await _site_with_uplink(client, token, "router-b", "203.0.113.2", "198.51.100.7")

    assert clash.status_code == 409
    detail = clash.json()["detail"]
    assert "198.51.100.7" in detail and "router-a/isp" in detail


async def test_one_site_cannot_give_two_uplinks_the_same_public_ip(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    resp = await client.post(
        "/sites",
        headers=_auth(token),
        json={
            "name": "branch",
            "mgmt_host": "203.0.113.9",
            "username": "sdwan",
            "password": "device-secret",
            "wans": [
                {"name": "isp-1", "interface": "ether1", "public_ip": "198.51.100.8"},
                {"name": "isp-2", "interface": "ether2", "public_ip": "198.51.100.8"},
            ],
        },
    )

    assert resp.status_code == 409
    assert "198.51.100.8" in resp.json()["detail"]


async def test_patching_an_uplink_onto_a_taken_public_ip_is_refused(api) -> None:
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    taken = await _site_with_uplink(client, token, "router-a", "203.0.113.1", "198.51.100.7")
    mine = await _site_with_uplink(client, token, "router-b", "203.0.113.2", "198.51.100.9")
    assert taken.status_code == 201 and mine.status_code == 201

    site_id = mine.json()["id"]
    wan_id = mine.json()["wans"][0]["id"]
    resp = await client.patch(
        f"/sites/{site_id}/wans/{wan_id}",
        headers=_auth(token),
        json={"public_ip": "198.51.100.7"},
    )
    assert resp.status_code == 409


async def test_an_uplink_can_keep_its_own_public_ip_on_update(api) -> None:
    """The check must not trip over the row it is updating."""
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    made = await _site_with_uplink(client, token, "router-a", "203.0.113.1", "198.51.100.7")
    site_id = made.json()["id"]
    wan_id = made.json()["wans"][0]["id"]

    resp = await client.patch(
        f"/sites/{site_id}/wans/{wan_id}",
        headers=_auth(token),
        json={"public_ip": "198.51.100.7", "cost": 5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cost"] == 5


async def test_uplinks_behind_nat_may_share_an_empty_public_ip(api) -> None:
    """Two routers behind one NAT is a real shape: both dial out, neither can
    be dialled. That is expressed as no public IP, and must stay allowed."""
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    a = await _site_with_uplink(client, token, "router-a", "203.0.113.1", None)
    b = await _site_with_uplink(client, token, "router-b", "203.0.113.2", None)

    assert a.status_code == 201 and b.status_code == 201


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


# -- the audit trail, read back ---------------------------------------------


async def test_audit_lists_what_happened_newest_first(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")

    await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "one", "mgmt_host": "10.0.0.1", "username": "admin",
              "password": "pw", "local_prefixes": ["10.1.0.0/24"]},
    )
    await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "two", "mgmt_host": "10.0.0.2", "username": "admin",
              "password": "pw", "local_prefixes": ["10.2.0.0/24"]},
    )

    resp = await client.get("/audit", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    creates = [r for r in rows if r["action"] == "site.create"]
    assert len(creates) == 2
    assert creates[0]["detail"]["name"] == "two"  # newest first
    assert creates[0]["actor_email"] == "admin@example.com"


async def test_the_audit_trail_is_admin_only(api) -> None:
    """It reports on people, not devices: source addresses, and -- because
    failed logins are audited -- which email addresses exist. An operator does
    not need that, and a viewer with it has an account-enumeration endpoint."""
    client, maker = api
    await _seed(maker, Role.operator, "op@example.com")
    token = await _token(client, "op@example.com")

    assert (await client.get("/audit", headers=_auth(token))).status_code == 403
    assert (await client.get("/audit/actions", headers=_auth(token))).status_code == 403


async def test_audit_can_be_filtered_to_one_object(api) -> None:
    """The question people actually ask is "who touched this site", not "show
    me everything"."""
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")

    first = await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "one", "mgmt_host": "10.0.0.1", "username": "admin",
              "password": "pw", "local_prefixes": ["10.1.0.0/24"]},
    )
    await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "two", "mgmt_host": "10.0.0.2", "username": "admin",
              "password": "pw", "local_prefixes": ["10.2.0.0/24"]},
    )
    site_id = first.json()["id"]

    resp = await client.get(f"/audit?object_id={site_id}", headers=_auth(token))

    assert resp.status_code == 200
    rows = resp.json()
    assert rows
    assert all(r["object_id"] == site_id for r in rows)


async def test_the_action_filter_list_comes_from_the_data(api) -> None:
    """A hardcoded list of action names goes stale the first time an endpoint
    is added, and a stale filter hides events rather than failing."""
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")
    await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": "one", "mgmt_host": "10.0.0.1", "username": "admin",
              "password": "pw", "local_prefixes": ["10.1.0.0/24"]},
    )

    resp = await client.get("/audit/actions", headers=_auth(token))

    assert resp.status_code == 200
    actions = resp.json()
    assert "site.create" in actions
    assert actions == sorted(actions)
    assert len(actions) == len(set(actions))


async def test_a_failed_login_is_audited_without_storing_the_password(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    await client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "hunter2-is-wrong"},
    )
    token = await _token(client, "admin@example.com")

    resp = await client.get("/audit", headers=_auth(token))

    rows = resp.json()
    serialised = str(rows)
    assert "hunter2-is-wrong" not in serialised
    assert any("login" in r["action"] for r in rows)


# -- the API's own documentation --------------------------------------------


async def test_the_reference_is_served_under_the_proxied_prefix(api) -> None:
    """Caddy proxies /api/* to this service and everything else to the UI, so
    the default /docs and /openapi.json were served by the container and
    reachable by nobody."""
    client, _ = api

    schema = await client.get("/openapi.json")
    docs = await client.get("/docs")

    assert schema.status_code == 200, schema.text
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    assert "/api/v1/api-tokens" in schema.json()["paths"]


# -- deleting a device ------------------------------------------------------
#
# The only delete test here used to be the RBAC one, which 403s before it
# reaches the endpoint body. So the body was never executed by any test, and
# it raised MissingGreenlet on every single call: the guard reads
# site.memberships, which was a lazy relationship, and a lazy load inside an
# async request is not allowed. Deleting a device was impossible.


async def _make_site(client, token, name="edge", host="10.0.0.1", prefix="10.1.0.0/24"):
    resp = await client.post(
        "/sites",
        headers=_auth(token),
        json={"name": name, "mgmt_host": host, "username": "admin",
              "password": "pw", "local_prefixes": [prefix]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_an_admin_can_actually_delete_a_device(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")
    site_id = await _make_site(client, token)

    resp = await client.delete(f"/sites/{site_id}", headers=_auth(token))

    assert resp.status_code == 204, resp.text
    assert (await client.get(f"/sites/{site_id}", headers=_auth(token))).status_code == 404


async def test_deleting_a_device_takes_its_uplinks_with_it(api) -> None:
    """The uplinks belong to the device. Leaving them behind would leave rows
    that no longer name anything."""
    from sqlalchemy import func, select

    from app.models import Wan

    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")
    site_id = await _make_site(client, token)
    await client.post(
        f"/sites/{site_id}/wans",
        headers=_auth(token),
        json={"name": "wan1", "interface": "ether1", "public_ip": "203.0.113.1"},
    )

    assert (await client.delete(f"/sites/{site_id}", headers=_auth(token))).status_code == 204

    async with maker() as s:
        left = await s.scalar(select(func.count()).select_from(Wan))
    assert left == 0


async def test_deleting_a_device_is_audited(api) -> None:
    from sqlalchemy import select

    from app.models import AuditEvent

    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")
    site_id = await _make_site(client, token, name="doomed")

    await client.delete(f"/sites/{site_id}", headers=_auth(token))

    async with maker() as s:
        events = [e for e in await s.scalars(select(AuditEvent)) if e.action == "site.delete"]
    assert len(events) == 1
    assert events[0].detail["name"] == "doomed"


async def test_deleting_a_device_that_is_not_there_is_a_404(api) -> None:
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")

    resp = await client.delete("/sites/no-such-site", headers=_auth(token))

    assert resp.status_code == 404


# -- recovering from a changed device certificate ---------------------------
#
# RouterOS regenerates its certificate on a reset, a re-key, and some upgrades.
# The controller pins the identity on first contact and refuses anything else
# afterwards, which is the point -- but the error told operators to "clear the
# pin on the site", and nothing in the UI could do that. An instruction that
# cannot be followed is not a recovery path.


async def test_the_pinned_identity_is_visible(api) -> None:
    """It is compared against the router by eye, so it has to be readable."""
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")
    site_id = await _make_site(client, token)

    async with maker() as s:
        from app.models import Site

        site = await s.get(Site, site_id)
        site.tls_fingerprint = "AA:BB:CC"
        site.ssh_host_key = "ssh-ed25519 AAAA"
        await s.commit()

    body = (await client.get(f"/sites/{site_id}", headers=_auth(token))).json()

    assert body["tls_fingerprint"] == "AA:BB:CC"
    # The key itself is long and only its presence is actionable.
    assert body["has_ssh_host_key"] is True
    assert "ssh_host_key" not in body


async def test_forgetting_the_identity_clears_both_halves(api) -> None:
    """One call, because "clear both pins" is the whole operation -- spelling
    it out at each call site is how one of them ends up clearing only TLS, and
    the SSH half then refuses the next connection on its own."""
    client, maker = api
    await _seed(maker, Role.admin, "admin@example.com")
    token = await _token(client, "admin@example.com")
    site_id = await _make_site(client, token)

    from app.models import Site

    async with maker() as s:
        site = await s.get(Site, site_id)
        site.tls_fingerprint = "AA:BB:CC"
        site.ssh_host_key = "ssh-ed25519 AAAA"
        await s.commit()

    resp = await client.patch(
        f"/sites/{site_id}",
        headers=_auth(token),
        json={"tls_fingerprint": None, "ssh_host_key": None},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["tls_fingerprint"] is None
    assert resp.json()["has_ssh_host_key"] is False

    async with maker() as s:
        site = await s.get(Site, site_id)
        assert site.tls_fingerprint is None
        assert site.ssh_host_key is None

