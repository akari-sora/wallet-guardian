"""Telegram Bot API sender. Provider acceptance is not a read receipt.

The bot token is read from the environment variable named by the private
profile. It is never written into errors, receipts, or repr. The only allowed
destination is ``https://api.telegram.org/bot<token>/sendMessage``. Egress uses
the same SSRF checks as the read-only transport, with a separate explicit allow
for that host. This module does not follow redirects and does not call any
other Telegram method.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from guardian.configuration import MaskedSecret, PublicConfig
from guardian.delivery import read_telegram_channel
from guardian.errors import SecurityError
from guardian.security.redact import redact
from guardian.security.ssrf import validate_url
from guardian.sources.fixtures import PreparedCall
from guardian.sources.transport import pinned_request

HOST = "api.telegram.org"
_MAX_BODY = 1_048_576
_MAX_RETRY_AFTER = 86_400


class WrittenTransportError(Exception):
    """The request bytes may already have left the process."""

    def __init__(self, reason: str) -> None:
        if reason not in {"timeout", "connection_reset"}:
            reason = "connection_reset"
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SendOutcome:
    """Result of one provider call. ``provider_accepted`` is not end-to-end delivery."""

    status: str
    message_id: str | None = None
    retry_after_seconds: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.reason:
            object.__setattr__(self, "reason", redact(self.reason)[:120])


class TelegramSender:
    """One opted-in chat. ``tracks_inflight`` tells delivery to persist before the POST."""

    tracks_inflight = True

    def __init__(
        self,
        *,
        token: MaskedSecret,
        chat_id: str,
        backend: Any = None,
        resolve: Any = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._backend = backend if backend is not None else TelegramHttpBackend()
        self._resolve = resolve

    def __repr__(self) -> str:
        return "TelegramSender(channel='telegram')"

    @classmethod
    def from_public(
        cls,
        public_config: PublicConfig,
        environ: Mapping[str, str],
        *,
        backend: Any = None,
        resolve: Any = None,
    ) -> TelegramSender | None:
        channel = read_telegram_channel(public_config, environ)
        if channel is None:
            return None
        raw = environ.get(channel.token_env)
        if not isinstance(raw, str) or not raw.strip():
            return None
        return cls(token=MaskedSecret(raw.strip()), chat_id=channel.chat_id, backend=backend, resolve=resolve)

    def send(self, message: Mapping[str, Any]) -> SendOutcome:
        token = self._token.reveal()
        url = f"https://{HOST}/bot{token}/sendMessage"
        try:
            pinned = guard_telegram_send(url, resolve=self._resolve)
        except SecurityError:
            raise
        body = json.dumps(
            {"chat_id": self._chat_id, "disable_web_page_preview": True, "text": _text(message)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        call = PreparedCall(
            source_id="telegram",
            method="POST",
            url=url,
            body=body,
            protocol="rest",
            connect_ip=pinned,
        )
        try:
            raw = self._backend.send(call)
        except WrittenTransportError as exc:
            reason = "timeout_after_write" if exc.reason == "timeout" else "connection_reset_after_write"
            return SendOutcome("unknown", reason=reason)
        except Exception:
            return SendOutcome("unknown", reason="send_failed")
        return _classify(getattr(raw, "status", 0), getattr(raw, "headers", {}), getattr(raw, "body", b""))


def guard_telegram_send(url: str, *, resolve: Any = None) -> str | None:
    """Allow one HTTPS POST target. Return the pinned address, or None for a literal host.

    Raises SecurityError with no URL and no token in the message.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or host != HOST or parts.port not in (None, 443):
        raise SecurityError("endpoint_rejected")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise SecurityError("endpoint_rejected")
    prefix = "/bot"
    path = parts.path or ""
    if not path.startswith(prefix) or not path.endswith("/sendMessage"):
        raise SecurityError("endpoint_rejected")
    token_part = path[len(prefix):-len("/sendMessage")]
    if not token_part or "/" in token_part:
        raise SecurityError("endpoint_rejected")
    answers: list[str] | None = None
    try:
        literal = _literal(host)
    except ValueError:
        literal = None
    if literal is None:
        resolver = resolve if resolve is not None else _resolve_host
        try:
            answers = [str(item) for item in resolver(host)]
        except OSError:
            raise SecurityError("dns_failed") from None
        if not answers:
            raise SecurityError("dns_failed")

    def cached(_host: str) -> list[str]:
        return list(answers or [])

    try:
        validate_url(url, resolve=cached if answers is not None else resolve)
    except SecurityError:
        raise SecurityError("endpoint_rejected") from None
    return answers[0] if answers else None


