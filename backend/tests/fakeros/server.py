"""A fake RouterOS 7 /rest endpoint for tests.

It reproduces the behaviours that actually bite:

* every JSON value is emitted as a string, booleans as "true"/"false";
* PUT creates, PATCH updates by .id, DELETE removes, POST runs a command;
* .id values look like RouterOS ones (*1, *2) and are not stable across resets;
* 401 on bad credentials, 404 on an unknown menu.

It is deliberately not a RouterOS emulator. It proves the driver's request
shapes and coercion are right; the containerlab suite proves the CLI syntax is.
"""

from __future__ import annotations

import base64
import itertools
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


def _stringify(value: Any) -> Any:
    """RouterOS encodes every scalar as a string. Faithfully reproduce that."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ",".join(str(_stringify(v)) for v in value)
    if value is None:
        return ""
    return str(value)


def _row_out(row: dict[str, Any]) -> dict[str, str]:
    return {k: _stringify(v) for k, v in row.items()}


class FakeRouterOS:
    """In-memory RouterOS. ``menus`` maps a REST path to a list of rows."""

    def __init__(
        self,
        *,
        username: str = "admin",
        password: str = "",
        version: str = "7.14.3 (stable)",
        board_name: str = "CCR2004-1G-12S+2XS",
        architecture: str = "arm64",
        identity: str = "MikroTik",
        menus: dict[str, list[dict[str, Any]]] | None = None,
        wireguard: bool = True,
        reachable: set[str] | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self._ids = itertools.count(1)
        self.commands: list[tuple[str, dict[str, Any]]] = []
        # Addresses ping and traceroute answer for. None means everything
        # answers, which is the boring case; a set is how a test asks for the
        # interesting one.
        self.reachable = reachable

        self.menus: dict[str, list[dict[str, Any]]] = {
            "system/resource": [
                {
                    "version": version,
                    "board-name": board_name,
                    "architecture-name": architecture,
                    "uptime": "1d2h3m",
                    "cpu-load": 3,
                    "free-memory": 402653184,
                }
            ],
            "system/identity": [{"name": identity}],
            "system/package": [
                {"name": "routeros", "version": version.split()[0], "disabled": False},
                {"name": "security", "version": version.split()[0], "disabled": False},
            ],
        }
        # Menus every RouterOS has, empty until something is added to them.
        # Without these the firewall sections read as 404 and the reconciler
        # correctly refuses to apply -- which is right behaviour against a
        # device that genuinely lacks a menu, and wrong as a model of a real
        # router, where /ip/firewall/nat always exists.
        for always_present in (
            "ip/firewall/nat",
            "ip/firewall/filter",
            "ip/firewall/address-list",
            # Every router has a log, even a freshly booted one.
            "log",
            # ROS 7.20+ always has the BGP instance menu; the reconciler reads
            # it to diff, and a 404 there reads as "unreadable" and fails the
            # apply.
            "routing/bgp/instance",
            # Always present; the policy "any" fallback lives here.
            "routing/rule",
        ):
            self.menus[always_present] = []
        if wireguard:
            self.menus["interface/wireguard"] = []
        for path, rows in (menus or {}).items():
            self.menus[path.strip("/")] = [self._with_id(dict(r)) for r in rows]

        self.app = Starlette(
            routes=[
                Route(
                    "/rest/{path:path}",
                    self._handle,
                    methods=["GET", "PUT", "PATCH", "DELETE", "POST"],
                )
            ]
        )

    # -- helpers -----------------------------------------------------------

    def _with_id(self, row: dict[str, Any]) -> dict[str, Any]:
        row.setdefault(".id", f"*{next(self._ids)}")
        return row

    def rows(self, path: str) -> list[dict[str, Any]]:
        return self.menus.setdefault(path.strip("/"), [])

    # RouterOS lists every interface in /interface whatever menu created it.
    # Without this a tunnel the reconciler just built into /interface/gre is
    # invisible to anything that asks the generic question -- which is the
    # question diagnostics asks.
    _IFACE_TYPES = ("ether", "bridge", "gre", "ipip", "wireguard", "eoip", "vxlan", "vlan")

    def _all_interfaces(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in self._IFACE_TYPES:
            for row in self.menus.get(f"interface/{kind}", []):
                rows.append({
                    "type": kind,
                    # A tunnel the reconciler created is running unless a test
                    # says otherwise; a real one comes up as soon as it is made.
                    "running": row.get("running", True),
                    "disabled": row.get("disabled", False),
                    **row,
                })
        return rows

    def _authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(header.split(None, 1)[1]).decode()
        except Exception:
            return False
        user, _, pw = decoded.partition(":")
        return user == self.username and pw == self.password

    # -- request handling --------------------------------------------------

    async def _handle(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"detail": "not authorized"}, status_code=401)

        path = request.path_params["path"].strip("/")
        method = request.method

        if method == "GET":
            return self._get(path, dict(request.query_params))
        if method == "POST":
            return await self._post(path, request)
        if method == "PUT":
            return await self._put(path, request)
        if method == "PATCH":
            return await self._patch(path, request)
        if method == "DELETE":
            return self._delete(path)
        return JSONResponse({"detail": "unsupported"}, status_code=405)

    def _get(self, path: str, query: dict[str, str]) -> Response:
        if path == "interface" and "interface" not in self.menus:
            rows = self._all_interfaces()
        elif path not in self.menus:
            return JSONResponse({"detail": "no such command prefix"}, status_code=404)
        else:
            rows = self.menus[path]
        if query:
            rows = [
                r
                for r in rows
                if all(_stringify(r.get(k)) == v for k, v in query.items() if not k.startswith("."))
            ]
        return JSONResponse([_row_out(r) for r in rows])

    async def _put(self, path: str, request: Request) -> Response:
        body = await _json(request)
        # Real RouterOS validates cross-references at insert time: an ipsec
        # identity or policy naming a peer that does not exist yet is refused
        # with "input does not match any value of peer". Without mirroring
        # that here, an apply-ordering bug (identity pushed before its peer)
        # passes against the fake and only blows up on real hardware, which is
        # exactly what happened once.
        if path in ("ip/ipsec/identity", "ip/ipsec/policy") and body.get("peer"):
            peers = {r.get("name") for r in self.menus.get("ip/ipsec/peer", [])}
            if body["peer"] not in peers:
                return JSONResponse(
                    {"detail": "input does not match any value of peer"},
                    status_code=400,
                )
        # ROS 7's /ip/ipsec/profile rejects a comment; the reconciler must own
        # it by name, not comment. Mirror the rejection so a regression that
        # re-adds the comment is caught here instead of on real hardware.
        if path == "ip/ipsec/profile" and "comment" in body:
            return JSONResponse(
                {"detail": "unknown parameter comment"}, status_code=400
            )
        # ROS 7 netwatch has no "thr-latency"; the latency fail threshold is
        # thr-avg (thr-max for peak). Mirror the rejection.
        if path == "tool/netwatch" and "thr-latency" in body:
            return JSONResponse(
                {"detail": "unknown parameter thr-latency"}, status_code=400
            )
        # ROS 7.24's /routing/bgp/template has no router-id (it lives on the
        # instance now) and names the address family "afi", not
        # "address-families". Both are rejected as unknown parameters.
        if path == "routing/bgp/template":
            for bad in ("router-id", "address-families"):
                if bad in body:
                    return JSONResponse(
                        {"detail": f"unknown parameter {bad}"}, status_code=400
                    )
        # ROS 7.20+ requires a BGP connection to name an instance
        # (/routing/bgp/instance) that already exists.
        if path == "routing/bgp/connection":
            if not body.get("instance"):
                return JSONResponse({"detail": "missing =instance="}, status_code=400)
            insts = {r.get("name") for r in self.menus.get("routing/bgp/instance", [])}
            if body["instance"] not in insts:
                return JSONResponse(
                    {"detail": "input does not match any value of instance"},
                    status_code=400,
                )
        # A BGP connection naming a template it has never seen is rejected;
        # the template must be created first.
        if path == "routing/bgp/connection" and body.get("templates"):
            names = {r.get("name") for r in self.menus.get("routing/bgp/template", [])}
            if body["templates"] not in names:
                return JSONResponse(
                    {"detail": "input does not match any value of template"},
                    status_code=400,
                )
        # ROS 7.24 accepts only ibgp / ebgp / ibgp-rr for a BGP connection's
        # local.role; "ibgp-rr-client" (which the code once assumed) is not a
        # value. Mirror the rejection so the suite catches a regression.
        if path == "routing/bgp/connection" and body.get("local.role") not in (
            None, "ibgp", "ebgp", "ibgp-rr",
        ):
            return JSONResponse(
                {"detail": "input does not match any value of local.role"},
                status_code=400,
            )
        # A mangle new-routing-mark / a route's routing-table must name a
        # /routing/table that already exists.
        if path in ("ip/firewall/mangle", "ip/route"):
            field = "new-routing-mark" if "new-routing-mark" in body else "routing-table"
            mark = body.get("new-routing-mark") or body.get("routing-table")
            if mark and mark != "main":
                tables = {r.get("name") for r in self.menus.get("routing/table", [])}
                if mark not in tables:
                    return JSONResponse(
                        {"detail": f"input does not match any value of {field}"},
                        status_code=400,
                    )
        # An AEAD proposal (GCM) must set auth-algorithms explicitly empty; a
        # non-empty value -- including the sha1 default kept when the field is
        # omitted -- is rejected as "AEAD already provides authentication".
        if path == "ip/ipsec/proposal" and "gcm" in str(body.get("enc-algorithms", "")):
            if body.get("auth-algorithms", "sha1") != "":
                return JSONResponse(
                    {"detail": "failure: AEAD already provides authentication"},
                    status_code=400,
                )
        # Model the two normalisations real ROS applies to an ipsec peer on
        # read, so a render that fights them shows up as non-convergence here:
        # a bare host address becomes /32, and passive=false is dropped (false
        # is the default and is not stored).
        if path == "ip/ipsec/peer":
            addr = body.get("address")
            if isinstance(addr, str) and addr and "/" not in addr and ":" not in addr:
                body["address"] = addr + "/32"
            if str(body.get("passive", "")).lower() in ("false", ""):
                body.pop("passive", None)
        # A WireGuard interface is one UDP listener. RouterOS does *not* refuse
        # a second interface asking for a port that is taken -- it accepts the
        # row and leaves it running=false, in silence. That is the worst
        # possible shape for a bug, so model it exactly: accept, but mark it
        # not running, and let the test assert on that rather than on an error
        # the device never raises.
        if path == "interface/wireguard":
            port = str(body.get("listen-port", ""))
            taken = {
                str(r.get("listen-port", ""))
                for r in self.menus.get("interface/wireguard", [])
            }
            body["running"] = not (port and port in taken)
        # ROS stores a routing table's fib flag but reads it back as an empty
        # string, so a render that keeps sending fib=true diffs dirty forever
        # unless it ignores the field. Model that here.
        if path == "routing/table" and str(body.get("fib", "")).lower() in ("true", "yes"):
            body["fib"] = ""
        if path not in self.menus:
            self.menus[path] = []
        row = self._with_id(dict(body))
        self.menus[path].append(row)
        return JSONResponse(_row_out(row))

    async def _patch(self, path: str, request: Request) -> Response:
        menu, _, item_id = path.rpartition("/")
        rows = self.menus.get(menu)
        if rows is None:
            return JSONResponse({"detail": "no such command prefix"}, status_code=404)
        for row in rows:
            if row.get(".id") == item_id:
                row.update(await _json(request))
                return JSONResponse(_row_out(row))
        return JSONResponse({"detail": "no such item"}, status_code=404)

    def _delete(self, path: str) -> Response:
        menu, _, item_id = path.rpartition("/")
        rows = self.menus.get(menu)
        if rows is None:
            return JSONResponse({"detail": "no such command prefix"}, status_code=404)
        for index, row in enumerate(rows):
            if row.get(".id") == item_id:
                rows.pop(index)
                return Response(status_code=204)
        return JSONResponse({"detail": "no such item"}, status_code=404)

    async def _post(self, path: str, request: Request) -> Response:
        body = await _json(request)
        self.commands.append((path, body))
        if path.endswith("backup/save"):
            self.rows("file").append(
                self._with_id({"name": f"{body.get('name', 'backup')}.backup", "type": "backup"})
            )
            return JSONResponse([])
        if path == "ping":
            return JSONResponse(self._ping(body))
        if path == "tool/traceroute":
            return JSONResponse(self._traceroute(body))
        if path in self.menus:  # POST to a menu is a query in RouterOS
            return self._get(path, {})
        return JSONResponse([])

    def _answers(self, address: str) -> bool:
        return self.reachable is None or address in self.reachable

    def _ping(self, body: dict[str, Any]) -> list[dict[str, str]]:
        """One row per probe, cumulative counters folded into each.

        The units are the ones that caused trouble: RouterOS writes
        "11ms391us", never a bare number of milliseconds.
        """
        address = str(body.get("address", ""))
        count = int(body.get("count", 1) or 1)
        up = self._answers(address)
        rows: list[dict[str, Any]] = []
        received = 0
        for seq in range(count):
            if up:
                received += 1
                rows.append(
                    {
                        "seq": seq,
                        "host": address,
                        "size": 56,
                        "ttl": 64,
                        "time": f"{seq + 1}ms{391 + seq}us",
                        "sent": seq + 1,
                        "received": received,
                        "packet-loss": 0,
                    }
                )
            else:
                rows.append(
                    {
                        "seq": seq,
                        # A timed-out probe carries a status and no time at all
                        # -- not a time of zero.
                        "status": "timeout",
                        "sent": seq + 1,
                        "received": 0,
                        "packet-loss": 100,
                    }
                )
        return [_row_out(r) for r in rows]

    def _traceroute(self, body: dict[str, Any]) -> list[dict[str, str]]:
        address = str(body.get("address", ""))
        hops: list[dict[str, Any]] = [
            {
                "address": "10.0.0.1",
                "loss": 0,
                "sent": 1,
                "last": "1ms200us",
                "avg": "1ms300us",
                "best": "1ms100us",
                "worst": "1ms500us",
                "status": "",
            }
        ]
        if self._answers(address):
            hops.append(
                {
                    "address": address,
                    "loss": 0,
                    "sent": 1,
                    "last": "12ms",
                    "avg": "12ms",
                    "best": "11ms",
                    "worst": "13ms",
                    "status": "",
                }
            )
        else:
            # A hop that never answers has no address at all. Reproducing that
            # is the point: it is what a broken path looks like.
            hops.append({"loss": 100, "sent": 1, "status": "timeout"})
        # RouterOS does not answer a traceroute once: it streams, re-emitting
        # every hop each round until the duration expires, with `sent`
        # counting the rounds. Reproducing that is the point -- reading the
        # rows in arrival order otherwise invents hops.
        rounds: list[dict[str, Any]] = []
        for round_no in (1, 2):
            for hop in hops:
                rounds.append({**hop, "sent": round_no})
        return [_row_out(h) for h in rounds]


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
