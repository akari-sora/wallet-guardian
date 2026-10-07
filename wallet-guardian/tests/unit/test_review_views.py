"""Test the same scoping functions used by snapshot/claims/actions CLI views."""
import pytest
from guardian.errors import ConfigError
from guardian.storage import Database
from guardian.read_views import latest_items, resolve_tenant


def item(label, amount):
    return {"position_id": label + ":lista:1", "chain_id": 56, "raw_amount": amount}


def test_views_exclude_old_generations_and_other_tenants(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.commit_snapshot("a", 1, "old", [item("A", "1")])
        db.commit_snapshot("a", 2, "new", [item("A", "2"), item("B", "3")])
        db.commit_snapshot("b", 1, "new", [item("A", "99")])
        with pytest.raises(ConfigError, match="--tenant"):
            resolve_tenant(db)
        tenant = resolve_tenant(db, "a")
        assert [r["raw_amount"] for r in latest_items(db, tenant)] == ["2", "3"]
        assert [r["raw_amount"] for r in latest_items(db, tenant, "A")] == ["2"]
    finally:
        db.close()


def test_single_tenant_is_resolved_without_a_flag(tmp_path):
    db = Database(tmp_path / "db")
    try:
        assert resolve_tenant(db) is None
        assert latest_items(db, None) == []
        db.upsert_tenant("local", "local")
        assert resolve_tenant(db) == "local"
    finally:
        db.close()


def test_unknown_tenant_or_wallet_fails_closed(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.upsert_tenant("a", "a")
        with pytest.raises(ConfigError):
            resolve_tenant(db, "b")
        with pytest.raises(ConfigError):
            latest_items(db, "a", "not-my-wallet")
    finally:
        db.close()


def test_legacy_unlabelled_items_still_filter_exactly(tmp_path):
    db = Database(tmp_path / "db")
    try:
        db.commit_snapshot("a", 1, "t", [item("A", "1"), item("AA", "2")])
        assert [r["raw_amount"] for r in latest_items(db, "a", "A")] == ["1"]
    finally:
        db.close()
