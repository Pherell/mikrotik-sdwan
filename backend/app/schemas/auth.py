"""Auth and user shapes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Role
from app.schemas.email import AccountEmail


class LoginRequest(BaseModel):
    email: AccountEmail
    password: str = Field(repr=False)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserCreate(BaseModel):
    email: AccountEmail
    password: str = Field(min_length=8, repr=False)
    full_name: str | None = None
    role: Role = Role.viewer


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    password: str | None = Field(default=None, min_length=8, repr=False)
    role: Role | None = None
    is_active: bool | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    # Deliberately unvalidated: this renders a row that is already stored and
    # already normalised. Re-validating on the way out turns any address the
    # rules no longer like into a 500 on a plain GET.
    email: str
    full_name: str | None
    role: Role
    is_active: bool
    created_at: datetime
