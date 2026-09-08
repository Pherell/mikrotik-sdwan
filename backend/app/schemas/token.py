"""What an API token looks like going in and coming out.

The asymmetry is the point: a token is created once and read many times, and
the usable value appears in exactly one of those.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Role
from app.schemas.time import UtcDatetime


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role: Role = Role.viewer
    # Days, not a date: "90 days" is the decision people actually make, and a
    # date field invites a timezone bug for no gain. None means no expiry --
    # allowed, because a CI token that dies unannounced at 3am is its own kind
    # of outage, but it is not the default.
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)


class ApiTokenUpdate(BaseModel):
    """Only the name is editable.

    Changing a token's role in place would silently widen a credential that
    somebody already copied into a script, with no record at the point of use.
    Revoke and mint a new one instead -- that is a decision, and it leaves a
    trail.
    """

    name: str = Field(min_length=1, max_length=128)


class ApiTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    # The public half only. It is enough to tell two tokens apart in a list
    # and to match a row against a credential someone is holding.
    prefix: str
    role: Role
    owner_id: str
    created_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    last_used_at: UtcDatetime | None = None
    revoked_at: UtcDatetime | None = None


class ApiTokenCreated(ApiTokenRead):
    """The one response that carries the usable credential.

    Not stored, not retrievable, not logged. If it is lost the answer is to
    revoke this token and mint another, which is the same answer as if it had
    leaked -- and that equivalence is exactly what makes it safe.
    """

    token: str
