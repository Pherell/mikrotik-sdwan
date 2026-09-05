"""Shared FastAPI dependencies: authentication, RBAC, and audit context."""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.enums import Role
from app.models.job import AuditEvent
from app.models.user import User
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Ordered least to most privileged, so a role check is a comparison.
_RANK = {Role.viewer: 0, Role.operator: 1, Role.admin: 2}


async def current_user(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    if creds is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
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


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(minimum: Role):
    """Dependency factory enforcing a minimum role.

    viewer reads, operator applies configuration, admin manages users and
    credentials.
    """

    async def _check(user: CurrentUser) -> User:
        if _RANK[user.role] < _RANK[minimum]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Requires {minimum} role or higher (you are {user.role})",
            )
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
