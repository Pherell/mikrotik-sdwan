"""Outbound alerting: notice state transitions, record them, push them out.

Before this, everything worth waking someone for -- a tunnel down, a site
gone dark, an apply that rolled itself back, a device drifting from intent --
was visible only to somebody already looking at the UI. This module turns
those into rows in ``alerts`` and, from the worker, into webhook / Telegram
messages.

Three rules shape all of it:

**Transitions, not observations.** The poller sees "down" every 30 seconds
for as long as a tunnel is down. Alerting on each of those would bury the one
message that matters (it went down) under a hundred that do not (it is still
down), and the first thing anyone does with a noisy channel is mute it. So
every hook here compares against the last state recorded in
``alert_states`` and fires only on an edge. A *good* first observation (up,
reachable, clean) only records a baseline -- there is no "it came back"
without a "it went away" -- while a *bad* first observation does fire, because
a link that is already down the first time we look is still news.

**Hooks never break their caller.** Each hook is one call from the poller,
the drift check, an apply or a probe, and each swallows (and logs) its own
failure. Observing an outage must never be the thing that makes the poll, or
the apply that caused it, fall over.

**Delivery is not in the hook.** Hooks only write a ``pending`` row inside
the caller's transaction -- so an apply that is rolled back takes its alert
with it, and no network call ever happens while a device session is open.
The worker's ``deliver_pending`` sweep sends them afterwards, with a short
timeout, a couple of retries on errors that might be transient, and the
destination's credentials kept out of every log line and error message.

Webhook payload
---------------

``POST <url>`` with ``Content-Type: application/json`` and
``User-Agent: mikrotik-sdwan-alerts/1``::

    {
      "version": 1,
      "source": "mikrotik-sdwan",
      "id": "6f1c...",                  # alert id; stable across retries,
                                        # use it to de-duplicate on your side
      "kind": "link_down",              # link_down | link_up | sla_breach |
                                        # drift_detected | apply_failed |
                                        # apply_rolled_back | site_unreachable |
                                        # site_reachable | test
      "severity": "critical",           # info | warning | critical
      "tenant_id": "default",
      "site": {"id": "...", "name": "branch-1"},   # null if not site-scoped
      "link_id": "...",                 # null unless a link event
      "message": "Link branch-1-hq is down (seen from branch-1)",
      "details": {...},                 # kind-specific, see each hook
      "created_at": "2026-10-01T08:00:00+00:00"
    }

Any 2xx is success. 429 and 5xx are retried, as are connection errors and
timeouts; any other status is a permanent failure for that alert.

Telegram uses the Bot API's ``sendMessage`` with ``chat_id`` and a plain-text
rendering of the same event (no ``parse_mode``: alert text includes device
names and error strings that Markdown/HTML would need escaping for, and a
message rejected for bad markup is an alert lost).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.alert import Alert, AlertState, NotificationChannel
from app.models.base import utcnow
from app.models.enums import AlertKind, AlertSeverity, ChannelType, JobState
from app.security import SecretBox, mask

log = logging.getLogger(__name__)

SEVERITY_RANK = {AlertSeverity.info: 0, AlertSeverity.warning: 1, AlertSeverity.critical: 2}

DEFAULT_SEVERITY: dict[AlertKind, AlertSeverity] = {
    AlertKind.link_down: AlertSeverity.critical,
    AlertKind.sla_breach: AlertSeverity.warning,
    AlertKind.link_up: AlertSeverity.info,
    AlertKind.site_unreachable: AlertSeverity.critical,
    AlertKind.site_reachable: AlertSeverity.info,
    AlertKind.drift_detected: AlertSeverity.warning,
    # failed: the device state is uncertain and somebody has to look.
    # rolled_back: the dead-man did its job and the router restored itself --
    # still worth knowing, but nothing is currently broken because of it.
    AlertKind.apply_failed: AlertSeverity.critical,
    AlertKind.apply_rolled_back: AlertSeverity.warning,
}

# Subject states. "bad" ones fire even on the first observation.
LINK_UP, LINK_DOWN, LINK_SLA = "up", "down", "sla_breach"
SITE_REACHABLE, SITE_UNREACHABLE = "reachable", "unreachable"
DRIFT_CLEAN, DRIFT_DRIFTED = "clean", "drifted"
_BAD_STATES = {LINK_DOWN, LINK_SLA, SITE_UNREACHABLE, DRIFT_DRIFTED}

TELEGRAM_API = "https://api.telegram.org"
USER_AGENT = "mikrotik-sdwan-alerts/1"
# Seconds before retry n is base * 2**n. Module-level so tests can zero it.
RETRY_BACKOFF_SECONDS = 0.5
# One sweep's worth. Anything beyond waits for the next sweep, which keeps a
# backlog (worker down for an hour) from turning one job into a ten-minute one.
DELIVERY_BATCH = 200


@dataclass
class AlertEvent:
    """One thing that happened, before it is a row.

    ``details`` is kind-specific and goes out verbatim in the webhook payload,
    so nothing secret may ever be put in it -- the hooks below only use
    counts, ids, states and already-masked device error strings.
    """

    kind: AlertKind
    tenant_id: str
    message: str
    severity: AlertSeverity | None = None
    site_id: str | None = None
    site_name: str | None = None
    link_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity is None:
            self.severity = DEFAULT_SEVERITY[self.kind]


# -- never let an alert hook fail its caller ---------------------------------

P = ParamSpec("P")
R = TypeVar("R")


# TypeVar/ParamSpec rather than PEP 695 syntax: see app.deps.get_owned (CI
# still runs 3.11).
def _never_raises(  # noqa: UP047
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R | None]]:
    """Log and swallow. Every public hook wears this: an exception while
    *recording* an outage must not become a second outage in the poll or
    apply that observed it."""

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return await fn(*args, **kwargs)
        except Exception:
            log.exception("alert hook %s failed; continuing without it", fn.__name__)
            return None

    return wrapper


# -- recording ---------------------------------------------------------------


async def record(session: AsyncSession, event: AlertEvent) -> Alert:
    """Write the event as a pending alert in the caller's transaction."""
    alert = Alert(
        tenant_id=event.tenant_id,
        kind=event.kind,
        severity=event.severity,
        site_id=event.site_id,
        site_name=event.site_name,
        link_id=event.link_id,
        message=event.message,
        details=event.details or None,
        delivery_state="pending",
    )
    session.add(alert)
    await session.flush()
    log.info("alert %s [%s] %s", event.kind, event.severity, event.message)
    return alert


