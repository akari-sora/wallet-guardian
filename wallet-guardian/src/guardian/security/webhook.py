"""Webhook authentication skeleton. Nothing here opens a public listener."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlsplit

from guardian.errors import SecurityError
from guardian.security.ssrf import validate_url


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def verify_hmac_webhook(
    *,
    body: bytes,
    timestamp: int,
    signature: str,
    secret: bytes,
    now: int,
    seen: set[str],
    max_skew_seconds: int = 300,
) -> str:
    if abs(int(now) - int(timestamp)) > max_skew_seconds:
        raise SecurityError("webhook timestamp is outside the allowed window")
    payload = str(timestamp).encode("ascii") + b"." + body
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise SecurityError("webhook signature mismatch")
    digest = _body_hash(body)
    if digest in seen:
        return "replay"
    seen.add(digest)
    return "ok"


def assert_pinned_cert_url(url: str, allowlist: set[str], *, resolve=None) -> str:
    checked = validate_url(url, resolve=resolve)
    host = (urlsplit(checked).hostname or "").lower()
    if host not in {item.lower() for item in allowlist}:
        raise SecurityError("certificate url is not pinned to an official host")
    return checked
