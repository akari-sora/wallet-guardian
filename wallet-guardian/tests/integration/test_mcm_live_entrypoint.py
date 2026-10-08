"""MCM-LIVE-L0. The public CLI drives three offline rounds against a fake backend.

Assertions go through ``guardian.cli.main``. No test calls the rules, the
plugin runner, or the approval scanner to produce the result it checks.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from guardian.chains.evm.accounting import TRANSFER_TOPIC, pad_address
from guardian.chains.evm.permissions import ERC20_APPROVAL_TOPIC
from guardian.cli import main
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET_A = "11111111-1111-4111-8111-111111111111"
WALLET_B = "33333333-3333-4333-8333-333333333333"
ADDRESS_A = to_checksum_address("0x" + ("0" * 39) + "1")
ADDRESS_B = to_checksum_address("0x" + ("0" * 39) + "2")
TOKEN = to_checksum_address("0x" + ("0" * 38) + "aa")
SPENDER = to_checksum_address("0x" + ("0" * 38) + "bb")
SENDER = to_checksum_address("0x" + ("1" * 40))
KEY = "synthetic-etherscan-key"
NATIVE_ASSET = "evm-native:1"
TX_NATIVE = "0x" + ("11" * 32)
TX_TOKEN = "0x" + ("22" * 32)
TX_APPROVAL = "0x" + ("33" * 32)
TX_SHARED = "0x" + ("ab" * 32)
TX_AWAITING = "0x" + ("cd" * 32)
TX_B_ONLY = "0x" + ("ef" * 32)
TX_LEGACY_ONLY = "0x" + ("12" * 32)
TX_ALERTED = "0x" + ("34" * 32)
TX_OUTSIDE = "0x" + ("56" * 32)
WEI = 10**18
UINT256_MAX = 2**256 - 1
AAVE_SELECTOR = "0x" + function_signature_to_4byte_selector("getUserAccountData(address)").hex()
ALLOWANCE_SELECTOR = "0x" + function_signature_to_4byte_selector("allowance(address,address)").hex()
BALANCE_SELECTOR = "0x" + function_signature_to_4byte_selector("balanceOf(address)").hex()
STAMP_EPOCH = 1_700_000_000


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class ChainBackend:
    """JSON-RPC and Etherscan answers for one synthetic ethereum wallet set."""

    def __init__(self) -> None:
        self.phase = 1
        self.head = 100
        self.fail_logs = False
        self.aave = (0, 0, 0, 0, 0, 0)
        self.logs: list[dict] = []
        self.native_rows: list[dict] = []
        self.token_rows: list[dict] = []
        self.rpc: list[dict] = []
        self.http: list[dict] = []

    def show_material(self) -> None:
        self.phase = 2
        self.head = 130
        self.aave = (100, 40, 1, 1, 1, 2 * WEI)
        self.logs = [_transfer_log(TX_TOKEN, 120, 7), _approval_log(TX_APPROVAL, 120, 4)]
        self.native_rows = [_native_row(TX_NATIVE, ADDRESS_A, 120, "1", WEI)]
        self.token_rows = [_token_row(TX_TOKEN, ADDRESS_A, 120, "7")]

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        body = getattr(call, "body", b"") or b""
        host = urlsplit(url).hostname or ""
        if not body:
            return self._etherscan(url, host)
        payload = json.loads(body.decode("utf-8"))
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        record: dict = {"method": method, "host": host}
        if method == "eth_getLogs" and params and isinstance(params[0], dict):
            record["from_block"] = int(str(params[0].get("fromBlock", "0x0")), 16)
            record["to_block"] = int(str(params[0].get("toBlock", "0x0")), 16)
            topic0 = (params[0].get("topics") or [None])[0]
            record["kind"] = "approval" if isinstance(topic0, list) else "transfer"
        if method == "eth_call" and params and isinstance(params[0], dict):
            data = str(params[0].get("data") or "")
            record["selector"] = data[:10]
        self.rpc.append(record)
        result = self._rpc(method, record)
        if result is None:
            encoded = json.dumps({"id": 1, "jsonrpc": "2.0", "error": {"code": -32000, "message": "fixture-log-failure"}}).encode()
        else:
            encoded = json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode()
        return RawResponse(200, {}, encoded)

    def _rpc(self, method: str, record: dict) -> object | None:
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBalance":
            return "0x1"
        if method == "eth_call":
            selector = record.get("selector")
            if selector == AAVE_SELECTOR:
                return "0x" + encode(["uint256"] * 6, list(self.aave)).hex()
            if selector == ALLOWANCE_SELECTOR:
                return "0x" + encode(["uint256"], [UINT256_MAX]).hex()
            if selector == BALANCE_SELECTOR:
                return "0x" + encode(["uint256"], [1]).hex()
            return "0x"
        if method == "eth_getLogs":
            if self.fail_logs:
                return None
            start = int(record.get("from_block") or 0)
            end = int(record.get("to_block") or 0)
            kind = record.get("kind")
            return [
                log for log in self.logs
                if start <= int(str(log["blockNumber"]), 16) <= end and (
                    (kind == "approval" and log["topics"][0] == ERC20_APPROVAL_TOPIC)
                    or (kind == "transfer" and log["topics"][0] == TRANSFER_TOPIC)
                )
            ]
        return []

    def _etherscan(self, url: str, host: str) -> RawResponse:
        query = parse_qs(urlsplit(url).query)
        action = (query.get("action") or [""])[0]
        start = int((query.get("startblock") or ["0"])[0] or "0")
        end = int((query.get("endblock") or ["0"])[0] or "0")
        address = (query.get("address") or [""])[0].lower()
        self.http.append({
            "action": action,
            "end": end,
            "host": host,
            "module": (query.get("module") or [""])[0],
            "start": start,
        })
        source = self.native_rows if action == "txlist" else self.token_rows if action == "tokentx" else []
        rows = [
            row for row in source
            if row.get("to", "").lower() == address and start <= int(row["blockNumber"]) <= end
        ]
        if rows:
            body = {"message": "OK", "result": rows, "status": "1"}
        else:
            body = {"message": "No transactions found", "result": [], "status": "0"}
        return RawResponse(200, {}, json.dumps(body).encode())


def test_mcm_live_l0_01_second_round_material_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-01. The second CLI round emits one native transfer outbox row."""
    desk = _desk(tmp_path, monkeypatch)
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send"]
    first = _cli(capsys, desk, argv)
    baseline_rows = _outbox(desk)
    assert first["baseline"] is True
    assert baseline_rows
    assert {row["payload"]["kind"] for row in baseline_rows} == {"baseline_summary"}
    desk.backend.show_material()
    second = _cli(capsys, desk, argv)
    added = [row for row in _outbox(desk) if row["id"] not in {item["id"] for item in baseline_rows}]
    native = [row for row in added if row["payload"]["kind"] == "native_eth"]
    assert len(native) == 1
    assert native[0]["payload"]["tx_id"] == TX_NATIVE
    assert native[0]["payload"]["network"] == "ethereum-mainnet"
    assert second["outbox_written"] == 3
    assert len(added) == 3
    assert second["status"] == "ok"
    third = _cli(capsys, desk, argv)
    assert third["outbox_written"] == 0
    assert len(_outbox(desk)) == len(baseline_rows) + 3
    assert _cursor(desk, "history.native_external") == "130"


