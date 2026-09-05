"""RouterOS 6 driver, over SSH.

RouterOS 6 has no ``/rest``, so this drives the console. That costs three things
the REST driver gets for free, and each one shapes the code:

* **No structured reads.** ``print`` output must be parsed. ``as-value`` gives a
  machine-readable form -- ``key=value;key=value`` per row -- which is stable
  enough to rely on, unlike the aligned columns of a plain ``print``.
* **No atomicity per request.** Commands are sent in batches and a failure
  partway leaves the earlier ones applied. The dead-man rollback in
  ``app.reconcile.apply`` is the only real safety net here.
* **Different syntax.** v6 BGP is ``/routing bgp peer``, there is no WireGuard,
  and there is no ``/routing/table``. ``capabilities()`` reports that so the
  planner refuses unsupported features up front instead of failing halfway.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import asyncssh

from app.drivers.base import (
    ApplyResult,
    ConfigOp,
    DeviceAuthError,
    DeviceCaps,
    DeviceUnreachable,
    DriverError,
    OpKind,
)
from app.drivers.coerce import canonical, coerce_row

log = logging.getLogger(__name__)

# RouterOS prints one row per line as key=value;key=value when asked for values.
_ROW_SEPARATOR = re.compile(r";(?=[A-Za-z_.][\w.-]*=)")


class Ros6SshDriver:
    """Console driver for RouterOS 6. Also works against 7 as a fallback when
    ``www-ssl`` cannot be enabled."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str = "",
        *,
        port: int = 22,
        client_key: str | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
    ) -> None:
        self.host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_key = client_key
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._conn: asyncssh.SSHClientConnection | None = None
        self._caps: DeviceCaps | None = None

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        if self._conn is not None:
            return
        options: dict[str, Any] = {
            "username": self._username,
            "known_hosts": None,  # device keys are not managed; TOFU is the norm here
        }
        if self._client_key:
            options["client_keys"] = [asyncssh.import_private_key(self._client_key)]
        else:
            options["password"] = self._password

        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(self.host, port=self._port, **options),
                timeout=self._connect_timeout,
            )
        except asyncssh.PermissionDenied as exc:
            raise DeviceAuthError(f"{self.host}: authentication rejected") from exc
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            raise DeviceUnreachable(f"{self.host}: cannot connect ({exc})") from exc

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def __aenter__(self) -> Ros6SshDriver:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- command plumbing --------------------------------------------------

    async def _exec(self, command: str) -> str:
        if self._conn is None:
            raise DriverError("driver used before connect()")
        try:
            result = await asyncio.wait_for(
                self._conn.run(command, check=False), timeout=self._read_timeout
            )
        except (OSError, asyncssh.Error) as exc:
            raise DeviceUnreachable(f"{self.host}: {exc}") from exc
        except TimeoutError as exc:
            raise DeviceUnreachable(f"{self.host}: timed out running {command!r}") from exc

        output = str(result.stdout or "")
        stderr = str(result.stderr or "")
        # RouterOS reports failures on stdout with a "failure:" or "expected"
        # prefix and still exits 0, so the exit status alone proves nothing.
        for line in (output + stderr).splitlines():
            lowered = line.strip().lower()
            if lowered.startswith(("failure:", "syntax error", "expected ", "bad command")):
                raise DriverError(f"{self.host}: {line.strip()} (running {command!r})")
        return output

    # -- DeviceDriver ------------------------------------------------------

    async def read(
        self, path: str, query: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Read a menu via ``print as-value``, which is parseable output."""
        menu = path.strip("/").replace("/", " ")
        where = ""
        if query:
            terms = " ".join(f'{k}="{canonical(v)}"' for k, v in query.items())
            where = f" where {terms}"
        raw = await self._exec(f":put [/{menu} print as-value{where}]")

        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            row: dict[str, Any] = {}
            for pair in _ROW_SEPARATOR.split(line):
                key, sep, value = pair.partition("=")
                if sep:
                    row[key.strip()] = value.strip()
            if row:
                rows.append(coerce_row(row))
        return rows

    async def run(self, command: str, params: dict[str, Any] | None = None) -> Any:
        menu = command.strip("/").replace("/", " ")
        args = " ".join(f"{k}={_quote(v)}" for k, v in (params or {}).items())
        return await self._exec(f"/{menu} {args}".strip())

    async def backup(self, name: str) -> None:
        await self._exec(f"/system backup save name={_quote(name)} dont-encrypt=yes")

    async def apply(self, ops: list[ConfigOp]) -> ApplyResult:
        """One command per op, stopping at the first failure.

        No batching: a batch that fails halfway gives no reliable way to tell
        which commands ran, and knowing that is what makes a partial apply
        recoverable.
        """
        result = ApplyResult(ok=True)
        for op in ops:
            try:
                await self._exec(self._command_for(op))
            except DriverError as exc:
                result.ok = False
                result.failed_op = op.redacted()
                result.error = str(exc)
                result.log.append(f"FAIL {op.kind} {op.path}: {exc}")
                return result
            result.applied += 1
            result.log.append(f"ok   {op.kind} {op.path}")
        return result

    def _command_for(self, op: ConfigOp) -> str:
        menu = op.path.strip("/").replace("/", " ")
        props = dict(op.props)
        if op.comment:
            props.setdefault("comment", op.comment)
        args = " ".join(f"{k}={_quote(v)}" for k, v in props.items() if v is not None)

        match op.kind:
            case OpKind.add:
                if op.place_before:
                    args += f" place-before={_quote(op.place_before)}"
                return f"/{menu} add {args}"
            case OpKind.set:
                if not op.item_id:
                    raise DriverError(f"set on {op.path} without an item id")
                return f"/{menu} set {op.item_id} {args}"
            case OpKind.remove:
                if not op.item_id:
                    raise DriverError(f"remove on {op.path} without an item id")
                return f"/{menu} remove {op.item_id}"
        raise DriverError(f"unsupported op {op.kind}")

    async def capabilities(self) -> DeviceCaps:
        if self._caps is not None:
            return self._caps

        resource = await self.read("/system resource")
        identity = await self.read("/system identity")
        row = resource[0] if resource else {}
        version = str(row.get("version", ""))
        major = int(version.split(".")[0]) if version and version[0].isdigit() else 6

        self._caps = DeviceCaps(
            ros_major=major,
            version=version,
            board_name=str(row.get("board-name", "")),
            architecture=str(row.get("architecture-name", "")),
            identity=str(identity[0].get("name", "")) if identity else "",
            has_rest=False,
            # None of these exist in RouterOS 6. Reporting them honestly is what
            # makes the planner refuse a WireGuard fabric up front rather than
            # failing partway through an apply on a production edge.
            has_wireguard=False,
            has_container=False,
            has_v7_bgp=major >= 7,
            has_netwatch_thresholds=major >= 7,
            packages=[],
        )
        return self._caps


def _quote(value: Any) -> str:
    """Quote a value for the RouterOS console.

    Everything is quoted rather than only what looks dangerous: a value that
    happens to contain a space or a semicolon would otherwise end the command
    and start another one.
    """
    text = canonical(value)
    if text == "":
        return '""'
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
