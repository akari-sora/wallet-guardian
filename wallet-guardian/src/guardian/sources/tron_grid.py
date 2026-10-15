"""Read-only TronGrid client.

History forces only_confirmed=true and a frozen min/max timestamp. TronGrid
treats a missing max_timestamp as "now", so every page of one walk sends the
same pair. Pages stop at the documented 200-row maximum and at a local page
cap. A page that is not fully verified keeps the fingerprint that fetched it
and the identities of rows already classified. A frozen timestamp window does
not mean those rows are unchanged, so a saved row count is not a skip list.
The following fingerprint is not a resume point. A repeated fingerprint or a
capped window is not complete.
links.next URLs are ignored. The optional key is sent only as
TRON-PRO-API-KEY. Retries sleep for the full Retry-After, or defer when that
wait would pass the round deadline. The deadline is checked before every
attempt, including after that sleep. The shared request-rate budget is not
that wait, and the shared helper's short cap is not applied to it. One call
is in flight. Six HTTP 200s are not a quota. Nothing here signs, builds, or
broadcasts.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from guardian.chains.tron.abi import READ_SELECTORS
from guardian.errors import SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BootstrapStop, BudgetExhausted
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

PAGE_LIMIT = 200
PAGE_CAP = 2
MAX_REQUESTS = 48
LOCAL_MAX_RPS = 1
LOCAL_MAX_CONCURRENCY = 1
HEADER_NAME = "TRON-PRO-API-KEY"
# Used when polling.round_deadline_seconds is omitted. Same bound as the EVM scan.
_DEFAULT_DEADLINE_SECONDS = 180
# read_with_retry clips Retry-After to 30s. The round deadline is this client's cap.
_RETRY_AFTER_UNCLIPPED = float(2**31)
_FINGERPRINT = re.compile(r"^[A-Za-z0-9+/=_-]{1,256}$")
_VERIFIED = re.compile(r"^(?:tx:[0-9a-f]{64}(?::[a-z0-9]+)*|log:[0-9a-f]{64}:[0-9]+)$")
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
    """One frozen window.

    ``cursor`` is the fingerprint to send next time. After a fully processed
    page it is the following page. After an unfinished page it is the
    fingerprint that fetched that page, which may be None for the first page.
    """

    rows: tuple[Mapping[str, Any], ...]
    complete: bool
    cursor: str | None
    reason: str
    blocked: bool
    error: bool


@dataclass(frozen=True)
class Resume:
    """A stored cursor. ``page`` repeats one fingerprint and its window.

    ``verified`` names rows and receipt logs already classified. ``row_at`` is
    only how many leading rows were classified when the page was saved. A later
    response can insert or reorder rows inside the same timestamp window.
    """

    fingerprint: str | None
    min_timestamp: int | None
    max_timestamp: int | None
    kind: str
    row_at: int = 0
    verified: tuple[str, ...] = ()


class TronGridClient:
    """Sequential reader. The full Retry-After is slept on the round clock, or deferred."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        *,
        clock: Any = None,
        sleep: Any = None,
        started_at: float | None = None,
        deadline: int | None = None,
    ) -> None:
        self.transport = transport
        self.source_id = source_id
        self.requests = 0
        self.clock = time.time if clock is None else clock
        self._sleep_fn = sleep
        self.started_at = started_at
        self.deadline = deadline

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

    def history(
        self,
        path: str,
        frozen: Mapping[str, str],
        resume: str | None,
        *,
        accept_page: Any = None,
        page_cap: int | None = None,
    ) -> HistoryWalk:
        """Walk one frozen filter set.

        ``accept_page(rows)`` returns false when this page is not fully
        processed. The walk then stops on the fingerprint that fetched the
        page and does not request the next one. ``page_cap`` defaults to
        ``PAGE_CAP``. Omitting it keeps the previous walk length.
        """
        if resume is not None and _FINGERPRINT.fullmatch(resume) is None:
            return HistoryWalk((), False, None, "bad_cursor", False, True)
        if not path.startswith("/v1/accounts/") or any(token in path for token in ("..", "%", "//", "?", "#")):
            return HistoryWalk((), False, None, "bad_path", True, True)
        cap = PAGE_CAP
        if page_cap is not None:
            if isinstance(page_cap, bool) or not isinstance(page_cap, int) or page_cap < 1:
                return HistoryWalk((), False, None, "bad_page", False, True)
            cap = page_cap
        frozen_query = _freeze_query(frozen, self.clock)
        requested = resume
        seen: set[str] = set()
        rows: list[Mapping[str, Any]] = []
        pages = 0
        while pages < cap:
            query = dict(frozen_query)
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
            if batch and accept_page is not None and not accept_page(batch):
                return HistoryWalk(tuple(rows), False, requested, "verification_pending", False, False)
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
            if self._remaining() <= 0:
                raise BootstrapStop("deadline")
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
                sleep=self._pause,
                random=lambda: 0.0,
                clock=self.clock,
                max_wait=_RETRY_AFTER_UNCLIPPED,
            )
        except BootstrapStop:
            return Got(False, None, "deadline", True)
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

    def _remaining(self) -> float:
        limit = _DEFAULT_DEADLINE_SECONDS
        if isinstance(self.deadline, int) and not isinstance(self.deadline, bool) and self.deadline >= 1:
            limit = self.deadline
        started = self.started_at
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            started = self.clock()
            self.started_at = started
        return float(limit) - (self.clock() - float(started))

    def _sleeper(self) -> Any:
        if self._sleep_fn is not None:
            return self._sleep_fn
        backend = getattr(self.transport, "backend", None)
        for owner in (backend, self.transport):
            sleeper = getattr(owner, "sleeper", None)
            if callable(sleeper):
                return sleeper
        return time.sleep

    def _pause(self, seconds: float) -> None:
        """Wait out Retry-After. A wait past the round deadline is not taken."""
        try:
            planned = float(seconds)
        except (TypeError, ValueError):
            planned = 0.0
        if planned < 0:
            planned = 0.0
        if planned > self._remaining():
            raise BootstrapStop("deadline")
        if planned > 0:
            self._sleeper()(planned)
        backend = getattr(self.transport, "backend", None)
        record = getattr(backend, "record_sleep", None)
        if callable(record):
            record(planned)


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