async def _transition(
    session: AsyncSession,
    *,
    tenant_id: str,
    subject: str,
    site_id: str | None,
    new_state: str,
) -> tuple[bool, str | None]:
    """Store ``new_state`` for ``subject``; return (changed, previous).

    ``changed`` is True on an edge, and on a first observation that is
    already bad. A good first observation sets the baseline silently.
    """
    current = await session.scalar(
        select(AlertState)
        .where(AlertState.subject == subject)
        .order_by(AlertState.changed_at.desc())
        .limit(1)
    )
    if current is None:
        session.add(
            AlertState(
                tenant_id=tenant_id, subject=subject, site_id=site_id, state=new_state
            )
        )
        await session.flush()
        return new_state in _BAD_STATES, None
    if current.state == new_state:
        return False, current.state
    previous = current.state
    current.state = new_state
    current.changed_at = utcnow()
    await session.flush()
    return True, previous


# -- hooks -------------------------------------------------------------------


def _link_state(row: dict[str, Any]) -> str | None:
    """Netwatch row -> up / down / sla_breach, or None if it says nothing.

    RouterOS 7 netwatch reports a single ``status``: it goes ``down`` both
    when the far end stops answering and when it answers but breaches the
    thr-* thresholds the SLA profile rendered. The two need different
    alerts -- "the tunnel is gone" and "the tunnel is bad" page different
    people at different urgency -- so ``loss-percent`` tells them apart: total
    loss (or no loss figure at all) is down, anything less is an SLA breach.
    """
    status = str(row.get("status") or "").strip().lower()
    if status == "up":
        return LINK_UP
    if status != "down":
        return None  # "unknown" while netwatch warms up, or no status at all
    loss = row.get("loss-percent")
    try:
        loss_value = float(str(loss).rstrip("%")) if loss is not None else None
    except ValueError:
        loss_value = None
    if loss_value is None or loss_value >= 100:
        return LINK_DOWN
    return LINK_SLA


