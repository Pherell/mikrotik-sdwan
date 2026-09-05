"""Baseline per-site configuration.

This is what every managed site gets regardless of which fabrics it joins: a
loopback the overlay can address, and an address-list naming the prefixes the
site originates. Fabric-specific rendering (tunnels, crypto, BGP) lands in M3
and layers on top of these.
"""

from __future__ import annotations

from ipaddress import ip_interface

from app.drivers.base import OWNER_PREFIX, ConfigItem, ConfigSection
from app.models.site import Site
from app.render.engine import owner_tag, section

# RouterOS has no loopback interface type. The idiomatic substitute is a bridge
# with no ports, which stays up independently of any physical link.
LOOPBACK_NAME = "lo-sdwan"

# Ownership scope for baseline rows. Deliberately excludes the site name: a
# device belongs to exactly one site, and scoping by name would orphan every
# row the moment the site is renamed.
SITE_SCOPE = OWNER_PREFIX + "site:"


def render_site(site: Site) -> list[ConfigSection]:
    """Sections the controller owns on this device, before any fabric."""
    scope = owner_tag("site", site.name)
    sections: list[ConfigSection] = [
        _loopback_interface(scope),
        _loopback_address(site, scope),
        _local_prefixes(site, scope),
    ]
    return [s for s in sections if s.items or s.owner_tag]


def _loopback_interface(scope: str) -> ConfigSection:
    tag = f"{scope}:loopback"
    return section(
        "/interface/bridge",
        "interface",
        owner=SITE_SCOPE,
        key=("name",),
        items=[
            ConfigItem(
                props={
                    "name": LOOPBACK_NAME,
                    # No ports are ever added, so STP would only burn CPU.
                    "protocol-mode": "none",
                },
                tag=tag,
            )
        ],
    )


def _loopback_address(site: Site, scope: str) -> ConfigSection:
    tag = f"{scope}:loopback-address"
    items: list[ConfigItem] = []
    if site.loopback_ip:
        # A loopback is a single host; force /32 even if the operator typed a
        # prefix length, otherwise RouterOS installs a connected route covering
        # other sites' loopbacks and blackholes them.
        address = f"{ip_interface(site.loopback_ip).ip}/32"
        items.append(
            ConfigItem(
                props={"address": address, "interface": LOOPBACK_NAME},
                tag=tag,
            )
        )
    return section(
        "/ip/address",
        "address",
        owner=SITE_SCOPE,
        key=("address",),
        items=items,
    )


def _local_prefixes(site: Site, scope: str) -> ConfigSection:
    """The prefixes this site originates, as an address-list other rules match."""
    tag = f"{scope}:local"
    list_name = f"sdwan-local-{site.name}"
    items = [
        ConfigItem(
            props={"list": list_name, "address": prefix},
            tag=tag,
        )
        for prefix in sorted(site.local_prefixes or [])
    ]
    return section(
        "/ip/firewall/address-list",
        "address_list",
        owner=SITE_SCOPE,
        key=("list", "address"),
        items=items,
    )
