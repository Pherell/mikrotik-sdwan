"""Build the right DeviceDriver for a Site."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import get_settings
from app.drivers.base import DeviceDriver, DriverError
from app.drivers.ros6_ssh import Ros6SshDriver
from app.drivers.ros7_rest import Ros7RestDriver
from app.drivers.safemode import SafeModeSession, SshSafeModeSession
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


def build_safe_mode_session(
    site: Site, box: SecretBox | None = None
) -> SafeModeSession | None:
    """A Safe Mode session for this site, or None when B1 does not apply.

    ROS7/softhub only. ROS6 gets None: its driver *is* the SSH console,
    its applies are one command at a time with no REST leg for Safe Mode
    to cover, and the legacy dead-man flow remains its safety net.

    The session is returned unconnected -- ``enter()`` opens it. The SSH
    port is the site's own ``ssh_port`` (default 22), never ``mgmt_port``:
    for a ROS7 site that is the REST port, and knocking there with SSH
    would fail every apply into the legacy flow for a silly reason.
    Credentials are the site's management credentials; a site carrying an
    SSH client key uses it, mirroring the ROS6 driver's auth choice.
    """
    if site.device_kind not in (DeviceKind.ros7, DeviceKind.softhub):
        return None
    box = box or SecretBox()
    settings = get_settings()
    password = box.decrypt(site.password_enc) if site.password_enc else ""
    return SshSafeModeSession(
        site.mgmt_host,
        site.username,
        password,
        port=site.ssh_port or 22,
        client_key=box.decrypt(site.ssh_key_enc) if site.ssh_key_enc else None,
        connect_timeout=settings.device_connect_timeout,
        read_timeout=settings.device_read_timeout,
        pinned_host_key=site.ssh_host_key,
        pin=settings.pin_device_identity,
    )


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
