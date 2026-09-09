"""M8: one-touch provisioning.

An operator mints a token in the UI. A field tech pastes one line into a
factory-default router's terminal. The router fetches a script that gives it
a certificate, HTTPS management, and a dedicated controller-only account, then
calls back to confirm. The controller creates the site from the address that
call arrived on, probes it, and -- if the token named a fabric -- joins it and
applies. Nobody ever types or sees the device's password: it is generated
here, encrypted at rest, and used exactly twice -- once to build the script,
once to build the resulting Site row.

See docs/plan-v2.md M8 for the three-tier framing (this implements Tier 1,
one-touch; Tiers 2 and 3 are the same endpoint reached by DHCP option 66/67
or a pre-staged image instead of a pasted line -- no extra code, just how the
URL reaches the device).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.enrollment import EnrollmentToken
from app.models.enums import JobKind, SiteRole
from app.models.fabric import FabricMember
from app.models.site import Site, Wan
from app.security import (
    SecretBox,
    api_secret_matches,
    new_enrollment_token,
    split_enrollment_token,
)
from app.services.fabric import expand_fabric, load_fabric
from app.services.probe import apply_probe, probe_site
from app.services.reconcile import apply_site, new_job
from app.transports.base import generate_psk

log = logging.getLogger(__name__)


class EnrollmentError(Exception):
    """Base for a failure the API layer must show as something other than a
    bare 500 -- a device or an operator reading the message either way."""


class EnrollmentTokenInvalid(EnrollmentError):
    """No such token, expired, revoked, already used, or the wrong source."""


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even from a timezone-aware column;
    the same problem deps._aware exists for, on a different table."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def mint_enrollment_token(
    session: AsyncSession,
    *,
    tenant_id: str,
    created_by: str | None,
    name: str,
    site_name: str,
    site_role: SiteRole,
    local_prefixes: list[str],
    fabric_id: str | None,
    source_cidr: str | None,
    expires_in_hours: int,
) -> tuple[EnrollmentToken, str]:
    """Create the row and return it with the one-time credential.

    The generated device password is not part of the return value: it is
    encrypted onto the row and nothing outside this module and
    render_bootstrap_script ever needs the plaintext again.
    """
    credential, prefix, token_hash = new_enrollment_token()
    box = SecretBox()
    token = EnrollmentToken(
        tenant_id=tenant_id,
        created_by=created_by,
        name=name,
        prefix=prefix,
        token_hash=token_hash,
        site_name=site_name,
        site_role=site_role,
        local_prefixes=local_prefixes,
        fabric_id=fabric_id,
        device_password_enc=box.encrypt(generate_psk(24)),
        source_cidr=source_cidr,
        expires_at=utcnow() + timedelta(hours=expires_in_hours),
    )
    session.add(token)
    try:
        await session.flush()
    except IntegrityError as exc:  # pragma: no cover - a token_hex collision
        await session.rollback()
        raise EnrollmentError("Could not mint a unique token; try again") from exc
    return token, credential


async def _load_valid_token(
    session: AsyncSession, credential: str, source_ip: str | None
) -> EnrollmentToken:
    parts = split_enrollment_token(credential)
    if parts is None:
        raise EnrollmentTokenInvalid("Not an enrollment credential")
    prefix, secret = parts

    token = await session.scalar(
        select(EnrollmentToken).where(EnrollmentToken.prefix == prefix)
    )
    if token is None or not api_secret_matches(secret, token.token_hash):
        raise EnrollmentTokenInvalid("Invalid enrollment token")
    if token.revoked_at is not None:
        raise EnrollmentTokenInvalid("This token has been revoked")
    if token.used_at is not None:
        raise EnrollmentTokenInvalid("This token has already been used")
    if _aware(token.expires_at) <= utcnow():
        raise EnrollmentTokenInvalid("This token has expired")
    if token.source_cidr and source_ip:
        if ip_address(source_ip) not in ip_network(token.source_cidr, strict=False):
            raise EnrollmentTokenInvalid(
                "Not reachable from the address range this token is scoped to"
            )
    return token


async def render_bootstrap_script(
    session: AsyncSession, credential: str, source_ip: str | None, public_url: str
) -> str:
    """The .rsc a factory-default router fetches and imports.

    Read-only against the token -- fetching the script does not spend it.
    Only a successful confirm callback (confirm_enrollment) does, so a
    network hiccup between fetch and import can simply be retried.
    """
    token = await _load_valid_token(session, credential, source_ip)
    box = SecretBox()
    device_password = box.decrypt(token.device_password_enc)
    return _render_script(token, credential, device_password, public_url)


def _render_script(
    token: EnrollmentToken, credential: str, device_password: str, public_url: str
) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "-", token.site_name)[:20] or "sdwan"
    cert_name = f"{safe_name}-cert"[:31]
    confirm_url = f"{public_url.rstrip('/')}/api/v1/enroll/{credential}/confirm"

    # /user's address= restriction wants an IP, not a name. Only add it when
    # the controller's own address is one -- a public_url with a DNS name
    # simply gets a wider-open account rather than a script that fails to
    # import on every device that fetches it.
    host = urlsplit(public_url).hostname or ""
    address_clause = ""
    try:
        ip_address(host)
        address_clause = f" address={host}/32"
    except ValueError:
        pass

    # Escaped for a RouterOS double-quoted string: backslash first, then the
    # quote, so an escaped quote is not re-escaped by the quote pass after it.
    def _rsc_str(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return f"""# mikrotik-sdwan enrollment script -- generated, single use.
