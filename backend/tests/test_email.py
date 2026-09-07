"""Account identifiers.

The stack shipped a default admin of ``admin@local`` and a login schema that
rejected it, so the seeded account could never sign in and ``GET /users`` would
have raised on the way out. Neither the suite nor CI caught it: the suite never
constructed a LoginRequest from the configured default, and CI's stack test was
failing earlier for unrelated reasons and never reached the login step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas.auth import LoginRequest, UserCreate, UserRead
from app.schemas.email import normalise_account_email

ROOT = Path(__file__).resolve().parents[2]


# -- the bug ----------------------------------------------------------------


def test_the_shipped_default_admin_can_be_used_to_log_in() -> None:
    """If this fails, a fresh install seeds an account nobody can sign in as."""
    default = Settings.model_fields["bootstrap_admin_email"].default
    assert LoginRequest(email=default, password="x").email == default


def test_admin_at_local_is_accepted_everywhere_it_appears() -> None:
    assert LoginRequest(email="admin@local", password="x").email == "admin@local"
    assert UserCreate(email="admin@local", password="longenough").email == "admin@local"


def test_reading_a_user_back_never_re_validates_the_address() -> None:
    """An output schema renders what is stored. Validating on the way out turns
    a stored address the rules no longer like into a 500 on a plain GET."""
    user = UserRead(
        id="u1",
        email="whatever-is-in-the-database",
        full_name=None,
        role="admin",
        is_active=True,
        created_at="2026-01-01T00:00:00Z",
    )
    assert user.email == "whatever-is-in-the-database"


# -- what the type accepts and refuses --------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "admin@local",
        "admin@localhost",
        "ops@sdwan.local",  # reserved under RFC 6762, still a fine login name
        "ops@sdwan.internal",
        "someone@example.com",
        "first.last+tag@sub.example.co.uk",
    ],
)
def test_accepted(value: str) -> None:
    assert normalise_account_email(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "noatsign",
        "@local",
        "admin@",
        "admin@@local",
        "a b@example.com",
        "admin@-bad.com",
        "admin@bad-.com",
        "admin@x..y",
        "a" * 65 + "@local",
    ],
)
def test_refused(value: str) -> None:
    with pytest.raises(ValueError):
        normalise_account_email(value)


def test_the_domain_is_lowercased_and_the_local_part_is_not() -> None:
    """Exactly what email_validator does, so a private domain and a public one
    normalise the same way and a lookup cannot miss one but match the other."""
    assert normalise_account_email("Admin@LOCAL") == "Admin@local"
    assert normalise_account_email("Admin@EXAMPLE.com") == "Admin@example.com"


def test_surrounding_whitespace_is_dropped() -> None:
    assert normalise_account_email("  admin@local\n") == "admin@local"


def test_a_bad_bootstrap_email_fails_at_startup(monkeypatch) -> None:
    """Rather than seeding an account that cannot log in and saying nothing."""
    get_settings.cache_clear()
    monkeypatch.setenv("SDWAN_SECRET_KEY", "a-secret-key-that-is-long-enough-here")
    monkeypatch.setenv("SDWAN_JWT_SECRET", "a-jwt-secret-that-is-long-enough-here")
    monkeypatch.setenv("SDWAN_BOOTSTRAP_ADMIN_EMAIL", "not-an-address")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
    get_settings.cache_clear()


# -- the documented default and the code must agree -------------------------


def test_every_place_that_names_the_default_admin_agrees() -> None:
    """compose, .env.example and the code each state this value independently.
    A change to one and not the others is a login failure nobody can explain."""
    code = Settings.model_fields["bootstrap_admin_email"].default

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    declared = compose["x-backend-env"]["SDWAN_BOOTSTRAP_ADMIN_EMAIL"]
    from_compose = re.fullmatch(r"\$\{SDWAN_BOOTSTRAP_ADMIN_EMAIL:-(.+)\}", declared)
    assert from_compose, f"compose no longer defaults this: {declared!r}"

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    from_env = re.search(r"^SDWAN_BOOTSTRAP_ADMIN_EMAIL=(.+)$", env_example, re.M)
    assert from_env, ".env.example no longer documents a default admin"

    assert code == from_compose.group(1) == from_env.group(1).strip()
