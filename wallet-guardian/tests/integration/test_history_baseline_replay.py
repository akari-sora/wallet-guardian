"""A blocked history read is not a baseline. Recovery summarizes the lookback window.

The first round has no indexer key, so history.native_external is stored as an
error. The second round reads several old transfers inside that bootstrap
window. They are one baseline summary. A transfer after that baseline alerts
on its own. An unlimited approval discovered on the recovering round still
alerts once.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from guardian.chains.evm.accounting import pad_address
from guardian.chains.evm.permissions import ERC20_APPROVAL_TOPIC
from guardian.cli import main
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
ADDRESS = to_checksum_address("0x" + ("0" * 39) + "1")
TOKEN = to_checksum_address("0x" + ("0" * 38) + "aa")
SPENDER = to_checksum_address("0x" + ("0" * 38) + "bb")
SENDER = to_checksum_address("0x" + ("1" * 40))
KEY = "synthetic-etherscan-key"
WEI = 10**18
UINT256_MAX = 2**256 - 1
ALLOWANCE_SELECTOR = "0x" + function_signature_to_4byte_selector("allowance(address,address)").hex()
OLD_TXS = (
    "0x" + ("11" * 32),
    "0x" + ("22" * 32),
    "0x" + ("33" * 32),
)
TX_APPROVAL = "0x" + ("44" * 32)
TX_NEW = "0x" + ("55" * 32)
HISTORY = "history.native_external"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class ChainBackend:
    """JSON-RPC plus an indexer that is empty until the test releases old rows."""

    def __init__(self) -> None:
        self.head = 100
        self.native_rows: list[dict] = []
        self.logs: list[dict] = []

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        body = getattr(call, "body", b"") or b""
        if not body:
            return self._etherscan(url)
        payload = json.loads(body.decode("utf-8"))
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        record: dict = {"method": method}
        if method == "eth_getLogs" and params and isinstance(params[0], dict):
            record["from_block"] = int(str(params[0].get("fromBlock", "0x0")), 16)
            record["to_block"] = int(str(params[0].get("toBlock", "0x0")), 16)
            topic0 = (params[0].get("topics") or [None])[0]
            record["kind"] = "approval" if isinstance(topic0, list) else "transfer"
        if method == "eth_call" and params and isinstance(params[0], dict):
            record["selector"] = str(params[0].get("data") or "")[:10]
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
            if record.get("selector") == ALLOWANCE_SELECTOR:
                return "0x" + encode(["uint256"], [UINT256_MAX]).hex()
            return "0x"
        if method == "eth_getLogs":
            start = int(record.get("from_block") or 0)
            end = int(record.get("to_block") or 0)
            kind = record.get("kind")
            return [
                log for log in self.logs
                if start <= int(str(log["blockNumber"]), 16) <= end and (
                    (kind == "approval" and log["topics"][0] == ERC20_APPROVAL_TOPIC)
                    or (kind == "transfer" and log["topics"][0] != ERC20_APPROVAL_TOPIC)
                )
            ]
        return []

    def _etherscan(self, url: str) -> RawResponse:
        query = parse_qs(urlsplit(url).query)
        action = (query.get("action") or [""])[0]
        start = int((query.get("startblock") or ["0"])[0] or "0")
        end = int((query.get("endblock") or ["0"])[0] or "0")
        address = (query.get("address") or [""])[0].lower()
        source = self.native_rows if action == "txlist" else []
        rows = [
            row for row in source
            if str(row.get("to", "")).lower() == address and start <= int(row["blockNumber"]) <= end
        ]
        if rows:
            body = {"message": "OK", "result": rows, "status": "1"}
        else:
            body = {"message": "No transactions found", "result": [], "status": "0"}
        return RawResponse(200, {}, json.dumps(body).encode())


def test_blocked_history_recovers_as_a_baseline_not_a_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    desk = _desk(tmp_path, monkeypatch)
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send"]
    first = _cli(capsys, desk, argv)
    history = _coverage(first, HISTORY)
    assert history["quality"] == "error"
    assert history["quality"] != "complete_in_declared_scope"
    assert "blocked" in history["reason_codes"]
    assert "credential_missing" in history["reason_codes"]
    stored = _state(desk, HISTORY)
    assert stored["baselined"] is False
    assert stored["last_success_at"] in {None, ""}
    assert _cursor(desk, HISTORY) is None
    assert not any(row["payload"].get("identity") == f"baseline:{HISTORY}" for row in _outbox(desk))
    assert not any(row["payload"].get("kind") == "native_eth" for row in _outbox(desk))

    _write_secrets(desk.secrets, _secret_values(include_key=True))
    desk.backend.head = 180
    desk.backend.native_rows = [
        _native_row(OLD_TXS[0], 20, "1"),
        _native_row(OLD_TXS[1], 40, "2"),
        _native_row(OLD_TXS[2], 60, "3"),
    ]
    desk.backend.logs = [_approval_log(TX_APPROVAL, 150, 4)]
    before = {row["id"] for row in _outbox(desk)}
    second = _cli(capsys, desk, argv)
    added = [row for row in _outbox(desk) if row["id"] not in before]
    summaries = [
        row for row in added
        if row["payload"].get("kind") == "baseline_summary" and row["payload"].get("identity") == f"baseline:{HISTORY}"
    ]
    assert len(summaries) == 1
    assert summaries[0]["payload"]["counts"]["events"] == 3
    replayed = [
        row for row in added
        if row["payload"].get("tx_id") in OLD_TXS and row["payload"].get("kind") != "baseline_summary"
    ]
    assert replayed == []
    assert not any(row["payload"].get("kind") == "native_eth" for row in added)
    approvals = [row for row in added if row["payload"].get("tx_id") == TX_APPROVAL]
    assert len(approvals) == 1
    assert approvals[0]["payload"]["kind"] in {"first_observed", "permission_new"}
    recovered = _state(desk, HISTORY)
    assert recovered["baselined"] is True
    assert recovered["quality"] != "error"
    assert _cursor(desk, HISTORY) == "180"
    assert second.get("live_pass") is not True

    desk.backend.head = 220
    desk.backend.native_rows.append(_native_row(TX_NEW, 210, "9"))
    before = {row["id"] for row in _outbox(desk)}
    _cli(capsys, desk, argv)
    fresh = [row for row in _outbox(desk) if row["id"] not in before]
    native = [row for row in fresh if row["payload"].get("kind") == "native_eth"]
    assert [row["payload"].get("tx_id") for row in native] == [TX_NEW]
    assert not any(row["payload"].get("tx_id") in OLD_TXS for row in fresh)
    assert not any(row["payload"].get("identity") == f"baseline:{HISTORY}" for row in fresh)
    assert _state(desk, HISTORY)["baselined"] is True


def _coverage(report: dict, capability: str) -> dict:
    found = [row for row in report["coverage"] if row["capability"] == capability and row["wallet_id"] == WALLET]
    assert len(found) == 1
    return found[0]


def _cli(capsys: pytest.CaptureFixture[str], desk: "_Desk", argv: list[str]) -> dict:
    code = main(argv, backend=desk.backend)
    payload = json.loads(capsys.readouterr().out)
    assert code in {0, 3}, payload
    assert payload.get("status") != "error"
    blob = json.dumps(payload)
    assert KEY not in blob
    assert ADDRESS.lower() not in blob.lower()
    return payload


class _Desk:
    def __init__(self, config: Path, state: Path, secrets: Path, backend: ChainBackend) -> None:
        self.config = config
        self.state = state
        self.secrets = secrets
        self.backend = backend


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Desk:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = []
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    accounts = {
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
        "accounts": [{
            "address": ADDRESS,
            "family": "evm",
            "label": "evm-a",
            "networks": ["ethereum-mainnet"],
            "own_group": "desk",
            "relationship": "owned_user_declared",
            "wallet_id": WALLET,
        }],
    }
    accounts_path = tmp_path / "accounts.json"
    secrets = tmp_path / "secrets.json"
    state = tmp_path / "state"
    _write_private(accounts_path, json.dumps(accounts) + "\n")
    _write_secrets(secrets, _secret_values(include_key=False))
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts_path))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secrets))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return _Desk(config, state, secrets, ChainBackend())


def _secret_values(*, include_key: bool) -> dict[str, str]:
    values = {
        "ETHERSCAN_API_BASE": "https://etherscan.example.invalid/v2",
        "RPC_ARBITRUM_ONE_URL": "https://arbitrum.example.invalid/rpc",
        "RPC_AVALANCHE_C_URL": "https://avalanche.example.invalid/rpc",
        "RPC_BASE_MAINNET_URL": "https://base.example.invalid/rpc",
        "RPC_BSC_MAINNET_URL": "https://bsc.example.invalid/rpc",
        "RPC_ETHEREUM_MAINNET_URL": "https://ethereum.example.invalid/rpc",
        "RPC_OPTIMISM_MAINNET_URL": "https://optimism.example.invalid/rpc",
        "RPC_POLYGON_POS_URL": "https://polygon.example.invalid/rpc",
        "RPC_SOLANA_MAINNET_URL": "https://solana.example.invalid/rpc",
    }
    if include_key:
        values["ETHERSCAN_API_KEY"] = KEY
    return values


def _write_secrets(path: Path, values: dict[str, str]) -> None:
    _write_private(path, json.dumps(values) + "\n")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _native_row(tx: str, block: int, position: str) -> dict:
    return {
        "blockNumber": str(block),
        "from": SENDER,
        "hash": tx,
        "isError": "0",
        "nonce": position,
        "to": ADDRESS,
        "transactionIndex": position,
        "value": str(WEI),
    }


def _approval_log(tx: str, block: int, index: int) -> dict:
    return {
        "address": TOKEN,
        "blockNumber": hex(block),
        "data": "0x" + UINT256_MAX.to_bytes(32, "big").hex(),
        "logIndex": hex(index),
        "topics": [ERC20_APPROVAL_TOPIC, pad_address(ADDRESS), pad_address(SPENDER)],
        "transactionHash": tx,
    }


def _outbox(desk: _Desk) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.outbox_for(TENANT)
    finally:
        database.close()


def _state(desk: _Desk, capability: str) -> dict:
    database = Database(desk.state / "monitor.sqlite")
    try:
        found = [
            row for row in database.capability_state_for(TENANT, wallet_id=WALLET, network="ethereum-mainnet")
            if row["capability"] == capability
        ]
    finally:
        database.close()
    assert len(found) == 1
    return found[0]


def _cursor(desk: _Desk, capability: str) -> str | None:
    database = Database(desk.state / "monitor.sqlite")
    try:
        rows = database.capability_cursors_for(TENANT, wallet_id=WALLET, network="ethereum-mainnet")
    finally:
        database.close()
    found = [row["block_number"] for row in rows if row["capability"] == capability]
    return found[0] if found else None
