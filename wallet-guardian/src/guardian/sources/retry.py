"""Bounded retries. 429 honors Retry-After. Client errors are not retried.

Each attempt is the caller's job to bill against the shared budget. This module
only decides whether another attempt is allowed and how long to wait.
"""

from __future__ import annotations

import email.utils
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timezone
from typing import Any

Clock = Callable[[], float]
Sleeper = Callable[[float], None]
RandomUnit = Callable[[], float]


@dataclass(frozen=True)
class Classification:
    category: str
    quality: str
    retryable: bool
    blocked: bool
    reason: str


def classify_response(status: int, body: Any) -> Classification:
    """Classify one HTTP result. A 200 error body is not an empty observation."""
    if status == 429:
        return Classification("rate_limited", "error", True, False, "rate_limited")
    if status == 401:
        return Classification("credential_rejected", "error", False, True, "credential_rejected")
    if status == 403:
        return Classification("forbidden", "error", False, True, "forbidden")
    if status == 408:
        return Classification("timeout", "error", True, False, "timeout")
    if status in {400, 404} or (400 <= status < 500):
        reason = "bad_request" if status == 400 else "not_found" if status == 404 else "http_status"
        return Classification(reason, "error", False, False, reason)
    if status >= 500:
        return Classification("http_5xx", "error", True, False, "http_5xx")
    if not 200 <= status < 300:
        return Classification("http_status", "error", False, False, "http_status")
    return _classify_ok(body)


def read_with_retry(
    attempt: Callable[[], Any],
    *,
    retry_attempts: int,
    sleep: Sleeper,
    random: RandomUnit,
    clock: Clock,
    base_delay: float = 0.5,
    max_wait: float = 30.0,
) -> Any:
    """Run ``attempt`` once plus up to ``retry_attempts`` repeats. The last failure is returned or raised."""
    total = retry_attempts + 1
    for index in range(total):
        try:
            result = attempt()
        except Exception as exc:
            if not getattr(exc, "retryable", False) or index + 1 == total:
                raise
            delay = retry_delay(
                Classification(str(getattr(exc, "reason", "error")), "error", True, False, str(getattr(exc, "reason", "error"))),
                {},
                attempt=index,
                now=clock(),
                random=random,
                base_delay=base_delay,
                max_wait=max_wait,
            )
            if delay > 0:
                sleep(delay)
            continue
        if getattr(result, "retryable", False) and index + 1 < total:
            delay = retry_delay(
                result.classification,
                result.headers,
                attempt=index,
                now=clock(),
                random=random,
                base_delay=base_delay,
                max_wait=max_wait,
            )
            if delay > 0:
                sleep(delay)
            continue
        return result
    raise RuntimeError("retry loop ended without a result")


def retry_delay(
    classification: Classification,
    headers: Mapping[str, str],
    *,
    attempt: int,
    now: float,
    random: RandomUnit,
    base_delay: float = 0.5,
    max_wait: float = 30.0,
) -> float:
    """Seconds to wait before the next attempt. Retry-After is capped, not jittered away."""
    if classification.category == "rate_limited":
        parsed = _retry_after(headers, now)
        if parsed is not None:
            return min(max_wait, max(0.0, parsed))
    ceiling = min(max_wait, base_delay * (2**attempt))
    unit = random()
    if unit < 0 or unit > 1:
        unit = 0.0
    return unit * ceiling


def _classify_ok(body: Any) -> Classification:
    if isinstance(body, list):
        errors = [item for item in body if isinstance(item, dict) and "error" in item]
        oks = [item for item in body if isinstance(item, dict) and "result" in item and "error" not in item]
        if errors and oks:
            return Classification("partial", "partial", False, False, "partial")
        if errors:
            return Classification("error", "error", False, False, "rpc_error")
        return Classification("ok", "partial", False, False, "ok")
    if isinstance(body, dict) and body.get("partial") is True:
        return Classification("partial", "partial", False, False, "partial")
    if isinstance(body, dict) and "error" in body and "result" not in body:
        return Classification("error", "error", False, False, "rpc_error")
    if isinstance(body, dict) and "error" in body and "result" in body:
        return Classification("partial", "partial", False, False, "partial")
    if _etherscan_rate_limit(body):
        return Classification("rate_limited", "error", True, False, "rate_limited")
    if _etherscan_empty(body):
        return Classification("empty", "complete_in_declared_scope", False, False, "empty")
    if isinstance(body, dict) and str(body.get("status")) == "0" and isinstance(body.get("result"), list):
        return Classification("partial", "partial", False, False, "partial")
    if isinstance(body, dict) and str(body.get("status")) == "0" and isinstance(body.get("result"), str):
        return Classification("error", "error", False, False, "source_error")
    return Classification("ok", "partial", False, False, "ok")


def _etherscan_rate_limit(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    result = body.get("result")
    message = body.get("message")
    return _contains_rate_limit(result) or _contains_rate_limit(message)


def _contains_rate_limit(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return "Max rate limit reached" in value or "Max calls per sec rate limit reached" in value


def _etherscan_empty(body: Any) -> bool:
    """Etherscan's real empty shape is {"status":"0","message":"No transactions found","result":[]}.

    Older/proxy variants put the text in ``result``. Either way the result must be
    empty: a non-empty list with status 0 stays partial.
    """
    if not isinstance(body, dict) or str(body.get("status")) != "0":
        return False
    result = body.get("result")
    texts = [value for value in (body.get("message"), result) if isinstance(value, str)]
    if not any("No transactions found" in text or "No records found" in text for text in texts):
        return False
    return result == [] or isinstance(result, str)


def _retry_after(headers: Mapping[str, str], now: float) -> float | None:
    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        return float(text)
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() - now
