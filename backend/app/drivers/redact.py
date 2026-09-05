"""Secret masking for logs, job records, and API responses.

Every rendered config passes through here before it is stored or displayed.
"""

from __future__ import annotations

from typing import Any

from app.security import mask

SECRET_PROPS = frozenset(
    {
        "secret",
        "ipsec-secret",
        "password",
        "private-key",
        "preshared-key",
        "key",
        "passphrase",
        "tcp-md5-key",
        "wpa-pre-shared-key",
        "wpa2-pre-shared-key",
    }
)


def redact_props(props: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (mask(str(v)) if k in SECRET_PROPS and v is not None else v)
        for k, v in props.items()
    }


def redact_text(text: str, secrets: list[str]) -> str:
    """Scrub known secret values out of free-form output such as CLI logs."""
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, mask(s))
    return text
