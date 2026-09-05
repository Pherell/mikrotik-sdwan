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
    ) -> None:
        self.username = username
        self.password = password
        self._ids = itertools.count(1)
        self.commands: list[tuple[str, dict[str, Any]]] = []

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
        if path not in self.menus:
            return JSONResponse({"detail": "no such command prefix"}, status_code=404)
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
        if path in self.menus:  # POST to a menu is a query in RouterOS
            return self._get(path, {})
        return JSONResponse([])


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
