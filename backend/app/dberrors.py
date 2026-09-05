"""Turn database connection failures into something an operator can act on.

SQLAlchemy raises through several layers, so the useful sentence arrives at the
end of a sixty-line traceback. For the handful of failures that are actually
common at deploy time, the cause is knowable and the fix is one command -- so
say that instead of making someone read the stack.
"""

from __future__ import annotations

import re

# The password lives in the URL. Never let it reach a log or a console.
_CREDENTIALS = re.compile(r"://([^:/@]+):([^@]*)@")


def redact_url(url: str) -> str:
    return _CREDENTIALS.sub(r"://\1:***@", url)


def explain(exc: BaseException, url: str) -> str | None:
    """A specific, actionable message, or None if this failure is not one we
    recognise -- in which case the caller should let the traceback through
    rather than guess."""
    name = type(exc).__name__
    text = str(exc).lower()
    safe = redact_url(url)

    if name == "InvalidPasswordError" or "password authentication failed" in text:
        return (
            f"Database rejected the password ({safe}).\n"
            "\n"
            "Almost always the Postgres volume, not your .env. Postgres reads\n"
            "POSTGRES_PASSWORD only when it initialises an *empty* data directory.\n"
            "Once the volume exists the role's password is fixed, and later edits\n"
            "to .env are ignored.\n"
            "\n"
            "If you have no data worth keeping:\n"
            "    docker compose down -v && docker compose up -d\n"
            "\n"
            "To keep the data, change the role's password to match .env instead:\n"
            "    docker compose exec db psql -U postgres "
            "-c \"ALTER USER sdwan WITH PASSWORD 'the-value-from-your-.env';\""
        )

    if name in {"InvalidCatalogNameError"} or "does not exist" in text and "database" in text:
        return (
            f"The database named in {safe} does not exist.\n"
            "The Postgres container creates it from POSTGRES_DB on first start;\n"
            "if the volume predates that setting, recreate it with\n"
            "    docker compose down -v && docker compose up -d"
        )

    if name in {"ConnectionRefusedError", "CannotConnectNowError"} or "connection refused" in text:
        return (
            f"Nothing is accepting connections at {safe}.\n"
            "Check the database container is up and healthy:\n"
            "    docker compose ps db && docker compose logs db --tail 30"
        )

    if "could not translate host name" in text or name == "gaierror":
        return (
            f"The host in {safe} does not resolve.\n"
            "Inside the compose network the hostname is the service name, 'db'.\n"
            "A value like localhost only works outside a container."
        )

    return None
