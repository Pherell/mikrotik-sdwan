"""Periodic uplink re-detection.

Uplink detection used to run exactly once, when a site was probed or
enrolled, and every fact it learned was then frozen into the Wan row. That
is fine for a leased line with a static address and wrong for almost
everything else a branch actually has: a PPPoE session comes back with a new
address after every reconnect, a DHCP lease moves, an LTE modem is
CGNAT-ed one day and not the next. The controller kept building tunnels to
an address the device had stopped holding, and nothing said so until a
human probed by hand and read the conflicts.

This module re-runs the same detection (services.probe.detect_uplinks) and
compares it with what is stored, per site, under the site's ``uplink_sync``
policy:

``off``
    Never looked at. For a site whose uplinks are managed by hand on purpose.

``report`` (default)
    Differences are computed and returned; nothing is written. This is what
    GET /sites/{id}/uplinks/check always does, whatever the mode.

``auto``
    The *facts* of *dynamic* uplinks -- public_ip, gateway, prefix_len,
    nat_behind -- are kept current. Everything else stays report-only:

    * a static uplink is never changed. Its facts were entered by someone,
      possibly deliberately differently from what the router sees (a port
      forward is exactly that), and the device's view is evidence, not
      authority;
    * a newly seen uplink is added ``enabled=False`` with
      ``tags.pending_review``. An uplink appearing is not consent to route
      over it -- it could be a test modem plugged in for an hour;
    * a vanished uplink is reported, never deleted. Links and policies hang
      off the row, and an interface that disappears is far more often a
      renamed or unplugged port than a decommissioned ISP.

Nothing here ever pushes configuration. An automatic change makes the site's
rendered intent differ from the device, so the site is marked ``drifted``
with an explanatory last_error -- the same state the drift sweep uses, which
the UI already shows, which an apply clears, and which the drift sweep itself
clears if the change turns out not to affect anything rendered. Reusing it
avoided a new column whose only meaning would have been "drifted, but for
this particular reason".

Every automatic change is written to the audit trail, attributed to the
sweep rather than to a person.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.base import DriverError
from app.drivers.factory import open_driver
from app.drivers.identity import IdentityMismatch
from app.models.enums import SiteStatus
from app.models.job import AuditEvent
from app.models.site import Site, Wan
from app.schemas.site import UplinkChange, UplinkCheck, WanCreate
from app.services.probe import UplinkDetection, _loaded_wans, detect_uplinks

log = logging.getLogger(__name__)

# The facts re-detection compares, in the order they are reported. Only these
# are ever written automatically; name, cost, enabled, masquerade, provider
# and tags are the operator's and never touched.
DYNAMIC_FACTS = ("public_ip", "gateway", "prefix_len", "nat_behind")

PENDING_TAG = "pending_review"
SWEEP_ACTOR = "system:uplink-sweep"


def diff_uplinks(
    stored: list[Wan], found: UplinkDetection
) -> list[UplinkChange]:
    """What differs between the stored uplinks and one detection pass.

    Pure: no writes, no IO. Uplinks are matched by interface name, which is
    the one thing both sides agree on -- the wan1/wan2 names detection
    invents are positional and mean nothing against a stored row.
    """
    observed = {w.interface: w for w in found.wans}
    # Interfaces the device still has but detection deliberately did not
    # offer (a LAN, a tunnel). A stored uplink on one of those has not
    # vanished, and calling it vanished would be noise.
    still_there = {n.interface for n in found.lan} | {n.interface for n in found.notes}
    by_iface = {w.interface: w for w in stored}
    changes: list[UplinkChange] = []

    for wan in stored:
        device = observed.get(wan.interface)
        if device is None:
            if wan.enabled and wan.interface not in still_there:
                changes.append(
                    UplinkChange(
                        kind="vanished",
                        interface=wan.interface,
                        wan_id=wan.id,
                        wan_name=wan.name,
                        note=(
                            "The device no longer shows this interface as an "
                            "uplink. Not deleted: check whether it was renamed "
                            "or unplugged before removing it."
                        ),
                    )
                )
            continue

        if wan.interface in found.down:
            # A down PPPoE session has no address and an LTE modem with no
            # signal has no gateway. Comparing facts now would report (and in
            # auto mode, write) the absence of a value as a change.
            changes.append(
                UplinkChange(
                    kind="down",
                    interface=wan.interface,
                    wan_id=wan.id,
                    wan_name=wan.name,
                    note="Uplink is down on the device; its facts were not compared.",
                )
            )
            continue

        for fact in DYNAMIC_FACTS:
            before = getattr(wan, fact)
            after = getattr(device, fact)
            if before == after:
                continue
            if not _evidence(fact, after, device):
                continue
            changes.append(
                UplinkChange(
                    kind="changed",
                    interface=wan.interface,
                    wan_id=wan.id,
                    wan_name=wan.name,
                    field=fact,
                    stored=_text(before),
                    observed=_text(after),
                    note=None if wan.dynamic else (
                        "Static uplink: reported only, never changed automatically."
                    ),
                )
            )

    for iface, device in observed.items():
        if iface in by_iface:
            continue
        changes.append(
            UplinkChange(
                kind="new",
                interface=iface,
                observed=device.public_ip,
                note=(
                    "down on the device right now"
                    if iface in found.down
                    else None
                ),
            )
        )
    return changes


async def check_site_uplinks(
    session: AsyncSession, site: Site, *, apply: bool | None = None
) -> UplinkCheck:
    """Detect, compare, and -- in ``auto`` mode with ``apply`` -- update.

    ``apply`` defaults to "whatever the site's policy says"; the read-only
    API passes False so that a check never writes, whatever the mode.
    """
    mode = site.uplink_sync or "report"
    if apply is None:
        apply = mode == "auto"

    try:
        async with open_driver(site) as driver:
            found = await detect_uplinks(driver)
    except (DriverError, IdentityMismatch) as exc:
        # Not this job's to mark the site unreachable: the telemetry poll and
        # drift sweep already own the site's status, and a third writer would
        # only make it flap.
        return UplinkCheck(site_id=site.id, mode=mode, reachable=False, error=str(exc))

    stored = _loaded_wans(site)
    changes = diff_uplinks(stored, found)
    result = UplinkCheck(site_id=site.id, mode=mode, reachable=True, changes=changes)
    if not apply or mode != "auto" or not changes:
        return result

    by_id = {w.id: w for w in stored}
    observed = {w.interface: w for w in found.wans}
    written = 0
    for change in changes:
        if change.kind == "changed":
            wan = by_id.get(change.wan_id or "")
            if wan is None or not wan.dynamic:
                continue
            new_value = getattr(observed[wan.interface], change.field or "")
            setattr(wan, change.field or "", new_value)
            change.applied = True
            written += 1
            _audit(
                session, site,
                action="wan.auto_update",
                object_type="wan",
                object_id=wan.id,
                detail={
                    "wan": wan.name,
                    "interface": wan.interface,
                    "field": change.field,
                    "from": change.stored,
                    "to": change.observed,
                },
            )
        elif change.kind == "new":
            wan = _pending_wan(site, stored, observed[change.interface])
            session.add(wan)
            stored.append(wan)
            await session.flush()
            change.wan_id = wan.id
            change.wan_name = wan.name
            change.applied = True
            written += 1
            _audit(
                session, site,
                action="wan.auto_add",
                object_type="wan",
                object_id=wan.id,
                detail={
                    "wan": wan.name,
                    "interface": wan.interface,
                    "enabled": False,
                    "pending_review": True,
                },
            )

    if written:
        result.needs_apply = True
        _mark_needs_apply(site, written)
        await session.flush()
    return result


def _pending_wan(site: Site, stored: list[Wan], device: WanCreate) -> Wan:
    """A newly seen uplink, added so an operator can see it -- and only that.

    Disabled, so nothing renders for it; tagged so the UI and the API can
    list what is waiting. Costed after every existing uplink so enabling it
    later does not silently reorder failover.
    """
    taken = {w.name for w in stored}
    index = len(stored) + 1
    while f"wan{index}" in taken:
        index += 1
    cost = max((w.cost for w in stored if w.cost is not None), default=0.0) + 1.0
    data: dict[str, Any] = device.model_dump()
    data.update(
        name=f"wan{index}",
        cost=cost,
        enabled=False,
        tags={**(device.tags or {}), PENDING_TAG: True},
    )
    return Wan(site_id=site.id, **data)


def _mark_needs_apply(site: Site, written: int) -> None:
    """Flag the site as differing from its rendered intent.

    Only a healthy or already-drifted site is touched: an error or an
    unreachable status carries a message someone has to see, and replacing
    it with this one would hide it.
    """
    if site.status not in (SiteStatus.reachable, SiteStatus.drifted):
        return
    site.status = SiteStatus.drifted
    site.last_error = (
        f"Uplink re-detection updated {written} uplink fact(s); "
        "apply the site to push the change."
    )


def _audit(
    session: AsyncSession,
    site: Site,
    *,
    action: str,
    object_type: str,
    object_id: str,
    detail: dict,
) -> None:
    """Audit row for an automatic change.

    Written directly rather than through deps.write_audit: that helper
    takes the tenant from the acting user, and there is no user here --
    with actor=None it would file the event under the default tenant, out
    of sight of the tenant whose uplink changed.
    """
    session.add(
        AuditEvent(
            tenant_id=site.tenant_id,
            actor_id=None,
            actor_email=SWEEP_ACTOR,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail={**detail, "site_id": site.id, "site": site.name},
        )
    )


def _evidence(fact: str, value: object, device: WanCreate) -> bool:
    """Whether the device actually said something about this fact.

    Absence is not evidence. The device not showing a gateway (a PPP link
    has none) or a public address does not prove the stored one is wrong --
    a port-forwarded public IP is invisible on the router. The exception is
    an uplink whose address the device *does* show and which is private:
    that positively says the uplink is behind NAT, so public_ip=None is then
    a real observation. An interface with no address at all says nothing
    about NAT either way.
    """
    has_address = device.public_ip is not None or device.nat_behind
    if fact == "nat_behind":
        return has_address
    if fact == "public_ip" and value is None:
        return device.nat_behind
    return value is not None


def _text(value: object) -> str | None:
    return None if value is None else str(value).lower() if isinstance(value, bool) else str(value)


async def sweep_uplinks(session: AsyncSession) -> dict[str, int]:
    """Re-detect every reachable site whose policy is not ``off``.

    One site failing -- unreachable, or a bug tripped by one device's odd
    output -- is logged and counted, never allowed to stop the rest.
    """
    sites = list(
        await session.scalars(
            select(Site).where(
                Site.status.in_([SiteStatus.reachable, SiteStatus.drifted]),
                Site.uplink_sync != "off",
            )
        )
    )
    counts = {"checked": 0, "changed": 0, "applied": 0, "failed": 0}
    for site in sites:
        try:
            result = await check_site_uplinks(session, site)
        except Exception:
            log.exception("uplink re-detection crashed for %s", site.name)
            counts["failed"] += 1
            continue
        if not result.reachable:
            counts["failed"] += 1
            continue
        counts["checked"] += 1
        if any(c.kind in ("changed", "new", "vanished") for c in result.changes):
            counts["changed"] += 1
        counts["applied"] += sum(1 for c in result.changes if c.applied)
    return counts
