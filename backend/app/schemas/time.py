"""One timestamp type, so the API never emits an ambiguous one.

SQLite has no timezone storage, so a `DateTime(timezone=True)` column hands
back a *naive* datetime while the same value still in the session is aware.
Serialised, that is the difference between "2026-09-08T08:14:40.592054Z" and
"2026-09-08T08:14:40.592054" -- the same instant, one of which a client will
read as local time.

Two responses in the same second disagreed about this: the row returned
straight after a write was aware, and the one read back was not. Attaching UTC
to anything naive on the way out costs nothing and makes every timestamp this
API emits mean one thing.

Postgres does keep the zone, so this is a no-op there. That is why it belongs
at the boundary rather than in the models: the fix should not depend on which
database is underneath.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator


def _as_utc(value: datetime) -> datetime:
    """Naive means UTC here, because everything is written as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]
