"""Read-only TronGrid client.

History forces only_confirmed=true, freezes that filter set, and carries it
with the fingerprint. Pages stop at the documented 200-row maximum and at a
local page cap. A repeated fingerprint or a capped window is not complete.
links.next URLs are ignored. The optional key is sent only as
TRON-PRO-API-KEY. One call is in flight. Six HTTP 200s are not a quota.
Nothing here signs, builds, or broadcasts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from guardian.chains.tron.abi import READ_SELECTORS
from guardian.errors import SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

PAGE_LIMIT = 200
PAGE_CAP = 2
MAX_REQUESTS = 48
LOCAL_MAX_RPS = 1
LOCAL_MAX_CONCURRENCY = 1
HEADER_NAME = "TRON-PRO-API-KEY"
FROZEN_FILTERS = {
    "limit": str(PAGE_LIMIT),
    "only_confirmed": "true",
    "order_by": "block_timestamp,desc",
}
_FINGERPRINT = re.compile(r"^[A-Za-z0-9+/=_-]{1,256}$")
_BASE58 = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_REJECTED_BODY = frozenset({"call_value", "raw_data_hex", "signature"})


@dataclass(frozen=True)
class Got:
    ok: bool
    body: Any
    reason: str
    blocked: bool


@dataclass(frozen=True)
class HistoryWalk:
    """One frozen window. ``cursor`` is the next fingerprint, not yet fetched."""

    rows: tuple[Mapping[str, Any], ...]
    complete: bool
    cursor: str | None
    reason: str
    blocked: bool
    error: bool


class TronGridClient:
    """Sequential reader. Retries honor Retry-After and stay inside the budget."""

    def __init__(self, transport: Any, source_id: str) -> None:
        self.transport = transport
        self.source_id = source_id
        self.requests = 0

    def now_block(self, *, solidity: bool) -> Got:
        path = "/walletsolidity/getnowblock" if solidity else "/wallet/getnowblock"
        return self.post(path, {"visible": False})

    def account(self, address: str, *, solidity: bool) -> Got:
        if _BASE58.fullmatch(address) is None:
            return Got(False, None, "bad_address", True)
        path = "/walletsolidity/getaccount" if solidity else "/wallet/getaccount"
        return self.post(path, {"address": address, "visible": True})

    def transaction_by_id(self, txid: str, *, info: bool) -> Got:
        path = "/walletsolidity/gettransactioninfobyid" if info else "/walletsolidity/gettransactionbyid"
        return self.post(path, {"value": txid, "visible": False})

    def trigger_constant(self, body: Mapping[str, Any], *, solidity: bool) -> Got:
        """Allowlisted reads only. A write selector never reaches the transport."""
        selector = body.get("function_selector")
        if not isinstance(selector, str) or selector not in READ_SELECTORS:
            return Got(False, None, "selector_rejected", True)
        if any(key in body for key in _REJECTED_BODY):
            return Got(False, None, "selector_rejected", True)
        path = "/walletsolidity/triggerconstantcontract" if solidity else "/wallet/triggerconstantcontract"
        payload = dict(body)
        payload["visible"] = True
        return self.post(path, payload)

    def history(self, path: str, frozen: Mapping[str, str], resume: str | None) -> HistoryWalk:
        if resume is not None and _FINGERPRINT.fullmatch(resume) is None:
            return HistoryWalk((), False, None, "bad_cursor", False, True)
        if not path.startswith("/v1/accounts/") or any(token in path for token in ("..", "%", "//", "?", "#")):
            return HistoryWalk((), False, None, "bad_path", True, True)
        requested = resume
        seen: set[str] = set()
        rows: list[Mapping[str, Any]] = []
        pages = 0
        while pages < PAGE_CAP:
            query = {str(key): str(value) for key, value in frozen.items()}
            query.pop("fingerprint", None)
            query["only_confirmed"] = "true"
            query["limit"] = str(PAGE_LIMIT)
            if requested:
                query["fingerprint"] = requested
            got = self.get(path, query)
            if not got.ok:
                return HistoryWalk(tuple(rows), False, requested, got.reason or "read_error", got.blocked, not rows)
            parsed = _parse_page(got.body)
            if parsed is None:
                return HistoryWalk(tuple(rows), False, requested, "bad_history", False, not rows)
            batch, nxt = parsed
            pages += 1
            rows.extend(batch)
            if not nxt:
                return HistoryWalk(tuple(rows), True, None, "", False, False)
            if not batch:
                return HistoryWalk(tuple(rows), False, requested, "bad_history", False, not rows)
            if nxt == requested or nxt in seen:
                return HistoryWalk(tuple(rows), False, requested, "repeated_fingerprint", False, False)
            seen.add(nxt)
            requested = nxt
        return HistoryWalk(tuple(rows), False, requested, "page_cap", False, False)

    def get(self, path: str, query: Mapping[str, str]) -> Got:
        if not path.startswith("/v1/accounts/") or any(token in path for token in ("..", "%", "//", "?", "#")):
            return Got(False, None, "bad_path", True)
        operation = Operation(
            protocol="rest",
            adapter="tron_grid",
            http_method="GET",
            path=path,
            query=dict(query),
            headers=self._headers(),
        )
        return self._send(operation)

    def post(self, path: str, body: Mapping[str, Any]) -> Got:
        if not path.startswith("/") or any(token in path for token in ("..", "%", "//", "?", "#")):
            return Got(False, None, "bad_path", True)
        operation = Operation(
            protocol="rest",
            adapter="tron_grid",
            http_method="POST",
            path=path,
            body=dict(body),
            headers=self._headers(),
        )
        return self._send(operation)

    def _headers(self) -> tuple[str, ...] | None:
        registry = getattr(self.transport, "endpoints", None)
        if registry is None:
            return None
        try:
            source = registry.get(self.source_id)
        except (SecurityError, KeyError):
            return None
        credential = getattr(source, "credential", None)
        if credential is not None and credential.present:
            return (HEADER_NAME,)
        return None

    def _send(self, operation: Operation) -> Got:
        def attempt() -> Any:
            if self.requests >= MAX_REQUESTS:
                return _Stopped()
            self.requests += 1
            return self.transport.request(self.source_id, operation)

        try:
            attempts = self.transport.retry_attempts(self.source_id)
        except Exception:
            attempts = 0
        try:
            result = read_with_retry(
                attempt,
                retry_attempts=attempts,
                sleep=self._sleep,
                random=lambda: 0.0,
                clock=lambda: 0.0,
            )
        except BudgetExhausted as exc:
            return Got(False, None, exc.reason or "budget", True)
        except (TransportError, SecurityError) as exc:
            reason = getattr(exc, "reason", "") or "read_error"
            blocked = reason in {"endpoint_missing", "credential_missing", "selector_rejected"} or getattr(exc, "blocked", False)
            return Got(False, None, reason, bool(blocked))
        if isinstance(result, _Stopped) or not getattr(result, "ok", False):
            reason = getattr(result, "reason", "") or "read_error"
            return Got(False, None, reason, bool(getattr(result, "blocked", False)))
        return Got(True, result.body, "", False)

    def _sleep(self, seconds: float) -> None:
        backend = getattr(self.transport, "backend", None)
        record = getattr(backend, "record_sleep", None)
        if callable(record):
            record(float(seconds))


class _Stopped:
    ok = False
    retryable = False
    reason = "request_cap"
    blocked = True
    body = None
    headers: dict[str, str] = {}

    @property
    def classification(self) -> Any:
        from guardian.sources.retry import Classification

        return Classification("error", "error", False, True, "request_cap")


def resume_fingerprint(cursor: str | None) -> str | None:
    """``fp:`` retries that page. ``solidified:`` starts a new window. A bad token is refused."""
    if not cursor or cursor.startswith("solidified:"):
        return None
    if not cursor.startswith("fp:"):
        raise ValueError("bad_cursor")
    token = cursor[3:]
    if _FINGERPRINT.fullmatch(token) is None:
        raise ValueError("bad_cursor")
    return token


def _parse_page(body: Any) -> tuple[list[Mapping[str, Any]], str | None] | None:
    """Return rows and the next fingerprint. links.next is not a request target."""
    if not isinstance(body, dict) or body.get("success") is False:
        return None
    data = body.get("data")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        return None
    if len(data) > PAGE_LIMIT:
        return None
    fingerprint = None
    meta = body.get("meta")
    if isinstance(meta, dict) and meta.get("fingerprint") not in (None, ""):
        token = meta.get("fingerprint")
        if not isinstance(token, str) or _FINGERPRINT.fullmatch(token) is None:
            return None
        fingerprint = token
    return list(data), fingerprint
