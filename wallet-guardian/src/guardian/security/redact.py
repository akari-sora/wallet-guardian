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


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    masked = _QUERY.sub(r"\1***", text)
    masked = _BEARER.sub(r"\1***", masked)
    masked = _HEADER.sub(r"\1***", masked)
    masked = _BOT_TOKEN.sub("***", masked)
    masked = _ALCHEMY_V2.sub(r"\1***", masked)
    for secret in extra_secrets or []:
        if secret and len(secret) >= 6:
            masked = masked.replace(secret, "***")
    return masked
