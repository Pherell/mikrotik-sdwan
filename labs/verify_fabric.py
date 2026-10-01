#!/usr/bin/env python3
"""Drive the controller against the containerlab fabric and assert it works.

This is the only layer that proves the rendered RouterOS syntax is correct.
Everything below it runs against a fake device and can only prove the shapes.

    sudo clab deploy -t labs/hub-spoke.clab.yml
    python labs/verify_fabric.py --api http://localhost:8000 \\
        --password "$SDWAN_BOOTSTRAP_ADMIN_PASSWORD" --json-out verify.json

Exit codes, so CI can tell a broken fabric from a broken harness:

    0  every check passed
    1  at least one check failed
    2  the run could not complete (controller unreachable, API error, ...)

``--json-out`` writes a machine-readable summary either way -- including after
an exit-2 abort, so the artifact always says how far the run got.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

SITES = [
    ("hub1", "172.30.30.11", "hub", "10.1.0.0/24"),
    ("spoke1", "172.30.30.21", "spoke", "10.2.0.0/24"),
    ("spoke2", "172.30.30.22", "spoke", "10.3.0.0/24"),
]
# Addresses on the shared "internet" segment, which the fabric treats as public.
UPLINKS = {"hub1": "198.51.100.5", "spoke1": "198.51.100.11", "spoke2": "198.51.100.12"}
UPLINK_PREFIX_LEN = 24
# RouterOS name of the uplink. Under vrnetlab, ether1 is the VM's management
# port (stitched to the container's eth0), so the first *data* port -- the one
# hub-spoke.clab.yml wires as eth1 -- is ether2 inside RouterOS.
UPLINK_INTERFACE = "ether2"

DEVICE_USER = "sdwan"
DEVICE_PASSWORD = "sdwan-lab"

# Steering policy used to exercise the policy renderer on real RouterOS. LANs
# only: a destination covering the overlay pool would mark the routers' own
# BGP sessions into a policy table, which is a different test. Each site's own
# LAN is deliberately inside the destination, so the LAN guard has work to do.
# Scoped to the spokes: a spoke has one tunnel, to the hub, so "every LAN via
# the overlay" is a correct policy there. On the hub it would pin all three
# LANs to whichever spoke tunnel sorts first.
POLICY_DST = ["10.1.0.0/24", "10.2.0.0/24", "10.3.0.0/24"]
POLICY_SITES = ("spoke1", "spoke2")
POLICY_RECOVERY_SECONDS = 30
OWNER_POLICY = "sdwan:policy:"

# HTTP statuses worth retrying: the controller restarting, or a proxy in front
# of it not having found it yet.
RETRY_STATUS = {502, 503, 504}


class Abort(Exception):
    """The run cannot continue; reported as exit 2."""


def truthy(value: Any) -> bool:
    """A RouterOS boolean, whether the controller coerced it or not.

    The passthrough coerces "true"/"false" to bool today. If it ever stops,
    ``bool("false")`` is True and every "is it established?" check would pass
    on a dead session -- so never test a device flag with plain truthiness.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes"}
    return bool(value)


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.phase = "init"

    @property
    def failures(self) -> list[str]:
        return [r["name"] for r in self.results if r["status"] == "fail"]

    def ok(self, condition: bool, message: str, detail: Any = None) -> bool:
        status = "pass" if condition else "fail"
        print(f"  {status.upper()}  {message}", flush=True)
        if not condition and detail is not None:
            print(f"        {detail}", flush=True)
        self.results.append(
            {
                "phase": self.phase,
                "name": message,
                "status": status,
                "detail": None if condition else detail,
            }
        )
        return condition

    def skip(self, message: str, reason: str) -> None:
        print(f"  SKIP  {message} ({reason})", flush=True)
        self.results.append(
            {"phase": self.phase, "name": message, "status": "skip", "detail": reason}
        )

    def section(self, name: str) -> None:
        self.phase = name
        print(f"\n== {name} ==", flush=True)