@_never_raises
async def observe_link(session: AsyncSession, site: Any, link: Any, row: dict[str, Any]) -> None:
    """Telemetry hook: one netwatch row for one link, seen from ``site``.

    Keyed per (link, site): both ends of a tunnel probe it, and each end's
    view is its own fact -- an asymmetric failure is exactly the case where
    they disagree.
    """
    new_state = _link_state(row)
    if new_state is None:
        return
    changed, previous = await _transition(
        session,
        tenant_id=site.tenant_id,
        subject=f"link:{link.id}@{site.id}",
        site_id=site.id,
        new_state=new_state,
    )
    if not changed:
        return
    name = getattr(link, "slug", None) or link.id
    details = {
        "previous": previous,
        "state": new_state,
        "loss_percent": row.get("loss-percent"),
        "rtt_avg": row.get("rtt-avg"),
        "rtt_jitter": row.get("rtt-jitter"),
        "host": row.get("host"),
    }
    if new_state == LINK_DOWN:
        kind, message = AlertKind.link_down, f"Link {name} is down (seen from {site.name})"
    elif new_state == LINK_SLA:
        kind = AlertKind.sla_breach
        message = (
            f"Link {name} is breaching its SLA (seen from {site.name}): "
            f"loss {row.get('loss-percent')}%, rtt {row.get('rtt-avg')}"
        )
    else:
        kind, message = AlertKind.link_up, f"Link {name} is up again (seen from {site.name})"
    await record(
        session,
        AlertEvent(
            kind=kind,
            tenant_id=site.tenant_id,
            site_id=site.id,
            site_name=site.name,
            link_id=link.id,
            message=message,
            details={k: v for k, v in details.items() if v is not None},
        ),
    )


@_never_raises
async def observe_reachability(
    session: AsyncSession, site: Any, *, reachable: bool, error: str | None = None
) -> None:
    """Telemetry / probe / drift hook: did the device answer just now?

    Shared state across every caller, so the poller and an interactive probe
    seeing the same outage produce one alert, not two.
    """
    changed, previous = await _transition(
        session,
        tenant_id=site.tenant_id,
        subject=f"site:{site.id}",
        site_id=site.id,
        new_state=SITE_REACHABLE if reachable else SITE_UNREACHABLE,
    )
    if not changed:
        return
    if reachable:
        kind, message = AlertKind.site_reachable, f"Site {site.name} is reachable again"
    else:
        kind = AlertKind.site_unreachable
        message = f"Site {site.name} is unreachable" + (f": {error}" if error else "")
    await record(
        session,
        AlertEvent(
            kind=kind,
            tenant_id=site.tenant_id,
            site_id=site.id,
            site_name=site.name,
            message=message,
            details={"previous": previous, **({"error": error} if error else {})},
        ),
    )


@_never_raises
async def observe_drift(
    session: AsyncSession, site: Any, *, drifted: bool, counts: dict[str, int] | None = None
) -> None:
    """Drift-check hook. Fires on clean -> drifted only; the hourly sweep
    finding the same drift again is not news, and drift going away is the
    result of somebody's deliberate apply, which they already know about."""
    changed, _ = await _transition(
        session,
        tenant_id=site.tenant_id,
        subject=f"drift:{site.id}",
        site_id=site.id,
        new_state=DRIFT_DRIFTED if drifted else DRIFT_CLEAN,
    )
    if not (changed and drifted):
        return
    counts = counts or {}
    await record(
        session,
        AlertEvent(
            kind=AlertKind.drift_detected,
            tenant_id=site.tenant_id,
            site_id=site.id,
            site_name=site.name,
            message=(
                f"Site {site.name} drifted from intent: {counts.get('add', 0)} missing, "
                f"{counts.get('set', 0)} changed, {counts.get('remove', 0)} unexpected"
            ),
            details={"changes": counts, "action": getattr(site, "drift_action", None)},
        ),
    )


@_never_raises
async def observe_apply(session: AsyncSession, site: Any, job: Any) -> None:
    """Apply hook. Not transition-based: every failed apply is its own
    event, with its own job id, and two in a row are two things to look at."""
    if job.state == JobState.rolled_back:
        kind = AlertKind.apply_rolled_back
        message = f"Apply to {site.name} failed and was rolled back"
    elif job.state == JobState.failed:
        kind, message = AlertKind.apply_failed, f"Apply to {site.name} failed"
    else:
        return
    if job.error:
        message += f": {job.error}"
    await record(
        session,
        AlertEvent(
            kind=kind,
            tenant_id=site.tenant_id,
            site_id=site.id,
            site_name=site.name,
            message=message,
            details={"job_id": job.id, "state": str(job.state), "backup": job.backup_name},
        ),
    )


