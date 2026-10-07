"""Entry-point integration, with real SQLite and mocked network I/O.

Requires the normal project dependencies. A dependency skip is not a passing
integration gate; CI must install uv.lock before this suite is accepted.
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

pytest.importorskip("eth_abi", reason="normal project dependency is required for service integration")
pytest.importorskip("eth_utils", reason="normal project dependency is required for service integration")
from guardian import cli, service
from guardian.errors import ConfigError, RpcError
from guardian.storage import Database

A, B = "0x" + "1" * 40, "0x" + "2" * 40


def state(claimable=False):
    return {"chain_id": 56, "block_number": 10, "block_hash": "0x" + "a" * 64,
        "block_timestamp": int(time.time()), "finality": "provisional", "native_wei": "1000",
        "slisbnb_raw": "50", "paused": False, "implementation": "0x" + "3" * 40,
        "proxy_code_sha256": "1" * 64, "implementation_code_sha256": "2" * 64,
        "requests": [{"idx": 0, "uuid": "1", "amount_in_slis_bnb": "5", "start_time": 1,
            "is_claimable": claimable, "bnb_amount": "6"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = {"tenant_label": "a", "mode": "read_only", "wallets": [
        {"label": "A", "address": A, "family": "evm"},
        {"label": "B", "address": B, "family": "evm"}]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg))
    states = {A: state(), B: state()}
    def read(_rpc, address):
        value = states[address]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(service, "RpcClient", lambda _url: object())
    monkeypatch.setattr(service, "read_lista_state", read)
    monkeypatch.setattr(service, "_hyperliquid", lambda _address: {"quality": "PARTIAL"})
    monkeypatch.setattr(service, "screen_ofac", lambda _wallets: {"level": "LIMITED_DISCOVERY"})
    return config_path, tmp_path / "state.sqlite", states


def test_run_once_mixed_failure_preserves_portfolio_and_new_claim(setup):
    config, path, states = setup
    service.run_once(config, path)
    states[A] = state(True)
    states[B] = httpx.ReadTimeout("synthetic timeout")
    result = service.run_once(config, path)
    assert result["status"] == "degraded"
    assert result["diff"]["removed"] == []
    assert {a["kind"] for a in result["notification_decision"]} == {"claimable", "monitoring_blind"}
    assert result["notifications_sent"] is False and result["agent_broadcast"] is False
    db = Database(path)
    try:
        assert any(i["wallet_label"] == "B" and i["quality"] == "STALE" for i in db.snapshot_items("a"))
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2
    finally:
        db.close()


def test_run_once_full_failure_does_not_delete_positions(setup):
    config, path, states = setup
    service.run_once(config, path)
    states[A] = RpcError("offline")
    states[B] = RpcError("offline")
    assert service.run_once(config, path)["diff"]["removed"] == []
    assert service.run_once(config, path)["notification_decision"] == []


def test_hyperliquid_failure_is_not_a_healthy_run(setup, monkeypatch):
    config, path, _ = setup
    def fail(_address):
        raise httpx.ConnectError("synthetic")
    monkeypatch.setattr(service, "_hyperliquid", fail)
    result = service.run_once(config, path)
    assert result["status"] == "degraded"
    assert result["source_health"]["failed_scopes"] == ["A:hyperliquid", "B:hyperliquid"]


def test_fresh_planning_has_real_ttl_and_no_fake_simulation(setup):
    config, path, states = setup
    states[A] = state(True)
    result = service.run_once(config, path)
    db = Database(path)
    try:
        plan = db.get_task("a", result["action_plans"][0]["task_id"])
        assert plan["local_ttl_unix"] > time.time()
        assert plan["state"] == "DRAFT"
        assert plan["simulation"]["source"] == "not_run"
        assert plan["wallet_request_allowed"] is False
    finally:
        db.close()


def test_stale_block_is_rejected_before_a_snapshot_is_trusted(setup):
    config, path, states = setup
    states[A]["block_timestamp"] = 1
    result = service.run_once(config, path)
    assert result["wallets"][0]["bsc"]["quality"] == "ERROR"
    assert "A:bsc" in result["source_health"]["failed_scopes"]


def test_cli_latest_tenant_and_wallet_scope(setup, capsys):
    config, path, _ = setup
    service.run_once(config, path)
    service.run_once(config, path)
    assert cli.main(["claims", "--db", str(path), "--tenant", "a"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["claims"]) == 2  # Not four historical copies.
    assert cli.main(["actions", "--db", str(path), "--tenant", "a", "list", "--wallet", "A"]) == 0
    assert len(json.loads(capsys.readouterr().out)["claims"]) == 1
    db = Database(path)
    db.upsert_tenant("b", "b")
    db.close()
    assert cli.main(["snapshot", "--db", str(path)]) == 2
    assert "--tenant" in json.loads(capsys.readouterr().out)["error"]


def test_duplicate_label_and_empty_wallets_rejected_before_io(tmp_path):
    path = tmp_path / "bad.json"
    for wallets in ([], [{"label": "A", "family": "evm", "address": A}] * 2):
        path.write_text(json.dumps({"wallets": wallets}))
        with pytest.raises(ConfigError):
            service.load_config(path)


def test_cli_discover_degraded_has_nonzero_exit(setup, capsys):
    config, path, states = setup
    states[A] = RpcError("offline")
    assert cli.main(["wallets", "discover", "--config", str(config), "--db", str(path)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"
