#!/usr/bin/env python3
"""Drive the controller against the containerlab fabric and assert it works.

This is the only layer that proves the rendered RouterOS syntax is correct.
Everything below it runs against a fake device and can only prove the shapes.

    sudo clab deploy -t labs/hub-spoke.clab.yml
    python labs/verify_fabric.py --api http://localhost:8000

Exits non-zero on the first failed assertion, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import httpx

SITES = [
    ("hub1", "172.30.30.11", "hub", "10.1.0.0/24"),
    ("spoke1", "172.30.30.21", "spoke", "10.2.0.0/24"),
    ("spoke2", "172.30.30.22", "spoke", "10.3.0.0/24"),
]
# Addresses on the shared "internet" segment, which the fabric treats as public.
UPLINKS = {"hub1": "198.51.100.5", "spoke1": "198.51.100.11", "spoke2": "198.51.100.12"}

DEVICE_USER = "sdwan"
DEVICE_PASSWORD = "sdwan-lab"


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, condition: bool, message: str) -> bool:
        print(f"  {'PASS' if condition else 'FAIL'}  {message}")
        if not condition:
            self.failures.append(message)
        return condition


def wait_for(fn, *, timeout: float, interval: float = 5.0, what: str = "condition"):
    """Poll until ``fn`` returns something truthy. Convergence is not instant:
    IKE, GRE keepalives and BGP each take their own time."""
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    print(f"  timed out after {timeout:.0f}s waiting for {what}")
    return last


class Controller:
    def __init__(self, base: str, email: str, password: str) -> None:
        self.c = httpx.Client(base_url=f"{base.rstrip('/')}/api/v1", timeout=120)
        resp = self.c.post("/auth/login", json={"email": email, "password": password})
        resp.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def post(self, path: str, body: dict | None = None) -> Any:
        resp = self.c.post(path, json=body or {})
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def get(self, path: str) -> Any:
        resp = self.c.get(path)
        resp.raise_for_status()
        return resp.json()


def build(api: Controller) -> tuple[str, dict[str, str]]:
    sites: dict[str, str] = {}
    for name, mgmt, role, prefix in SITES:
        print(f"  adding {name}")
        site = api.post(
            "/sites",
            {
                "name": name,
                "mgmt_host": mgmt,
                "username": DEVICE_USER,
                "password": DEVICE_PASSWORD,
                "role": role,
                "local_prefixes": [prefix],
                "wans": [
                    {
                        "name": "wan1",
                        "interface": "ether1",
                        "public_ip": UPLINKS[name],
                    }
                ],
            },
        )
        sites[name] = site["id"]
        probe = api.post(f"/sites/{site['id']}/probe")
        print(f"    {probe.get('version')} on {probe.get('board_name')}")

    fabric = api.post(
        "/fabrics",
        {
            "name": "core",
            "transport": "ipsec_gre",
            "topology": "hub_spoke",
            "ip_pool": "10.255.0.0/24",
            "asn": 65000,
            "member_site_ids": list(sites.values()),
        },
    )
    return fabric["id"], sites


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email", default="admin@local")
    parser.add_argument("--password", default="changeme")
    parser.add_argument("--converge-timeout", type=float, default=180.0)
    args = parser.parse_args()

    api = Controller(args.api, args.email, args.password)
    check = Checks()

    print("\n== building the fabric ==")
    fabric_id, sites = build(api)

    expansion = api.post(f"/fabrics/{fabric_id}/expand")
    print(f"  expansion: {expansion}")
    check.ok(expansion["created"] == 2, "hub-and-spoke produced two links")
    check.ok(expansion["skipped"] == 0, "no pair was skipped")

    print("\n== applying ==")
    for name, site_id in sites.items():
        job = api.post(f"/sites/{site_id}/apply", {"confirm": True})
        print(f"  {name}: {job['state']}, {job['result'].get('applied')} changes")
        check.ok(job["state"] == "succeeded", f"{name} applied cleanly")
        check.ok(
            not job.get("rollback_token"), f"{name} disarmed its rollback"
        )

    print("\n== idempotency ==")
    for name, site_id in sites.items():
        plan = api.post(f"/sites/{site_id}/plan")
        check.ok(plan["empty"], f"{name} re-plans clean")
        if not plan["empty"]:
            print(plan["text"])

    print("\n== tunnels establish ==")

    def ipsec_up() -> bool:
        peers = api.get(f"/sites/{sites['hub1']}/device/ip/ipsec/active-peers")
        return len(peers) >= 2

    def bgp_up() -> Any:
        sessions = api.get(f"/sites/{sites['hub1']}/device/routing/bgp/session")
        established = [s for s in sessions if s.get("established")]
        return established if len(established) >= 2 else None

    check.ok(
        bool(wait_for(ipsec_up, timeout=args.converge_timeout, what="IPsec SAs")),
        "both IPsec SAs came up on the hub",
    )
    check.ok(
        bool(wait_for(bgp_up, timeout=args.converge_timeout, what="BGP sessions")),
        "both BGP sessions established on the hub",
    )

    print("\n== prefixes are exchanged ==")

    def learned() -> Any:
        routes = api.get(f"/sites/{sites['spoke1']}/device/ip/route")
        remote = [
            r
            for r in routes
            if r.get("dst-address") in {"10.1.0.0/24", "10.3.0.0/24"}
            and not r.get("inactive")
        ]
        return remote if len(remote) >= 2 else None

    check.ok(
        bool(wait_for(learned, timeout=args.converge_timeout, what="learned routes")),
        "spoke1 learned the hub's and spoke2's prefixes over BGP",
    )

    print("\n== hand-built config survived ==")
    for name, site_id in sites.items():
        addresses = api.get(f"/sites/{site_id}/device/ip/address")
        lab_rows = [a for a in addresses if "lab" in str(a.get("comment", ""))]
        check.ok(len(lab_rows) == 2, f"{name} kept both of its lab addresses")

    print("\n" + "=" * 60)
    if check.failures:
        print(f"{len(check.failures)} check(s) FAILED:")
        for failure in check.failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
