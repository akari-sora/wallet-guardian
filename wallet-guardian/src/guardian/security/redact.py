"""Mask API keys and other secrets before they reach logs, errors, or reports."""

from __future__ import annotations

import re

_QUERY = re.compile(
    r"(?i)([?&]?(?:api[_-]?key|access[_-]?key|apikey|token|authorization|secret)=)([^&\s\"']+)"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{6,}")
_HEADER = re.compile(r"(?i)((?:x-api-key|tron-pro-api-key)['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)")
# Telegram bot tokens travel in the URL path (`/bot<id>:<secret>/`). Mask the
# whole token, not only query parameters.
_BOT_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{8,}")
# Alchemy dashboard URLs keep the key in the path: https://host/v2/<key>
_ALCHEMY_V2 = re.compile(r"(?i)(https?://[^\s\"']*alchemy[^\s\"']*?/v2/)([A-Za-z0-9_\-]{8,})")
# NodeReal dashboard URLs keep the key in the path: https://host/v1/<key>
_NODEREAL_V1 = re.compile(r"(?i)(https?://[^\s\"']*nodereal[^\s\"']*?/v1/)([A-Za-z0-9_\-]{8,})")
# IP pinning drops the provider hostname. Mask the key segment only on an IP authority.
_PINNED_PATH_KEY = re.compile(
    r"(?i)(https?://(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:]+\])(?::\d+)?/v[12]/)([A-Za-z0-9_\-]{8,})"
)
_registered_secrets: list[str] = []


def register_extra_secret(value: str) -> None:
    """Remember a credential so later ``redact`` calls mask it without a hostname."""
    if not isinstance(value, str):
        return
    secret = value.strip()
    if len(secret) < 6 or secret in _registered_secrets:
        return
    _registered_secrets.append(secret)


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    masked = _QUERY.sub(r"\1***", text)
    masked = _BEARER.sub(r"\1***", masked)
    masked = _HEADER.sub(r"\1***", masked)
    masked = _BOT_TOKEN.sub("***", masked)
    masked = _ALCHEMY_V2.sub(r"\1***", masked)
    masked = _NODEREAL_V1.sub(r"\1***", masked)
    masked = _PINNED_PATH_KEY.sub(r"\1***", masked)
    secrets = list(_registered_secrets)
    if extra_secrets:
        secrets.extend(extra_secrets)
    for secret in secrets:
        if secret and len(secret) >= 6:
            masked = masked.replace(secret, "***")
    return masked
