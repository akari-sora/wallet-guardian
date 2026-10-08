"""Chainabuse is optional and blocked in this package.

Auth is an HTTP header. The read-only transport cannot carry headers, so this
client never requests a report and never returns an empty report list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from guardian.security.redact import redact
from guardian.sources.etherscan import resolve_credential

_REASONS = frozenset({"credential_missing", "credential_transport_unsupported"})


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
        if self.status != "blocked" or self.blocked is not True:
            raise ValueError("chainabuse_must_stay_blocked")
        if self.safe or self.reports is not None or self.requested:
            raise ValueError("chainabuse_cannot_report_clear")
        if self.reason not in _REASONS:
            raise ValueError("reason")


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
        """Return blocked without a request. ``address`` is not sent anywhere."""
        del address
        if self._credential is None:
            reason = "credential_missing"
        else:
            reason = "credential_transport_unsupported"
        return ChainabuseRead(
            status="blocked",
            reason=reason,
            blocked=True,
            reports=None,
            safe=False,
            requested=False,
        )
