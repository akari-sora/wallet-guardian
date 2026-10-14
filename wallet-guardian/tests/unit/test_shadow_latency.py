"""WP-7 detection latency and the P1-5 outbox threshold check.

Missing measurements stay null. A null count does not pass by standing in for zero.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from guardian.comparison import compare_with_legacy, latency_delta_seconds
from guardian.shadow_recorder import (
    attach_slot_metrics,
    detection_latency_summary,
    evaluate_slot,
    outbox_threshold_check,
    record_slot,
)
from guardian.storage import Database

TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
TX = "0x" + ("ab" * 32)
SLOT = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)


def test_percentiles_are_null_when_the_window_has_no_events() -> None:
    empty = detection_latency_summary([])
    assert empty["trigger_count"] == 0
    assert empty["detect_latency_p50_seconds"] is None
    assert empty["detect_latency_p95_seconds"] is None
    assert empty["detect_latency_p95_seconds"] != 0

    missing = detection_latency_summary(None)
    assert missing["trigger_count"] is None
    assert missing["detect_latency_p50_seconds"] is None
    assert missing["detect_latency_p95_seconds"] is None


def test_measured_latency_keeps_zero_and_computes_percentiles() -> None:
    instant = detection_latency_summary([
        {"occurred_at": "2026-10-08T03:00:00Z", "detected_at": "2026-10-08T03:00:00Z"},
    ])
    assert instant["trigger_count"] == 1
    assert instant["detect_latency_p50_seconds"] == 0
    assert instant["detect_latency_p95_seconds"] == 0

    summary = detection_latency_summary([
        _event(10),
        _event(100),
    ])
    assert summary["trigger_count"] == 2
    assert summary["detect_latency_p50_seconds"] == 55
    assert summary["detect_latency_p95_seconds"] == 95.5

    partial = detection_latency_summary([
        _event(10),
        {"observed_at": "2026-10-08T03:00:10Z"},
    ])
    assert partial["trigger_count"] == 2
    assert partial["detect_latency_p50_seconds"] is None
    assert partial["detect_latency_p95_seconds"] is None


def test_events_outside_the_window_are_not_a_zero_latency(tmp_path: Path) -> None:
    start = SLOT
    end = SLOT + timedelta(minutes=30)
    inside = _event(40, detected=SLOT + timedelta(minutes=10))
    outside = _event(5, detected=SLOT + timedelta(hours=2))
    summary = detection_latency_summary([inside, outside], window_start=start, window_end=end)
    assert summary["trigger_count"] == 1
    assert summary["detect_latency_p50_seconds"] == 40
    assert summary["detect_latency_p95_seconds"] == 40

    unplaced = detection_latency_summary(
        [{"occurred_at": "2026-10-08T03:00:00Z"}],
        window_start=start,
        window_end=end,
    )
    assert unplaced["trigger_count"] is None
    assert unplaced["detect_latency_p95_seconds"] is None

    verdict = evaluate_slot(
        slot_at=SLOT,
        started_at=SLOT + timedelta(minutes=5),
        sealed_at=SLOT + timedelta(minutes=20),
        required_scans_ok=True,
        comparison_sealed=True,
        evidence_present=True,
        detection_events=[inside],
        window_start=start,
        window_end=end,
    )
    assert verdict["on_time"] is True
    assert verdict["detect_latency_p95_seconds"] == 40
    saved = record_slot(tmp_path, verdict)
    assert saved["on_time_count"] == 1
    ledger = json.loads((tmp_path / "shadow-slots.json").read_text(encoding="utf-8"))
    assert ledger["schema"] == "guardian.shadow-slots.v1"
    slot = ledger["slots"][verdict["slot_id"]]
    assert slot["detect_latency_p95_seconds"] == 40
    assert slot["detect_latency_p50_seconds"] == 40
    assert slot["trigger_count"] == 1
    assert slot["on_time_count"] == 1


def test_old_slot_file_stays_readable_and_null_latency_is_not_zero(tmp_path: Path) -> None:
    slot_id = "2026-10-08T03:00:00Z"
    legacy = {"schema": "guardian.shadow-slots.v1", "slots": {slot_id: {"attempts": [], "data_filled": False, "on_time_count": 1}}}
    path = tmp_path / "shadow-slots.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    verdict = evaluate_slot(
        slot_at=SLOT,
        started_at=SLOT + timedelta(minutes=5),
        sealed_at=SLOT + timedelta(minutes=20),
        required_scans_ok=True,
        comparison_sealed=True,
        evidence_present=True,
    )
    saved = record_slot(tmp_path, verdict)
    assert saved["on_time_count"] == 1
    assert saved["counted"] is False
    assert saved["detect_latency_p95_seconds"] is None
    assert saved["trigger_count"] is None
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["schema"] == "guardian.shadow-slots.v1"
    slot = stored["slots"][slot_id]
    assert slot["on_time_count"] == 1
    assert slot["detect_latency_p95_seconds"] is None
    assert slot["detect_latency_p50_seconds"] is None
    metrics = attach_slot_metrics(tmp_path, slot_id, detection_latency_summary([_event(12), _event(18), _event(90)]))
    assert metrics["detect_latency_p95_seconds"] != 0
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["slots"][slot_id]["on_time_count"] == 1
    assert again["slots"][slot_id]["trigger_count"] == 3
    assert again["schema"] == "guardian.shadow-slots.v1"


def test_outbox_check_does_not_treat_missing_as_zero() -> None:
    matched = outbox_threshold_check(
        [{"id": 1}],
        [{"threshold_decision": "above"}, {"threshold_decision": "below"}],
    )
    assert matched == {
        "above_threshold_count": 1,
        "matches": True,
        "outbox_count": 1,
        "status": "match",
    }

    quiet = outbox_threshold_check([], [])
    assert quiet["outbox_count"] == 0
    assert quiet["above_threshold_count"] == 0
    assert quiet["matches"] is True

    missing_events = outbox_threshold_check([], None)
    assert missing_events["outbox_count"] == 0
    assert missing_events["above_threshold_count"] is None
    assert missing_events["matches"] is False
    assert missing_events["status"] == "missing"

    missing_outbox = outbox_threshold_check(None, [])
    assert missing_outbox["outbox_count"] is None
    assert missing_outbox["above_threshold_count"] == 0
    assert missing_outbox["matches"] is False

    unmarked = outbox_threshold_check([], [{"threshold_decision": "above"}, {}])
    assert unmarked["above_threshold_count"] is None
    assert unmarked["outbox_count"] == 0
    assert unmarked["matches"] is False

    valued = outbox_threshold_check(
        [{"id": 1}],
        [{"usd_value": "10"}, {"usd_value": 5}, {"value": 4}],
        threshold=5,
    )
    assert valued["above_threshold_count"] == 1
    assert valued["matches"] is True

    missing_value = outbox_threshold_check([{"id": 1}], [{"usd_value": None}], threshold=5)
    assert missing_value["above_threshold_count"] is None
    assert missing_value["matches"] is False

    mismatch = outbox_threshold_check([{"id": 1}, {"id": 2}], [{"above_threshold": True}])
    assert mismatch["status"] == "mismatch"
    assert mismatch["matches"] is False


def test_compare_legacy_latency_delta_column(tmp_path: Path) -> None:
    assert latency_delta_seconds(None, "2026-10-08T03:00:00Z") is None
    assert latency_delta_seconds("2026-10-08T03:01:30Z", None) is None
    assert latency_delta_seconds("2026-10-08T03:01:30Z", "2026-10-08T03:00:00Z") == 90
    assert latency_delta_seconds("2026-10-08T03:00:00Z", "2026-10-08T03:00:00Z") == 0

    database = Database(tmp_path / "monitor.sqlite")
    try:
        _insert_event(database, "2026-10-08T03:01:30Z")
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps({
            "events": [{
                "asset": "evm-native:1",
                "finality": "confirmed",
                "kind": "asset_in",
                "network": "ethereum-mainnet",
                "observed_at": "2026-10-08T03:00:00Z",
                "position": "1",
                "tx": TX,
                "wallet_id": WALLET,
            }],
        }), encoding="utf-8")
        report = compare_with_legacy(legacy, database, TENANT)
    finally:
        database.close()
    assert report["counts"]["matched"] == 1
    assert report["matched"][0]["latency_delta_seconds"] == 90
    for row in report["legacy_only"] + report["mcm_only"] + report["uncomparable"]:
        assert "latency_delta_seconds" in row


def _event(latency: int, *, detected: datetime | None = None) -> dict[str, str]:
    seen = detected or datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)
    occurred = seen - timedelta(seconds=latency)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "detected_at": seen.strftime(stamp),
        "occurred_at": occurred.strftime(stamp),
    }


def _insert_event(database: Database, observed: str) -> None:
    payload = {
        "asset": "evm-native:1",
        "finality": "confirmed",
        "kind": "asset_in",
        "network": "ethereum-mainnet",
        "observed_at": observed,
        "sub_index": "1",
        "tx_id": TX,
        "wallet_id": WALLET,
    }
    identity = "|".join((WALLET, "ethereum-mainnet", TX, "1", "asset_in", "evm-native:1"))
    database.conn.execute(
        """INSERT INTO chain_events(
               tenant_id, identity, wallet_id, network, block_number, block_hash, finality, tx_status, payload
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (TENANT, identity, WALLET, "ethereum-mainnet", "10", None, "confirmed", "success", json.dumps(payload)),
    )
    database.conn.commit()
