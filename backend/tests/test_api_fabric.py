"""End-to-end fabric flow: sites -> fabric -> expand -> apply, on fake routers.

Each site gets its own FakeRouterOS, so this exercises the part of M3 that does
not need hardware: that both ends of every tunnel are rendered consistently,
that applying converges, and that a second apply is a no-op.

Whether the resulting IPsec/BGP syntax actually establishes is what the
containerlab suite in `labs/` is for.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Base, User
from app.models.enums import Role
from app.security import hash_password
from tests.fakeros.server import FakeRouterOS

# Menus the site + fabric renderers touch.
MENUS: dict[str, list] = {
    "interface/bridge": [],
    "interface/gre": [],
    "ip/address": [],
    "ip/firewall/address-list": [],
    "ip/ipsec/profile": [],
    "ip/ipsec/proposal": [],
    "ip/ipsec/peer": [],
    "ip/ipsec/identity": [],
    "ip/ipsec/policy": [],
    "routing/bgp/template": [],
    "routing/bgp/connection": [],
    "routing/bgp/network": [],
    "tool/netwatch": [],
    "system/scheduler": [],
    "interface/wireguard": [],
    "interface/wireguard/peers": [],
    "interface/ipip": [],
    "interface/vxlan": [],
    "interface/vxlan/vteps": [],
    "interface/eoip": [],
    "interface/bridge/port": [],
    "ip/firewall/mangle": [],
    "routing/table": [],
    "ip/route": [],
}


@pytest.fixture
def routers() -> dict[str, FakeRouterOS]:
    """One fake device per management address."""
    return {
        host: FakeRouterOS(password="secret", menus={k: list(v) for k, v in MENUS.items()})
        for host in ("198.51.100.5", "203.0.113.1", "203.0.113.2")
    }


@pytest.fixture
async def api(routers, monkeypatch) -> AsyncIterator[tuple]:
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
    async def fake_open_driver(site, _box=None):
        ros = routers[site.mgmt_host]
        d = Ros7RestDriver(
            site.mgmt_host,
            "admin",
            "secret",
            transport=httpx.ASGITransport(app=ros.app),
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.services.reconcile.open_driver", fake_open_driver)

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with maker() as s:
        s.add(
            User(
                email="op@example.com",
                role=Role.admin,
                password_hash=hash_password("correct-horse"),
            )
        )
        await s.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        yield client, maker, routers
    await engine.dispose()


async def _auth(client: httpx.AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/auth/login", json={"email": "op@example.com", "password": "correct-horse"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _site(client, headers, name, host, role, prefix) -> str:
    resp = await client.post(
        "/sites",
        headers=headers,
        json={
            "name": name,
            "mgmt_host": host,
            "username": "admin",
            "password": "secret",
            "role": role,
            "local_prefixes": [prefix],
            "wans": [{"name": "wan1", "interface": "ether1", "public_ip": host}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _three_site_fabric(client, headers) -> tuple[str, dict[str, str]]:
    sites = {
        "hub1": await _site(client, headers, "hub1", "198.51.100.5", "hub", "10.1.0.0/24"),
        "spoke1": await _site(client, headers, "spoke1", "203.0.113.1", "spoke", "10.2.0.0/24"),
        "spoke2": await _site(client, headers, "spoke2", "203.0.113.2", "spoke", "10.3.0.0/24"),
    }
    resp = await client.post(
        "/fabrics",
        headers=headers,
        json={
            "name": "core",
            "transport": "ipsec_gre",
            "topology": "hub_spoke",
            "ip_pool": "10.255.0.0/24",
            "member_site_ids": list(sites.values()),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], sites


# -- fabric CRUD ------------------------------------------------------------


async def test_transports_are_advertised(api) -> None:
    client, _, _ = api
    headers = await _auth(client)

    resp = await client.get("/fabrics/transports", headers=headers)

    names = {t["name"] for t in resp.json()}
    assert "ipsec_gre" in names
    ipsec = next(t for t in resp.json() if t["name"] == "ipsec_gre")
    assert ipsec["requires_reachable_responder"] is True


async def test_unknown_transport_is_rejected(api) -> None:
    client, _, _ = api
    headers = await _auth(client)

    resp = await client.post(
        "/fabrics", headers=headers, json={"name": "x", "transport": "carrier-pigeon"}
    )
    assert resp.status_code == 422


async def test_pool_smaller_than_a_link_is_rejected(api) -> None:
    client, _, _ = api
    headers = await _auth(client)

    resp = await client.post(
        "/fabrics", headers=headers, json={"name": "x", "ip_pool": "10.255.0.0/32"}
    )
    assert resp.status_code == 422


async def test_changing_the_pool_under_live_links_is_refused(api) -> None:
    """Renumbering an addressed overlay drops every tunnel on it."""
    client, _, _ = api
    headers = await _auth(client)
    fabric_id, _ = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    resp = await client.patch(
        f"/fabrics/{fabric_id}", headers=headers, json={"ip_pool": "10.200.0.0/24"}
    )

    assert resp.status_code == 409
    assert "renumber" in resp.json()["detail"]


# -- expansion --------------------------------------------------------------


async def test_expand_builds_hub_spoke_links(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    fabric_id, _ = await _three_site_fabric(client, headers)

    resp = await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 2  # hub-spoke1, hub-spoke2; no spoke-spoke
    assert resp.json()["skipped"] == 0

    links = (await client.get(f"/fabrics/{fabric_id}/links", headers=headers)).json()
    assert len(links) == 2
    assert {link["subnet"] for link in links} == {"10.255.0.0/31", "10.255.0.2/31"}
    assert all(link["has_secrets"] for link in links)


async def test_expand_is_idempotent(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    fabric_id, _ = await _three_site_fabric(client, headers)

    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    second = await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    assert second.json() == {
        **second.json(),
        "created": 0,
        "kept": 2,
        "removed": 0,
    }


async def test_link_secrets_are_never_exposed(api) -> None:
    client, maker, _ = api
    headers = await _auth(client)
    fabric_id, _ = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    resp = await client.get(f"/fabrics/{fabric_id}/links", headers=headers)

    assert "psk" not in resp.text
    assert "secrets_enc" not in resp.text

    # The stored value really is encrypted, and really does decrypt.
    from sqlalchemy import select

    from app.models import Link
    from app.services.fabric import link_secrets

    async with maker() as s:
        link = (await s.scalars(select(Link))).first()
    assert link is not None
    assert len(link_secrets(link)["psk"]) >= 48
    assert "psk" not in link.secrets_enc


async def test_removing_a_member_drops_its_links(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    await client.delete(
        f"/fabrics/{fabric_id}/members/{sites['spoke2']}", headers=headers
    )
    resp = await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    assert resp.json()["removed"] == 1
    links = (await client.get(f"/fabrics/{fabric_id}/links", headers=headers)).json()
    assert len(links) == 1


# -- rendering and apply ----------------------------------------------------


async def test_plan_includes_the_whole_ipsec_stack_and_bgp(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    plan = (await client.post(f"/sites/{sites['hub1']}/plan", headers=headers)).json()
    paths = {s["path"] for s in plan["sections"]}

    assert {
        "/ip/ipsec/profile",
        "/ip/ipsec/proposal",
        "/ip/ipsec/peer",
        "/ip/ipsec/identity",
        "/ip/ipsec/policy",
        "/interface/gre",
        "/routing/bgp/template",
        "/routing/bgp/connection",
    } <= paths
    # Netwatch is a policy concern, not a fabric one.
    assert "/tool/netwatch" not in {s["path"] for s in plan["sections"] if s["lines"]}


async def test_psk_never_appears_in_a_plan(api) -> None:
    client, maker, _ = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    from sqlalchemy import select

    from app.models import Link
    from app.services.fabric import link_secrets

    async with maker() as s:
        link = (await s.scalars(select(Link))).first()
    psk = link_secrets(link)["psk"]  # type: ignore[arg-type]

    resp = await client.post(f"/sites/{sites['hub1']}/plan", headers=headers)
    assert psk not in resp.text


async def test_applying_every_site_converges(api) -> None:
    """The M3 property: apply each member once, then nothing more to do."""
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    for site_id in sites.values():
        resp = await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )
        assert resp.json()["state"] == "succeeded", resp.text

    for site_id in sites.values():
        plan = (await client.post(f"/sites/{site_id}/plan", headers=headers)).json()
        assert plan["empty"] is True, plan["text"]


async def test_both_ends_of_a_link_agree(api) -> None:
    """The two sides must address the same /31, name the same interface, and
    pick exactly one initiator. A disagreement here is a tunnel that never
    comes up."""
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    for site_id in sites.values():
        await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )

    hub = routers["198.51.100.5"]
    spoke = routers["203.0.113.1"]

    hub_gre = [g for g in hub.rows("interface/gre") if "spoke1" in g["name"]]
    spoke_gre = [g for g in spoke.rows("interface/gre") if "hub1" in g["name"]]
    assert len(hub_gre) == 1 and len(spoke_gre) == 1

    # Same interface name on both ends -- it is derived from the shared slug.
    assert hub_gre[0]["name"] == spoke_gre[0]["name"]
    # Endpoints are mirrored.
    assert hub_gre[0]["remote-address"] == "203.0.113.1"
    assert spoke_gre[0]["remote-address"] == "198.51.100.5"

    # Exactly one side is passive.
    hub_peer = [p for p in hub.rows("ip/ipsec/peer") if "spoke1" in p["name"]][0]
    spoke_peer = [p for p in spoke.rows("ip/ipsec/peer") if "hub1" in p["name"]][0]
    # The fake device stores what was sent, in RouterOS wire form.
    assert {hub_peer["passive"], spoke_peer["passive"]} == {"true", "false"}

    # Both ends of the /31, one each.
    hub_addr = {
        a["address"] for a in hub.rows("ip/address") if a["interface"].startswith("gre-")
    }
    spoke_addr = {
        a["address"] for a in spoke.rows("ip/address") if a["interface"].startswith("gre-")
    }
    assert hub_addr & spoke_addr == set()
    assert {a.split("/")[0] for a in hub_addr | spoke_addr} >= {"10.255.0.0", "10.255.0.1"}


async def test_hub_is_a_route_reflector_and_spokes_are_clients(api) -> None:
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    for site_id in sites.values():
        await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )

    hub_conns = routers["198.51.100.5"].rows("routing/bgp/connection")
    spoke_conns = routers["203.0.113.1"].rows("routing/bgp/connection")

    assert len(hub_conns) == 2  # one per spoke
    assert all(c["local.role"] == "ibgp-rr" for c in hub_conns)
    assert len(spoke_conns) == 1
    assert spoke_conns[0]["local.role"] == "ibgp-rr-client"


async def test_local_prefixes_are_advertised(api) -> None:
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    await client.post(
        f"/sites/{sites['spoke1']}/apply", headers=headers, json={"confirm": True}
    )

    networks = routers["203.0.113.1"].rows("routing/bgp/network")
    assert [n["network"] for n in networks] == ["10.2.0.0/24"]


async def test_a_fabric_alone_installs_no_probes(api) -> None:
    """Netwatch belongs to policy, not to the fabric. Liveness is already
    covered by GRE keepalives and the BGP hold timer; netwatch is for spotting a
    path that is up but degraded, and that threshold comes from an SLA profile.

    Rendering it in both places made two rows claim the same probe host and the
    device flapped between them on alternate applies."""
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    await client.post(
        f"/sites/{sites['hub1']}/apply", headers=headers, json={"confirm": True}
    )

    assert routers["198.51.100.5"].rows("tool/netwatch") == []
    # The tunnels themselves still detect a dead far end.
    gres = routers["198.51.100.5"].rows("interface/gre")
    assert all(g["keepalive"] == "10s,3" for g in gres)


async def test_removing_a_site_from_the_fabric_tears_its_tunnels_down(api) -> None:
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    for site_id in sites.values():
        await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )
    assert len(routers["198.51.100.5"].rows("interface/gre")) == 2

    await client.delete(f"/fabrics/{fabric_id}/members/{sites['spoke2']}", headers=headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    resp = await client.post(
        f"/sites/{sites['hub1']}/apply", headers=headers, json={"confirm": True}
    )

    assert resp.json()["state"] == "succeeded"
    gres = routers["198.51.100.5"].rows("interface/gre")
    assert len(gres) == 1
    assert "spoke1" in gres[0]["name"]


# -- read-only device passthrough -------------------------------------------


@pytest.fixture
def patched_sites_driver(routers, monkeypatch):
    """The passthrough opens its own driver, outside the service layer."""

    @asynccontextmanager
    async def fake(site, _box=None):
        d = Ros7RestDriver(
            site.mgmt_host,
            "admin",
            "secret",
            transport=httpx.ASGITransport(app=routers[site.mgmt_host].app),
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.api.v1.sites.open_driver", fake)


async def test_passthrough_reads_an_allowed_menu(api, patched_sites_driver) -> None:
    client, _, _ = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    await client.post(
        f"/sites/{sites['hub1']}/apply", headers=headers, json={"confirm": True}
    )

    resp = await client.get(
        f"/sites/{sites['hub1']}/device/interface/gre", headers=headers
    )

    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_passthrough_refuses_a_menu_that_is_not_allowlisted(
    api, patched_sites_driver
) -> None:
    """/user would expose accounts; nothing is readable just because RouterOS
    exposes it."""
    client, _, _ = api
    headers = await _auth(client)
    _, sites = await _three_site_fabric(client, headers)

    resp = await client.get(f"/sites/{sites['hub1']}/device/user", headers=headers)

    assert resp.status_code == 400
    assert "not readable" in resp.json()["detail"]


async def test_passthrough_strips_secrets_even_on_an_allowed_menu(
    api, patched_sites_driver
) -> None:
    """/ip/ipsec/peer is allowed, but no response may ever carry key material."""
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    await client.post(
        f"/sites/{sites['hub1']}/apply", headers=headers, json={"confirm": True}
    )
    # Plant a secret where a real device would have one.
    routers["198.51.100.5"].rows("ip/ipsec/peer")[0]["secret"] = "leaked-psk-value"

    resp = await client.get(
        f"/sites/{sites['hub1']}/device/ip/ipsec/peer", headers=headers
    )

    assert resp.status_code == 200
    assert "leaked-psk-value" not in resp.text
    assert all("secret" not in row for row in resp.json())


async def test_passthrough_requires_operator(api, patched_sites_driver) -> None:
    client, maker, _ = api
    headers = await _auth(client)
    _, sites = await _three_site_fabric(client, headers)

    async with maker() as s:
        s.add(
            User(
                email="v@example.com",
                role=Role.viewer,
                password_hash=hash_password("correct-horse"),
            )
        )
        await s.commit()
    token = (
        await client.post(
            "/auth/login", json={"email": "v@example.com", "password": "correct-horse"}
        )
    ).json()["access_token"]

    resp = await client.get(
        f"/sites/{sites['hub1']}/device/ip/route",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# -- transport migration ----------------------------------------------------


async def test_switching_transport_replaces_ipsec_with_wireguard(api) -> None:
    """The M4 property: change one dropdown, re-apply, and the overlay moves.
    No CLI, no hand-editing, no orphaned tunnels left behind."""
    client, _, routers = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    for site_id in sites.values():
        await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )

    hub = routers["198.51.100.5"]
    assert len(hub.rows("interface/gre")) == 2
    assert len(hub.rows("ip/ipsec/peer")) == 2

    resp = await client.patch(
        f"/fabrics/{fabric_id}", headers=headers, json={"transport": "wireguard"}
    )
    assert resp.status_code == 200, resp.text

    for site_id in sites.values():
        job = await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )
        assert job.json()["state"] == "succeeded", job.text

    # The IPsec/GRE stack is gone, not merely unused.
    assert hub.rows("interface/gre") == []
    assert hub.rows("ip/ipsec/peer") == []
    assert hub.rows("ip/ipsec/policy") == []
    assert hub.rows("ip/ipsec/identity") == []

    # WireGuard replaced it, with a peer per spoke.
    assert len(hub.rows("interface/wireguard")) == 2
    assert len(hub.rows("interface/wireguard/peers")) == 2

    # BGP still points at the same overlay addresses -- routing did not move.
    conns = hub.rows("routing/bgp/connection")
    assert {c["remote.address"] for c in conns} == {"10.255.0.1", "10.255.0.3"}


async def test_switching_transport_converges(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    for site_id in sites.values():
        await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )

    await client.patch(
        f"/fabrics/{fabric_id}", headers=headers, json={"transport": "wireguard"}
    )
    for site_id in sites.values():
        await client.post(
            f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
        )

    for name, site_id in sites.items():
        plan = (await client.post(f"/sites/{site_id}/plan", headers=headers)).json()
        assert plan["empty"] is True, f"{name}: {plan['text']}"


async def test_switching_transport_rekeys_every_link(api) -> None:
    """WireGuard cannot use an IPsec PSK. Without re-keying, every tunnel would
    come up with an empty private key."""
    client, maker, _ = api
    headers = await _auth(client)
    fabric_id, _ = await _three_site_fabric(client, headers)
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)

    from sqlalchemy import select

    from app.models import Link
    from app.services.fabric import link_secrets

    async with maker() as s:
        before = {link.id: link_secrets(link) for link in await s.scalars(select(Link))}
    assert all(set(v) == {"psk"} for v in before.values())

    await client.patch(
        f"/fabrics/{fabric_id}", headers=headers, json={"transport": "wireguard"}
    )

    async with maker() as s:
        after = {link.id: link_secrets(link) for link in await s.scalars(select(Link))}

    assert set(after) == set(before)
    for link_id, secrets in after.items():
        assert set(secrets) == {"a_private", "a_public", "b_private", "b_public", "preshared"}
        assert secrets != before[link_id]


async def test_switching_to_a_transport_a_member_cannot_run_is_refused(api) -> None:
    """A half-landed migration strands whichever sites could not follow."""
    client, maker, _ = api
    headers = await _auth(client)
    fabric_id, sites = await _three_site_fabric(client, headers)

    from app.models import Site

    async with maker() as s:
        site = await s.get(Site, sites["spoke2"])
        site.capabilities = {"ros_major": 6}
        await s.commit()

    resp = await client.patch(
        f"/fabrics/{fabric_id}", headers=headers, json={"transport": "wireguard"}
    )

    assert resp.status_code == 400
    assert "spoke2" in resp.json()["detail"]
    assert "RouterOS [7]" in resp.json()["detail"]


# -- policy steering, end to end --------------------------------------------


async def _dual_homed_fabric(client, headers) -> tuple[str, dict[str, str]]:
    """hub1 plus a spoke with two uplinks, so a policy has something to choose
    between."""
    hub = await _site(client, headers, "hub1", "198.51.100.5", "hub", "10.1.0.0/24")
    resp = await client.post(
        "/sites",
        headers=headers,
        json={
            "name": "spoke1",
            "mgmt_host": "203.0.113.1",
            "username": "admin",
            "password": "secret",
            "role": "spoke",
            "local_prefixes": ["10.2.0.0/24"],
            "wans": [
                {
                    "name": "wan1",
                    "interface": "ether1",
                    "public_ip": "203.0.113.1",
                    "cost": 1,
                    "tags": {"mpls": "yes"},
                },
                {
                    "name": "wan2",
                    "interface": "ether2",
                    "public_ip": "203.0.113.2",
                    "cost": 5,
                    "tags": {"broadband": "yes"},
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    spoke = resp.json()["id"]

    fabric = await client.post(
        "/fabrics",
        headers=headers,
        json={
            "name": "core",
            "transport": "ipsec_gre",
            "topology": "hub_spoke",
            "ip_pool": "10.255.0.0/24",
            "member_site_ids": [hub, spoke],
        },
    )
    fabric_id = fabric.json()["id"]
    await client.post(f"/fabrics/{fabric_id}/expand", headers=headers)
    return fabric_id, {"hub1": hub, "spoke1": spoke}


async def test_policy_steers_onto_the_preferred_uplink(api) -> None:
    client, _, routers = api
    headers = await _auth(client)
    _, sites = await _dual_homed_fabric(client, headers)

    sla = await client.post(
        "/sla-profiles",
        headers=headers,
        json={"name": "voice", "loss_percent": 2, "latency_ms": 150, "jitter_ms": 30},
    )
    assert sla.status_code == 201, sla.text

    resp = await client.post(
        "/policies",
        headers=headers,
        json={
            "name": "voice",
            "priority": 10,
            "dst_prefixes": ["10.1.0.0/24"],
            "protocol": "udp",
            "dst_ports": "5060",
            "prefer_tags": ["mpls", "broadband"],
            "sla_profile_id": sla.json()["id"],
        },
    )
    assert resp.status_code == 201, resp.text

    job = await client.post(
        f"/sites/{sites['spoke1']}/apply", headers=headers, json={"confirm": True}
    )
    assert job.json()["state"] == "succeeded", job.text

    spoke = routers["203.0.113.1"]
    mangle = spoke.rows("ip/firewall/mangle")
    assert len(mangle) == 1
    assert mangle[0]["new-routing-mark"] == "sdwan-voice"
    assert mangle[0]["protocol"] == "udp"
    assert mangle[0]["dst-port"] == "5060"

    assert [t["name"] for t in spoke.rows("routing/table")] == ["sdwan-voice"]

    # wan1 carries "mpls" and is preferred, so it gets the lower distance.
    routes = {r["gateway"]: r["distance"] for r in spoke.rows("ip/route")}
    assert routes["10.255.0.0"] == "1"   # via wan1
    assert routes["10.255.0.2"] == "2"   # via wan2
    assert routes["main"] == "250"       # fallback

    probes = {p["host"]: p for p in spoke.rows("tool/netwatch")}
    assert probes["10.255.0.0"]["thr-loss-percent"] == "2"
    assert probes["10.255.0.0"]["thr-latency"] == "150ms"
    # Breaching demotes below the backup but stays above the fallback.
    assert "distance=101" in probes["10.255.0.0"]["down-script"]
    assert "distance=1" in probes["10.255.0.0"]["up-script"]


async def test_policy_apply_converges(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    _, sites = await _dual_homed_fabric(client, headers)

    await client.post(
        "/policies",
        headers=headers,
        json={"name": "bulk", "prefer_tags": ["broadband"], "dst_prefixes": ["0.0.0.0/0"]},
    )
    await client.post(
        f"/sites/{sites['spoke1']}/apply", headers=headers, json={"confirm": True}
    )

    plan = (await client.post(f"/sites/{sites['spoke1']}/plan", headers=headers)).json()
    assert plan["empty"] is True, plan["text"]


async def test_deleting_a_policy_sweeps_its_rules_off_the_device(api) -> None:
    client, _, routers = api
    headers = await _auth(client)
    _, sites = await _dual_homed_fabric(client, headers)

    created = await client.post(
        "/policies", headers=headers, json={"name": "bulk", "prefer_tags": ["mpls"]}
    )
    await client.post(
        f"/sites/{sites['spoke1']}/apply", headers=headers, json={"confirm": True}
    )
    spoke = routers["203.0.113.1"]
    assert len(spoke.rows("ip/firewall/mangle")) == 1

    await client.delete(f"/policies/{created.json()['id']}", headers=headers)
    job = await client.post(
        f"/sites/{sites['spoke1']}/apply", headers=headers, json={"confirm": True}
    )

    assert job.json()["state"] == "succeeded"
    assert spoke.rows("ip/firewall/mangle") == []
    assert spoke.rows("routing/table") == []
    assert spoke.rows("tool/netwatch") == []


async def test_a_policy_naming_no_uplink_here_is_not_pushed(api) -> None:
    """Marking traffic into an empty routing table blackholes the site."""
    client, _, routers = api
    headers = await _auth(client)
    _, sites = await _dual_homed_fabric(client, headers)

    await client.post(
        "/policies", headers=headers, json={"name": "sat", "prefer_tags": ["satellite"]}
    )
    await client.post(
        f"/sites/{sites['spoke1']}/apply", headers=headers, json={"confirm": True}
    )

    assert routers["203.0.113.1"].rows("ip/firewall/mangle") == []


async def test_a_policy_without_preferences_is_rejected(api) -> None:
    client, _, _ = api
    headers = await _auth(client)

    resp = await client.post(
        "/policies", headers=headers, json={"name": "nowhere", "prefer_tags": []}
    )
    assert resp.status_code == 422


async def test_an_sla_profile_in_use_cannot_be_deleted(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    sla = await client.post("/sla-profiles", headers=headers, json={"name": "voice"})
    await client.post(
        "/policies",
        headers=headers,
        json={
            "name": "voice",
            "prefer_tags": ["mpls"],
            "sla_profile_id": sla.json()["id"],
        },
    )

    resp = await client.delete(f"/sla-profiles/{sla.json()['id']}", headers=headers)

    assert resp.status_code == 409
    assert "voice" in resp.json()["detail"]
