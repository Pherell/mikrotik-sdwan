"""Application settings, loaded from the environment."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SDWAN_", env_file=".env", extra="ignore"
    )

    env: Literal["dev", "prod", "test"] = "dev"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://sdwan:sdwan@db:5432/sdwan"
    redis_url: str = "redis://redis:6379/0"

    # Fernet key used to encrypt device credentials and link secrets at rest.
    # Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    secret_key: str = Field(..., min_length=32)

    # JWT signing. Distinct from secret_key so one can be rotated without the other.
    jwt_secret: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 12

    # Seeded on first boot when the user table is empty.
    bootstrap_admin_email: str = "admin@local"
    bootstrap_admin_password: str = "changeme"

    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # Device access defaults. Per-site overrides live on the Site row.
    device_connect_timeout: float = 10.0
    device_read_timeout: float = 30.0
    device_verify_tls: bool = False  # RouterOS ships a self-signed cert by default

    # Trust-on-first-use for device identity. RouterOS ships a self-signed
    # certificate and an unmanaged SSH host key, so chain validation is not
    # available -- but the identity can still be *pinned*: record it the first
    # time, refuse to talk to anything else afterwards. Without this, an
    # attacker who can intercept traffic to a router collects its credentials
    # and every PSK pushed through it.
    pin_device_identity: bool = True

    # Login throttling. This is the front door to a service that can decrypt
    # every router credential you own, so unlimited guessing is not acceptable.
    login_max_attempts: int = 5
    login_lockout_seconds: int = 300

    rollback_timeout_seconds: int = 120

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
