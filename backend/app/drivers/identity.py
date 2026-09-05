"""Trust-on-first-use pinning of device identity.

RouterOS ships a self-signed certificate and an unmanaged SSH host key, so
ordinary chain validation is not available and `verify_tls` defaults to off.
That leaves the management connection encrypted but the far end
*unauthenticated* -- an attacker who can intercept traffic to a router collects
its credentials and every pre-shared key pushed through it.

Pinning closes most of that. Record the device's identity the first time it is
contacted; refuse to talk to anything presenting a different one afterwards. The
first contact is still trusted blindly -- that is inherent to TOFU -- so pin over
a network you trust, and treat a later mismatch as hostile until proven boring.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl


class IdentityMismatch(Exception):
    """The device presented a different identity than the one pinned.

    Deliberately not a DriverError subclass: this is not a transport problem to
    be retried, it is a refusal to continue.
    """


def format_fingerprint(digest: str) -> str:
    """aabbcc... -> AA:BB:CC:..., which is how people compare these by eye."""
    pairs = [digest[i : i + 2] for i in range(0, len(digest), 2)]
    return ":".join(p.upper() for p in pairs)


def certificate_fingerprint(der: bytes) -> str:
    """SHA-256 over the DER form, the same value OpenSSL prints."""
    return format_fingerprint(hashlib.sha256(der).hexdigest())


def _fetch_peer_certificate(host: str, port: int, timeout: float) -> bytes:
    """Complete a TLS handshake purely to read the certificate.

    Verification is off on purpose: the point is to see what the far end
    presents, not to judge it against a CA that was never going to sign it.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ConnectionError(f"{host}:{port} presented no certificate")
    return der


async def peer_fingerprint(
    host: str, port: int, connect_timeout: float = 10.0
) -> str:
    """The far end's certificate fingerprint, without blocking the loop.

    The timeout is applied at the socket, not with ``asyncio.timeout``: the
    handshake runs in a worker thread, and cancelling the await would return
    control while leaking the thread. The socket deadline actually stops it.
    """
    der = await asyncio.to_thread(_fetch_peer_certificate, host, port, connect_timeout)
    return certificate_fingerprint(der)


def check_pin(expected: str | None, presented: str, *, what: str, host: str) -> None:
    """Compare a presented identity against the pinned one.

    ``expected`` being None is first contact and is accepted -- the caller is
    responsible for storing what it learned.
    """
    if expected is None:
        return
    if _normalise(expected) != _normalise(presented):
        raise IdentityMismatch(
            f"{host}: the {what} does not match the one pinned for this site.\n"
            f"  pinned:    {expected}\n"
            f"  presented: {presented}\n"
            "If the device was legitimately rebuilt or re-keyed, clear the pin on "
            "the site and connect again. If it was not, treat this as an active "
            "interception: the credentials for this device should be considered "
            "compromised."
        )


def _normalise(value: str) -> str:
    return value.strip().replace(":", "").lower()
