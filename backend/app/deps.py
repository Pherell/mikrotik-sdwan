"""Shared FastAPI dependencies: authentication, RBAC, and audit context."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.base import utcnow
from app.models.enums import Role
from app.models.job import AuditEvent
from app.models.token import ApiToken
from app.models.user import User
from app.security import api_secret_matches, decode_access_token, split_api_token

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Ordered least to most privileged, so a role check is a comparison.
_RANK = {Role.viewer: 0, Role.operator: 1, Role.admin: 2}


async def current_user(
    request: Request,
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    if creds is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # An API token and a login JWT arrive the same way, in the same header.
    # They are told apart by shape, and a value that is not token-shaped falls
    # through to the JWT path rather than failing here.
    if split_api_token(creds.credentials):
        return await _user_for_api_token(request, session, creds.credentials)

    try:
        payload = decode_access_token(creds.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    user = await session.scalar(select(User).where(User.id == payload.get("sub")))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")
    return user


async def _user_for_api_token(
    request: Request, session: AsyncSession, credential: str
) -> User:
    """Resolve an API token to the person it acts for.

    Every failure below is the same message. A caller holding a wrong
    credential learns only that it is wrong -- not whether the token exists,
    whether it expired, or whether it was revoked, each of which is a fact
    about this installation.
    """
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid API token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    parts = split_api_token(credential)
    if parts is None:  # pragma: no cover - guarded by the caller
        raise unauthorized
    prefix, secret = parts

    token = await session.scalar(select(ApiToken).where(ApiToken.prefix == prefix))
    if token is None or not api_secret_matches(secret, token.token_hash):
        raise unauthorized
    if token.revoked_at is not None:
        raise unauthorized
    now = utcnow()
    if token.expires_at is not None and _aware(token.expires_at) <= now:
        raise unauthorized

    user = await session.get(User, token.owner_id)
    if user is None or not user.is_active:
        raise unauthorized

    # Best effort, and deliberately coarse: writing this on every request
    # would turn every read into a write. A minute's resolution is enough to
    # answer "is anything still using this?" before revoking it.
    if token.last_used_at is None or (now - _aware(token.last_used_at)).total_seconds() > 60:
        token.last_used_at = now

    request.state.api_token = token
    return user


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even from a timezone-aware column.

    Comparing one of those to an aware ``utcnow()`` raises, so an expired
    token would 500 instead of being refused.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def api_token_of(request: Request) -> ApiToken | None:
    """The API token this request authenticated with, if it used one."""
    return getattr(request.state, "api_token", None)


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(minimum: Role):
    """Dependency factory enforcing a minimum role.

    viewer reads, operator applies configuration, admin manages users and
    credentials.
    """

    async def _check(request: Request, user: CurrentUser) -> User:
        token = api_token_of(request)
        # The lesser of the two, so a token can never be a way to keep rights
        # its owner has lost: demote the person and their tokens weaken with
        # them, without anyone having to remember to go and revoke them.
        effective = user.role
        if token is not None and _RANK[Role(token.role)] < _RANK[effective]:
            effective = Role(token.role)
        if _RANK[effective] < _RANK[minimum]:
            detail = f"Requires {minimum} role or higher (you are {effective})"
            if token is not None and effective != user.role:
                detail += f"; the API token {token.name!r} is limited to {token.role}"
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail)
        return user

    return _check


RequireViewer = Annotated[User, Depends(require_role(Role.viewer))]
RequireOperator = Annotated[User, Depends(require_role(Role.operator))]
RequireAdmin = Annotated[User, Depends(require_role(Role.admin))]


async def write_audit(
    session: AsyncSession,
    *,
    actor: User | None,
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    detail: dict | None = None,
    request: Request | None = None,
    commit: bool = False,
) -> None:
    """Append an audit row. Never raises -- an audit failure must not fail the
    request it is recording, but it is logged.

    Set ``commit`` when the caller is about to raise. A flushed row is undone by
    the rollback that the session dependency performs on any exception, so a
    failed login or a lockout would otherwise leave no trace at all -- losing
    precisely the events worth auditing.
    """
    import logging

    # "admin@example.com did this" is not the whole answer once automation
    # exists. Which credential acted is the difference between a person at a
    # keyboard and a CI job, and it is the first thing anyone asks after an
    # unexpected change.
    token = api_token_of(request) if request else None
    if token is not None:
        detail = {**(detail or {}), "via_token": token.name, "token_id": token.id}

    try:
        session.add(
            AuditEvent(
                tenant_id=actor.tenant_id if actor else "default",
                actor_id=actor.id if actor else None,
                actor_email=actor.email if actor else None,
                action=action,
                object_type=object_type,
                object_id=object_id,
                detail=detail,
                source_ip=request.client.host if request and request.client else None,
            )
        )
        await session.flush()
        if commit:
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logging.getLogger(__name__).exception("failed to write audit event %s", action)
