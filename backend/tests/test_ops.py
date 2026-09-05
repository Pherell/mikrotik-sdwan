"""Drift detection, portable intent, metrics, and the RouterOS 6 driver."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.ros6_ssh import _quote
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Base, User
from app.models.enums import Role
from app.security import hash_password
from tests.fakeros.server import FakeRouterOS

MENUS: dict[str, list] = {
    "interface/bridge": [],
    "interface/gre": [],
    "ip/address": [],
    "ip/firewall/address-list": [],
    "ip/firewall/mangle": [],
    "ip/ipsec/profile": [],
    "ip/ipsec/proposal": [],
    "ip/ipsec/peer": [],
    "ip/ipsec/identity": [],
    "ip/ipsec/policy": [],
    "routing/bgp/template": [],
    "routing/bgp/connection": [],
    "routing/bgp/network": [],
    "routing/table": [],
    "ip/route": [],
    "tool/netwatch": [],
    "system/scheduler": [],
}


@pytest.fixture
def ros() -> FakeRouterOS:
    return FakeRouterOS(password="secret", menus={k: list(v) for k, v in MENUS.items()})


@pytest.fixture
async def api(ros, monkeypatch) -> AsyncIterator[tuple]:
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

    @asynccontextmanager
    async def fake(_site, _box=None):
        d = Ros7RestDriver(
            "fake", "admin", "secret", transport=httpx.ASGITransport(app=ros.app)
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.services.reconcile.open_driver", fake)
    monkeypatch.setattr("app.services.drift.open_driver", fake)

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with maker() as s:
        s.add(
            User(
                email="admin@example.com",
                role=Role.admin,
                password_hash=hash_password("correct-horse"),
            )
        )
        await s.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        yield client, maker, ros
    await engine.dispose()


async def _auth(client) -> dict[str, str]:
    resp = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "correct-horse"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _applied_site(client, headers, **overrides) -> str:
    body = {
        "name": "branch-1",
        "mgmt_host": "203.0.113.10",
        "username": "admin",
        "password": "secret",
        "loopback_ip": "10.254.0.7",
        "local_prefixes": ["192.168.10.0/24"],
        **overrides,
    }
    site_id = (await client.post("/sites", headers=headers, json=body)).json()["id"]
    await client.post(f"/sites/{site_id}/apply", headers=headers, json={"confirm": True})
    return site_id


# -- drift ------------------------------------------------------------------


async def test_a_matching_device_is_not_drifted(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers)

    job = (await client.post(f"/sites/{site_id}/drift", headers=headers)).json()

    assert job["state"] == "succeeded"
    assert job["result"]["drifted"] is False
    site = (await client.get(f"/sites/{site_id}", headers=headers)).json()
    assert site["status"] == "reachable"


async def test_an_edited_device_is_flagged_but_not_corrected(api) -> None:
    """The default is to tell someone, not to silently revert their work."""
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers)

    # Someone logs in and turns STP on.
    ros.rows("interface/bridge")[0]["protocol-mode"] = "rstp"

    job = (await client.post(f"/sites/{site_id}/drift", headers=headers)).json()

    assert job["result"]["drifted"] is True
    assert job["result"]["action"] == "alert"
    assert "protocol-mode" in job["diff"]["text"]
    # Still edited: alert mode changes nothing.
    assert ros.rows("interface/bridge")[0]["protocol-mode"] == "rstp"

    site = (await client.get(f"/sites/{site_id}", headers=headers)).json()
    assert site["status"] == "drifted"
    assert "Drifted from intent" in site["last_error"]


async def test_auto_remediate_puts_it_back(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers, drift_action="auto-remediate")
    ros.rows("interface/bridge")[0]["protocol-mode"] = "rstp"

    job = (await client.post(f"/sites/{site_id}/drift", headers=headers)).json()

    assert job["result"]["drifted"] is True
    assert job["result"]["remediation_job"]
    assert ros.rows("interface/bridge")[0]["protocol-mode"] == "none"


async def test_a_deleted_row_is_drift_too(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers)
    ros.menus["ip/firewall/address-list"].clear()

    job = (await client.post(f"/sites/{site_id}/drift", headers=headers)).json()

    assert job["result"]["drifted"] is True
    assert job["result"]["changes"]["add"] == 1


async def test_hand_built_config_is_never_reported_as_drift(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers)
    ros.rows("interface/bridge").append(
        {".id": "*90", "name": "bridge-lan", "comment": "operator built"}
    )

    job = (await client.post(f"/sites/{site_id}/drift", headers=headers)).json()

    assert job["result"]["drifted"] is False


async def test_drift_recovers_the_status_when_the_edit_is_undone(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers)
    ros.rows("interface/bridge")[0]["protocol-mode"] = "rstp"
    await client.post(f"/sites/{site_id}/drift", headers=headers)

    ros.rows("interface/bridge")[0]["protocol-mode"] = "none"
    await client.post(f"/sites/{site_id}/drift", headers=headers)

    site = (await client.get(f"/sites/{site_id}", headers=headers)).json()
    assert site["status"] == "reachable"
    assert site["last_error"] is None


async def test_unprovisioned_sites_are_skipped_in_a_sweep(api) -> None:
    """Everything is 'missing' on a device that was never applied, which is not
    drift."""
    client, _, _ = api
    headers = await _auth(client)
    await client.post(
        "/sites",
        headers=headers,
        json={"name": "never-applied", "mgmt_host": "10.0.0.9", "username": "admin"},
    )
    await _applied_site(client, headers)

    jobs = (await client.post("/drift", headers=headers)).json()

    assert len(jobs) == 1


# -- portable intent --------------------------------------------------------


async def test_export_round_trips_through_yaml(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers)
    await client.post(
        "/fabrics",
        headers=headers,
        json={"name": "core", "topology": "hub_spoke", "member_site_ids": [site_id]},
    )
    await client.post(
        "/policies", headers=headers, json={"name": "voice", "prefer_tags": ["wan1"]}
    )

    resp = await client.get("/intent/export", headers=headers)

    assert resp.status_code == 200
    doc = yaml.safe_load(resp.text)
    assert doc["version"] == 1
    assert [s["name"] for s in doc["sites"]] == ["branch-1"]
    assert doc["fabrics"][0]["members"] == ["branch-1"]
    assert doc["policies"][0]["name"] == "voice"


async def test_export_never_contains_credentials(api) -> None:
    """The file is meant to go in git. Credentials are environment-specific and
    belong in a secret store."""
    client, _, _ = api
    headers = await _auth(client)
    await client.post(
        "/sites",
        headers=headers,
        json={
            "name": "branch-2",
            "mgmt_host": "10.0.0.2",
            "username": "admin",
            "password": "zebra-canyon-91",
        },
    )

    resp = await client.get("/intent/export", headers=headers)

    assert "zebra-canyon-91" not in resp.text
    assert "password" not in resp.text
    assert "secrets_enc" not in resp.text


async def test_export_omits_links_because_they_are_derived(api) -> None:
    """Exporting them would let a file and its own fabric disagree."""
    client, _, _ = api
    headers = await _auth(client)
    site_id = await _applied_site(client, headers)
    await client.post(
        "/fabrics", headers=headers, json={"name": "core", "member_site_ids": [site_id]}
    )

    doc = yaml.safe_load((await client.get("/intent/export", headers=headers)).text)

    assert "links" not in doc
    assert "links" not in doc["fabrics"][0]


async def test_import_defaults_to_a_dry_run(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    document = yaml.safe_dump(
        {
            "version": 1,
            "sites": [
                {
                    "name": "imported",
                    "mgmt_host": "10.9.9.9",
                    "username": "admin",
                    "role": "spoke",
                }
            ],
        }
    )

    resp = await client.post("/intent/import", headers=headers, content=document)

    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert resp.json()["sites"] == 1
    # Nothing was actually created.
    names = {s["name"] for s in (await client.get("/sites", headers=headers)).json()}
    assert "imported" not in names


async def test_import_creates_sites_and_warns_about_credentials(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    document = yaml.safe_dump(
        {
            "version": 1,
            "sites": [
                {
                    "name": "imported",
                    "mgmt_host": "10.9.9.9",
                    "username": "admin",
                    "role": "spoke",
                    "wans": [{"name": "wan1", "interface": "ether1"}],
                }
            ],
        }
    )

    resp = await client.post(
        "/intent/import", headers=headers, content=document, params={"dry_run": "false"}
    )

    assert resp.json()["sites"] == 1
    assert any("credentials" in w for w in resp.json()["warnings"])
    sites = (await client.get("/sites", headers=headers)).json()
    imported = next(s for s in sites if s["name"] == "imported")
    assert imported["has_credentials"] is False
    assert len(imported["wans"]) == 1


async def test_import_never_deletes_what_the_document_omits(api) -> None:
    """A partial document is the normal case; treating omissions as deletions
    would make sharing one fabric destructive."""
    client, _, _ = api
    headers = await _auth(client)
    await _applied_site(client, headers)

    await client.post(
        "/intent/import",
        headers=headers,
        params={"dry_run": "false"},
        content=yaml.safe_dump(
            {
                "version": 1,
                "sites": [
                    {"name": "other", "mgmt_host": "10.1.1.1", "username": "admin"}
                ],
            }
        ),
    )

    names = {s["name"] for s in (await client.get("/sites", headers=headers)).json()}
    assert names == {"branch-1", "other"}


async def test_import_rejects_an_unknown_schema_version(api) -> None:
    client, _, _ = api
    headers = await _auth(client)

    resp = await client.post(
        "/intent/import", headers=headers, content=yaml.safe_dump({"version": 99})
    )

    assert resp.status_code == 400
    assert "schema version" in resp.json()["detail"]


async def test_import_rejects_garbage(api) -> None:
    client, _, _ = api
    headers = await _auth(client)

    resp = await client.post("/intent/import", headers=headers, content="[1, 2, 3]")

    assert resp.status_code == 400


# -- metrics ----------------------------------------------------------------


async def test_metrics_expose_the_numbers_worth_alerting_on(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    await _applied_site(client, headers)

    resp = await client.get("/metrics")

    assert resp.status_code == 200
    body = resp.text
    assert "sdwan_sites_drifted" in body
    assert "sdwan_rollbacks_armed" in body
    assert 'sdwan_sites{status="reachable"} 1' in body


async def test_metrics_do_not_leak_the_topology(api) -> None:
    """Unauthenticated by design, so it must carry counts and states only."""
    client, _, _ = api
    headers = await _auth(client)
    await _applied_site(client, headers)

    body = (await client.get("/metrics")).text

    assert "branch-1" not in body
    assert "203.0.113.10" not in body


# -- RouterOS 6 console quoting ---------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("simple", '"simple"'),
        ("", '""'),
        (1400, '"1400"'),
        (True, '"true"'),
        ("with space", '"with space"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
    ],
)
def test_console_quoting(value: object, expected: str) -> None:
    assert _quote(value) == expected


def test_quoting_blocks_command_injection() -> None:
    """An unquoted semicolon would end the command and start another one."""
    quoted = _quote('x"; /user add name=evil password=evil; :put "')

    assert quoted.startswith('"') and quoted.endswith('"')
    # Every inner quote is escaped, so nothing closes the string early.
    assert quoted.count('"') - quoted.count('\\"') == 2


def test_v6_capabilities_are_reported_honestly() -> None:
    """The planner refuses a WireGuard fabric up front only because the driver
    admits RouterOS 6 cannot do it."""
    from app.drivers.base import DeviceCaps

    caps = DeviceCaps(ros_major=6, version="6.49.10", has_rest=False)

    assert caps.supports_transport("wireguard") is False
    assert caps.supports_transport("vxlan") is False
    assert caps.supports_transport("ipsec_gre") is True
