"""SSH-held Safe Mode sessions: the transactional half of B1.

RouterOS Safe Mode is a console feature: changes made while a session
holds it are live but tentative, they commit when the holder releases
Safe Mode cleanly, and they roll back when the holding session dies.
Verified on real RouterOS 7.24.5 (CHR, see
``~/workspace/chr-harness/FINDINGS-bfd-safemode.md``):

* Ctrl+X (0x18) over an SSH **PTY** takes Safe Mode -- the console prints
  "Taking Safe Mode session... Success!" and the prompt gains a marker:
  ``[admin@CHR] <SAFE>``. (A non-PTY exec channel cannot do this: the
  console features that matter here -- safe mode, and the password prompt
  -- need a terminal.)
* A second Ctrl+X releases it ("Releasing Safe Mode... Success!") and the
  changes persist.
* Kill the SSH client without releasing and the held changes are already
  rolled back at the first poll a second later. The documented ~9 minute
  delay is only the silent-partition case, where the device cannot tell
  the session is gone until TCP times out. On a healthy network, closing
  the socket *is* the abort.

Changes pushed over REST by the normal driver are covered while the hold
lasts: Safe Mode is a property of the device's configuration session, not
of the connection that typed them. So an apply becomes: hold Safe Mode
here, push a chunk over REST, verify, release; the chunk either commits
whole or vanishes when this session closes.

**The human-lock rule.** If Safe Mode cannot be acquired -- because the
prompt never shows ``<SAFE>`` -- the caller falls back to the legacy
dead-man apply. That includes the case where a *human* already holds
Safe Mode from WinBox or another console: this module deliberately does
not try to tell "held by a person" apart from "device would not give it
to us". Both look identical from here (no ``<SAFE>`` prompt), both get
the same answer (fall back, do not hijack), and pretending to distinguish
them would invent a detection mechanism RouterOS does not offer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import asyncssh

from app.drivers.base import DriverError
from app.drivers.identity import IdentityMismatch

log = logging.getLogger(__name__)

_CTRL_X = "\x18"
_SAFE_MARKER = "<SAFE>"


class SafeModeUnavailable(DriverError):
    """Safe Mode could not be acquired, for any reason.

    One exception for every acquisition failure -- connect, auth, timeout,
    another session (human or otherwise) already holding it -- because the
    caller's response is the same for all of them: fall back to the legacy
    apply. The message keeps the underlying reason for the job log.
    """


class SafeModeSession(Protocol):
    """One holdable Safe Mode session against one device.

    Lifecycle within a session object: ``enter`` (possibly repeatedly --
    after a ``commit`` the same SSH connection re-enters for the next
    chunk), ``commit`` per chunk, ``abort`` to abandon the held chunk, and
    ``close`` when the apply is done. Exactly one shape is implemented
    (``SshSafeModeSession``) and this protocol is what ``safe_apply`` and
    its tests program against.
    """

    async def enter(self) -> None:
        """Take Safe Mode, connecting first if needed.

        Raises SafeModeUnavailable if the ``<SAFE>`` prompt cannot be
        confirmed. A no-op when this session already holds Safe Mode.
        """
        ...

    async def commit(self) -> None:
        """Release Safe Mode, persisting the held changes.

        The SSH connection stays open afterwards, so the same session can
        ``enter`` again for the next chunk -- Safe Mode is a toggle on a
        console session, not the session itself. Raises DriverError when
        the release cannot be confirmed; the held changes are then in an
        unknown state and the caller must treat the apply as failed.
        """
        ...

    async def abort(self) -> None:
        """Abandon the held changes: close the connection WITHOUT releasing.

        Closing the socket is what makes the device roll the held chunk
        back (see the module docstring), so after ``abort`` this session
        is spent -- a later ``enter`` reconnects from scratch. Never
        raises: an abort that "failed" must not mask the error that
        caused it.
        """
        ...

    async def close(self) -> None:
        """Close the session, if open. Idempotent, never raises.

        Closing while Safe Mode is still held abandons the held changes,
        exactly like ``abort`` -- callers commit first when they mean to
        keep them.
        """
        ...


class SshSafeModeSession:
    """A Safe Mode session over asyncssh, mirroring Ros6SshDriver's
    connection options and host-key pinning.

    The duplication of the options block is deliberate at this size: the
    two SSH users (the ROS6 console driver and this session) need the
    same handshake policy -- pin if we have one, learn on first contact,
    refuse a mismatch *before* the password crosses -- and a shared helper
    would have to be parameterised by exactly the parts that differ.
    """

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
        pinned_host_key: str | None = None,
        pin: bool = True,
    ) -> None:
        self.host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_key = client_key
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._pin = pin
        self._pinned_host_key = pinned_host_key
        # Set on first contact so the caller can persist it, mirroring the
        # ROS6 driver. (apply_site does not persist it today -- the REST
        # driver's TLS pin is the primary identity for ROS7 sites -- but
        # the session learns it all the same rather than discarding it.)
        self.learned_host_key: str | None = None
        self._conn: asyncssh.SSHClientConnection | None = None
        self._process: Any = None
        self._holding = False

    # -- lifecycle ---------------------------------------------------------

    async def enter(self) -> None:
        if self._holding:
            return
        try:
            await self._connect()
            process = self._process
            await self._drain(process)
            process.stdin.write(_CTRL_X)
            seen = await self._read_until(
                process, (_SAFE_MARKER,), within=min(self._read_timeout, 15.0)
            )
        except SafeModeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one signal for the caller
            raise SafeModeUnavailable(
                f"{self.host}: could not take safe mode ({exc})"
            ) from exc
        if _SAFE_MARKER not in seen:
            # Covers the human-lock case as well as a device that simply
            # refuses: from here they are indistinguishable, by design.
            await self.close()
            raise SafeModeUnavailable(
                f"{self.host}: safe mode was not granted (no <SAFE> prompt; "
                "another session may already hold it)"
            )
        self._holding = True
        log.info("%s: safe mode held", self.host)

    async def commit(self) -> None:
        if not self._holding:
            raise DriverError(f"{self.host}: commit without holding safe mode")
        process = self._process
        process.stdin.write(_CTRL_X)
        seen = await self._read_until(
            process, ("Safe Mode released",), within=min(self._read_timeout, 15.0)
        )
        # Confirmed when the console says so ("Releasing Safe Mode...
        # Success!" / "Safe Mode released" -- both appear on 7.24.5), or
        # when a fresh prompt comes back without the safe marker. Anything
        # else -- silence, or a prompt still wearing <SAFE> -- means the
        # release cannot be vouched for, and guessing here would report a
        # commit that may roll back when the session eventually dies.
        lowered = seen.lower()
        confirmed = (
            "released" in lowered
            or "releasing safe mode" in lowered
            or (">" in seen and _SAFE_MARKER not in seen)
        )
        if not confirmed:
            raise DriverError(
                f"{self.host}: safe mode release was not confirmed"
            )
        self._holding = False
        log.info("%s: safe mode released (chunk committed)", self.host)

    async def abort(self) -> None:
        # No second Ctrl+X, no clean exit: the socket closing is the
        # rollback trigger. Everything after this is a fresh session.
        await self.close()

    async def close(self) -> None:
        process, self._process = self._process, None
        conn, self._conn = self._conn, None
        self._holding = False
        try:
            if process is not None:
                process.stdin.write_eof()
                process.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
        if conn is not None:
            try:
                conn.close()
                await asyncio.wait_for(conn.wait_closed(), timeout=5)
            except Exception:  # noqa: BLE001 - closing must never raise
                pass

    # -- plumbing ----------------------------------------------------------

    async def _connect(self) -> None:
        if self._conn is not None and self._process is not None:
            return
        options: dict[str, Any] = {"username": self._username}
        if self._pin and self._pinned_host_key:
            try:
                trusted = asyncssh.import_public_key(self._pinned_host_key)
            except Exception as exc:  # malformed pin is an operator error
                raise SafeModeUnavailable(
                    f"{self.host}: stored SSH host key is unreadable ({exc})"
                ) from exc
            options["known_hosts"] = ([trusted], [], [])
        else:
            options["known_hosts"] = None
        if self._client_key:
            options["client_keys"] = [asyncssh.import_private_key(self._client_key)]
        else:
            options["password"] = self._password

        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(self.host, port=self._port, **options),
                timeout=self._connect_timeout,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            # An identity mismatch is NOT "safe mode unavailable" in any
            # useful sense -- it is the interception signal, and the REST
            # leg would refuse it too. But enter()'s contract gives the
            # caller one fallback signal, and falling back lands on the
            # REST driver's own pin check, which refuses properly. The
            # message keeps the word "mismatch" so the job log does not
            # read as a routine fallback.
            raise SafeModeUnavailable(
                f"{self.host}: SSH host key mismatch ({exc})"
            ) from exc
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            raise SafeModeUnavailable(
                f"{self.host}: cannot open the safe-mode SSH session ({exc})"
            ) from exc

        if self._pin and not self._pinned_host_key:
            key = self._conn.get_server_host_key()
            if key is not None:
                self.learned_host_key = key.export_public_key().decode().strip()

        try:
            # term_type is what makes this a console: without a PTY the
            # device will not offer Safe Mode at all.
            self._process = await self._conn.create_process(term_type="xterm")
        except (OSError, asyncssh.Error) as exc:
            await self.close()
            raise SafeModeUnavailable(
                f"{self.host}: could not open a console process ({exc})"
            ) from exc

    async def _drain(self, process: Any, idle: float = 1.5) -> None:
        """Read the login banner/prompt until the console goes quiet.

        Sending Ctrl+X into the middle of the banner is harmless (the
        console buffers input), but waiting for the prompt first keeps the
        ``<SAFE>`` search below from having to reason about half-printed
        login text.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(self._connect_timeout, 5.0)
        while loop.time() < deadline:
            try:
                await asyncio.wait_for(process.stdout.read(4096), timeout=idle)
            except TimeoutError:
                return

    async def _read_until(
        self, process: Any, markers: tuple[str, ...], *, within: float
    ) -> str:
        """Read console output until any marker appears or time runs out.

        Returns whatever was read (possibly without any marker); the
        caller decides what counts as confirmation. Output is decoded
        lossily -- the console interleaves ANSI escapes and box-drawing
        characters, and none of them matter next to the marker text.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + within
        seen = ""
        while loop.time() < deadline and not any(m in seen for m in markers):
            try:
                chunk = await asyncio.wait_for(
                    process.stdout.read(4096),
                    timeout=max(deadline - loop.time(), 0.1),
                )
            except TimeoutError:
                break
            if not chunk:
                break  # EOF: the console went away
            seen += chunk.decode("utf-8", errors="replace") if isinstance(
                chunk, bytes
            ) else str(chunk)
        return seen


# IdentityMismatch is a DriverError the ROS6 driver raises for the same
# condition on its own path; re-exported here so callers of this module
# see the full family from one import if they ever need it.
__all__ = [
    "IdentityMismatch",
    "SafeModeSession",
    "SafeModeUnavailable",
    "SshSafeModeSession",
]
