"""Alerts and notification channels on the wire.

The asymmetry on channels is the same one API tokens have: a destination's
URL or bot token goes *in*, and never comes back *out*. A Slack/Teams webhook
URL is a bearer credential in everything but name -- anyone holding it can
post into that channel -- so the read shape carries only ``target_hint``.
"""

from __future__ import annotations

from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import AlertKind, AlertSeverity, ChannelType
from app.schemas.time import UtcDatetime


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an absolute http(s) URL")
    return value


class _Destination(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    bot_token: str | None = Field(default=None, max_length=256)
    chat_id: str | None = Field(default=None, max_length=64)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v)

    def destination_for(self, channel_type: ChannelType) -> dict[str, str] | None:
        """The config dict to encrypt, or None when no destination field was
        supplied at all (an update that leaves the destination alone).
        Raises ValueError when the fields supplied do not fit the type."""
        if self.url is None and self.bot_token is None and self.chat_id is None:
            return None
        if channel_type == ChannelType.webhook:
            if not self.url or self.bot_token or self.chat_id:
                raise ValueError("a webhook channel takes url, and only url")
            return {"url": self.url}
        if not self.bot_token or not self.chat_id or self.url:
            raise ValueError("a telegram channel takes bot_token and chat_id, and no url")
        return {"bot_token": self.bot_token, "chat_id": self.chat_id}


class ChannelCreate(_Destination):
    name: str = Field(min_length=1, max_length=128)
    type: ChannelType
    enabled: bool = True
    min_severity: AlertSeverity = AlertSeverity.info

    @model_validator(mode="after")
    def _has_destination(self) -> Self:
        if self.destination_for(self.type) is None:
            raise ValueError("a destination is required (url, or bot_token and chat_id)")
        return self


class ChannelUpdate(_Destination):
    """Everything optional. Destination fields, when present, replace the
    stored destination wholesale -- for its existing type, which cannot be
    changed in place (delete and recreate instead)."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    min_severity: AlertSeverity | None = None


class ChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: ChannelType
    target_hint: str
    enabled: bool
    min_severity: AlertSeverity
    last_status: str | None = None
    last_error: str | None = None
    failure_count: int = 0
    created_at: UtcDatetime


class ChannelTestResult(BaseModel):
    ok: bool
    error: str | None = None


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: AlertKind
    severity: AlertSeverity
    site_id: str | None
    site_name: str | None
    link_id: str | None
    message: str
    details: dict[str, Any] | None
    delivery_state: str
    delivered_at: UtcDatetime | None = None
    delivery_error: str | None = None
    created_at: UtcDatetime


class AlertPage(BaseModel):
    items: list[AlertRead]
    total: int
    limit: int
    offset: int