def wait_for(
    fn: Callable[[], Any],
    *,
    timeout: float,
    interval: float = 5.0,
    what: str = "condition",
) -> Any:
    """Poll until ``fn`` returns something truthy. Convergence is not instant:
    IKE, GRE keepalives and BGP each take their own time.

    A transient API error while polling counts as "not yet", not as a failure:
    a device read can time out while the router is busy renegotiating.
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        try:
            last = fn()
        except (httpx.HTTPError, Abort) as exc:
            print(f"  (still waiting for {what}: {exc})", flush=True)
            last = None
        if last:
            return last
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    print(f"  timed out after {timeout:.0f}s waiting for {what}", flush=True)
    return last


class Controller:
    def __init__(self, base: str, *, verify_tls: bool = True, retries: int = 3) -> None:
        self.base = base.rstrip("/")
        self.retries = retries
        self.c = httpx.Client(
            base_url=f"{self.base}/api/v1", timeout=120, verify=verify_tls
        )

    def wait_healthy(self, timeout: float) -> None:
        def healthy() -> bool:
            resp = self.c.get(f"{self.base}/healthz")
            return resp.status_code == 200 and resp.json().get("status") == "ok"

        if not wait_for(
            healthy, timeout=timeout, interval=3, what="controller /healthz"
        ):
            raise Abort(f"controller at {self.base} never became healthy")

    def login(self, email: str, password: str) -> None:
        resp = self._request(
            "POST", "/auth/login", {"email": email, "password": password}
        )
        if resp.status_code != 200:
            raise Abort(
                f"login as {email} failed: {resp.status_code} {resp.text[:200]}"
            )
        self.c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> httpx.Response:
        # Only connection failures and gateway statuses are retried. A 4xx is
        # the controller's answer, and retrying it would only hide the bug.
        for attempt in range(self.retries + 1):
            try:
                resp = self.c.request(method, path, json=body)
            except httpx.TransportError:
                if attempt >= self.retries:
                    raise
            else:
                if resp.status_code not in RETRY_STATUS or attempt >= self.retries:
                    return resp
            time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def post(
        self, path: str, body: dict | None = None, *, ok: tuple[int, ...] = ()
    ) -> Any:
        resp = self._request("POST", path, body or {})
        if resp.status_code in ok:
            return None
        if resp.is_error:
            raise Abort(f"POST {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else None

    def get(self, path: str) -> Any:
        resp = self._request("GET", path)
        if resp.is_error:
            raise Abort(f"GET {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def device(self, site_id: str, menu: str) -> list[dict[str, Any]]:
        return self.get(f"/sites/{site_id}/device/{menu}")


def _create_or_reuse(api: Controller, collection: str, body: dict) -> dict:
    """Create, or pick up the row a previous local run left behind.

    CI always starts from an empty database; a developer re-running against a
    long-lived controller does not, and a 409 there is not a fabric failure.
    """
    created = api.post(collection, body, ok=(409,))
    if created is not None:
        return created
    for row in api.get(collection):
        if row.get("name") == body["name"]:
            print(f"    reusing existing {collection.strip('/')} {body['name']!r}")
            return row
    raise Abort(f"{collection}: 409 for {body['name']!r} but no such row is listed")


def build(api: Controller) -> tuple[str, dict[str, str]]:
    sites: dict[str, str] = {}
    for name, mgmt, role, prefix in SITES:
        print(f"  adding {name}")
        site = _create_or_reuse(
            api,
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
                        "interface": UPLINK_INTERFACE,
                        "public_ip": UPLINKS[name],
                        "prefix_len": UPLINK_PREFIX_LEN,
                    }
                ],
            },
        )
        sites[name] = site["id"]
        probe = api.post(f"/sites/{site['id']}/probe")
        if not probe.get("reachable"):
            raise Abort(
                f"{name} is unreachable from the controller: {probe.get('error')}"
            )
        print(f"    {probe.get('version')} on {probe.get('board_name')}")

    fabric = _create_or_reuse(
        api,
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


def apply_all(
    api: Controller, check: Checks, sites: dict[str, str], label: str
) -> None:
    for name, site_id in sites.items():
        job = api.post(f"/sites/{site_id}/apply", {"confirm": True})
        result = job.get("result") or {}
        print(f"  {name}: {job['state']}, {result.get('applied')} changes")
        check.ok(
            job["state"] == "succeeded", f"{name} applied cleanly ({label})", result
        )
        check.ok(
            not job.get("rollback_token"), f"{name} disarmed its rollback ({label})"
        )


def replan_all(
    api: Controller, check: Checks, sites: dict[str, str], label: str
) -> None:
    for name, site_id in sites.items():
        plan = api.post(f"/sites/{site_id}/plan")
        check.ok(plan["empty"], f"{name} re-plans clean ({label})", plan.get("text"))


# -- convergence ---------------------------------------------------------------


def ipsec_up(api: Controller, hub: str) -> Any:
    peers = api.device(hub, "ip/ipsec/active-peers")
    return peers if len(peers) >= 2 else None


def bgp_up(api: Controller, hub: str) -> Any:
    sessions = api.device(hub, "routing/bgp/session")
    established = [s for s in sessions if truthy(s.get("established"))]
    return established if len(established) >= 2 else None


def learned(api: Controller, spoke: str) -> Any:
    routes = api.device(spoke, "ip/route")
    remote = [
        r
        for r in routes
        if r.get("dst-address") in {"10.1.0.0/24", "10.3.0.0/24"}
        and not truthy(r.get("inactive"))
        # Policy tables hold routes too; only main proves BGP delivered them.
        and r.get("routing-table", "main") == "main"
    ]
    return remote if len(remote) >= 2 else None


def converge(
    api: Controller, check: Checks, sites: dict[str, str], timeout: float, label: str
) -> None:
    hub, spoke = sites["hub1"], sites["spoke1"]
    check.ok(
        bool(wait_for(lambda: ipsec_up(api, hub), timeout=timeout, what="IPsec SAs")),
        f"both IPsec SAs up on the hub ({label})",
    )
    check.ok(
        bool(wait_for(lambda: bgp_up(api, hub), timeout=timeout, what="BGP sessions")),
        f"both BGP sessions established on the hub ({label})",
    )
    check.ok(
        bool(
            wait_for(
                lambda: learned(api, spoke), timeout=timeout, what="learned routes"
            )
        ),
        f"spoke1 learned the hub's and spoke2's prefixes over BGP ({label})",
    )


# -- steering policy -------------------------------------------------------------


def create_policy(api: Controller, sites: dict[str, str]) -> None:
    sla = _create_or_reuse(
        api,
        "/sla-profiles",
        {
            "name": "lab-sla",
            "loss_percent": 20,
            "latency_ms": 300,
            "probe_interval_seconds": 5,
            "probe_count": 5,
            "recovery_seconds": POLICY_RECOVERY_SECONDS,
        },
    )
    group = _create_or_reuse(
        api,
        "/sdwan-groups",
        {
            "name": "lab-overlay",
            "members": [{"uplink": "wan1"}],
            "strategy": "failover",
            "sla_profile_id": sla["id"],
        },
    )
    _create_or_reuse(
        api,
        "/policies",
        {
            "name": "lab-remote-lans",
            "priority": 100,
            "dst_prefixes": POLICY_DST,
            "site_ids": [sites[name] for name in POLICY_SITES],
            "sdwan_group_id": group["id"],
            "fallback": "any",
        },
    )


def _is_ours(row: dict[str, Any]) -> bool:
    return str(row.get("comment", "")).startswith(OWNER_POLICY)


def check_policy_rendering(
    api: Controller, check: Checks, sites: dict[str, str]
) -> None:
    """Assert the renderer's recent fixes hold on a real device, not just in render.

    - the LAN guard (accept dst=<site LAN list>) sits above every mark;
    - PCC classifiers classify once: connection-state=new connection-mark=no-mark;
    - netwatch up-scripts hold down with ``:delay <recovery_seconds>s``.
    """
    any_pcc = False
    for name in POLICY_SITES:
        site_id = sites[name]
        prerouting = [
            r
            for r in api.device(site_id, "ip/firewall/mangle")
            if _is_ours(r) and r.get("chain") == "prerouting"
        ]
        lan_list = f"sdwan-{name}-lan"
        guard = next(
            (
                i
                for i, r in enumerate(prerouting)
                if r.get("action") == "accept" and r.get("dst-address-list") == lan_list
            ),
            None,
        )
        first_mark = next(
            (
                i
                for i, r in enumerate(prerouting)
                if r.get("action") in {"mark-routing", "mark-connection"}
            ),
            None,
        )
        check.ok(
            guard is not None,
            f"{name}: policy LAN guard present (accept dst-address-list={lan_list})",
            [r.get("comment") for r in prerouting],
        )
        check.ok(
            first_mark is not None,
            f"{name}: policy mark rules rendered",
            [r.get("comment") for r in prerouting],
        )
        if guard is not None and first_mark is not None:
            check.ok(
                guard < first_mark,
                f"{name}: LAN guard sits above the first mark rule",
                {"guard_index": guard, "first_mark_index": first_mark},
            )
        populated = any(
            a.get("list") == lan_list
            for a in api.device(site_id, "ip/firewall/address-list")
        )
        check.ok(populated, f"{name}: LAN guard address-list {lan_list} populated")

        pcc = [r for r in prerouting if r.get("per-connection-classifier")]
        if pcc:
            any_pcc = True
            bad = [
                r.get("comment")
                for r in pcc
                if r.get("connection-state") != "new"
                or r.get("connection-mark") != "no-mark"
            ]
            check.ok(
                not bad,
                f"{name}: every PCC classifier has connection-state=new connection-mark=no-mark",
                bad,
            )

        netwatch = [n for n in api.device(site_id, "tool/netwatch") if _is_ours(n)]
        check.ok(bool(netwatch), f"{name}: SLA netwatch probes rendered")
        hold = f":delay {POLICY_RECOVERY_SECONDS}s"
        missing = [
            n.get("comment")
            for n in netwatch
            if hold not in str(n.get("up-script", ""))
        ]
        check.ok(
            bool(netwatch) and not missing,
            f"{name}: every netwatch up-script holds down with '{hold}'",
            missing,
        )

    if not any_pcc:
        # One uplink per lab site, so a load_balance group has nothing to
        # spread across and renders no classifiers. Reported, not failed.
        check.skip(
            "PCC classifiers have connection-state=new connection-mark=no-mark",
            "the lab has one uplink per site, so no load_balance classifiers render",
        )


# -- main ------------------------------------------------------------------------


def run(args: argparse.Namespace, check: Checks) -> None:
    api = Controller(args.api, verify_tls=not args.insecure, retries=args.retries)
    check.section("controller")
    api.wait_healthy(args.api_wait)
    api.login(args.email, args.password)

    check.section("building the fabric")
    fabric_id, sites = build(api)

    expansion = api.post(f"/fabrics/{fabric_id}/expand")
    print(f"  expansion: {expansion}")
    # A re-run against a reused fabric keeps its links rather than creating them.
    links = expansion.get("created", 0) + expansion.get("kept", 0)
    check.ok(links == 2, "hub-and-spoke produced two links", expansion)
    check.ok(expansion.get("skipped") == 0, "no pair was skipped", expansion)

    check.section("applying")
    apply_all(api, check, sites, "fabric")

    check.section("idempotency")
    replan_all(api, check, sites, "fabric")

    check.section("tunnels establish")
    converge(api, check, sites, args.converge_timeout, "fabric")

    check.section("steering policy")
    if args.skip_policy:
        check.skip("steering policy checks", "--skip-policy")
    else:
        create_policy(api, sites)
        apply_all(api, check, sites, "policy")
        replan_all(api, check, sites, "policy")
        check_policy_rendering(api, check, sites)

        check.section("overlay still converged with steering on")
        converge(api, check, sites, args.converge_timeout, "policy")

    check.section("hand-built config survived")
    for name, site_id in sites.items():
        addresses = api.device(site_id, "ip/address")
        lab_rows = [a for a in addresses if "lab" in str(a.get("comment", ""))]
        check.ok(len(lab_rows) == 2, f"{name} kept both of its lab addresses", lab_rows)


def summarize(check: Checks, started: float, error: str | None) -> dict[str, Any]:
    counts = {
        s: sum(r["status"] == s for r in check.results)
        for s in ("pass", "fail", "skip")
    }
    return {
        "ok": error is None and counts["fail"] == 0,
        "error": error,
        "aborted_in_phase": check.phase if error else None,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "skipped": counts["skip"],
        "duration_seconds": round(time.monotonic() - started, 1),
        "finished_at": datetime.now(UTC).isoformat(),
        "checks": check.results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the containerlab SD-WAN fabric through the controller API."
    )
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email", default="admin@local")
    parser.add_argument("--password", default="changeme")
    parser.add_argument(
        "--converge-timeout",
        type=float,
        default=180.0,
        help="seconds to wait for IPsec, BGP and learned routes, per wait (default 180)",
    )
    parser.add_argument(
        "--api-wait",
        type=float,
        default=120.0,
        help="seconds to wait for the controller's /healthz (default 120)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="retries on connection errors and 502/503/504 (default 3)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification of the controller",
    )
    parser.add_argument(
        "--skip-policy",
        action="store_true",
        help="verify the fabric only, not steering",
    )
    parser.add_argument(
        "--json-out", metavar="PATH", help="write a JSON summary to PATH"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    check = Checks()
    started = time.monotonic()
    error: str | None = None
    try:
        run(args, check)
    except (Abort, httpx.HTTPError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 -- the summary must still be written
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(summarize(check, started, error), fh, indent=2, default=str)
        print(f"\nsummary written to {args.json_out}")

    print("\n" + "=" * 60)
    if error:
        print(f"ABORTED in phase {check.phase!r}: {error}")
        return 2
    if check.failures:
        print(f"{len(check.failures)} check(s) FAILED:")
        for failure in check.failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