# -- channel configuration ---------------------------------------------------


def encode_config(channel_type: ChannelType, config: dict[str, str]) -> tuple[str, str]:
    """(ciphertext, display hint) for a channel's destination.

    The hint keeps just enough to tell two channels apart -- a webhook's
    scheme and host, a Telegram chat id's first characters -- and nothing
    that would let the reader post to it.
    """
    if channel_type == ChannelType.webhook:
        parts = urlsplit(config["url"])
        hint = f"{parts.scheme}://{parts.hostname or ''}/***"
    else:
        hint = f"telegram chat {mask(str(config['chat_id']), keep=4)}"
    return SecretBox().encrypt(json.dumps(config)), hint


def decode_config(channel: NotificationChannel) -> dict[str, str]:
    return json.loads(SecretBox().decrypt(channel.config_enc))


def _secrets_of(config: dict[str, str]) -> list[str]:
    """Every value that must never appear in a log line or stored error.

    The whole webhook URL, not just its query string: for Slack, Teams,
    Discord and most others the path *is* the credential.
    """
    out = [str(v) for k, v in config.items() if k in ("url", "bot_token") and v]
    if "url" in config:
        parts = urlsplit(config["url"])
        # httpx error text sometimes shows the URL re-encoded or path-only.
        out += [p for p in (parts.path, parts.query) if p and len(p) > 1]
    return out


_TELEGRAM_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")


def redact(text: str, secrets_: list[str]) -> str:
    """Replace every known secret, longest first, plus anything shaped like
    a Telegram bot token (belt and braces: a token echoed back by a proxy in
    some other form is still a token)."""
    for secret in sorted(set(secrets_), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return _TELEGRAM_TOKEN.sub("***", text)


# -- delivery ----------------------------------------------------------------


class DeliveryError(Exception):
    """A send that did not succeed. The message is already redacted."""


def payload_of(alert: Alert | AlertEvent, *, alert_id: str | None = None,
               created_at: datetime | None = None) -> dict[str, Any]:
    created = created_at or getattr(alert, "created_at", None) or utcnow()
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "version": 1,
        "source": "mikrotik-sdwan",
        "id": alert_id or getattr(alert, "id", None),
        "kind": str(alert.kind),
        "severity": str(alert.severity),
        "tenant_id": alert.tenant_id,
        "site": {"id": alert.site_id, "name": alert.site_name} if alert.site_id else None,
        "link_id": alert.link_id,
        "message": alert.message,
        "details": alert.details or {},
        "created_at": created.isoformat(),
    }


def _telegram_text(payload: dict[str, Any]) -> str:
    site = (payload.get("site") or {}).get("name")
    head = f"[{payload['severity'].upper()}] {payload['kind']}"
    if site:
        head += f" @ {site}"
    return f"{head}\n{payload['message']}"


def make_client() -> httpx.AsyncClient:
    """The HTTP client delivery uses. A function so tests can swap in an
    ``httpx.MockTransport`` without touching the network."""
    return httpx.AsyncClient(
        timeout=get_settings().alert_delivery_timeout_seconds,
        # A redirect would re-send the payload somewhere the admin did not
        # configure; refuse rather than follow.
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )


async def send(
    client: httpx.AsyncClient,
    channel_type: ChannelType,
    config: dict[str, str],
    payload: dict[str, Any],
) -> None:
    """Deliver one payload to one destination, retrying what might be
    transient. Raises DeliveryError with a redacted reason on failure."""
    if channel_type == ChannelType.webhook:
        url, body = config["url"], payload
    else:
        url = f"{TELEGRAM_API}/bot{config['bot_token']}/sendMessage"
        body = {
            "chat_id": config["chat_id"],
            "text": _telegram_text(payload),
            "disable_web_page_preview": True,
        }
    secrets_ = _secrets_of(config)
    attempts = 1 + max(0, get_settings().alert_delivery_retries)
    reason = "not attempted"
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))
        try:
            resp = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            # Never str(exc) unredacted: httpx puts the full URL -- for a
            # webhook, the credential -- into most of its messages.
            reason = redact(f"{type(exc).__name__}: {exc}", secrets_)
            continue
        if 200 <= resp.status_code < 300:
            return
        reason = f"HTTP {resp.status_code}"
        if resp.status_code != 429 and resp.status_code < 500:
            break  # the request itself is wrong; sending it again changes nothing
    raise DeliveryError(reason)


