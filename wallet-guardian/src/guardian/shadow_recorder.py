"""On-time slot ledger for one unattended shadow writer.

A slot counts only when the round starts inside ±30 minutes and the required
scans plus the comparison are sealed within 60 minutes after the slot.
Misses, timeouts, and missing evidence stay explicit. A catch-up may fill
data and never adds a second on-time round. Each slot can also carry a trigger
count and detection-latency percentiles. Those are null when the window was
not measured, including when it had no events. Null is not stored as 0.
This module does not scan, sign, broadcast, or open a socket.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError

START_TOLERANCE = timedelta(minutes=30)
SEAL_DEADLINE = timedelta(minutes=60)
_SCHEMA = "guardian.shadow-slots.v1"
_FILE_MODE = 0o600
_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_LATENCY_COUNT = "trigger_count"
_LATENCY_P50 = "detect_latency_p50_seconds"
_LATENCY_P95 = "detect_latency_p95_seconds"
_LATENCY_FIELDS = (_LATENCY_COUNT, _LATENCY_P50, _LATENCY_P95)
_OCCURRED_KEYS = ("occurred_at", "event_time", "block_timestamp")
_DETECTED_KEYS = ("detected_at", "observed_at")
_ABOVE = frozenset({"above", "above_threshold"})
_NOT_ABOVE = frozenset({"below", "below_threshold", "valuation_unknown"})
_OUTBOX_TIME_KEYS = ("created_at", "observed_at", "detected_at")


def evaluate_slot(
    *,
    slot_at: datetime,
    started_at: datetime | None,
    sealed_at: datetime | None,
    required_scans_ok: bool,
    comparison_sealed: bool,
    evidence_present: bool,
    timed_out: bool = False,
    missed: bool = False,
    catch_up: bool = False,
    detection_events: Sequence[Mapping[str, Any]] | None = None,
    window_start: datetime | str | None = None,
    window_end: datetime | str | None = None,
) -> dict[str, Any]:
    """Decide whether one attempt counts. Failures are named, never zero.

    ``detection_events`` omitted means the latency was not measured (null).
    An empty list means the window had no events: the trigger count is 0 and
    both percentiles stay null.
    """
    _flags(required_scans_ok, comparison_sealed, evidence_present, timed_out, missed, catch_up)
    slot = _aware(slot_at, "slot_at")
    started = _aware(started_at, "started_at") if started_at is not None else None
    sealed = _aware(sealed_at, "sealed_at") if sealed_at is not None else None
    reason = _reason(
        slot,
        started,
        sealed,
        required_scans_ok=required_scans_ok,
        comparison_sealed=comparison_sealed,
        evidence_present=evidence_present,
        timed_out=timed_out,
        missed=missed,
    )
    data_filled = bool(
        catch_up
        and evidence_present
        and required_scans_ok
        and comparison_sealed
        and sealed is not None
        and not missed
        and not timed_out
    )
    on_time = reason == "on_time"
    if data_filled and on_time:
        reason = "catch_up"
        on_time = False
    body = _body(slot, started, sealed, on_time=on_time, reason=reason, data_filled=data_filled)
    if detection_events is None:
        body.update(_blank_latency())
    else:
        body.update(detection_latency_summary(
            detection_events,
            window_start=window_start,
            window_end=window_end,
        ))
    return body


def record_slot(directory: Path, verdict: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one attempt. The on-time count for a slot stays 0 or 1."""
    slot_id = verdict.get("slot_id")
    if not isinstance(slot_id, str) or not slot_id:
        raise ConfigError("slot_id")
    with sole_writer_lock(directory):
        state = _load(directory)
        slots = state["slots"]
        current = slots.get(slot_id)
        if not isinstance(current, dict):
            current = {"attempts": [], "data_filled": False, "on_time_count": 0}
        attempts = current.get("attempts")
        if not isinstance(attempts, list):
            attempts = []
        attempt = {
            "counted": False,
            "data_filled": verdict.get("data_filled") is True,
            "on_time": False,
            "reason": str(verdict.get("reason") or "unreadable"),
        }
        attempt.update(_latency_from_verdict(verdict))
        if verdict.get("on_time") is True and verdict.get("counted") is True:
            if int(current.get("on_time_count") or 0) >= 1:
                attempt["reason"] = "already_counted"
            else:
                current["on_time_count"] = 1
                attempt["counted"] = True
                attempt["on_time"] = True
                attempt["reason"] = "on_time"
        if attempt["data_filled"]:
            current["data_filled"] = True
        if int(current.get("on_time_count") or 0) > 1:
            current["on_time_count"] = 1
        attempts.append(attempt)
        current["attempts"] = attempts
        current.update(_latency_from_verdict(verdict))
        slots[slot_id] = current
        state["slots"] = slots
        _save(directory, state)
        return {
            "counted": attempt["counted"] is True,
            "data_filled": current.get("data_filled") is True,
            "detect_latency_p50_seconds": current.get(_LATENCY_P50),
            "detect_latency_p95_seconds": current.get(_LATENCY_P95),
            "on_time": attempt["on_time"] is True,
            "on_time_count": int(current.get("on_time_count") or 0),
            "reason": attempt["reason"],
            "slot_id": slot_id,
            "trigger_count": current.get(_LATENCY_COUNT),
        }


