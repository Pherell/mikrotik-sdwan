"""Failures during the identity probe have to look like driver failures.

``peer_fingerprint`` runs its own TLS handshake before authentication and
before httpx is involved at all, so nothing in the driver's error mapping sees
it. An ``ssl.SSLError`` from that handshake escaped every exception handler the
API installs, and FastAPI turned it into a bare 500 -- which the UI rendered as
the single word "500" next to a Plan button, with no indication that the device
had refused the TLS handshake.
"""

from __future__ import annotations

import ssl
from unittest.mock import patch

import pytest

from app.drivers.base import DeviceUnreachable, DriverError
from app.drivers.identity import peer_fingerprint

HANDSHAKE_FAILURE = ssl.SSLError(
    1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure (_ssl.c:1010)"
)


async def test_a_refused_handshake_is_a_driver_error() -> None:
    """Not a bare SSLError, which no exception handler catches."""
    with patch(
        "app.drivers.identity._fetch_peer_certificate", side_effect=HANDSHAKE_FAILURE
    ):
        with pytest.raises(DeviceUnreachable) as caught:
            await peer_fingerprint("10.10.10.30", 443)

    assert isinstance(caught.value, DriverError)


async def test_the_message_says_what_to_check_on_the_device() -> None:
    """A stack trace is not a fix. www-ssl without an assigned certificate is
    the usual cause and the operator cannot guess it."""
    with patch(
        "app.drivers.identity._fetch_peer_certificate", side_effect=HANDSHAKE_FAILURE
    ):
        with pytest.raises(DeviceUnreachable) as caught:
            await peer_fingerprint("10.10.10.30", 443)

    message = str(caught.value)
    assert "10.10.10.30:443" in message
    assert "SSLV3_ALERT_HANDSHAKE_FAILURE" in message, "keep the original cause"
    assert "/ip service" in message
    assert "certificate" in message


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionRefusedError("connection refused"),
        TimeoutError("timed out"),
        OSError("[Errno -2] Name or service not known"),
    ],
)
async def test_socket_failures_are_driver_errors_too(failure: Exception) -> None:
    """The same escape route: socket errors here bypass httpx entirely."""
    with patch("app.drivers.identity._fetch_peer_certificate", side_effect=failure):
        with pytest.raises(DeviceUnreachable) as caught:
            await peer_fingerprint("10.10.10.30", 443)

    assert "10.10.10.30:443" in str(caught.value)


async def test_a_successful_probe_still_returns_a_fingerprint() -> None:
    """The happy path must not have been wrapped away."""
    der = b"\x30\x82\x01\x0a"  # not a real certificate; only hashed
    with patch("app.drivers.identity._fetch_peer_certificate", return_value=der):
        fingerprint = await peer_fingerprint("10.10.10.30", 443)

    assert fingerprint.count(":") == 31, "SHA-256, colon separated"
