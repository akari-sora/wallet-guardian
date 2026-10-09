"""Qualification state machine for the private MCM shadow.

Round 0 is the baseline and never counts. Qualification is 24 consecutive
incremental windows. An incremental window is 3 hours plus or minus 30 minutes
(2.5h–3.5h). A gap larger than 30 minutes between one completion and the next
start breaks consecutiveness. The span from the first counted window's start
to the last counted window's completion must be at least 72 hours.

Drills are stored beside that sequence and never fill one of the 24 slots.
After the gate, cutover, then an 8-round reverse shadow spanning at least 24
hours. A scope change resets the counted sequence and keeps the earlier
records. Nothing in this module sends, signs, or opens a socket.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

WINDOW_TARGET_SECONDS = 3 * 60 * 60
WINDOW_TOLERANCE_SECONDS = 30 * 60
WINDOW_MIN_SECONDS = WINDOW_TARGET_SECONDS - WINDOW_TOLERANCE_SECONDS
WINDOW_MAX_SECONDS = WINDOW_TARGET_SECONDS + WINDOW_TOLERANCE_SECONDS
MAX_SEQUENCE_GAP_SECONDS = WINDOW_TOLERANCE_SECONDS
QUALIFYING_WINDOWS = 24
QUALIFYING_SPAN_SECONDS = 72 * 60 * 60
REVERSE_ROUNDS = 8
REVERSE_SPAN_SECONDS = 24 * 60 * 60

NON_QUALIFYING_MARKERS = (
    "http_200_only",
    "mock_fixture_pass",
    "native_balance_only",
    "no_events",
    "placeholder_feed",
)
GAP_TOKENS = (
    "budget_exhausted",
    "credential_missing",
    "fetch_error",
    "news_not_supplied",
    "no_allowlisted_feed",
    "private_endpoint",
)
_SCOPE_FIELDS = ("networks", "plugins", "required", "wallets")
# Evidence that must be present and explicit. A missing or malformed value is
# not "zero": the window fails closed with ``evidence_missing:<field>``.
_EVIDENCE_COUNTS = ("duplicates", "secret_leaks", "unexplained_legacy_only", "unexplained_mcm_only")
# Scope identity: config file hash plus a content hash of src/guardian,
# including the packaged resources that process runs (never a commit SHA).
_SCOPE_HASHES = ("config_hash", "content_hash")
_AFTER_GATE = frozenset({"cutover", "done", "qualified", "reverse_shadow"})


def evaluate_rounds(rounds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Replay round records. Prior records stay in ``history`` across a reset."""
    phase = "baseline"
    sequence: list[Mapping[str, Any]] = []
    reverse: list[Mapping[str, Any]] = []
    history: list[str] = []
    drills: list[dict[str, Any]] = []
    resets: list[dict[str, Any]] = []
    reasons: list[str] = []
    gap_evidence: list[dict[str, Any]] = []
    gate_met = False
    frozen: Mapping[str, Any] | None = None

    for record in rounds:
        if not isinstance(record, Mapping):
            reasons.append("unreadable_round")
            continue
        history.append(_round_id(record, len(history)))
        reasons.extend(compare_valid_block(record))
        gap_evidence.extend(_gap_evidence(record))
        drill = record.get("drill")
        if isinstance(drill, str) and drill:
            drills.append(_drill(record, drill))
            reasons.append("drill_separate")
            if drill == "source_failure":
                reasons.append("source_failure")
            if drill == "rollback":
                phase = "rolled_back"
                reasons.append("rolled_back")
            continue
        if record.get("action") == "cutover":
            if gate_met and phase in {"qualified", "cutover"}:
                phase = "cutover"
            else:
                reasons.append("cutover_before_qualified")
            continue
        if record.get("reverse_shadow") is True:
            if not gate_met or phase == "rolled_back":
                reasons.append("reverse_before_qualified" if phase != "rolled_back" else "rolled_back")
                continue
            blocked = _reverse_block(record)
            if blocked:
                reasons.extend(blocked)
                continue
            if phase in {"qualified", "cutover"}:
                phase = "reverse_shadow"
            reverse.append(record)
            if len(reverse) >= REVERSE_ROUNDS and _span(reverse) >= REVERSE_SPAN_SECONDS:
                phase = "done"
            else:
                phase = "reverse_shadow"
            continue
        if phase == "rolled_back":
            reasons.append("rolled_back")
            continue
        if _is_baseline(record):
            reasons.append("baseline_not_counted")
            if phase == "baseline":
                phase = "qualifying"
            if frozen is None and isinstance(record.get("scope"), Mapping):
                frozen = record["scope"]
            continue
        scope = record["scope"] if isinstance(record.get("scope"), Mapping) else {}
        if frozen is None:
            frozen = scope
        elif _scope_changed(frozen, scope):
            resets.append({"reason": "scope_changed", "round_id": _round_id(record, len(history) - 1)})
            reasons.append("scope_changed")
            sequence = []
            reverse = []
            gate_met = False
            phase = "qualifying"
            frozen = scope
        if gate_met:
            reasons.append("after_gate")
            continue
        if phase == "baseline":
            phase = "qualifying"
        blocked = _disqualify(record)
        if blocked:
            reasons.extend(blocked)
            sequence = []
            continue
        length = _window_seconds(record)
        if length is None or length < WINDOW_MIN_SECONDS or length > WINDOW_MAX_SECONDS:
            reasons.append("window_not_incremental")
            sequence = []
            continue
        if sequence:
            gap = _gap_seconds(sequence[-1], record)
            if gap is None or gap > MAX_SEQUENCE_GAP_SECONDS or gap < -MAX_SEQUENCE_GAP_SECONDS:
                reasons.append("sequence_gap")
                sequence = []
        sequence.append(record)
        if len(sequence) >= QUALIFYING_WINDOWS and _span(sequence) >= QUALIFYING_SPAN_SECONDS:
            gate_met = True
            phase = "qualified"

    span = _span(sequence) if sequence else 0
    if not gate_met and phase == "qualifying":
        if len(sequence) < QUALIFYING_WINDOWS:
            reasons.append("qualifying_windows_below_24")
        elif span < QUALIFYING_SPAN_SECONDS:
            reasons.append("span_below_72h")
    reverse_span = _span(reverse) if reverse else 0
    return {
        "baseline_counted": False,
        "drills": drills,
        "gap_evidence": gap_evidence,
        "history": history,
        "phase": phase,
        "qualified": gate_met and phase in _AFTER_GATE,
        "qualifying_windows": len(sequence),
        "reasons": sorted(set(reasons)),
        "resets": resets,
        "reverse_complete": phase == "done",
        "reverse_rounds": len(reverse),
        "reverse_span_seconds": reverse_span,
        "sequence_round_ids": [_round_id(item, index) for index, item in enumerate(sequence)],
        "span_seconds": span,
    }