def detection_latency_summary(
    events: Sequence[Mapping[str, Any]] | None,
    *,
    window_start: datetime | str | None = None,
    window_end: datetime | str | None = None,
) -> dict[str, Any]:
    """Trigger count plus detect-latency P50 and P95 for one slot.

    ``events`` omitted, unreadable, or not placeable in the window yields null
    for every field. An empty window yields trigger count 0 and null
    percentiles. A measured latency of 0 seconds stays 0. Percentiles are never
    filled in with 0 when the window has no usable events.
    """
    blank = _blank_latency()
    if events is None or isinstance(events, str | bytes) or not isinstance(events, Sequence):
        return blank
    start, end, bounded = _window_bounds(window_start, window_end)
    if bounded is None:
        return blank
    selected: list[Mapping[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            return blank
        if bounded:
            detected = _first_instant(event, _DETECTED_KEYS)
            if detected is None or start is None or end is None:
                return blank
            if detected < start or detected > end:
                continue
        selected.append(event)
    latencies: list[float] = []
    for event in selected:
        latency = _latency_seconds(event)
        if latency is None:
            return {
                _LATENCY_COUNT: len(selected),
                _LATENCY_P50: None,
                _LATENCY_P95: None,
            }
        latencies.append(latency)
    if not latencies:
        return {_LATENCY_COUNT: 0, _LATENCY_P50: None, _LATENCY_P95: None}
    return {
        _LATENCY_COUNT: len(latencies),
        _LATENCY_P50: _percentile(latencies, 0.50),
        _LATENCY_P95: _percentile(latencies, 0.95),
    }


def attach_slot_metrics(directory: Path, slot_id: str, metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Store detection metrics on one slot. Does not change the on-time count."""
    if not isinstance(slot_id, str) or not slot_id:
        raise ConfigError("slot_id")
    stored = _latency_from_verdict(metrics)
    with sole_writer_lock(directory):
        state = _load(directory)
        slots = state["slots"]
        current = slots.get(slot_id)
        if not isinstance(current, dict):
            current = {"attempts": [], "data_filled": False, "on_time_count": 0}
        current.update(stored)
        slots[slot_id] = current
        state["slots"] = slots
        _save(directory, state)
    return {"slot_id": slot_id, **stored}


def outbox_threshold_check(
    outbox_rows: Sequence[Mapping[str, Any]] | None,
    chain_events: Sequence[Mapping[str, Any]] | None,
    *,
    threshold: int | float | str | None = None,
    window_start: datetime | str | None = None,
    window_end: datetime | str | None = None,
) -> dict[str, Any]:
    """Recorded check: outbox rows equal chain events that exceed the threshold.

    A missing list, an unreadable row, or an event with no explicit threshold
    status makes that count null. The check passes only when both counts are
    known and equal. A null count is not treated as zero, so it does not pass
    against a real zero.
    """
    outbox_count = _scoped_count(outbox_rows, window_start, window_end, _OUTBOX_TIME_KEYS)
    above = _above_count(chain_events, threshold, window_start, window_end)
    known = isinstance(outbox_count, int) and isinstance(above, int)
    return {
        "above_threshold_count": above,
        "matches": known and outbox_count == above,
        "outbox_count": outbox_count,
        "status": "match" if known and outbox_count == above else "missing" if not known else "mismatch",
    }


@contextmanager
def sole_writer_lock(directory: Path) -> Iterator[None]:
    """One shadow writer. A second round raises ``shadow_round_locked``."""
    root = Path(directory)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError("shadow state directory") from exc
    key = str(root.resolve())
    thread_lock = _thread_lock(key)
    if not thread_lock.acquire(blocking=False):
        raise ConfigError("shadow_round_locked")
    fd: int | None = None
    locked = False
    try:
        lock_path = root / ".shadow-round.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), _FILE_MODE)
        if os.name == "posix":
            os.chmod(lock_path, _FILE_MODE)
        _lock_fd(fd)
        locked = True
        yield
    finally:
        if fd is not None:
            if locked:
                _unlock_fd(fd)
            os.close(fd)
        thread_lock.release()


def _reason(
    slot: datetime,
    started: datetime | None,
    sealed: datetime | None,
    *,
    required_scans_ok: bool,
    comparison_sealed: bool,
    evidence_present: bool,
    timed_out: bool,
    missed: bool,
) -> str:
    if missed:
        return "missed"
    if timed_out:
        return "timeout"
    if started is None:
        return "not_started"
    if not evidence_present:
        return "evidence_missing"
    if not required_scans_ok:
        return "required_scans_incomplete"
    if not comparison_sealed:
        return "comparison_not_sealed"
    delta = (started - slot).total_seconds()
    if delta < -START_TOLERANCE.total_seconds():
        return "early_start"
    if delta > START_TOLERANCE.total_seconds():
        return "late_start"
    if sealed is None:
        return "not_sealed"
    if sealed < started:
        return "seal_before_start"
    if (sealed - slot).total_seconds() > SEAL_DEADLINE.total_seconds():
        return "late_seal"
    return "on_time"


def _body(
    slot: datetime,
    started: datetime | None,
    sealed: datetime | None,
    *,
    on_time: bool,
    reason: str,
    data_filled: bool,
) -> dict[str, Any]:
    return {
        "counted": on_time,
        "data_filled": data_filled,
        "on_time": on_time,
        "reason": reason,
        "sealed_at": _iso(sealed) if sealed is not None else None,
        "slot_at": _iso(slot),
        "slot_id": _iso(slot),
        "started_at": _iso(started) if started is not None else None,
        "status": reason,
    }


def _flags(*values: bool) -> None:
    for value in values:
        if type(value) is not bool:
            raise ConfigError("slot flags must be booleans")


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConfigError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank_latency() -> dict[str, Any]:
    return {_LATENCY_COUNT: None, _LATENCY_P50: None, _LATENCY_P95: None}


def _latency_from_verdict(verdict: Mapping[str, Any]) -> dict[str, Any]:
    return {
        _LATENCY_COUNT: _count_or_null(verdict.get(_LATENCY_COUNT)) if _LATENCY_COUNT in verdict else None,
        _LATENCY_P50: _seconds_or_null(verdict.get(_LATENCY_P50)) if _LATENCY_P50 in verdict else None,
        _LATENCY_P95: _seconds_or_null(verdict.get(_LATENCY_P95)) if _LATENCY_P95 in verdict else None,
    }


def _count_or_null(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _seconds_or_null(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return _json_number(float(value))


def _json_number(value: float) -> int | float:
    nearest = round(value, 6)
    if nearest == int(nearest):
        return int(nearest)
    return nearest


def _percentile(values: Sequence[float], fraction: float) -> int | float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return _json_number(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    blended = ordered[lower] * (1 - weight) + ordered[upper] * weight
    return _json_number(blended)


def _window_bounds(
    window_start: datetime | str | None,
    window_end: datetime | str | None,
) -> tuple[datetime | None, datetime | None, bool | None]:
    """Return start, end, and whether a window was requested.

    The third value is False for an open window, True for a closed one, and
    None when the bounds are incomplete or unreadable.
    """
    if window_start is None and window_end is None:
        return None, None, False
    if window_start is None or window_end is None:
        return None, None, None
    start = _instant(window_start)
    end = _instant(window_end)
    if start is None or end is None or end < start:
        return None, None, None
    return start, end, True


def _latency_seconds(event: Mapping[str, Any]) -> float | None:
    occurred = _first_instant(event, _OCCURRED_KEYS)
    detected = _first_instant(event, _DETECTED_KEYS)
    if occurred is None or detected is None:
        return None
    return (detected - occurred).total_seconds()


def _first_instant(event: Mapping[str, Any], keys: tuple[str, ...]) -> datetime | None:
    for key in keys:
        if key not in event:
            continue
        parsed = _instant(event.get(key))
        if parsed is not None:
            return parsed
    return None


def _instant(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return _instant(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _scoped_count(
    rows: Sequence[Mapping[str, Any]] | None,
    window_start: datetime | str | None,
    window_end: datetime | str | None,
    time_keys: tuple[str, ...],
) -> int | None:
    if rows is None or isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        return None
    start, end, bounded = _window_bounds(window_start, window_end)
    if bounded is None:
        return None
    count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        if not bounded:
            count += 1
            continue
        moment = _row_instant(row, time_keys)
        if moment is None or start is None or end is None:
            return None
        if start <= moment <= end:
            count += 1
    return count


def _row_instant(row: Mapping[str, Any], keys: tuple[str, ...]) -> datetime | None:
    found = _first_instant(row, keys)
    if found is not None:
        return found
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        return _first_instant(payload, keys)
    return None


def _above_count(
    events: Sequence[Mapping[str, Any]] | None,
    threshold: int | float | str | None,
    window_start: datetime | str | None,
    window_end: datetime | str | None,
) -> int | None:
    if events is None or isinstance(events, str | bytes) or not isinstance(events, Sequence):
        return None
    if threshold is not None and _numeric(threshold) is None:
        return None
    start, end, bounded = _window_bounds(window_start, window_end)
    if bounded is None:
        return None
    count = 0
    for event in events:
        if not isinstance(event, Mapping):
            return None
        if bounded:
            moment = _first_instant(event, _DETECTED_KEYS)
            if moment is None or start is None or end is None:
                return None
            if moment < start or moment > end:
                continue
        flag = _above_flag(event, threshold)
        if flag is None:
            return None
        if flag:
            count += 1
    return count


def _above_flag(event: Mapping[str, Any], threshold: int | float | str | None) -> bool | None:
    if threshold is not None:
        limit = _numeric(threshold)
        value = _event_value(event)
        if limit is None or value is None:
            return None
        return value > limit
    if "above_threshold" in event:
        flag = event.get("above_threshold")
        if type(flag) is bool:
            return flag
        return None
    decision = event.get("threshold_decision")
    if isinstance(decision, str):
        token = decision.strip().lower()
        if token in _ABOVE:
            return True
        if token in _NOT_ABOVE:
            return False
    return None


def _event_value(event: Mapping[str, Any]) -> float | None:
    for key in ("usd_value", "value"):
        if key in event:
            return _numeric(event.get(key))
    return None


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"nan", "inf", "+inf", "-inf"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        return number
    return None


def _load(directory: Path) -> dict[str, Any]:
    path = directory / "shadow-slots.json"
    if not path.is_file():
        return {"schema": _SCHEMA, "slots": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("shadow slot ledger is not json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("slots"), dict):
        raise ConfigError("shadow slot ledger is not a ledger")
    payload["schema"] = _SCHEMA
    return payload


def _save(directory: Path, state: Mapping[str, Any]) -> None:
    blob = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target = directory / "shadow-slots.json"
    temporary = directory / "shadow-slots.json.tmp"
    temporary.write_text(blob, encoding="utf-8", newline="\n")
    os.replace(temporary, target)


def _thread_lock(key: str) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _lock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            if os.lseek(fd, 0, os.SEEK_END) < 1:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ConfigError("shadow_round_locked") from exc


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
