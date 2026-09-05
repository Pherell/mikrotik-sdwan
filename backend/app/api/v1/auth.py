"""Login and user administration."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.config import get_settings
from app.deps import CurrentUser, RequireAdmin, SessionDep, write_audit
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse, UserCreate, UserRead, UserUpdate
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: SessionDep, request: Request) -> TokenResponse:
    user = await session.scalar(select(User).where(User.email == body.email))
    # Verify even when the user is missing so the response time does not reveal
    # which addresses exist.
    ok = user is not None and verify_password(body.password, user.password_hash)
    if not ok or user is None or not user.is_active:
        await write_audit(
            session,
            actor=None,
            action="auth.login.failed",
            detail={"email": body.email},
            request=request,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    await write_audit(session, actor=user, action="auth.login", request=request)
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.id, {"role": user.role, "email": user.email}),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> User:
    return user


users = APIRouter(prefix="/users", tags=["users"])


@users.get("", response_model=list[UserRead])
async def list_users(session: SessionDep, _: RequireAdmin) -> list[User]:
    return list(await session.scalars(select(User).order_by(User.email)))


@users.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate, session: SessionDep, admin: RequireAdmin, request: Request
) -> User:
    if await session.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "That email is already registered")
    user = User(
        email=body.email,
        full_name=body.full_name,
        role=body.role,
        password_hash=hash_password(body.password),
        tenant_id=admin.tenant_id,
    )
    session.add(user)
    await session.flush()
    await write_audit(
        session,
        actor=admin,
        action="user.create",
        object_type="user",
        object_id=user.id,
        detail={"email": user.email, "role": user.role},
        request=request,
    )
    return user


@users.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: str,
    body: UserUpdate,
    session: SessionDep,
    admin: RequireAdmin,
    request: Request,
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")

    data = body.model_dump(exclude_unset=True)
    if (pw := data.pop("password", None)) is not None:
        user.password_hash = hash_password(pw)
    for field, value in data.items():
        setattr(user, field, value)

    # Do not let the last active admin lock everyone out.
    if user.id == admin.id and (data.get("is_active") is False or data.get("role") != admin.role):
        remaining = await session.scalars(
            select(User).where(User.role == "admin", User.is_active.is_(True), User.id != user.id)
        )
        if not list(remaining):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "You are the only active admin; promote another user first",
            )

    await write_audit(
        session,
        actor=admin,
        action="user.update",
        object_type="user",
        object_id=user.id,
        detail={"fields": sorted(data)},
        request=request,
    )
    return user
