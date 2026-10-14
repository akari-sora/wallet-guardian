"""Watchdog delivers, known gaps stay quiet, and a new gap is a separate alert."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from guardian.cli import main
from guardian.configuration import load_public_config
from guardian.delivery import deliver, load_handoff
from guardian.scheduler import Watchdog
from guardian.storage import Database

def _private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"


class _Sender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, payload: dict) -> SimpleNamespace:
        self.sent.append(payload)
        return SimpleNamespace(status="sent", message_id="synthetic-1")


def _public(tmp_path: Path) -> tuple[Path, object]:
    config = tmp_path / "monitor.json"
    config.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return config, load_public_config(config)


def _dog(tmp_path: Path) -> tuple[Watchdog, Database]:
    _config, public = _public(tmp_path)
    database = Database(tmp_path / "monitor.sqlite")
    database.upsert_tenant(TENANT, TENANT)
    return Watchdog(database, TENANT, public), database


def test_known_gap_stays_quiet_and_a_new_gap_alerts(tmp_path: Path) -> None:
    dog, database = _dog(tmp_path)
    healthy = {"desk|ethereum-mainnet|balances.native": "complete_in_declared_scope"}
    opened = dog.evaluate(SimpleNamespace(status="ok", required=healthy), "2026-10-14T00:00:00Z")
    assert opened["emitted"] == []
    worse = {
        "desk|ethereum-mainnet|balances.native": "complete_in_declared_scope",
        "desk|ethereum-mainnet|history.fungible": "blocked",
    }
    first = dog.evaluate(SimpleNamespace(status="degraded", required=worse), "2026-10-14T00:05:00Z")
    assert first["emitted"][0]["kind"] == "monitor_blind"
    again = dog.evaluate(SimpleNamespace(status="degraded", required=worse), "2026-10-14T00:10:00Z")
    assert again["emitted"] == []
    assert again["blind"] is True
    added = dict(worse)
    added["desk|ethereum-mainnet|permissions.erc20"] = "error"
    new = dog.evaluate(SimpleNamespace(status="degraded", required=added), "2026-10-14T00:15:00Z")
    assert [item["kind"] for item in new["emitted"]] == ["monitor_new_gap"]
    assert new["blind"] is True
    restored = {
        "desk|ethereum-mainnet|balances.native": "complete_in_declared_scope",
        "desk|ethereum-mainnet|history.fungible": "complete_in_declared_scope",
        "desk|ethereum-mainnet|permissions.erc20": "complete_in_declared_scope",
    }
    cleared = dog.evaluate(SimpleNamespace(status="ok", required=restored), "2026-10-14T00:20:00Z")
    assert cleared["emitted"][0]["kind"] == "monitor_recovered"
    assert cleared["blind"] is False
    database.close()


def test_watchdog_calls_deliver_and_outbox_ack_does_not_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config, public = _public(tmp_path)
    database = Database(tmp_path / "state" / "monitor.sqlite")
    database.upsert_tenant(TENANT, TENANT)
    dog = Watchdog(database, TENANT, public)
    dog.evaluate(SimpleNamespace(status="failed", required={}), "2026-10-14T00:00:00Z")
    sender = _Sender()
    handoff = load_handoff(public, {})
    report = deliver(database, TENANT, mode="send", sender=sender, handoff=handoff, clock=lambda: 0)
    assert report.status == "send_disabled"
    assert sender.sent == []
    pending = [row for row in database.outbox_for(TENANT) if row["status"] == "pending"]
    assert pending and pending[0]["payload"]["kind"] == "monitor_blind"
    database.close()

    accounts = tmp_path / "accounts.json"
    secrets = tmp_path / "secrets.json"
    _private(accounts, json.dumps({
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
        "accounts": [],
    }))
    _private(secrets, "{}")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secrets))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(tmp_path / "state"))
    code = main(["watchdog", "--mcm-config", str(config)])
    body = json.loads(capsys.readouterr().out)
    assert code == 0, body
    assert body["delivery_status"] == "send_disabled"
    assert body["sent"] == 0
    listed = main(["outbox", "pending", "--mcm-config", str(config)])
    pending_body = json.loads(capsys.readouterr().out)
    assert listed == 0, pending_body
    assert pending_body["rows"]
    row_id = pending_body["rows"][0]["id"]
    blob = json.dumps(pending_body)
    assert "0x" not in blob
    acked = main(["outbox", "ack", "--mcm-config", str(config), "--id", str(row_id)])
    ack_body = json.loads(capsys.readouterr().out)
    assert acked == 0, ack_body
    assert ack_body["status"] == "acknowledged"
    empty = main(["outbox", "pending", "--mcm-config", str(config)])
    empty_body = json.loads(capsys.readouterr().out)
    assert empty == 0
    assert empty_body["rows"] == []
    again = main(["outbox", "ack", "--mcm-config", str(config), "--id", str(row_id)])
    again_body = json.loads(capsys.readouterr().out)
    assert again == 0
    assert again_body["status"] == "acknowledged"


def test_degraded_exit_is_distinct_from_failed() -> None:
    from guardian.cli import _mcm_exit_code

    assert _mcm_exit_code(SimpleNamespace(status="ok", misconfigured=False)) == 0
    assert _mcm_exit_code(SimpleNamespace(status="degraded", misconfigured=False)) == 3
    assert _mcm_exit_code(SimpleNamespace(status="failed", misconfigured=False)) == 1
    assert _mcm_exit_code(SimpleNamespace(status="degraded", misconfigured=True)) == 2