def test_mcm_live_l0_02_post_baseline_new_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-02. A new unlimited approval after the stored cursor alerts once."""
    desk = _desk(tmp_path, monkeypatch)
    _three_rounds(desk, capsys)
    approvals = [row for row in _outbox(desk) if row["payload"]["kind"] == "first_observed"]
    assert len(approvals) == 1
    assert approvals[0]["payload"]["tx_id"] == TX_APPROVAL
    assert "approval" in approvals[0]["payload"]["identity"]
    assert _cursor(desk, "permissions.erc20") == "130"
    starts = [row["from_block"] for row in desk.backend.rpc if row.get("kind") == "approval"]
    assert 99 in starts
    assert 129 in starts


def test_mcm_live_l0_03_restart_cursor_pagination_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-03. A new process resumes from the cursor and does not skip a gap."""
    desk = _desk(tmp_path, monkeypatch)
    _cli(capsys, desk, ["run-once", "--mcm-config", str(desk.config), "--no-send"])
    assert _cursor(desk, "history.fungible") == "100"
    desk.backend.rpc.clear()
    desk.backend.head = 100 + 4500
    report = _cli(capsys, desk, ["run-once", "--mcm-config", str(desk.config), "--no-send"])
    pages = [row for row in desk.backend.rpc if row.get("kind") == "transfer"]
    assert len(pages) >= 2
    assert min(row["from_block"] for row in pages) == 99
    assert max(row["from_block"] for row in pages) > 99
    assert _cursor(desk, "history.fungible") == "100"
    assert _cursor(desk, "permissions.erc20") == "100"
    history = next(row for row in report["coverage"] if row["capability"] == "history.fungible" and row["wallet_id"] == WALLET_A)
    assert "unread_gap" in history["reason_codes"]
    assert report.get("live_pass") is not True