# Safe to read: the credential in the URL below is spent the moment this
# script's callback succeeds, so a copy of this file is worthless afterward.
:log info "sdwan enrollment ({_rsc_str(token.site_name)}): starting"

/certificate add name="{cert_name}" common-name="{safe_name}" days-valid=3650 key-usage=tls-server
/certificate sign "{cert_name}"
/ip service set www-ssl certificate="{cert_name}" disabled=no
/ip service set www disabled=yes
/ip service set api disabled=yes
/ip service set api-ssl disabled=yes
/ip service set telnet disabled=yes
/ip service set ftp disabled=yes

/user add name=sdwan password="{_rsc_str(device_password)}" group=full{address_clause}

:do {{
    :local result [/tool fetch url="{confirm_url}" http-method=post as-value output=none]
    :local statusLine ("sdwan enrollment ({_rsc_str(token.site_name)}): confirmed, status=" \
        . ($result->"status"))
    :log info $statusLine
}} on-error={{
    :log error "sdwan enrollment ({_rsc_str(token.site_name)}): callback failed"
}}
"""


async def confirm_enrollment(
    session: AsyncSession, credential: str, source_ip: str | None
) -> Site:
    """Spend the token: create the site at the address this call arrived
    from, probe it, and -- if the token named a fabric -- join and apply.

    The site is created and the token spent even if the probe or the fabric
    apply that follows fails; those are the normal ways a freshly-onboarded
    site starts out (unreachable, or reachable but not yet applied), not
    reasons to fail an enrollment that has already, factually, happened.
    """
    if source_ip is None:
        raise EnrollmentTokenInvalid("No source address to enroll the device at")
    token = await _load_valid_token(session, credential, source_ip)

    site = Site(
        tenant_id=token.tenant_id,
        name=token.site_name,
        role=token.site_role,
        mgmt_host=source_ip,
        username="sdwan",
        # The same ciphertext the script embedded in plaintext -- no need to
        # decrypt and re-encrypt what is already encrypted with this
        # installation's own key.
        password_enc=token.device_password_enc,
        local_prefixes=list(token.local_prefixes or []),
    )
    session.add(site)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise EnrollmentTokenInvalid(
            f"A site named {token.site_name!r} already exists in this tenant"
        ) from exc

    token.used_at = utcnow()
    token.used_from_ip = source_ip
    token.enrolled_site_id = site.id
    await session.flush()

    box = SecretBox()
    result = await probe_site(site, box)
    apply_probe(site, result)
    if result.reachable:
        # The onboarding wizard leaves suggested_wans for a human to review;
        # there is no human in this flow, so they are taken as-is. That is
        # the "no other human input" the enrollment done-when actually asks
        # for.
        #
        # session.add() per row, not site.wans = [...]: site was just
        # created in this session rather than loaded with wans eagerly
        # selected, so assigning the collection would first read its
        # *current* value to diff against -- a lazy load, which is exactly
        # the synchronous-IO-under-asyncio crash Timestamps.updated_at's own
        # docstring describes for the same underlying reason. sites.add_wan
        # avoids it the same way.
        for suggestion in result.suggested_wans:
            session.add(Wan(site_id=site.id, **suggestion.model_dump()))
    await session.flush()

    if token.fabric_id and result.reachable:
        await _join_fabric(session, site, token.fabric_id)

    return site


async def _join_fabric(session: AsyncSession, site: Site, fabric_id: str) -> None:
    """Best-effort: the enrollment itself has already succeeded by this
    point, so a fabric that has since been deleted, or an apply that fails,
    must not turn a real enrollment into an error response. The site is left
    onboarded and reachable either way; a fabric membership or an apply an
    operator can retry by hand is a smaller problem than that."""
    fabric = await load_fabric(session, fabric_id, site.tenant_id)
    if fabric is None:
        log.warning(
            "enrollment token for %s named fabric %s, which no longer exists",
            site.name, fabric_id,
        )
        return

    session.add(FabricMember(fabric_id=fabric.id, site_id=site.id))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log.warning("%s is already a member of fabric %s", site.name, fabric.name)
        return

    try:
        await expand_fabric(session, fabric)
        job = new_job(site, JobKind.apply, None)
        session.add(job)
        await session.flush()
        await apply_site(session, site, job)
    except Exception:  # pragma: no cover - defensive, see docstring
        log.exception("post-enrollment apply failed for %s", site.name)
