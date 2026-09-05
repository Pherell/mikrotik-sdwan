"""RouterOS 7.1+ driver, speaking the /rest API.

Reference: https://help.mikrotik.com/docs/spaces/ROS/pages/47579162/REST+API

Two RouterOS behaviours shape this module:

* every JSON value is a string, handled once in ``app.drivers.coerce``;
* long-running requests are cut off at 60 seconds, so applies are chunked and
  no single request ever carries a whole configuration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.drivers.base import (
    ApplyResult,
    ConfigOp,
    DeviceAuthError,
    DeviceCaps,
    DeviceUnreachable,
    DriverError,
    OpKind,
)
from app.drivers.coerce import coerce_row, encode_payload
from app.drivers.identity import check_pin, peer_fingerprint

log = logging.getLogger(__name__)

# RouterOS aborts at 60s. Stay under it so a timeout is ours, not theirs.
_MAX_REQUEST_SECONDS = 55.0
_APPLY_CHUNK = 20


class Ros7RestDriver:
    """Agentless driver for RouterOS 7. One instance per device, per job."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 443,
        scheme: str = "https",
        verify_tls: bool = False,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        pinned_fingerprint: str | None = None,
        pin: bool = True,
    ) -> None:
        self.host = host
        self._port = port
        self._pin = pin and transport is None
        self._pinned = pinned_fingerprint
        # Set on first contact so the caller can persist it.
        self.learned_fingerprint: str | None = None
        self.base_url = f"{scheme}://{host}:{port}/rest"
        self._auth = (username, password)
        self._verify = verify_tls
        # Test seam: the fake RouterOS server is mounted as an ASGI transport so
        # the whole request path, including coercion, runs unmodified.
        self._transport = transport
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=min(read_timeout, _MAX_REQUEST_SECONDS),
            write=read_timeout,
            pool=connect_timeout,
        )
        self._client: httpx.AsyncClient | None = None
        self._caps: DeviceCaps | None = None

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        # Check who is answering *before* sending credentials. Doing it after
        # the first request would mean the password had already been handed to
        # whoever was on the other end.
        if self._pin:
            presented = await peer_fingerprint(
                self.host, self._port, self._timeout.connect or 10.0
            )
            check_pin(
                self._pinned, presented, what="TLS certificate", host=self.host
            )
            if self._pinned is None:
                self.learned_fingerprint = presented

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=self._auth,
                verify=self._verify,
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Ros7RestDriver:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            raise DriverError("driver used before connect()")
        return self._client

    # -- request plumbing --------------------------------------------------

    @staticmethod
    def _norm(path: str) -> str:
        """Menu path to REST path: /ip/ipsec/peer becomes ip/ipsec/peer."""
        return path.strip("/").replace(" ", "/")

    def _check(self, resp: httpx.Response, method: str, url: str) -> None:
        """Map an HTTP status onto the driver's exception vocabulary.

        Single point of truth so reads and writes report failures identically.
        """
        if resp.status_code == 401:
            raise DeviceAuthError(f"{self.host}: authentication rejected")
        if resp.status_code == 404:
            # RouterOS returns 404 both for an unknown menu and an unknown row.
            raise DriverError(f"{self.host}: no such path or item: {url}")
        if resp.status_code >= 400:
            raise DriverError(
                f"{self.host}: {resp.status_code} on {method} {url}: {_err(resp)}"
            )

    async def _send(
        self, method: str, url: str, *, json: Any = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        try:
            resp = await self._c.request(method, url, json=json, params=params)
        except httpx.ConnectError as exc:
            raise DeviceUnreachable(f"{self.host}: cannot connect ({exc})") from exc
        except httpx.TimeoutException as exc:
            raise DeviceUnreachable(f"{self.host}: timed out on {method} {url}") from exc
        self._check(resp, method, url)
        return resp

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        url = "/" + self._norm(path)
        resp = await self._send(method, url, json=json)
        if not resp.content:
            return None
        return resp.json()

    # -- DeviceDriver ------------------------------------------------------

    async def read(
        self, path: str, query: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        url = "/" + self._norm(path)
        params = {k: str(v) for k, v in (query or {}).items()}
        resp = await self._send("GET", url, params=params)

        data = resp.json() if resp.content else []
        if isinstance(data, dict):  # single-row menus such as /system/resource
            data = [data]
        return [coerce_row(row) for row in data]

    async def run(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """POST to a console command, e.g. /system/backup/save."""
        return await self._request("POST", command, json=encode_payload(params or {}))

    async def backup(self, name: str) -> None:
        await self.run("/system/backup/save", {"name": name, "dont-encrypt": True})

    async def apply(self, ops: list[ConfigOp]) -> ApplyResult:
        """Execute mutations in order, stopping at the first failure.

        RouterOS has no transaction across menus, so ordering is the only
        safety available here. The dead-man rollback in app.reconcile is what
        makes a partial apply survivable.
        """
        result = ApplyResult(ok=True)
        for start in range(0, len(ops), _APPLY_CHUNK):
            for op in ops[start : start + _APPLY_CHUNK]:
                try:
                    await self._apply_one(op)
                except DriverError as exc:
                    result.ok = False
                    result.failed_op = op.redacted()
                    result.error = str(exc)
                    result.log.append(f"FAIL {op.kind} {op.path}: {exc}")
                    return result
                result.applied += 1
                result.log.append(f"ok   {op.kind} {op.path}")
            # Yield between chunks so one device cannot monopolise the worker.
            await asyncio.sleep(0)
        return result

    async def _apply_one(self, op: ConfigOp) -> None:
        props = dict(op.props)
        if op.comment:
            props.setdefault("comment", op.comment)

        match op.kind:
            case OpKind.add:
                if op.place_before:
                    props["place-before"] = op.place_before
                await self._request("PUT", op.path, json=encode_payload(props))
            case OpKind.set:
                if not op.item_id:
                    raise DriverError(f"set on {op.path} without an item id")
                await self._request(
                    "PATCH", f"{op.path}/{op.item_id}", json=encode_payload(props)
                )
            case OpKind.remove:
                if not op.item_id:
                    raise DriverError(f"remove on {op.path} without an item id")
                await self._request("DELETE", f"{op.path}/{op.item_id}")

    async def capabilities(self) -> DeviceCaps:
        if self._caps is not None:
            return self._caps

        resource = await self.read("/system/resource")
        identity = await self.read("/system/identity")
        try:
            pkgs = await self.read("/system/package")
        except DriverError:
            pkgs = []

        row = resource[0] if resource else {}
        version = str(row.get("version", ""))
        major = _major(version)
        names = {str(p.get("name", "")) for p in pkgs if not p.get("disabled")}

        # WireGuard and containers are built into 7.x rather than shipped as
        # packages, so probe the menu instead of the package list.
        has_wg = major >= 7 and await self._menu_exists("/interface/wireguard")
        has_container = major >= 7 and await self._menu_exists("/container")

        self._caps = DeviceCaps(
            ros_major=major,
            version=version,
            board_name=str(row.get("board-name", "")),
            architecture=str(row.get("architecture-name", "")),
            identity=str(identity[0].get("name", "")) if identity else "",
            has_rest=True,
            has_wireguard=has_wg,
            has_container=has_container,
            has_v7_bgp=major >= 7,
            has_netwatch_thresholds=_at_least(version, (7, 7)),
            packages=sorted(names),
        )
        return self._caps

    async def _menu_exists(self, path: str) -> bool:
        try:
            await self.read(path)
        except DriverError:
            return False
        return True


# -- helpers ---------------------------------------------------------------


def _err(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("message") or body)[:200]
    return str(body)[:200]


def _parse_version(version: str) -> tuple[int, ...]:
    """Turn a version banner into a comparable tuple.

    ``7.14.3 (stable)`` -> ``(7, 14, 3)``. Pre-release suffixes are cut at the
    first non-digit, so ``7.1beta4`` is ``(7, 1)`` -- taking every digit in the
    chunk would read it as 7.14 and wrongly clear a 7.7 feature gate.
    """
    head = version.split()[0] if version else ""
    parts: list[int] = []
    for chunk in head.split("."):
        leading = ""
        for char in chunk:
            if not char.isdigit():
                break
            leading += char
        if not leading:
            break
        parts.append(int(leading))
    return tuple(parts) or (0,)


def _major(version: str) -> int:
    return _parse_version(version)[0]


def _at_least(version: str, minimum: tuple[int, ...]) -> bool:
    got = _parse_version(version)
    got = got + (0,) * (len(minimum) - len(got))
    return got[: len(minimum)] >= minimum
