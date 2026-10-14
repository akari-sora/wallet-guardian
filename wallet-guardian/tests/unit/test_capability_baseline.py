"""The baselined flag is the first successful read, not the existence of a row."""

from __future__ import annotations

from pathlib import Path

from guardian.storage import Database

WALLET = "11111111-1111-4111-8111-111111111111"
NETWORK = "ethereum-mainnet"
SUCCESS = "2024-01-01T00:00:00Z"


def test_failed_read_does_not_set_or_clear_the_baseline_flag(tmp_path: Path) -> None:
    database = Database(tmp_path / "monitor.sqlite")
    try:
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        _commit(database, "history.native_external", "error", None, baselined=True)
        blocked = _row(database, "history.native_external")
        assert blocked["quality"] == "error"
        assert blocked["baselined"] is False
        assert blocked["last_success_at"] is None

        _commit(database, "history.fungible", "complete_in_declared_scope", SUCCESS, baselined=False)
        quiet = _row(database, "history.fungible")
        assert quiet["last_success_at"] == SUCCESS
        assert quiet["baselined"] is False

        _commit(database, "history.native_external", "complete_in_declared_scope", SUCCESS, baselined=True)
        established = _row(database, "history.native_external")
        assert established["baselined"] is True
        assert established["quality"] == "complete_in_declared_scope"

        _commit(database, "history.native_external", "error", "2024-06-01T00:00:00Z", baselined=True)
        failed_later = _row(database, "history.native_external")
        assert failed_later["quality"] == "error"
        assert failed_later["baselined"] is True
        assert failed_later["last_success_at"] == SUCCESS
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        database.close()


def test_existing_database_gains_baselined_column_without_bumping_user_version(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    original = Database._ensure_capability_baselined_column
    Database._ensure_capability_baselined_column = lambda self: None
    try:
        database = Database(path)
        try:
            assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 1
            columns = {row["name"] for row in database.conn.execute("PRAGMA table_info(capability_state)")}
            assert "baselined" not in columns
            _insert_legacy(database, "history.native_external", "error", None)
            _insert_legacy(database, "balances.native", "complete_in_declared_scope", SUCCESS)
            _insert_legacy(database, "history.fungible", "error", SUCCESS)
            database.conn.commit()
        finally:
            database.close()
    finally:
        Database._ensure_capability_baselined_column = original

    database = Database(path)
    try:
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row["name"] for row in database.conn.execute("PRAGMA table_info(capability_state)")}
        assert "baselined" in columns
        assert _row(database, "history.native_external")["baselined"] is False
        assert _row(database, "balances.native")["baselined"] is True
        assert _row(database, "history.fungible")["baselined"] is True
        assert _row(database, "history.fungible")["quality"] == "error"
    finally:
        database.close()
    again = Database(path)
    try:
        assert again.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert _row(again, "history.native_external")["baselined"] is False
        assert _row(again, "balances.native")["baselined"] is True
    finally:
        again.close()


def _commit(database: Database, capability: str, quality: str, success: str | None, *, baselined: bool) -> None:
    database.commit_capability_cycle(
        "local",
        events=[],
        capability_rows=[{
            "wallet_id": WALLET,
            "network": NETWORK,
            "capability": capability,
            "quality": quality,
            "last_success_at": success,
            "valid_until": None,
            "cursor": None,
            "baselined": baselined,
            "payload": {},
        }],
        cursors=[],
        alerts=[],
    )


def _insert_legacy(database: Database, capability: str, quality: str, success: str | None) -> None:
    database.conn.execute(
        """
        INSERT INTO capability_state(
            tenant_id, wallet_id, network, capability, quality, last_success_at, valid_until, cursor, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("local", WALLET, NETWORK, capability, quality, success, None, None, "{}"),
    )


def _row(database: Database, capability: str) -> dict:
    found = [row for row in database.capability_state_for("local") if row["capability"] == capability]
    assert len(found) == 1
    return found[0]
