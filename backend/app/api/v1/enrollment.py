"""Enrollment tokens and the two endpoints a factory-default device calls.

The first half (mint, list, revoke) is admin-only, the same class of decision
as minting an API token. The second half -- fetching the bootstrap script and
confirming enrollment -- is deliberately unauthenticated: a factory-default
router has no credentials to authenticate with yet, so the token in the URL
*is* the credential, verified the same way an API token is (prefix finds the
row, the secret half is checked against a stored hash, never compared to
anything in plaintext).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.config import get_settings
from app.deps import RequireAdmin, SessionDep, write_audit
from app.models.base import utcnow
from app.models.enrollment import EnrollmentToken
from app.schemas.enrollment import (
    EnrollmentTokenCreate,
    EnrollmentTokenCreated,
    EnrollmentTokenRead,
)
from app.services.enrollment import (
    EnrollmentError,
    confirm_enrollment,
    mint_enrollment_token,
    render_bootstrap_script,
)

router = APIRouter(tags=["enrollment"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# -- admin: mint, list, revoke ------------------------------------------------


@router.post(
    "/enrollment-tokens", response_model=EnrollmentTokenCreated, status_code=201
)
async def create_enrollment_token(
    body: EnrollmentTokenCreate, session: SessionDep, user: RequireAdmin, request: Request
) -> EnrollmentTokenCreated:
    settings = get_settings()
    if not settings.public_url:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "SDWAN_PUBLIC_URL is not configured. A bootstrap script needs a "
            "real address to call back to, and a device in the field cannot "
            "reach 'localhost' or a container-internal name.",
        )

    token, credential = await mint_enrollment_token(
        session,
        tenant_id=user.tenant_id,
        created_by=user.id,
        name=body.name,
        site_name=body.site_name,
        site_role=body.site_role,
        local_prefixes=body.local_prefixes,
        fabric_id=body.fabric_id,
        source_cidr=body.source_cidr,
        expires_in_hours=body.expires_in_hours,
    )

    await write_audit(
        session,
        actor=user,
        action="enrollment_token.create",
        object_type="enrollment_token",
        object_id=token.id,
        detail={"name": token.name, "site_name": token.site_name},
        request=request,
    )

    fetch_url = f"{settings.public_url.rstrip('/')}/api/v1/enroll/{credential}"
    return EnrollmentTokenCreated(
        **EnrollmentTokenRead.model_validate(token).model_dump(),
        enroll_command=(
            f'/tool fetch url="{fetch_url}" output=file dst-path=enroll.rsc; '
            "/import enroll.rsc"
        ),
    )


@router.get("/enrollment-tokens", response_model=list[EnrollmentTokenRead])
async def list_enrollment_tokens(
    session: SessionDep, user: RequireAdmin
) -> list[EnrollmentToken]:
    return list(
        await session.scalars(
            select(EnrollmentToken)
            .where(EnrollmentToken.tenant_id == user.tenant_id)
            .order_by(EnrollmentToken.created_at.desc())
        )
    )


@router.delete("/enrollment-tokens/{token_id}", response_model=EnrollmentTokenRead)
async def revoke_enrollment_token(
    token_id: str, session: SessionDep, user: RequireAdmin, request: Request
) -> EnrollmentToken:
    token = await session.scalar(
        select(EnrollmentToken).where(
            EnrollmentToken.id == token_id, EnrollmentToken.tenant_id == user.tenant_id
        )
    )
    if token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such enrollment token")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        await write_audit(
            session,
            actor=user,
            action="enrollment_token.revoke",
            object_type="enrollment_token",
            object_id=token.id,
            detail={"name": token.name},
            request=request,
        )
    return token


# -- unauthenticated: what the device itself calls ---------------------------


@router.get("/enroll/{credential}")
async def fetch_bootstrap_script(
    credential: str, session: SessionDep, request: Request
) -> Response:
    settings = get_settings()
    if not settings.public_url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Enrollment is not configured")

    source_ip = _client_ip(request)
    try:
        script = await render_bootstrap_script(
            session, credential, source_ip, settings.public_url
        )
    except EnrollmentError as exc:
        await write_audit(
            session,
            actor=None,
            action="enrollment.fetch_failed",
            detail={"reason": str(exc), "source_ip": source_ip},
            request=request,
            commit=True,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    await write_audit(
        session,
        actor=None,
        action="enrollment.fetched",
        detail={"source_ip": source_ip},
        request=request,
    )
    return Response(content=script, media_type="text/plain")


@router.post("/enroll/{credential}/confirm")
async def confirm(credential: str, session: SessionDep, request: Request) -> dict:
    source_ip = _client_ip(request)
    try:
        site = await confirm_enrollment(session, credential, source_ip)
    except EnrollmentError as exc:
        await write_audit(
            session,
            actor=None,
            action="enrollment.confirm_failed",
            detail={"reason": str(exc), "source_ip": source_ip},
            request=request,
            commit=True,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    await write_audit(
        session,
        actor=None,
        action="enrollment.confirmed",
        object_type="site",
        object_id=site.id,
        detail={"name": site.name, "mgmt_host": site.mgmt_host},
        request=request,
    )
    return {"status": "ok", "site_id": site.id}
