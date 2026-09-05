"""Build the right DeviceDriver for a Site."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import get_settings
from app.drivers.base import DeviceDriver, DriverError
from app.drivers.ros6_ssh import Ros6SshDriver
from app.drivers.ros7_rest import Ros7RestDriver
from app.models.enums import DeviceKind
from app.models.site import Site
from app.security import SecretBox

log = logging.getLogger(__name__)

_DEFAULT_PORTS = {
    DeviceKind.ros7: 443,
    DeviceKind.ros6: 22,
    DeviceKind.softhub: 8443,
}


def build_driver(site: Site, box: SecretBox | None = None) -> DeviceDriver:
    box = box or SecretBox()
    settings = get_settings()
    password = box.decrypt(site.password_enc) if site.password_enc else ""
    port = site.mgmt_port or _DEFAULT_PORTS[site.device_kind]

    match site.device_kind:
        case DeviceKind.ros7 | DeviceKind.softhub:
            return Ros7RestDriver(
                site.mgmt_host,
                site.username,
                password,
                port=port,
                verify_tls=site.verify_tls,
                connect_timeout=settings.device_connect_timeout,
                read_timeout=settings.device_read_timeout,
                pinned_fingerprint=site.tls_fingerprint,
                pin=settings.pin_device_identity,
            )
        case DeviceKind.ros6:
            return Ros6SshDriver(
                site.mgmt_host,
                site.username,
                password,
                port=port,
                client_key=box.decrypt(site.ssh_key_enc) if site.ssh_key_enc else None,
                connect_timeout=settings.device_connect_timeout,
                read_timeout=settings.device_read_timeout,
                pinned_host_key=site.ssh_host_key,
                pin=settings.pin_device_identity,
            )

    raise DriverError(f"no driver for device kind {site.device_kind}")


@asynccontextmanager
async def open_driver(
    site: Site, box: SecretBox | None = None
) -> AsyncIterator[DeviceDriver]:
    """Open a connection and record any device identity learned on first contact.

    The Site object is mutated in place rather than written here: whichever
    session owns it decides when to commit, and a read-only caller should not
    be silently issuing writes.
    """
    driver = build_driver(site, box)
    await driver.connect()
    try:
        learned_tls = getattr(driver, "learned_fingerprint", None)
        if learned_tls and not site.tls_fingerprint:
            log.info("pinned TLS certificate for %s: %s", site.name, learned_tls)
            site.tls_fingerprint = learned_tls

        learned_ssh = getattr(driver, "learned_host_key", None)
        if learned_ssh and not site.ssh_host_key:
            log.info("pinned SSH host key for %s", site.name)
            site.ssh_host_key = learned_ssh

        yield driver
    finally:
        await driver.close()
