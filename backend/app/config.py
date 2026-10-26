"""Application settings, loaded from the environment."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.schemas.email import AccountEmail


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

    # Seeded on first boot when the user table is empty. Validated with the
    # same type the login form uses, so a value that could never log in fails
    # at startup with a reason rather than seeding an unusable account.
    bootstrap_admin_email: AccountEmail = "admin@local"
    bootstrap_admin_password: str = "changeme"

    # NoDecode is load-bearing, not decoration. Without it pydantic-settings
    # JSON-decodes any list-typed environment variable *inside the settings
    # source*, before field validators run -- so a plain
    # "SDWAN_CORS_ORIGINS=http://localhost:8080" raises a JSONDecodeError at
    # import and the process never starts. That is the form every deployment
    # uses, so this broke the whole stack.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:8080",
    ]

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

    # B1 transactional apply. When enabled, a ROS7/softhub apply runs its
    # pushes inside an SSH-held Safe Mode session (one extra SSH session per
    # apply), committed chunk by chunk, instead of relying on the dead-man
    # scheduler alone. The bound is the plan's: under 100 actions per Safe
    # Mode session, so a chunk is 90. Only the final uncommitted chunk is
    # ever rolled back by an abort -- earlier chunks are already committed,
    # which is exactly why a mid-apply failure quarantines the device (C3).
    # Disabling this restores the legacy dead-man-only flow everywhere.
    safe_mode_enabled: bool = True
    safe_mode_chunk_size: int = 90

    # M7 telemetry: how often the poller reads netwatch and system/resource
    # back from every provisioned site. RouterOS is already measuring this for
    # SLA-based failover; this just keeps what it already said. 30s matches
    # the plan's own "one poller pass per site, every 30 seconds by default".
    #
    # Raise this for low-powered devices. Each pass opens a fresh HTTPS session,
    # and the TLS handshake is the expensive part -- on a router with no crypto
    # accelerator it costs far more than the data it fetches. Measured on an
    # RB2011 (600MHz single-core MIPS, RouterOS 7.24): at 30s the ssld process
    # sat at ~90% CPU and the box stayed pinned at 100%, which starved the
    # control plane badly enough to expire BGP hold timers and drop pings --
    # looking for all the world like a failing link. At 300s the same router
    # idles at ~24% with only brief spikes while a pass runs.
    telemetry_poll_seconds: int = 30

    # SLA retention: how many days of 5-minute sla_evaluations buckets the
    # controller keeps. The ceiling is 288 rows/day/path, so 90 days is
    # ~25,920 rows/path (~2.6M rows, ~1GB with indexes, at 100 paths) --
    # three times the longest compliance window (30d) with margin, cheap for
    # Postgres. A daily server-side reaper deletes rows whose bucket_start
    # is older than this; zero traffic to the devices, as always.
    sla_retention_days: int = 90

    # M8 provisioning: the address a factory-default router calls back to.
    # Baked into every generated bootstrap script (it is on the far side of a
    # /tool fetch run by a device that has no other way to find the
    # controller), so it has to be the address actually reachable from the
    # field -- not localhost, not a Docker-internal name. No default: a
    # silently-wrong URL baked into a script already pasted into a real
    # router is worse than refusing to mint the token at all.
    public_url: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept a comma-separated string, which is what an env var carries.

        A JSON list is still accepted, so an existing deployment that wrote one
        to work around the decoding bug keeps working.
        """
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                import json

                try:
                    return json.loads(text)
                except ValueError:
                    pass
            return [o.strip() for o in text.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
