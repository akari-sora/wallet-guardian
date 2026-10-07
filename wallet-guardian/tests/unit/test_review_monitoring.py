"""Exercise the production cycle with SQLite, failures, restarts and real diffs."""
from copy import deepcopy

import pytest

from guardian.monitoring import SnapshotCycle, reconcile_scopes
from guardian.storage import Database


def row(label="A", status="WAITING", amount="5", pos="lista:1"):
    return {"position_id": f"{label}:{pos}", "wallet_label": label, "chain_id": 56,
            "raw_amount": amount, "status": status, "quality": "COMPLETE", "fresh": True,
            "paused": False, "observed_at": "original", "symbol": "BNB"}


def cycle(db, incoming, success=None, failed=None, **kwargs):
    return SnapshotCycle(db, "a").finish(incoming=incoming,
        successful_scopes=success if success is not None else {("A", 56), ("B", 56)},
        failed_scopes=failed or [], created="now", **kwargs)


def kinds(result):
    return [alert["kind"] for alert in result["notification_decision"]]


def test_failed_wallet_retains_amount_and_observed_time(tmp_path):
    db = Database(tmp_path / "db")
    try:
        cycle(db, [row("A"), row("B", amount="9")])
        result = cycle(db, [row("A")], {("A", 56)}, ["B:bsc"])
        kept = next(x for x in result["items"] if x["wallet_label"] == "B")
        assert kept["raw_amount"] == "9" and kept["observed_at"] == "original"
        assert kept["quality"] == "STALE" and kept["fresh"] is False
        assert result["diff"]["removed"] == []
        assert kinds(result) == ["monitoring_blind"]
        assert db.snapshot_items("a") == result["items"]
    finally:
        db.close()


def test_all_failed_keeps_entire_previous_snapshot(tmp_path):
    db = Database(tmp_path / "db")
    try:
        cycle(db, [row("A"), row("B")])
        result = cycle(db, [], set(), ["A:bsc", "B:bsc"])
        assert len(result["items"]) == 2
        assert result["diff"] == {"added": [], "removed": [], "changed": []}
        assert all(x["fresh"] is False for x in result["items"])
    finally:
        db.close()


def test_waiting_to_claimable_after_baseline_alerts_once_across_restart(tmp_path):
    path = tmp_path / "db"
    db = Database(path)
    cycle(db, [row()])
    result = cycle(db, [row(status="CLAIMABLE")])
    assert result["baseline"] is False
    assert kinds(result) == ["claimable"]
    db.close()
    db = Database(path)
    try:
        assert kinds(cycle(db, [row(status="CLAIMABLE")])) == []
        assert len(db.alerts_for("a")) == 1
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    finally:
        db.close()


def test_claim_and_unrelated_outage_are_both_reported(tmp_path):
    db = Database(tmp_path / "db")
    try:
        result = cycle(db, [row(status="CLAIMABLE")], {("A", 56)}, ["B:bsc"])
        assert set(kinds(result)) == {"claimable", "monitoring_blind"}
    finally:
        db.close()


def test_blind_repeated_restart_then_recovery_and_new_episode(tmp_path):
    path = tmp_path / "db"
    db = Database(path)
    assert kinds(cycle(db, [], set(), ["A:hyperliquid"])) == ["monitoring_blind"]
    db.close()
    db = Database(path)
    try:
        assert kinds(cycle(db, [], set(), ["A:hyperliquid"])) == []
        assert kinds(cycle(db, [row()])) == ["monitoring_recovered"]
        assert kinds(cycle(db, [row()])) == []
        assert kinds(cycle(db, [], set(), ["A:hyperliquid"])) == ["monitoring_blind"]
        assert db.health_for("a")["episode"] == 2
    finally:
        db.close()


def test_successful_missing_request_is_not_claimed_or_zero(tmp_path):
    db = Database(tmp_path / "db")
    try:
        cycle(db, [row()])
        result = cycle(db, [])
        missing = result["items"][0]
        assert missing["status"] == "MISSING_NO_CLAIM_EVIDENCE"
        assert missing["raw_amount"] == "5" and missing["quality"] == "UNKNOWN"
        assert kinds(result) == ["claim_missing"]
        assert kinds(cycle(db, [])) == []
    finally:
        db.close()


def test_failed_scope_does_not_generate_fresh_claim_from_old_state(tmp_path):
    db = Database(tmp_path / "db")
    try:
        cycle(db, [row(status="CLAIMABLE")])
        result = cycle(db, [], set(), ["A:bsc"])
        assert kinds(result) == ["monitoring_blind"]
        assert result["items"][0]["fresh"] is False
    finally:
        db.close()


def test_paused_flag_blocks_claim_alert_until_unpaused(tmp_path):
    db = Database(tmp_path / "db")
    try:
        assert kinds(cycle(db, [{**row(status="CLAIMABLE"), "paused": True}])) == []
        assert kinds(cycle(db, [row(status="CLAIMABLE")])) == ["claimable"]
    finally:
        db.close()


def test_matching_external_owner_suppresses_only_normal_claim(tmp_path):
    db = Database(tmp_path / "db")
    try:
        result = cycle(db, [row(status="CLAIMABLE")], suppressed_claims={"A:lista:1"})
        assert kinds(result) == []
        assert kinds(cycle(db, [], suppressed_claims={"A:lista:1"})) == ["claim_missing"]
    finally:
        db.close()


def test_parent_captured_before_network_prevents_late_old_commit(tmp_path):
    path = tmp_path / "db"
    first, other = Database(path), Database(path)
    slow = SnapshotCycle(first, "a")
    try:
        cycle(other, [row(amount="9")])
        with pytest.raises(Exception, match="snapshot generation"):
            slow.finish(incoming=[row(amount="1")], successful_scopes={("A", 56)},
                        failed_scopes=[], created="late")
        assert first.snapshot_items("a")[0]["raw_amount"] == "9"
    finally:
        first.close()
        other.close()


def test_empty_initial_failure_does_not_repeat_baseline(tmp_path):
    db = Database(tmp_path / "db")
    try:
        assert cycle(db, [], set(), ["A:bsc"])["baseline"] is True
        result = cycle(db, [row(status="CLAIMABLE")])
        assert result["baseline"] is False
        assert "claimable" in kinds(result)
        assert "首次觀測此請求" in result["notification_decision"][0]["what"]
    finally:
        db.close()


def test_reconcile_does_not_mutate_last_known_evidence():
    previous = [row()]
    original = deepcopy(previous)
    result = reconcile_scopes(previous, [], set())
    assert previous == original
    assert result[0]["quality"] == "STALE"


def test_new_failure_scope_during_outage_is_visible(tmp_path):
    db = Database(tmp_path / "db")
    try:
        cycle(db, [], set(), ["A:bsc"])
        result = cycle(db, [], set(), ["A:bsc", "B:bsc"])
        assert kinds(result) == ["monitoring_blind"]
    finally:
        db.close()


def test_legacy_baseline_alert_is_not_reissued_on_upgrade(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.add_alert("a", "a|56|A:lista:1|claimable|baseline", {
            "dedup_key": "a|56|A:lista:1|claimable|baseline"})
        assert kinds(cycle(db, [row(status="CLAIMABLE")])) == []
    finally:
        db.close()
