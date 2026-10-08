"""Chainabuse reads. A missing key does not call the transport.

With a key registered on the source, the GET carries Authorization: Basic.
The value is base64 of ``secret:`` (documented, not live-verified). An empty
body is not an empty report list and is not a clear address. ``safe`` stays false.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from urllib.parse import quote

from guardian.errors import SecurityError
from guardian.security.redact import redact
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.etherscan import resolve_credential
from guardian.sources.transport import TransportError

_BLOCKED_REASONS = frozenset({
    "credential_missing",
    "credential_rejected",
    "credential_transport_unsupported",
})
_READ_REASONS = frozenset({
    "credential_rejected",
    "empty_not_clear",
    "malformed",
    "read",
    "read_error",
})


@dataclass(frozen=True)
class ChainabuseRead:
    """A blocked auth result. ``reports`` stays None so it cannot be read as 'no reports'."""

    status: str
    reason: str
    blocked: bool
    reports: None
    safe: bool
    requested: bool

    def __post_init__(self) -> None:
        if self.safe or self.reports is not None:
            raise ValueError("chainabuse_cannot_report_clear")
        if self.status == "blocked":
            if self.blocked is not True or self.requested:
                raise ValueError("chainabuse_must_stay_blocked")
            if self.reason not in _BLOCKED_REASONS:
                raise ValueError("reason")
            return
        if self.status == "unknown" and self.requested and self.blocked is False and self.reason in _READ_REASONS:
            return
        raise ValueError("chainabuse_must_stay_blocked")


class ChainabuseClient:
    """Does not call the transport. A key cannot be attached to the allowed GET."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        environ: Mapping[str, str],
        *,
        credential_env: str = "CHAINABUSE_API_KEY",
    ) -> None:
        self.transport = transport
        self.source_id = source_id
        self.credential_env = credential_env
        try:
            self._credential: str | None = resolve_credential(environ, credential_env)
        except ValueError as exc:
            if str(exc) != "credential_missing":
                raise
            self._credential = None

    def __repr__(self) -> str:
        return f"ChainabuseClient(source_id={self.source_id!r}, credential_env={self.credential_env!r}, blocked=True)"

    def redact_error(self, text: str) -> str:
        secrets = [self._credential] if self._credential else None
        return redact(text, secrets)

    def lookup(self, address: str) -> ChainabuseRead:
        """Read one address, or stay blocked when no credential is registered."""
        if not _source_has_credential(self.transport, self.source_id):
            return _blocked("credential_missing")
        if not isinstance(address, str) or not address or address != address.strip() or any(mark in address for mark in " /?#"):
            return _unknown("malformed")
        operation = Operation(
            protocol="rest",
            adapter="chainabuse",
            http_method="GET",
            path="/v0/addresses/" + quote(address, safe=""),
            headers=("authorization",),
        )
        try:
            result = self.transport.request(self.source_id, operation)
        except BudgetExhausted:
            return _unknown("read_error")
        except (SecurityError, TransportError):
            return _unknown("read_error")
        status = getattr(result, "status", 0)
        if status in {401, 403} or getattr(result, "reason", "") in {"credential_rejected", "forbidden"}:
            return _unknown("credential_rejected")
        if getattr(result, "reason", "") == "credential_missing" or not getattr(result, "ok", False):
            if getattr(result, "reason", "") == "credential_missing":
                return _blocked("credential_missing")
            return _unknown("malformed")
        body = getattr(result, "body", None)
        if body in ([], {}) or (isinstance(body, dict) and body.get("reports") == []):
            return _unknown("empty_not_clear")
        return _unknown("read")


def _source_has_credential(transport: Any, source_id: str) -> bool:
    endpoints = getattr(transport, "endpoints", None)
    sources = getattr(endpoints, "sources", None)
    if not isinstance(sources, dict):
        return False
    credential = getattr(sources.get(source_id), "credential", None)
    return credential is not None and bool(getattr(credential, "present", False))


def _blocked(reason: str) -> ChainabuseRead:
    return ChainabuseRead(
        status="blocked",
        reason=reason,
        blocked=True,
        reports=None,
        safe=False,
        requested=False,
    )


def _unknown(reason: str) -> ChainabuseRead:
    return ChainabuseRead(
        status="unknown",
        reason=reason,
        blocked=False,
        reports=None,
        safe=False,
        requested=True,
    )
