"""Settings parsing, exercised through the *environment* rather than defaults.

This file exists because of a bug that reached a real deployment and broke it
completely. ``cors_origins`` is a ``list[str]``, and pydantic-settings
JSON-decodes list-typed environment variables inside the settings source,
before any field validator runs. A plain
``SDWAN_CORS_ORIGINS=http://localhost:8080`` -- the form docker-compose.yml has
always set -- therefore raised JSONDecodeError at import, and every container
died before doing anything.

294 tests passed at the time. None of them set that variable, so the default
list was used and the environment source was never exercised. Hence: these
tests construct Settings from the environment, the way a container does.
"""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings

REQUIRED = {
    "SDWAN_SECRET_KEY": "a-secret-key-that-is-long-enough-here",
    "SDWAN_JWT_SECRET": "a-jwt-secret-that-is-long-enough-here",
}


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(monkeypatch, **env: str) -> Settings:
    for key, value in {**REQUIRED, **env}.items():
        monkeypatch.setenv(key, value)
    # An .env file next to the tests would silently supply values a container
    # would not have.
    return Settings(_env_file=None)  # type: ignore[call-arg]


# -- the bug ----------------------------------------------------------------


def test_a_single_plain_origin_parses(monkeypatch) -> None:
    """The exact value docker-compose.yml sets by default."""
    s = _settings(monkeypatch, SDWAN_CORS_ORIGINS="http://localhost:8080")
    assert s.cors_origins == ["http://localhost:8080"]


def test_comma_separated_origins_parse(monkeypatch) -> None:
    s = _settings(
        monkeypatch, SDWAN_CORS_ORIGINS="http://a.example, http://b.example"
    )
    assert s.cors_origins == ["http://a.example", "http://b.example"]


def test_a_json_list_still_parses(monkeypatch) -> None:
    """Anyone who wrote JSON to work around the bug must not be broken by the
    fix."""
    s = _settings(
        monkeypatch, SDWAN_CORS_ORIGINS='["http://a.example","http://b.example"]'
    )
    assert s.cors_origins == ["http://a.example", "http://b.example"]


def test_empty_entries_are_dropped(monkeypatch) -> None:
    s = _settings(monkeypatch, SDWAN_CORS_ORIGINS="http://a.example,,  ,")
    assert s.cors_origins == ["http://a.example"]


def test_the_default_survives_an_unset_variable(monkeypatch) -> None:
    monkeypatch.delenv("SDWAN_CORS_ORIGINS", raising=False)
    s = _settings(monkeypatch)
    assert "http://localhost:8080" in s.cors_origins


# -- the rest of the environment surface ------------------------------------


def test_the_compose_environment_block_parses(monkeypatch) -> None:
    """Every variable docker-compose.yml sets, together, as a container gets
    them. If this passes, the stack can at least start."""
    s = _settings(
        monkeypatch,
        SDWAN_ENV="prod",
        SDWAN_DEBUG="false",
        SDWAN_DATABASE_URL="postgresql+asyncpg://sdwan:pw@db:5432/sdwan",
        SDWAN_REDIS_URL="redis://redis:6379/0",
        SDWAN_BOOTSTRAP_ADMIN_EMAIL="admin@local",
        SDWAN_BOOTSTRAP_ADMIN_PASSWORD="bootstrap",
        SDWAN_CORS_ORIGINS="http://localhost:8080",
    )

    assert s.env == "prod"
    assert s.debug is False
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.cors_origins == ["http://localhost:8080"]


def test_booleans_accept_the_strings_compose_writes(monkeypatch) -> None:
    assert _settings(monkeypatch, SDWAN_DEBUG="true").debug is True
    assert _settings(monkeypatch, SDWAN_DEBUG="false").debug is False


def test_identity_pinning_is_on_unless_switched_off(monkeypatch) -> None:
    assert _settings(monkeypatch).pin_device_identity is True
    assert (
        _settings(monkeypatch, SDWAN_PIN_DEVICE_IDENTITY="false").pin_device_identity
        is False
    )


def test_a_short_secret_is_refused(monkeypatch) -> None:
    """min_length is the only thing standing between a deployment and a
    trivially guessable encryption key."""
    monkeypatch.setenv("SDWAN_JWT_SECRET", REQUIRED["SDWAN_JWT_SECRET"])
    monkeypatch.setenv("SDWAN_SECRET_KEY", "too-short")
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_missing_secrets_are_refused(monkeypatch) -> None:
    monkeypatch.delenv("SDWAN_SECRET_KEY", raising=False)
    monkeypatch.delenv("SDWAN_JWT_SECRET", raising=False)
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]
