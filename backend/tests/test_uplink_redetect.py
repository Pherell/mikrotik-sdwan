"""Uplink detection beyond ethernet+DHCP, and periodic re-detection.

Detection used to run once, and only understood a default route whose
gateway was an address. A PPPoE or LTE uplink -- a default route whose
gateway is an *interface* -- was lost entirely, a backup uplink whose route
happened to be inactive at probe time was dropped, and nothing ever looked
again after enrollment, so a PPPoE reconnect onto a new address left every
tunnel dialling the old one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.base import DriverError
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Base, Site, User, Wan
from app.models.enums import Role, SiteStatus
from app.models.job import AuditEvent
from app.security import hash_password
from app.services.probe import _suggest_wans, detect_uplinks
from app.services.uplinks import check_site_uplinks, sweep_uplinks
from tests.fakeros.server import FakeRouterOS


async def _detect(menus: dict):
    fake = FakeRouterOS(password="secret", menus=menus)
    async with Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=fake.app)
    ) as d:
        return await detect_uplinks(d)


# -- detection ----------------------------------------------------------------


PPPOE = {
    "ip/route": [
        # The point-to-point shape: no next hop, the interface is the gateway.
        {"dst-address": "0.0.0.0/0", "gateway": "pppoe-out1",
         "immediate-gw": "%pppoe-out1", "distance": 1},
    ],
    "ip/address": [
        # The local end of the PPP link is the WAN address; the "network" is
        # the far end's /32.
        {"address": "198.51.100.77/32", "network": "198.51.100.1",
         "interface": "pppoe-out1"},
    ],
    "interface/pppoe-client": [
        {"name": "pppoe-out1", "interface": "ether1", "add-default-route": True,
         "running": True, "disabled": False},
    ],
}


async def test_a_pppoe_default_route_through_an_interface_is_an_uplink() -> None:
    found = await _detect(PPPOE)

    assert [w.interface for w in found.wans] == ["pppoe-out1"]
    wan = found.wans[0]
    assert wan.public_ip == "198.51.100.77"
    # There is no next-hop address on a PPP link, and inventing one would be
    # worse than saying so.
    assert wan.gateway is None
    assert wan.dynamic is True
    assert wan.nat_behind is False
    assert found.down == set()


async def test_a_bare_interface_gateway_without_immediate_gw_is_still_found() -> None:
    """An inactive route often reports no immediate-gw at all."""
    menus = {
        "ip/route": [{"dst-address": "0.0.0.0/0", "gateway": "sstp-out1", "distance": 3}],
        "ip/address": [{"address": "100.70.1.2/32", "interface": "sstp-out1"}],
    }
    found = await _detect(menus)

    assert [w.interface for w in found.wans] == ["sstp-out1"]
    # CGNAT range: dial-out only.
    assert found.wans[0].nat_behind is True
    assert found.wans[0].public_ip is None


async def test_an_lte_modem_is_a_dynamic_uplink() -> None:
    menus = {
        "ip/route": [
            {"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1",
             "immediate-gw": "203.0.113.1%ether1", "distance": 1},
            {"dst-address": "0.0.0.0/0", "gateway": "lte1", "distance": 10},
        ],
        "ip/address": [
            {"address": "203.0.113.10/24", "interface": "ether1"},
            {"address": "10.64.12.9/32", "interface": "lte1"},
        ],
        "interface/lte": [{"name": "lte1", "running": True, "disabled": False}],
    }
    found = await _detect(menus)

    by_iface = {w.interface: w for w in found.wans}
    assert list(by_iface) == ["ether1", "lte1"]
    assert by_iface["lte1"].dynamic is True
    assert by_iface["lte1"].nat_behind is True
    assert by_iface["ether1"].dynamic is False


async def test_a_down_uplink_is_kept_and_noted() -> None:
    """A backup uplink whose route is inactive right now is still an uplink.
    Dropping it is what made re-detection call it vanished on every outage."""
    menus = {
        "ip/route": [
            {"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1",
             "immediate-gw": "203.0.113.1%ether1", "distance": 1},
            {"dst-address": "0.0.0.0/0", "gateway": "10.10.0.1",
             "immediate-gw": "10.10.0.1%ether2", "distance": 2, "inactive": True},
        ],
        "ip/address": [
            {"address": "203.0.113.10/24", "interface": "ether1"},
            {"address": "10.10.0.2/30", "interface": "ether2"},
        ],
    }
    found = await _detect(menus)

    assert [w.interface for w in found.wans] == ["ether1", "ether2"]
    assert found.down == {"ether2"}
    assert any(n.interface == "ether2" and "down" in n.reason for n in found.notes)


async def test_a_pppoe_session_that_is_down_is_still_an_uplink() -> None:
    """With the session down its dynamic route is gone too; the client
    config is the only thing left that says this is an uplink."""
    menus = {
        "interface/pppoe-client": [
            {"name": "pppoe-out1", "interface": "ether1", "add-default-route": True,
             "running": False, "disabled": False},
        ],
    }
    found = await _detect(menus)

    assert [w.interface for w in found.wans] == ["pppoe-out1"]
    assert found.down == {"pppoe-out1"}
    assert found.wans[0].dynamic is True


async def test_tunnels_and_controller_interfaces_are_never_uplinks() -> None:
    menus = {
        "ip/route": [
            {"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1",
             "immediate-gw": "203.0.113.1%ether1", "distance": 1},
            {"dst-address": "0.0.0.0/0", "gateway": "wg-office", "distance": 5},
            {"dst-address": "0.0.0.0/0", "gateway": "gre-hq", "distance": 6},
            # Excluded by name even with no /interface row to type it.
            {"dst-address": "0.0.0.0/0", "gateway": "sdwan-core-hub1", "distance": 7},
        ],
        "ip/address": [{"address": "203.0.113.10/24", "interface": "ether1"}],
        "interface": [
            {"name": "ether1", "type": "ether", "running": True},
            {"name": "wg-office", "type": "wg", "running": True},
            {"name": "gre-hq", "type": "gre-tunnel", "running": True},
        ],
    }
    found = await _detect(menus)

    assert [w.interface for w in found.wans] == ["ether1"]
    noted = {n.interface for n in found.notes}
    assert {"wg-office", "gre-hq", "sdwan-core-hub1"} <= noted


async def test_an_interface_that_is_not_running_marks_the_uplink_down() -> None:
    menus = {
        "ip/route": [
            {"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1",
             "immediate-gw": "203.0.113.1%ether1", "distance": 1},
        ],
        "ip/address": [{"address": "203.0.113.10/24", "interface": "ether1"}],
        "interface": [{"name": "ether1", "type": "ether", "running": False}],
    }
    found = await _detect(menus)

    assert found.down == {"ether1"}


async def test_the_wizard_shape_is_unchanged(driver: Ros7RestDriver) -> None:
    """_suggest_wans still returns (wans, lan) for enrollment and the wizard."""
    wans, lan = await _suggest_wans(driver)
    assert {w.interface for w in wans} == {"ether1", "ether2"}
    assert lan == []


# -- re-detection ---------------------------------------------------------------


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


# The device as it is now: the PPPoE session reconnected onto a new address,
# ether1 kept its static one but the router shows a different one than the
# operator typed, and a new DHCP uplink (ether3) appeared. ether9 is gone.
NOW = {
    "ip/route": [
        {"dst-address": "0.0.0.0/0", "gateway": "pppoe-out1",
         "immediate-gw": "%pppoe-out1", "distance": 1},
        {"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1",
         "immediate-gw": "203.0.113.1%ether1", "distance": 2},
        {"dst-address": "0.0.0.0/0", "gateway": "192.0.2.1",
         "immediate-gw": "192.0.2.1%ether3", "distance": 3},
    ],
    "ip/address": [
        {"address": "198.51.100.200/32", "interface": "pppoe-out1"},
        {"address": "203.0.113.99/24", "interface": "ether1"},
        {"address": "192.0.2.50/24", "interface": "ether3"},
    ],
    "ip/dhcp-client": [{"interface": "ether3", "gateway": "192.0.2.1"}],
    "interface/pppoe-client": [
        {"name": "pppoe-out1", "interface": "ether2", "running": True},
    ],
}


def _site(site_id: str, *, mode: str, host: str = "10.0.0.1") -> Site:
    site = Site(id=site_id, name=f"branch-{site_id}", mgmt_host=host, username="admin",
                tenant_id="t", status=SiteStatus.reachable, uplink_sync=mode)
    site.wans = [
        Wan(id=f"{site_id}-ppp", name="wan1", interface="pppoe-out1",
            public_ip="198.51.100.77", prefix_len=32, dynamic=True, cost=1.0),
        Wan(id=f"{site_id}-static", name="wan2", interface="ether1",
            public_ip="203.0.113.10", prefix_len=24, gateway="203.0.113.1",
            dynamic=False, cost=2.0),
        Wan(id=f"{site_id}-gone", name="wan3", interface="ether9",
            public_ip="192.0.2.200", dynamic=False, cost=3.0),
    ]
    return site


def _patch_driver(monkeypatch, devices: dict[str, FakeRouterOS | Exception]) -> None:
    """open_driver for services.uplinks, keyed by mgmt_host."""

    @asynccontextmanager
    async def fake_open_driver(site, _box=None):
        device = devices[site.mgmt_host]
        if isinstance(device, Exception):
            raise device
        d = Ros7RestDriver(site.mgmt_host, "admin", "secret",
                           transport=httpx.ASGITransport(app=device.app))
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.services.uplinks.open_driver", fake_open_driver)


async def _run(db, monkeypatch, mode: str):
    _patch_driver(monkeypatch, {"10.0.0.1": FakeRouterOS(password="secret", menus=NOW)})
    async with db() as s:
        s.add(_site("s1", mode=mode))
        await s.commit()
    async with db() as s:
        site = await s.get(Site, "s1")
        result = await check_site_uplinks(s, site)
        await s.commit()
    async with db() as s:
        wans = {w.interface: w for w in await s.scalars(select(Wan))}
        site = await s.get(Site, "s1")
        audit = list(await s.scalars(select(AuditEvent)))
    return result, wans, site, audit


def _kinds(result) -> set[tuple[str, str, str | None]]:
    return {(c.kind, c.interface, c.field) for c in result.changes}


async def test_auto_mode_updates_only_the_dynamic_uplink(db, monkeypatch) -> None:
    result, wans, site, audit = await _run(db, monkeypatch, "auto")

    ppp = wans["pppoe-out1"]
    assert ppp.public_ip == "198.51.100.200"
    changed = [c for c in result.changes if c.interface == "pppoe-out1"]
    assert [(c.field, c.applied) for c in changed] == [("public_ip", True)]

    # Every automatic write is in the audit trail, filed under the site's
    # tenant and attributed to the sweep.
    updates = [a for a in audit if a.action == "wan.auto_update"]
    assert len(updates) == 1
    assert updates[0].tenant_id == "t"
    assert updates[0].detail["to"] == "198.51.100.200"

    # Not pushed -- marked for an apply instead.
    assert result.needs_apply is True
    assert site.status == SiteStatus.drifted
    assert "apply" in (site.last_error or "")


async def test_a_static_uplink_is_reported_not_changed(db, monkeypatch) -> None:
    result, wans, _site_row, audit = await _run(db, monkeypatch, "auto")

    assert wans["ether1"].public_ip == "203.0.113.10"
    static = [c for c in result.changes if c.interface == "ether1"]
    assert len(static) == 1
    assert static[0].field == "public_ip"
    assert static[0].observed == "203.0.113.99"
    assert static[0].applied is False
    assert "Static" in (static[0].note or "")
    assert not any(a.object_id == "s1-static" for a in audit)


async def test_a_new_uplink_is_added_disabled_and_pending(db, monkeypatch) -> None:
    result, wans, _site_row, audit = await _run(db, monkeypatch, "auto")

    new = wans["ether3"]
    assert new.enabled is False
    assert new.tags == {"pending_review": True}
    assert new.dynamic is True
    assert new.name == "wan4"
    # After every existing uplink, so enabling it does not reorder failover.
    assert new.cost == 4.0
    assert ("new", "ether3", None) in _kinds(result)
    assert any(a.action == "wan.auto_add" and a.object_id == new.id for a in audit)


async def test_a_vanished_uplink_is_reported_never_deleted(db, monkeypatch) -> None:
    result, wans, _site_row, _audit = await _run(db, monkeypatch, "auto")

    assert "ether9" in wans
    assert wans["ether9"].enabled is True
    assert ("vanished", "ether9", None) in _kinds(result)


async def test_report_mode_changes_nothing(db, monkeypatch) -> None:
    result, wans, site, audit = await _run(db, monkeypatch, "report")

    # Everything is still reported...
    assert {("changed", "pppoe-out1", "public_ip"), ("changed", "ether1", "public_ip"),
            ("new", "ether3", None), ("vanished", "ether9", None)} <= _kinds(result)
    # ...and nothing is written.
    assert not any(c.applied for c in result.changes)
    assert wans["pppoe-out1"].public_ip == "198.51.100.77"
    assert "ether3" not in wans
    assert audit == []
    assert site.status == SiteStatus.reachable
    assert result.needs_apply is False


async def test_a_down_uplink_is_not_compared_or_called_vanished(db, monkeypatch) -> None:
    """A down PPPoE session has no address. That is not a new address."""
    menus = {
        "interface/pppoe-client": [{"name": "pppoe-out1", "running": False}],
        "ip/route": [{"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1",
                      "immediate-gw": "203.0.113.1%ether1", "distance": 2}],
        "ip/address": [{"address": "203.0.113.10/24", "interface": "ether1"}],
    }
    _patch_driver(monkeypatch, {"10.0.0.1": FakeRouterOS(password="secret", menus=menus)})
    async with db() as s:
        site = _site("s1", mode="auto")
        site.wans = site.wans[:2]
        s.add(site)
        await s.commit()
    async with db() as s:
        result = await check_site_uplinks(s, await s.get(Site, "s1"))
        await s.commit()
        ppp = await s.get(Wan, "s1-ppp")

    assert ppp.public_ip == "198.51.100.77"
    assert {(c.kind, c.interface) for c in result.changes} == {("down", "pppoe-out1")}


async def test_a_dynamic_uplink_that_lands_behind_nat_drops_its_public_ip(
    db, monkeypatch
) -> None:
    """A private address on the interface is positive evidence of NAT, so the
    stored public address is cleared rather than kept as if still dialable."""
    menus = {**NOW, "ip/address": [
        {"address": "100.64.3.4/32", "interface": "pppoe-out1"},
        {"address": "203.0.113.10/24", "interface": "ether1"},
    ]}
    _patch_driver(monkeypatch, {"10.0.0.1": FakeRouterOS(password="secret", menus=menus)})
    async with db() as s:
        site = _site("s1", mode="auto")
        site.wans = site.wans[:2]
        s.add(site)
        await s.commit()
    async with db() as s:
        await check_site_uplinks(s, await s.get(Site, "s1"))
        await s.commit()
        ppp = await s.get(Wan, "s1-ppp")

    assert ppp.public_ip is None
    assert ppp.nat_behind is True


async def test_the_sweep_survives_a_failing_site(db, monkeypatch) -> None:
    """One unreachable device, and one whose detection blows up outright,
    must not stop the healthy site from being re-detected."""
    _patch_driver(monkeypatch, {
        "10.0.0.1": DriverError("connection refused"),
        "10.0.0.2": RuntimeError("bug tripped by odd device output"),
        "10.0.0.3": FakeRouterOS(password="secret", menus=NOW),
    })
    async with db() as s:
        s.add_all([
            _site("a", mode="auto", host="10.0.0.1"),
            _site("b", mode="auto", host="10.0.0.2"),
            _site("c", mode="auto", host="10.0.0.3"),
            # Never looked at.
            _site("d", mode="off", host="10.0.0.4"),
        ])
        await s.commit()

    async with db() as s:
        counts = await sweep_uplinks(s)
        await s.commit()
        healthy = await s.get(Wan, "c-ppp")

    assert counts["failed"] == 2
    assert counts["checked"] == 1
    assert counts["changed"] == 1
    assert counts["applied"] == 2   # the PPPoE address and the new uplink
    assert healthy.public_ip == "198.51.100.200"


async def test_the_worker_job_returns_counts(db, monkeypatch) -> None:
    from app.tasks import worker

    _patch_driver(monkeypatch, {"10.0.0.1": FakeRouterOS(password="secret", menus=NOW)})
    async with db() as s:
        s.add(_site("s1", mode="report"))
        await s.commit()
    monkeypatch.setattr(worker, "SessionLocal", db)

    counts = await worker.uplink_sweep({})

    assert counts == {"checked": 1, "changed": 1, "applied": 0, "failed": 0}
    assert worker.uplink_sweep in worker.WorkerSettings.functions


# -- API ------------------------------------------------------------------------


async def test_uplink_sync_is_exposed_and_the_check_endpoint_never_writes(
    db, monkeypatch
) -> None:
    async with db() as s:
        s.add(User(email="admin@example.com", role=Role.admin,
                   password_hash=hash_password("correct-horse")))
        await s.commit()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = _session
    _patch_driver(monkeypatch, {"10.0.0.1": FakeRouterOS(password="secret", menus=NOW)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        login = await client.post("/auth/login", json={"email": "admin@example.com",
                                                        "password": "correct-horse"})
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        created = await client.post("/sites", json={
            "name": "branch", "mgmt_host": "10.0.0.1", "username": "admin",
            "wans": [{"name": "wan1", "interface": "pppoe-out1",
                      "public_ip": "198.51.100.77", "dynamic": True}],
        })
        assert created.status_code == 201, created.text
        site = created.json()
        assert site["uplink_sync"] == "report"

        bad = await client.patch(f"/sites/{site['id']}", json={"uplink_sync": "always"})
        assert bad.status_code == 422
        ok = await client.patch(f"/sites/{site['id']}", json={"uplink_sync": "auto"})
        assert ok.status_code == 200
        assert ok.json()["uplink_sync"] == "auto"

        check = await client.get(f"/sites/{site['id']}/uplinks/check")
        assert check.status_code == 200, check.text
        body = check.json()
        assert body["mode"] == "auto"
        assert body["reachable"] is True
        kinds = {(c["kind"], c["interface"]) for c in body["changes"]}
        assert ("changed", "pppoe-out1") in kinds
        assert ("new", "ether3") in kinds
        # Auto mode, and still nothing written: the endpoint is a read.
        assert not any(c["applied"] for c in body["changes"])

        after = (await client.get(f"/sites/{site['id']}")).json()
        assert [w["interface"] for w in after["wans"]] == ["pppoe-out1"]
        assert after["wans"][0]["public_ip"] == "198.51.100.77"
