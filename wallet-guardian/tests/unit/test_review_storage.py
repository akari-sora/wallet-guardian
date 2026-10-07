"""Regression tests for tenant collisions, atomic generations and alert persistence."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from guardian.storage import Database


def item(amount="1"):
    return {"position_id": "same", "chain_id": 56, "raw_amount": amount}


def task(owner="a", revision=1):
    return {"task_id": "same", "revision": revision, "state": "DRAFT", "owner": owner}


def test_same_wallet_id_cannot_overwrite_other_tenant(tmp_path):
    db = Database(tmp_path / "db")
    try:
        for tenant in ("a", "b"):
            db.upsert_wallet("same", tenant, "Wallet", tenant, "watch")
        rows = db.conn.execute("SELECT tenant_id, address FROM wallets ORDER BY tenant_id").fetchall()
        assert [tuple(row) for row in rows] == [("a", "a"), ("b", "b")]
    finally:
        db.close()


def test_same_task_id_is_scoped_to_tenant(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.save_task("a", task("a"))
        db.save_task("b", task("b"))
        assert db.get_task("a", "same")["owner"] == "a"
        assert db.get_task("b", "same")["owner"] == "b"
    finally:
        db.close()


def test_tenants_can_commit_the_same_generation(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.commit_snapshot("a", 1, "t", [item("11")])
        db.commit_snapshot("b", 1, "t", [item("22")])
        assert db.snapshot_items("a")[0]["raw_amount"] == "11"
        assert db.snapshot_items("b")[0]["raw_amount"] == "22"
    finally:
        db.close()


def test_same_run_id_cannot_replace_other_tenant(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.save_run("a", "same", {"owner": "a"})
        db.save_run("b", "same", {"owner": "b"})
        rows = db.conn.execute("SELECT tenant_id, payload FROM runs ORDER BY tenant_id").fetchall()
        assert [(row["tenant_id"], json.loads(row["payload"])["owner"]) for row in rows] == [("a", "a"), ("b", "b")]
    finally:
        db.close()


def test_duplicate_alert_does_not_create_second_outbox_entry(tmp_path):
    path = tmp_path / "db"
    db = Database(path)
    db.add_alert("a", "same", {"dedup_key": "same"})
    db.close()
    db = Database(path)
    try:
        db.add_alert("a", "same", {"dedup_key": "same"})
        assert len(db.alerts_for("a")) == 1
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        db.add_alert("b", "same", {"dedup_key": "same"})
        assert len(db.alerts_for("b")) == 1
    finally:
        db.close()


def test_latest_snapshot_is_not_all_history(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.commit_snapshot("a", 1, "t", [item("1")])
        db.commit_snapshot("a", 2, "t", [item("2")])
        assert db.snapshot_items("a") == [item("2")]
        assert db.snapshot_items("a", 1) == [item("1")]
    finally:
        db.close()


def test_failed_generation_rolls_back_snapshot_alert_and_health(tmp_path):
    db = Database(tmp_path / "db")
    try:
        with pytest.raises(ValueError):
            db.commit_snapshot("a", 1, "t", [item(), item()], alerts=[{"dedup_key": "a"}], health={"blind": True})
        assert db.latest_generation("a") == 0
        assert db.alerts_for("a") == []
        assert db.health_for("a")["blind"] is False
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    finally:
        db.close()


def test_snapshot_alert_health_and_task_commit_together(tmp_path):
    db = Database(tmp_path / "db")
    try:
        emitted = db.commit_snapshot("a", 1, "t", [item()], alerts=[{"dedup_key": "a"}], health={"blind": True, "episode": 1}, tasks=[task()])
        assert emitted == [{"dedup_key": "a"}]
        assert db.get_task("a", "same")["owner"] == "a"
        assert db.health_for("a")["blind"] is True
        assert db.health_for("b")["blind"] is False
        assert db.conn.execute("SELECT status FROM outbox").fetchone()[0] == "pending"
    finally:
        db.close()


def test_parallel_parent_generation_cannot_overwrite(tmp_path):
    path = tmp_path / "db"
    first, second = Database(path), Database(path)
    parent = first.latest_generation("a")
    try:
        second.commit_snapshot("a", parent + 1, "t", [item("2")])
        with pytest.raises(Exception, match="snapshot generation"):
            first.commit_snapshot("a", parent + 1, "old", [item("1")])
        assert first.snapshot_items("a")[0]["raw_amount"] == "2"
    finally:
        first.close()
        second.close()


def test_older_task_revision_cannot_overwrite(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.save_task("a", task(revision=3))
        with pytest.raises(Exception, match="older task revision"):
            db.save_task("a", task(revision=1))
        assert db.get_task("a", "same")["revision"] == 3
    finally:
        db.close()


def test_empty_success_is_a_real_generation(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.commit_snapshot("a", 1, "t", [])
        assert db.latest_generation("a") == 1
        assert db.snapshot_items("a") == []
    finally:
        db.close()


def test_legacy_migration_preserves_history_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE wallets (wallet_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            label TEXT NOT NULL, address TEXT NOT NULL, ownership_basis TEXT NOT NULL);
        CREATE TABLE snapshots (generation INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, revision INTEGER NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, dedup_key TEXT NOT NULL, payload TEXT NOT NULL);
    """)
    old.execute("INSERT INTO wallets VALUES ('same','a','Wallet','a','watch')")
    old.execute("INSERT INTO snapshots VALUES (1,'a','old')")
    old.execute("INSERT INTO tasks VALUES ('same','a',1,'DRAFT',?)", (json.dumps(task()),))
    old.executemany("INSERT INTO alerts(tenant_id,dedup_key,payload) VALUES ('a','historic','{}')", [(), ()])
    old.commit()
    old.close()
    for _ in range(2):
        db = Database(path)
        assert db.latest_generation("a") == 1
        assert db.get_task("a", "same")["owner"] == "a"
        assert len(db.alerts_for("a")) == 2  # Never delete historic duplicates.
        assert not db.add_alert("a", "historic", {})
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        db.close()
    db = Database(path)
    try:
        db.commit_snapshot("b", 1, "new", [item("2")])
        db.save_task("b", task("b"))
        assert db.get_task("a", "same")["owner"] == "a"
    finally:
        db.close()


def test_schema_from_future_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "future"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(Exception, match="newer"):
        Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    conn.close()


def test_alert_encoding_failure_is_not_partially_committed(tmp_path):
    db = Database(tmp_path / "db")
    try:
        with pytest.raises(ValueError):
            db.add_alert("a", "bad", {"value": float("nan")})
        assert db.alerts_for("a") == []
        assert db.conn.execute("SELECT COUNT(*) FROM alert_dedup").fetchone()[0] == 0
    finally:
        db.close()


def test_same_revision_payload_is_immutable_but_identical_retry_is_ok(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.save_task("a", task())
        db.save_task("a", task())
        with pytest.raises(Exception, match="same revision"):
            db.save_task("a", task("changed"))
        assert db.get_task("a", "same")["owner"] == "a"
    finally:
        db.close()