def test_mcm_live_l0_04_configured_indexer_protocol_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-04. run-once calls the configured indexer and the configured plugin."""
    desk = _desk(tmp_path, monkeypatch)
    _three_rounds(desk, capsys)
    assert any(row["action"] == "txlist" and row["host"] == "etherscan.example.invalid" for row in desk.backend.http)
    assert any(row["action"] == "tokentx" for row in desk.backend.http)
    assert any(row.get("selector") == AAVE_SELECTOR for row in desk.backend.rpc)
    assert KEY not in json.dumps(desk.backend.http)
    positions = [row for row in _outbox(desk) if row["payload"]["kind"] == "position_change"]
    assert len(positions) == 1
    assert "aave_v3" in positions[0]["payload"]["identity"]
    coverage = _cli(capsys, desk, ["coverage", "--mcm-config", str(desk.config), "--json"])
    external = next(row for row in coverage["rows"] if row["capability"] == "history.native_external" and row["wallet_id"] == WALLET_A)
    assert external["quality"] != "error"
    assert "credential_missing" not in external["reason_codes"]


def test_mcm_live_l0_05_non_native_health_and_missing_required_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-05. Non-native failure, a vanished required key, and recovery."""
    desk = _desk(tmp_path, monkeypatch, interval=60)
    clock = _Clock(STAMP_EPOCH)
    missing = _watch(capsys, desk, clock)
    assert missing["blind"] is True
    assert missing["emitted"][0]["kind"] == "monitor_blind"
    assert missing["live_verified"] is False
    assert missing.get("live_pass") is not True

    clock.now += 10
    healthy = _cli(capsys, desk, ["run-once", "--mcm-config", str(desk.config), "--no-send"], clock)
    assert healthy["status"] == "ok"
    assert _required(healthy, "history.fungible") in {"partial", "complete_in_declared_scope"}
    assert _required(healthy, "permissions.erc20") in {"partial", "complete_in_declared_scope"}
    assert _required(healthy, "positions:aave_v3") == "complete_in_declared_scope"
    quiet = _watch(capsys, desk, clock)
    assert quiet["emitted"][0]["kind"] == "monitor_recovered"
    assert quiet["blind"] is False
    success_at = _health(desk)["last_success_at"]
    assert success_at

    clock.now += 10
    desk.backend.fail_logs = True
    degraded = _cli(capsys, desk, ["run-once", "--mcm-config", str(desk.config), "--no-send"], clock)
    assert degraded["status"] == "degraded"
    assert _required(degraded, "balances.native") in {"partial", "complete_in_declared_scope"}
    assert _required(degraded, "history.fungible") in {"error", "blocked"}
    assert _required(degraded, "permissions.erc20") in {"error", "blocked"}
    blind = _watch(capsys, desk, clock)
    assert blind["emitted"][0]["kind"] == "monitor_blind"
    again = _watch(capsys, desk, clock)
    assert again["emitted"] == []
    assert again["blind"] is True
    assert _health(desk)["last_success_at"] == success_at

    clock.now += 10
    desk.backend.fail_logs = False
    restored = _cli(capsys, desk, ["run-once", "--mcm-config", str(desk.config), "--no-send"], clock)
    assert restored["status"] == "ok"
    recovered = _watch(capsys, desk, clock)
    assert recovered["emitted"][0]["kind"] == "monitor_recovered"
    assert _health(desk)["last_success_at"] != success_at

    document = json.loads(desk.config.read_text(encoding="utf-8"))
    document["plugins"] = []
    desk.config.write_text(json.dumps(document), encoding="utf-8")
    clock.now += 10
    shrunk = _cli(capsys, desk, ["run-once", "--mcm-config", str(desk.config), "--no-send"], clock)
    assert not any(key.endswith("|positions:aave_v3") for key in shrunk["required"])
    lost = _watch(capsys, desk, clock)
    assert lost["blind"] is True
    assert lost["emitted"][0]["kind"] == "monitor_blind"


