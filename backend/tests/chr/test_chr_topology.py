"""Live C1 tests: hub + 2-spoke topology, live Safe Mode, chaos drills.

The rest of plan C1 (see tests/chr/README.md). Where test_chr_live.py
proves single-node driver behaviour, this module proves the whole
controller against three real CHRs wired as a hub and two spokes:

* Phase 1 -- the three lab CHRs are registered as sites in the real
  controller DB, a hub_spoke WireGuard fabric is expanded, and a real
  render+apply through ``services.reconcile.apply_site`` lands
  sdwan:-tagged state on all three devices. A second apply must be a
  no-op (the plan converges).
* Phase 2 -- those applies run with ``safe_mode_enabled`` (the settings
  default), so ``drivers/safemode.py``'s SshSafeModeSession holds real
  Safe Mode on real hardware: the Job result must say so and the apply
  journal must carry chunk events.
* Phase 3 -- chaos drills, each in its own test below: the controller
  killed mid-apply, management cut during held Safe Mode (both the
  healthy-close and silent-partition cases), a WAN flap baseline, and a
  restore from the C4 scoped pre-image.

Skipped by default, exactly like test_chr_live.py: they run only with
CHR_LIVE=1 and CHR_PASSWORD set, via scripts/chr-test.sh. The lab
topology (QEMU socket links, host forwards) is described in the
harness STATUS.md outside this repo.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.enums import JobState
from app.models.fabric import Link
from tests.chr.lab_topology import (
    NODES,
    TOPOLOGY,
    apply_one,
    dump_evidence,
    get_site,
    journal_for,
    make_driver,
    owned_rows,
    wipe_device_state,
)

pytestmark = pytest.mark.chr


async def _device_state(node: str) -> dict:
    driver = make_driver(node)
    await driver.connect()
    try:
        state = {}
        for path in (
            "/interface/wireguard",
            "/interface/wireguard/peers",
            "/ip/address",
            "/routing/bgp/connection",
        ):
            state[path] = await owned_rows(driver, path)
        return state
    finally:
        await driver.close()


async def test_hub_spoke_apply_converges_with_safe_mode(sm, topology) -> None:
    """Phases 1+2: real applies land the fabric; Safe Mode really held.

    Suite-order independent: chr-spoke3's device is wiped first, so its
    apply is a guaranteed full first push (the drills may already have
    converged the other sites, and an apply against a converged device
    correctly pushes nothing).
    """
    links = topology["links"]
    assert links, "no links expanded"

    driver = make_driver("chr3")
    await driver.connect()
    try:
        await wipe_device_state(driver)
    finally:
        await driver.close()

    jobs = {}
    for site_name in TOPOLOGY:
        jobs[site_name] = await apply_one(sm, site_name)

    for site_name, job in jobs.items():
        assert job.state == JobState.succeeded, f"{site_name}: {job.error}"
        if (job.result or {}).get("applied", 0) > 0:
            # Phase 2: any real push must have taken the B1 path, not
            # the legacy fallback.
            assert (job.result or {}).get("safe_mode_used") is True, (
                f"{site_name}: safe mode was not used: {job.result}"
            )
    spoke3 = jobs["chr-spoke3"]
    assert (spoke3.result or {}).get("applied", 0) > 0, (
        "chr-spoke3 was wiped but its apply pushed nothing"
    )
    assert (spoke3.result or {}).get("safe_mode_used") is True

    # Journal evidence: chunk events exist for the wiped spoke's push.
    journal_dump = {}
    for site_name in TOPOLOGY:
        site = await get_site(sm, site_name)
        entries = await journal_for(sm, site.id)
        journal_dump[site_name] = entries
    spoke3_entries = journal_dump["chr-spoke3"]
    events = {e["event"] for e in spoke3_entries}
    assert "chunk_commit" in events, "chr-spoke3: no chunk commits"
    assert any(
        e["event"] == "op" and e["state"] == "applied" for e in spoke3_entries
    )
    dump_evidence("phase2-journal.json", journal_dump)

    # Device state: every link touching a site has its WireGuard rows.
    state_dump = {}
    for node, info in NODES.items():
        state = await _device_state(node)
        state_dump[node] = state
        site_links = [
            link
            for link in links
            if info["site"] in (await _link_site_names(sm, link))
        ]
        assert len(state["/interface/wireguard"]) == len(site_links), (
            f"{node}: wireguard interfaces "
            f"{len(state['/interface/wireguard'])} != links {len(site_links)}"
        )
        assert len(state["/interface/wireguard/peers"]) == len(site_links)
    dump_evidence("phase1-device-state.json", state_dump)

    # Convergence: a second apply everywhere is a no-op.
    for site_name in TOPOLOGY:
        job = await apply_one(sm, site_name)
        assert job.state == JobState.succeeded, f"{site_name}: {job.error}"
        assert (job.result or {}).get("applied", 0) == 0, (
            f"{site_name}: second apply was not empty: {job.result}"
        )


async def _link_site_names(sm, link: Link) -> tuple[str, str]:
    from app.models.site import Wan

    async with sm() as s:
        names = []
        for wan_id in (link.a_wan_id, link.b_wan_id):
            wan = await s.get(Wan, wan_id)
            from app.models.site import Site

            site = await s.get(Site, wan.site_id)
            names.append(site.name)
    return names[0], names[1]


async def test_tunnels_carry_traffic_and_bgp_sessions_establish(
    sm, topology
) -> None:
    """The rendered fabric actually works: WG handshakes + BGP sessions."""
    deadline = asyncio.get_event_loop().time() + 240
    last = {}
    while True:
        last = {}
        all_up = True
        for node in NODES:
            driver = make_driver(node)
            await driver.connect()
            try:
                peers = await owned_rows(driver, "/interface/wireguard/peers")
                sessions = await driver.read("/routing/bgp/session")
                last[node] = {
                    "peers": [
                        {
                            "tx": p.get("tx"),
                            "rx": p.get("rx"),
                            "last-handshake": p.get("last-handshake"),
                        }
                        for p in peers
                    ],
                    "bgp_sessions": [
                        {
                            "name": s.get("name"),
                            "established": s.get("established"),
                            "uptime": s.get("uptime"),
                        }
                        for s in sessions
                    ],
                }
                handshakes = [
                    p for p in peers if int(p.get("tx") or 0) > 0
                ]
                established = [
                    s for s in sessions if s.get("established") in ("true", True)
                ]
                if not handshakes or not established:
                    all_up = False
            finally:
                await driver.close()
        if all_up or asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(10)
    dump_evidence("phase1-tunnels-bgp.json", last)
    for node, state in last.items():
        assert any(
            int(p["tx"] or 0) > 0 for p in state["peers"]
        ), f"{node}: no wireguard peer exchanged traffic: {state}"
        assert any(
            s["established"] in ("true", True) for s in state["bgp_sessions"]
        ), f"{node}: no established BGP session: {state}"
