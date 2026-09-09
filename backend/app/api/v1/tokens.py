"""API tokens: mint, list, rename, revoke.

Admin only, all of it. A token is a credential that outlives a session and
acts for a person, which makes minting one a decision about who can do what --
the same class of decision as creating a user, and it lives behind the same
gate.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.deps import RequireAdmin, SessionDep, api_token_of, get_owned, write_audit
from app.models.base import utcnow
from app.models.token import ApiToken
from app.schemas.token import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenRead,
    ApiTokenUpdate,
)
from app.security import new_api_token

router = APIRouter(prefix="/api-tokens", tags=["tokens"])


async def _get_or_404(session: SessionDep, token_id: str, tenant_id: str) -> ApiToken:
    token = await get_owned(session, ApiToken, token_id, tenant_id)
    if token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such API token")
    return token


@router.get("", response_model=list[ApiTokenRead])
async def list_tokens(session: SessionDep, user: RequireAdmin) -> list[ApiToken]:
    """Every token, revoked ones included.

    Revoked tokens stay in the list because they stay in the audit trail: a
    row that authorised something last week must still be nameable.
    """
    return list(
        await session.scalars(
            select(ApiToken)
            .where(ApiToken.tenant_id == user.tenant_id)
            .order_by(ApiToken.created_at.desc())
        )
    )


@router.post("", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    body: ApiTokenCreate, session: SessionDep, user: RequireAdmin, request: Request
) -> ApiTokenCreated:
    """Mint a token. The credential is in this response and nowhere else."""
    if api_token_of(request) is not None:
        # A token that can mint tokens is a token that can outlive its own
        # revocation. Minting stays something a person does.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "API tokens cannot create API tokens. Sign in to mint one.",
        )

    credential, prefix, token_hash = new_api_token()
    token = ApiToken(
        tenant_id=user.tenant_id,
        name=body.name,
        prefix=prefix,
        token_hash=token_hash,
        role=body.role,
        owner_id=user.id,
        expires_at=(
            utcnow() + timedelta(days=body.expires_in_days)
            if body.expires_in_days is not None
            else None
        ),
    )
    session.add(token)
    await session.flush()

    await write_audit(
        session,
        actor=user,
        action="token.create",
        object_type="api_token",
        object_id=token.id,
        # The name, the role and the prefix. Never the credential -- an audit
        # trail that records secrets is a second place to steal them from.
        detail={"name": token.name, "role": str(token.role), "prefix": token.prefix},
        request=request,
    )

    return ApiTokenCreated(
        **ApiTokenRead.model_validate(token).model_dump(), token=credential
    )


@router.patch("/{token_id}", response_model=ApiTokenRead)
async def rename_token(
    token_id: str,
    body: ApiTokenUpdate,
    session: SessionDep,
    user: RequireAdmin,
    request: Request,
) -> ApiToken:
    """Rename. Nothing else about a token is editable in place.

    Changing a role would silently widen a credential already sitting in
    somebody's CI configuration, with nothing at the point of use to show it
    changed.
    """
    token = await _get_or_404(session, token_id, user.tenant_id)
    before = token.name
    token.name = body.name
    await write_audit(
        session,
        actor=user,
        action="token.rename",
        object_type="api_token",
        object_id=token.id,
        detail={"from": before, "to": token.name},
        request=request,
    )
    return token


@router.delete("/{token_id}", response_model=ApiTokenRead)
async def revoke_token(
    token_id: str, session: SessionDep, user: RequireAdmin, request: Request
) -> ApiToken:
    """Revoke. A timestamp, not a delete.

    DELETE because that is what revoking means to a caller, but the row stays:
    the audit trail refers to it by id, and a trail full of dangling ids is
    not a trail.
    """
    token = await _get_or_404(session, token_id, user.tenant_id)
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        await write_audit(
            session,
            actor=user,
            action="token.revoke",
            object_type="api_token",
            object_id=token.id,
            detail={"name": token.name},
            request=request,
        )
    return token