def compare_valid_block(record: Mapping[str, Any]) -> list[str]:
    """A recorded false compare_valid disqualifies. A missing field stays compatible."""
    if "compare_valid" not in record:
        return []
    if record.get("compare_valid") is True:
        return []
    reason = record.get("compare_reason")
    if isinstance(reason, str) and reason:
        return [reason]
    return ["compare_invalid"]


def _is_baseline(record: Mapping[str, Any]) -> bool:
    return record.get("baseline") is True or record.get("index") == 0


def _disqualify(record: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    markers = record.get("markers") if isinstance(record.get("markers"), list) else []
    for marker in NON_QUALIFYING_MARKERS:
        if marker in markers:
            found.append(marker)
    if record.get("fixture") is True and "mock_fixture_pass" not in found:
        found.append("mock_fixture_pass")
    for field in _EVIDENCE_COUNTS:
        if not _explicit_count(record.get(field)):
            found.append(f"evidence_missing:{field}")
    scope = record.get("scope")
    if not isinstance(scope, Mapping) or not all(
        isinstance(scope.get(name), str) and scope.get(name) for name in _SCOPE_HASHES
    ):
        found.append("scope_missing")
    if _count(record.get("unexplained_legacy_only")) > 0:
        found.append("unexplained_legacy_only")
    if _count(record.get("unexplained_mcm_only")) > 0:
        found.append("unexplained_mcm_only")
    if _count(record.get("duplicates")) > 0:
        found.append("duplicate")
    if _count(record.get("secret_leaks")) > 0:
        found.append("secret_leak")
    if record.get("required_fresh") is not True:
        found.append("required_capability_gap")
    gaps = record.get("required_gaps") if isinstance(record.get("required_gaps"), list) else []
    for token in gaps:
        if isinstance(token, str) and token:
            found.append(token)
    if record.get("no_send") is False:
        found.append("sent")
    elif record.get("no_send") is not True:
        found.append("evidence_missing:no_send")
    found.extend(compare_valid_block(record))
    return found


def _scope_changed(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    for field in _SCOPE_FIELDS:
        if _members(previous.get(field)) != _members(current.get(field)):
            return True
    if previous.get("config_hash") != current.get("config_hash"):
        return True
    if previous.get("content_hash") != current.get("content_hash"):
        return True
    return False


def _gap_evidence(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    required = record.get("required_gaps") if isinstance(record.get("required_gaps"), list) else []
    declared = record.get("declared_gaps") if isinstance(record.get("declared_gaps"), list) else []
    for token in required:
        if isinstance(token, str) and token:
            found.append({"required": True, "status": token, "token": token})
    for token in declared:
        if isinstance(token, str) and token:
            found.append({"required": False, "status": token, "token": token})
    return found


def _drill(record: Mapping[str, Any], drill: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "counts_toward_24": False,
        "drill": drill,
        "round_id": record.get("round_id"),
    }
    if drill == "rollback":
        body["both_senders"] = False
        body["inflight_confirmed"] = True
        body["new_owner_stopped"] = True
        body["watermark"] = record.get("watermark")
    if drill == "source_failure":
        body["qualifying"] = False
        body["reported_as"] = "fetch_error"
    if drill == "restart":
        body["cursor_skip"] = False
    return body


def _reverse_block(record: Mapping[str, Any]) -> list[str]:
    if record.get("no_send") is False:
        return ["reverse_sent"]
    if record.get("no_send") is not True:
        return ["evidence_missing:no_send"]
    if _parse(record.get("started_at")) is None or _parse(record.get("finished_at")) is None:
        return ["reverse_window_unreadable"]
    return []


def _members(value: Any) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, list | tuple | set):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))


def _explicit_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _window_seconds(record: Mapping[str, Any]) -> float | None:
    start = _parse(record.get("started_at"))
    end = _parse(record.get("finished_at"))
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def _gap_seconds(previous: Mapping[str, Any], current: Mapping[str, Any]) -> float | None:
    end = _parse(previous.get("finished_at"))
    start = _parse(current.get("started_at"))
    if end is None or start is None:
        return None
    return (start - end).total_seconds()


def _span(rows: Sequence[Mapping[str, Any]]) -> float:
    start = _parse(rows[0].get("started_at"))
    end = _parse(rows[-1].get("finished_at"))
    if start is None or end is None or end < start:
        return 0
    return (end - start).total_seconds()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _round_id(record: Mapping[str, Any], index: int) -> str:
    name = record.get("round_id")
    if isinstance(name, str) and name:
        return name
    return f"round-{index}"
