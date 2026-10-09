"""On-time slot ledger for one unattended shadow writer.

A slot counts only when the round starts inside ±30 minutes and the required
scans plus the comparison are sealed within 60 minutes after the slot.
Misses, timeouts, and missing evidence stay explicit. A catch-up may fill
data and never adds a second on-time round. This module does not scan, sign,
broadcast, or open a socket.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
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
) -> dict[str, Any]:
    """Decide whether one attempt counts. Failures are named, never zero."""
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
    return _body(slot, started, sealed, on_time=on_time, reason=reason, data_filled=data_filled)


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
        slots[slot_id] = current
        state["slots"] = slots
        _save(directory, state)
        return {
            "counted": attempt["counted"] is True,
            "data_filled": current.get("data_filled") is True,
            "on_time": attempt["on_time"] is True,
            "on_time_count": int(current.get("on_time_count") or 0),
            "reason": attempt["reason"],
            "slot_id": slot_id,
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