def _qualifies(alert: Alert, channel: NotificationChannel) -> bool:
    return SEVERITY_RANK[AlertSeverity(alert.severity)] >= SEVERITY_RANK[
        AlertSeverity(channel.min_severity)
    ]


async def deliver_to_channel(
    client: httpx.AsyncClient, channel: NotificationChannel, payload: dict[str, Any]
) -> str | None:
    """Send and record the outcome on the channel row. Returns the redacted
    error, or None on success. Never raises."""
    try:
        config = decode_config(channel)
    except Exception:
        error = "stored destination could not be decrypted (SDWAN_SECRET_KEY changed?)"
    else:
        try:
            await send(client, ChannelType(channel.type), config, payload)
            error = None
        except DeliveryError as exc:
            error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            error = redact(f"{type(exc).__name__}: {exc}", _secrets_of(config))
    channel.last_status = "ok" if error is None else "error"
    channel.last_error = error
    channel.failure_count = 0 if error is None else (channel.failure_count or 0) + 1
    if error is not None:
        log.warning("alert delivery to channel %r failed: %s", channel.name, error)
    return error


async def deliver_pending(
    session: AsyncSession, *, client: httpx.AsyncClient | None = None
) -> dict[str, int]:
    """Push every pending alert to its tenant's channels. Commits per alert.

    States written: ``sent`` (every qualifying channel took it), ``partial``
    (some did), ``failed`` (none did), ``skipped`` (no enabled channel wants
    this severity), ``expired`` (pending longer than
    ``SDWAN_ALERT_MAX_AGE_SECONDS`` -- late news is noise). A failed alert is
    not retried by a later sweep: its retries already happened inside
    ``send``, and a destination down for longer than that would otherwise get
    a burst of stale alerts the moment it recovers.

    Each alert is *claimed* (pending -> sending, conditional on still being
    pending) and committed before any network call, so two workers running
    the sweep at once never send the same alert twice.
    """
    settings = get_settings()
    counts = {"sent": 0, "partial": 0, "failed": 0, "skipped": 0, "expired": 0}
    pending = list(
        await session.scalars(
            select(Alert)
            .where(Alert.delivery_state == "pending")
            .order_by(Alert.created_at)
            .limit(DELIVERY_BATCH)
        )
    )
    if not pending:
        return counts

    own_client = client is None
    client = client or make_client()
    cutoff = utcnow() - timedelta(seconds=settings.alert_max_age_seconds)
    channels: dict[str, list[NotificationChannel]] = {}
    try:
        for alert in pending:
            claimed = await session.execute(
                update(Alert)
                .where(Alert.id == alert.id, Alert.delivery_state == "pending")
                .values(delivery_state="sending")
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if claimed.rowcount != 1:
                continue

            created = alert.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < cutoff:
                alert.delivery_state = "expired"
                counts["expired"] += 1
                await session.commit()
                continue

            if alert.tenant_id not in channels:
                channels[alert.tenant_id] = list(
                    await session.scalars(
                        select(NotificationChannel).where(
                            NotificationChannel.tenant_id == alert.tenant_id,
                            NotificationChannel.enabled.is_(True),
                        )
                    )
                )
            targets = [c for c in channels[alert.tenant_id] if _qualifies(alert, c)]
            if not targets:
                alert.delivery_state = "skipped"
                counts["skipped"] += 1
                await session.commit()
                continue

            payload = payload_of(alert)
            errors = []
            for channel in targets:
                error = await deliver_to_channel(client, channel, payload)
                if error is not None:
                    errors.append(f"{channel.name}: {error}")
            ok = len(targets) - len(errors)
            alert.delivery_state = (
                "sent" if not errors else ("partial" if ok else "failed")
            )
            alert.delivery_error = "; ".join(errors) or None
            if ok:
                alert.delivered_at = utcnow()
            counts[alert.delivery_state] += 1
            await session.commit()
    finally:
        if own_client:
            await client.aclose()
    return counts
