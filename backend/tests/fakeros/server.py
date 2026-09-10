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
        return [_row_out(h) for h in hops]


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