def frozen_filters(min_timestamp: int, max_timestamp: int) -> dict[str, str]:
    """One pagination set. Both bounds are sent; an omitted max would mean "now"."""
    minimum = _window_bound(min_timestamp, 0)
    maximum = _window_bound(max_timestamp, minimum)
    if maximum < minimum:
        maximum = minimum
    return {
        "limit": str(PAGE_LIMIT),
        "max_timestamp": str(maximum),
        "min_timestamp": str(minimum),
        "only_confirmed": "true",
        "order_by": "block_timestamp,desc",
    }


def parse_resume(cursor: str | None) -> Resume:
    """Split a stored cursor into a fingerprint, window, and verified identities."""
    if cursor is None or cursor == "":
        return Resume(None, None, None, "fresh")
    if not isinstance(cursor, str):
        raise ValueError("bad_cursor")
    minimum: int | None = None
    maximum: int | None = None
    row_at: int | None = None
    verified: list[str] = []
    seen: set[str] = set()
    base = cursor
    if "|min:" in cursor or "|max:" in cursor or "|row:" in cursor or "|v:" in cursor:
        base, _, tail = cursor.partition("|")
        for part in tail.split("|"):
            if part.startswith("min:"):
                minimum = _cursor_int(part[4:])
            elif part.startswith("max:"):
                maximum = _cursor_int(part[4:])
            elif part.startswith("row:"):
                if row_at is not None:
                    raise ValueError("bad_cursor")
                row_at = _cursor_int(part[4:])
            elif part.startswith("v:"):
                token = part[2:]
                if _VERIFIED.fullmatch(token) is None or token in seen:
                    raise ValueError("bad_cursor")
                seen.add(token)
                verified.append(token)
            else:
                raise ValueError("bad_cursor")
    kept = 0 if row_at is None else row_at
    done = tuple(verified)
    if base.startswith("fp:"):
        token = base[3:]
        if _FINGERPRINT.fullmatch(token) is None:
            raise ValueError("bad_cursor")
        return Resume(token, minimum, maximum, "page", kept, done)
    if base.startswith("solidified:"):
        height = base[len("solidified:"):]
        if not height.isdigit():
            raise ValueError("bad_cursor")
        return Resume(None, minimum, maximum, "solidified", kept, done)
    if base == "window":
        return Resume(None, minimum, maximum, "window", kept, done)
    raise ValueError("bad_cursor")


def resume_fingerprint(cursor: str | None) -> str | None:
    """``fp:`` retries that page. A finished window has no fingerprint. A bad token is refused."""
    return parse_resume(cursor).fingerprint


def _freeze_query(frozen: Mapping[str, str], clock: Any) -> dict[str, str]:
    """Copy the caller's bounds once. Later pages cannot observe a new "now"."""
    query = {str(key): str(value) for key, value in frozen.items()}
    query.pop("fingerprint", None)
    query["only_confirmed"] = "true"
    query["limit"] = str(PAGE_LIMIT)
    query.setdefault("order_by", "block_timestamp,desc")
    if "max_timestamp" not in query:
        query["max_timestamp"] = str(int(clock() * 1000))
    if "min_timestamp" not in query:
        query["min_timestamp"] = "0"
    return query


def _window_bound(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def _cursor_int(text: str) -> int:
    if not text.isdigit():
        raise ValueError("bad_cursor")
    return int(text)


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
