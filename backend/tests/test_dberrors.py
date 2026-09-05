"""Database connection failures should read as instructions, not stack traces.

Written after a deployment spent three rounds on
``asyncpg.exceptions.InvalidPasswordError``. The cause was knowable and the fix
was one command, but the useful sentence arrived at the end of sixty lines of
SQLAlchemy internals.
"""

from __future__ import annotations

import pytest

from app.dberrors import explain, redact_url

URL = "postgresql+asyncpg://sdwan:ZX9RyiUfnE7Zcq7mjnCmhuKdt15UBGXV@db:5432/sdwan"


class InvalidPasswordError(Exception):
    """Stands in for asyncpg's, matched by class name."""


def test_the_password_never_appears_in_the_message() -> None:
    """These messages go to a console and a container log."""
    msg = explain(InvalidPasswordError("password authentication failed"), URL)
    assert msg is not None
    assert "ZX9RyiUfnE7" not in msg
    assert "sdwan:***@db" in msg


def test_redaction_keeps_the_bits_worth_reading() -> None:
    safe = redact_url(URL)
    assert safe == "postgresql+asyncpg://sdwan:***@db:5432/sdwan"


def test_bad_password_blames_the_volume_not_the_env_file() -> None:
    """The actual cause: Postgres reads POSTGRES_PASSWORD only when it
    initialises an empty data directory, so later .env edits are ignored."""
    msg = explain(InvalidPasswordError("password authentication failed"), URL)
    assert msg is not None
    assert "volume" in msg
    assert "down -v" in msg           # the fix, spelled out
    assert "ALTER USER" in msg        # and the one that keeps the data


def test_it_matches_on_message_too_not_only_class_name() -> None:
    """Not everyone reaches this through asyncpg."""
    msg = explain(Exception('password authentication failed for user "sdwan"'), URL)
    assert msg is not None and "volume" in msg


def test_connection_refused_points_at_the_container() -> None:
    msg = explain(ConnectionRefusedError("connection refused"), URL)
    assert msg is not None
    assert "compose ps db" in msg


def test_unresolvable_host_explains_compose_networking() -> None:
    msg = explain(Exception("could not translate host name"), URL)
    assert msg is not None
    assert "'db'" in msg


def test_a_missing_database_is_recognised() -> None:
    msg = explain(Exception('database "sdwan" does not exist'), URL)
    assert msg is not None
    assert "POSTGRES_DB" in msg


def test_an_unrecognised_error_returns_none() -> None:
    """Guessing at an unknown failure is worse than showing the traceback."""
    assert explain(Exception("something entirely novel"), URL) is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@h/db",
        "postgresql+asyncpg://u:@h/db",          # empty password
        "postgresql+asyncpg://u:p%40ss@h/db",    # encoded @
        "sqlite+aiosqlite:///./local.db",        # no credentials at all
    ],
)
def test_redaction_never_crashes_on_odd_urls(url: str) -> None:
    out = redact_url(url)
    assert isinstance(out, str)
    assert "p@ss" not in out