def test_mcm_live_l0_06_wallet_event_window_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-06. Wallet, position, and kind stay separate inside one window."""
    desk = _desk(tmp_path, monkeypatch, wallets=2)
    desk.backend.head = 200
    desk.backend.native_rows = [
        _native_row(TX_SHARED, ADDRESS_A, 100, "1", WEI),
        _native_row(TX_AWAITING, ADDRESS_A, 195, "2", WEI),
        _native_row(TX_ALERTED, ADDRESS_A, 100, "7", WEI),
        _native_row(TX_SHARED, ADDRESS_B, 100, "3", WEI),
    ]
    desk.backend.logs = [_approval_log(TX_SHARED, 100, 4, owner=ADDRESS_A)]
    stamp = _iso(STAMP_EPOCH)
    clock = _Clock(STAMP_EPOCH)
    _cli(capsys, desk, ["run-once", "--mcm-config", str(desk.config), "--no-send"], clock)
    legacy = {
        "alerted": [f"eth:desk:{TX_ALERTED}"],
        "events": [
            _legacy_event(WALLET_A, TX_SHARED, "1", "asset_in", NATIVE_ASSET, "confirmed", stamp),
            _legacy_event(WALLET_A, TX_SHARED, "4", "approval_set", TOKEN.lower(), "confirmed", stamp),
            _legacy_event(WALLET_A, TX_AWAITING, "2", "asset_in", NATIVE_ASSET, "observed", stamp),
            _legacy_event(WALLET_A, TX_LEGACY_ONLY, "1", "asset_in", NATIVE_ASSET, "confirmed", stamp),
            _legacy_event(WALLET_A, TX_OUTSIDE, "1", "asset_in", NATIVE_ASSET, "confirmed", "2000-01-01T00:00:00Z"),
            {"kind": "asset_in", "network": "ethereum-mainnet", "observed_at": stamp, "tx": TX_B_ONLY},
        ],
        "operator_note": "synthetic",
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    compared = _cli(capsys, desk, [
        "compare-legacy", "--mcm-config", str(desk.config), "--legacy-state", str(path),
        "--since", stamp, "--until", stamp,
    ])
    counts = compared["counts"]
    assert counts["matched"] >= 1
    assert counts["legacy_only"] == 1
    assert counts["mcm_only"] >= 1
    assert counts["awaiting_finality"] >= 1
    assert counts["uncomparable"] >= 2
    public = compared["matched"] + compared["legacy_only"] + compared["mcm_only"] + compared["awaiting_finality"]
    wallets = {row["wallet_id"] for row in public}
    assert WALLET_A in wallets and WALLET_B in wallets
    grouped: dict[str, set[tuple[str, str, str]]] = {}
    for row in public:
        grouped.setdefault(row["tx"], set()).add((row["wallet_id"], row["kind"], row["position"]))
    assert any(len(group) >= 2 for group in grouped.values())
    kinds = {row["kind"] for row in public}
    assert "asset_in" in kinds and "approval_set" in kinds
    blob = json.dumps(compared)
    for secret in (TX_SHARED, TX_ALERTED, TX_LEGACY_ONLY, ADDRESS_A, ADDRESS_B, TOKEN, SPENDER, KEY):
        assert secret.lower() not in blob.lower()
    assert compared.get("live_pass") is not True


def test_mcm_live_l0_07_unknown_price_no_zero_and_no_double_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-07. A missing price is unknown, and the log plus indexer row is one event."""
    desk = _desk(tmp_path, monkeypatch)
    _first, second, _third = _three_rounds(desk, capsys)
    token_events = [
        row for row in _events(desk)
        if row.get("tx_id") == TX_TOKEN and row.get("kind") == "asset_in"
    ]
    assert len(token_events) == 1
    assert token_events[0]["sub_index"] == "7"
    decisions = [row for row in second["decisions"] if row.get("tx_id") == TX_TOKEN]
    assert len(decisions) == 1
    assert decisions[0]["alert"] is False
    assert decisions[0]["valuation_unknown"] is True
    assert TX_TOKEN in second["valuation_unknown"] or any(TX_TOKEN in item for item in second["valuation_unknown"])
    blob = json.dumps(second)
    assert '"usd": 0' not in blob and '"usd": "0"' not in blob
    assert decisions[0].get("usd", None) in {None, ""}


