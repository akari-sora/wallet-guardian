"""Mask API keys and other secrets before they reach logs, errors, or reports."""

from __future__ import annotations

import re

_QUERY = re.compile(
    r"(?i)([?&]?(?:api[_-]?key|access[_-]?key|apikey|token|authorization|secret)=)([^&\s\"']+)"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{6,}")
_HEADER = re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)")


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    masked = _QUERY.sub(r"\1***", text)
    masked = _BEARER.sub(r"\1***", masked)
    masked = _HEADER.sub(r"\1***", masked)
    for secret in extra_secrets or []:
        if secret and len(secret) >= 6:
            masked = masked.replace(secret, "***")
    return masked
