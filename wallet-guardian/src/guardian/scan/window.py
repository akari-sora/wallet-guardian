"""Fixed-target catch-up windows.

The target is the head captured at the start of the round. Chunks stop at that
target, at a budget or deadline, or at the first unreadable block. A range the
endpoint rejects is split. A one-block failure is a gap and is not skipped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from guardian.chains.evm.adapter import LOG_RANGE_MAX
from guardian.errors import SecurityError
from guardian.sources.budget import BootstrapStop, BudgetExhausted
from guardian.sources.retry import read_with_retry

_RANGE_NEEDLES = (
    "10,000",
    "-32701",
    "block range",
    "exceed maximum block range",
    "exceeds max",
    "log response size exceeded",
    "more than 10000",
    "query returned more than",
    "range is too",
    "response size exceeded",
    "too many results",
)


@dataclass(frozen=True)
class LogRead:
    ok: bool
    reason: str
    gap: str | None
    covered_end: int | None
    refused_range: bool = False


def plan_chunks(
    stored: int | None,
    head: int,
    *,
    limit: int,
    bootstrap_origin: int | None,
    overlap: int,
    cap_at: int | None = LOG_RANGE_MAX,
) -> tuple[list[tuple[int, int]], int]:
    """Chunks from the caller-supplied origin, or from the cursor minus overlap, through head.

    ``cap_at`` limits one span. ``eth_getLogs`` keeps the 2000-block cap.
    An indexer that already bisects on a full result window passes ``cap_at=None``.
    """
    span = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else LOG_RANGE_MAX
    if cap_at is not None and span > cap_at:
        span = cap_at
    if head < 0:
        head = 0
    if stored is None:
        origin = 0 if bootstrap_origin is None else max(0, bootstrap_origin)
    else:
        origin = max(0, stored + 1 - overlap)
    if stored is not None and stored > head:
        return [], origin
    if origin > head:
        return [(head, head)], origin
    ranges: list[tuple[int, int]] = []
    cursor = origin
    while cursor <= head:
        end = min(head, cursor + span)
        ranges.append((cursor, end))
        if end >= head:
            break
        cursor = end + 1
    return ranges, origin


def range_refusal(body: Any, reason: str = "") -> bool:
    """True when the endpoint rejected the span. A generic read error is not one of these."""
    parts = [reason.lower()]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            parts.append(str(err.get("code", "")))
            parts.append(str(err.get("message", "")))
        elif isinstance(err, str):
            parts.append(err)
        message = body.get("message")
        if isinstance(message, str):
            parts.append(message)
        result = body.get("result")
        if isinstance(result, str):
            parts.append(result)
    text = " ".join(parts).lower()
    if "32701" in text:
        return True
    return any(needle in text for needle in _RANGE_NEEDLES)


def emitter_address(operation: Any) -> Any:
    params = getattr(operation, "params", None)
    if isinstance(params, list) and params and isinstance(params[0], dict):
        return params[0].get("address")
    return None


def reject_wallet_emitter(operation: Any, wallets: set[str]) -> None:
    """eth_getLogs.address is the emitting contract. A monitored wallet is refused."""
    emitter = emitter_address(operation)
    values = emitter if isinstance(emitter, list) else [emitter]
    for item in values:
        if isinstance(item, str) and item.lower() in wallets:
            raise SecurityError("wallet_is_not_log_emitter")


def read_log_leaf(
    transport: Any,
    source_id: str,
    build: Callable[[int, int], Any],
    start: int,
    end: int,
    *,
    wallets: set[str],
    retry_attempts: int,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[LogRead, list[Any]]:
    """One ``eth_getLogs`` for ``start..end``. No bisection.

    A sleeper that refuses the wait raises ``BootstrapStop`` and is not swallowed.
    """
    if end < start:
        return LogRead(False, "bad_range", None, None), []
    operation = build(start, end)
    reject_wallet_emitter(operation, wallets)
    try:
        result = read_with_retry(
            lambda: transport.request(source_id, operation),
            retry_attempts=retry_attempts,
            sleep=sleep,
            random=lambda: 0.0,
            clock=clock,
        )
    except (SecurityError, BudgetExhausted, BootstrapStop):
        raise
    except Exception as exc:
        reason = str(getattr(exc, "reason", "") or "read_error")
        if start == end:
            return LogRead(False, reason, f"single_block_unreadable:{start}", None), []
        return LogRead(False, reason, None, None), []
    logs = _logs(result)
    if logs is not None:
        return LogRead(True, "", None, end), logs
    body = getattr(result, "body", None)
    reason = str(getattr(result, "reason", "") or "read_error")
    if start == end:
        return LogRead(False, reason, f"single_block_unreadable:{start}", None), []
    if range_refusal(body, reason) and end > start:
        return LogRead(False, reason, None, None, refused_range=True), []
    return LogRead(False, reason, None, None), []


def read_logs(
    transport: Any,
    source_id: str,
    build: Callable[[int, int], Any],
    start: int,
    end: int,
    *,
    wallets: set[str],
    retry_attempts: int,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    on_leaf: Callable[[int, int, list[Any]], None] | None = None,
) -> LogRead:
    """Read one span. Bisect only when the endpoint says the range is too large."""
    leaf, logs = read_log_leaf(
        transport, source_id, build, start, end,
        wallets=wallets, retry_attempts=retry_attempts, sleep=sleep, clock=clock,
    )
    if leaf.ok:
        if on_leaf is not None:
            on_leaf(start, end, logs)
        return leaf
    if leaf.refused_range and end > start:
        mid = start + (end - start) // 2
        left = read_logs(
            transport, source_id, build, start, mid,
            wallets=wallets, retry_attempts=retry_attempts, sleep=sleep, clock=clock, on_leaf=on_leaf,
        )
        if not left.ok:
            return left
        return read_logs(
            transport, source_id, build, mid + 1, end,
            wallets=wallets, retry_attempts=retry_attempts, sleep=sleep, clock=clock, on_leaf=on_leaf,
        )
    return leaf


def _logs(result: Any) -> list[Any] | None:
    if not getattr(result, "ok", False):
        return None
    body = getattr(result, "body", None)
    if isinstance(body, dict):
        rows = body.get("result")
        if isinstance(rows, list) and all(isinstance(item, dict) for item in rows):
            return rows
        return None
    if isinstance(body, list):
        merged: list[Any] = []
        for item in body:
            if not isinstance(item, dict) or "error" in item:
                return None
            rows = item.get("result")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                return None
            merged.extend(rows)
        return merged
    return None


def window_covers(chunks: list[tuple[int, int]], origin: int, target: int, covered_end: int | None, gaps: list[str]) -> bool:
    """True only when the successful chunks' union is the whole origin..target span."""
    if gaps or covered_end is None or not chunks:
        return False
    if chunks[0][0] != origin or covered_end != target:
        return False
    previous = origin - 1
    for start, end in chunks:
        if start > previous + 1:
            return False
        previous = max(previous, end)
    return previous >= target