def test_mcm_live_l0_08_baseline_evidence_no_synthetic_live_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-LIVE-L0-08. Fixture output stays unverified and the first round is baseline."""
    desk = _desk(tmp_path, monkeypatch)
    first, second, third = _three_rounds(desk, capsys)
    assert first["baseline"] is True
    assert second["baseline"] is False
    assert third["baseline"] is False
    for report in (first, second, third):
        assert report["live_verified"] is False
        assert report.get("live_pass") is not True
        assert "live_pass" in report
    coverage = _cli(capsys, desk, ["coverage", "--mcm-config", str(desk.config), "--json"])
    assert coverage["live_verified"] is False
    assert coverage.get("live_pass") is not True
    captured = json.dumps({"coverage": coverage, "first": first, "second": second, "third": third})
    assert '"live_pass": true' not in captured
    assert '"live_verified": true' not in captured


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Desk:
    def __init__(self, config: Path, state: Path, backend: ChainBackend) -> None:
        self.config = config
        self.state = state
        self.backend = backend


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, wallets: int = 1, interval: int = 10800) -> _Desk:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = ["aave-v3"]
    document["polling"]["interval_seconds"] = interval
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    accounts = [_account(WALLET_A, ADDRESS_A, "evm-a")]
    if wallets == 2:
        accounts.append(_account(WALLET_B, ADDRESS_B, "evm-b"))
    secrets = {
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "ETHERSCAN_API_BASE": "https://etherscan.example.invalid/v2",
        "ETHERSCAN_API_KEY": KEY,
        "GOPLUS_API_BASE": "https://goplus.example.invalid/api",
        "HELIUS_PARSED_EVENTS_URL": "https://helius.example.invalid/events",
        "OFAC_SLS_FEED": "https://ofac.example.invalid/feed",
        "RPC_ARBITRUM_ONE_URL": "https://arbitrum.example.invalid/rpc",
        "RPC_AVALANCHE_C_URL": "https://avalanche.example.invalid/rpc",
        "RPC_BASE_MAINNET_URL": "https://base.example.invalid/rpc",
        "RPC_BSC_MAINNET_URL": "https://bsc.example.invalid/rpc",
        "RPC_ETHEREUM_MAINNET_URL": "https://ethereum.example.invalid/rpc",
        "RPC_OPTIMISM_MAINNET_URL": "https://optimism.example.invalid/rpc",
        "RPC_POLYGON_POS_URL": "https://polygon.example.invalid/rpc",
        "RPC_SOLANA_MAINNET_URL": "https://solana.example.invalid/rpc",
    }
    accounts_path = tmp_path / "accounts.json"
    secret_path = tmp_path / "secrets.json"
    state = tmp_path / "state"
    _write_private(accounts_path, json.dumps({"schema_version": "guardian.accounts.v1", "tenant_id": TENANT, "accounts": accounts}) + "\n")
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts_path))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return _Desk(config, state, ChainBackend())


def _account(wallet_id: str, address: str, label: str) -> dict:
    return {
        "address": address,
        "family": "evm",
        "label": label,
        "networks": ["ethereum-mainnet"],
        "own_group": "desk",
        "relationship": "owned_user_declared",
        "wallet_id": wallet_id,
    }


def _three_rounds(desk: _Desk, capsys: pytest.CaptureFixture[str]) -> tuple[dict, dict, dict]:
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send"]
    first = _cli(capsys, desk, argv)
    before = {row["id"] for row in _outbox(desk)}
    desk.backend.show_material()
    second = _cli(capsys, desk, argv)
    del before
    third = _cli(capsys, desk, argv)
    return first, second, third


def _cli(capsys: pytest.CaptureFixture[str], desk: _Desk, argv: list[str], clock: _Clock | None = None) -> dict:
    code = main(argv, backend=desk.backend, clock=None if clock is None else clock)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload.get("status") != "error"
    blob = json.dumps(payload)
    assert KEY not in blob
    assert ADDRESS_A.lower() not in blob.lower()
    assert ADDRESS_B.lower() not in blob.lower()
    return payload


def _watch(capsys: pytest.CaptureFixture[str], desk: _Desk, clock: _Clock) -> dict:
    return _cli(capsys, desk, ["watchdog", "--mcm-config", str(desk.config)], clock)


def _outbox(desk: _Desk) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.outbox_for(TENANT)
    finally:
        database.close()


def _events(desk: _Desk) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.chain_events_for(TENANT)
    finally:
        database.close()


def _cursor(desk: _Desk, capability: str) -> str | None:
    database = Database(desk.state / "monitor.sqlite")
    try:
        rows = database.capability_cursors_for(TENANT, wallet_id=WALLET_A, network="ethereum-mainnet")
    finally:
        database.close()
    found = [row["block_number"] for row in rows if row["capability"] == capability]
    return found[0] if found else None


def _health(desk: _Desk) -> dict:
    database = Database(desk.state / "monitor.sqlite")
    try:
        row = database.conn.execute("SELECT payload FROM monitor_health WHERE tenant_id=?", (TENANT,)).fetchone()
    finally:
        database.close()
    return json.loads(row["payload"])


def _required(report: dict, suffix: str) -> str:
    found = [value for key, value in report["required"].items() if key.endswith("|" + suffix)]
    assert len(found) == 1
    return found[0]


def _transfer_log(tx: str, block: int, index: int, receiver: str = ADDRESS_A) -> dict:
    return {
        "address": TOKEN,
        "blockNumber": hex(block),
        "data": "0x" + WEI.to_bytes(32, "big").hex(),
        "logIndex": hex(index),
        "topics": [TRANSFER_TOPIC, pad_address(SENDER), pad_address(receiver)],
        "transactionHash": tx,
    }


def _approval_log(tx: str, block: int, index: int, owner: str = ADDRESS_A) -> dict:
    return {
        "address": TOKEN,
        "blockNumber": hex(block),
        "data": "0x" + UINT256_MAX.to_bytes(32, "big").hex(),
        "logIndex": hex(index),
        "topics": [ERC20_APPROVAL_TOPIC, pad_address(owner), pad_address(SPENDER)],
        "transactionHash": tx,
    }


def _native_row(tx: str, receiver: str, block: int, position: str, value: int) -> dict:
    return {
        "blockNumber": str(block),
        "from": SENDER,
        "hash": tx,
        "isError": "0",
        "nonce": position,
        "to": receiver,
        "transactionIndex": position,
        "value": str(value),
    }


def _token_row(tx: str, receiver: str, block: int, log_index: str) -> dict:
    return {
        "blockNumber": str(block),
        "contractAddress": TOKEN,
        "from": SENDER,
        "hash": tx,
        "logIndex": log_index,
        "to": receiver,
        "tokenSymbol": "SYN",
        "transactionIndex": "9",
        "value": str(WEI),
    }


def _legacy_event(wallet_id: str, tx: str, position: str, kind: str, subject: str, finality: str, observed: str) -> dict:
    return {
        "asset": subject,
        "finality": finality,
        "kind": kind,
        "network": "ethereum-mainnet",
        "observed_at": observed,
        "position": position,
        "tx": tx,
        "wallet_id": wallet_id,
    }


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