class TelegramHttpBackend:
    """Production POST. Redirects stay off. Errors do not include the request URL."""

    def __init__(self, *, timeout: float = 20) -> None:
        self.timeout = timeout

    def send(self, call: PreparedCall) -> Any:
        import httpx

        from guardian.sources.fixtures import RawResponse

        url, headers, extensions = pinned_request(call)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                response = client.post(url, content=call.body or None, headers=headers, extensions=extensions)
                body = response.content
                if len(body) > _MAX_BODY:
                    body = b""
                return RawResponse(response.status_code, {key: value for key, value in response.headers.items()}, body)
        except httpx.ConnectError:
            raise SecurityError("telegram_connect_failed") from None
        except httpx.TimeoutException:
            raise WrittenTransportError("timeout") from None
        except httpx.HTTPError:
            raise WrittenTransportError("connection_reset") from None


def _literal(host: str) -> Any:
    import ipaddress

    return ipaddress.ip_address(host)


def _resolve_host(host: str) -> list[str]:
    import socket

    infos = socket.getaddrinfo(host, None)
    return [item[4][0] for item in infos]


def _text(message: Mapping[str, Any]) -> str:
    from guardian.security.display_text import escape_plain

    parts: list[str] = []
    for key in ("kind", "network", "dedup_key", "informational", "detail"):
        if key not in message:
            continue
        value = message[key]
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            parts.append(f"{key}={escape_plain(str(value))}")
    text = "\n".join(parts) or "wallet-guardian alert"
    return redact(text)[:3500]


def _classify(status: int, headers: Mapping[str, str], body: bytes) -> SendOutcome:
    if status in {301, 302, 303, 307, 308}:
        return SendOutcome("failed_permanent", reason="redirect_refused")
    if not isinstance(status, int) or isinstance(status, bool):
        return SendOutcome("unknown", reason="send_failed")
    parsed = _object(body if len(body) <= _MAX_BODY else b"")
    if status == 429 or (isinstance(parsed, dict) and parsed.get("error_code") == 429):
        return SendOutcome("retry_after", reason="rate_limited", retry_after_seconds=_retry_after(headers, parsed))
    if _wrong_recipient(status, parsed):
        return SendOutcome("failed_permanent", reason="wrong_recipient")
    if status == 200 and isinstance(parsed, dict) and parsed.get("ok") is True:
        message_id = _message_id(parsed)
        if message_id is None:
            return SendOutcome("unknown", reason="missing_message_id")
        return SendOutcome("provider_accepted", message_id=message_id, reason="provider_accepted")
    if 400 <= status < 500:
        return SendOutcome("failed_permanent", reason=f"http_{status}")
    if status >= 500:
        return SendOutcome("retry_after", reason="upstream_error", retry_after_seconds=_retry_after(headers, parsed))
    return SendOutcome("unknown", reason="send_failed")


def _wrong_recipient(status: int, parsed: Any) -> bool:
    if status == 403:
        return True
    if status != 400 or not isinstance(parsed, dict):
        return False
    description = parsed.get("description")
    if not isinstance(description, str):
        return False
    text = description.lower()
    return "chat not found" in text or "forbidden" in text


def _message_id(parsed: Mapping[str, Any]) -> str | None:
    result = parsed.get("result")
    if not isinstance(result, Mapping):
        return None
    value = result.get("message_id")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def _object(body: bytes) -> Any:
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_after(headers: Mapping[str, str], parsed: Any) -> int:
    if isinstance(parsed, dict):
        params = parsed.get("parameters")
        if isinstance(params, dict):
            seconds = _seconds(params.get("retry_after"))
            if seconds is not None:
                return seconds
    raw = None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    if isinstance(raw, str):
        seconds = _seconds(raw.strip())
        if seconds is not None:
            return seconds
    return 1


def _seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value) if value >= 0 else -1
    elif isinstance(value, str) and value.isdigit():
        number = int(value)
    else:
        return None
    if number < 1:
        return 1
    if number > _MAX_RETRY_AFTER:
        return _MAX_RETRY_AFTER
    return number
