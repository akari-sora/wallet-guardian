"""BAL-01 through BAL-06. Real ``guardian run-once`` entrypoint, fixture transport.

Synthetic addresses only. Amounts stay in integer wei. No live provider is called.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from eth_utils import to_checksum_address

from guardian.balances.labels import lookup
from guardian.cli import main
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
ADDRESS = to_checksum_address("0x" + ("0" * 39) + "1")
LISTA = "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6"
OTHER = to_checksum_address("0x" + ("0" * 39) + "2")
TOKEN = to_checksum_address("0x" + ("0" * 38) + "aa")
ASSET = "evm-native:56"
ONE = 10**18
NINE = 9421285000000000000
DELTA = NINE - ONE
CLOCK = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class Sink:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: object) -> None:
        del message
        self.calls += 1


class ScriptBackend:
    """One scripted chain. A balance error is not turned into a zero balance."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.block_number = 10
        self.block_hash = _hash(10)
        self.balance = ONE
        self.nonce = 7
        self.chain_id = 0x38
        self.head = 10
        self.fail_balance = False
        self.after_mismatch = False
        self.reject_eip1898 = False
        self.nodereal = "empty"
        self.by_category: dict[str, list[dict]] = {"internal": [], "external": [], "20": []}
        self.price: dict | None = None
        self.calls: list[dict] = []
        self.barrier: threading.Barrier | None = None
        self._owners: dict[int, int] = {}
        self._split = False

    def at(self, block: int, balance: int, *, nonce: int = 7) -> None:
        self.block_number = block
        self.block_hash = _hash(block)
        self.balance = balance
        self.head = block
        self.nonce = nonce

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        body = getattr(call, "body", b"") or b""
        if not body:
            return self._rest(url)
        payload = json.loads(body.decode("utf-8"))
        method = str(payload.get("method") or "")
        if method == "eth_getBalance" and self.barrier is not None:
            self.barrier.wait(10)
        with self.lock:
            return self._rpc(method, payload.get("params") if isinstance(payload.get("params"), list) else [])

    def _rest(self, url: str) -> RawResponse:
        if "simple/price" in url and isinstance(self.price, dict):
            return RawResponse(200, {}, json.dumps(self.price).encode())
        return RawResponse(200, {}, b"{}")

    def _rpc(self, method: str, params: list) -> RawResponse:
        self.calls.append({"method": method, "params": params})
        if method == "eth_chainId":
            return _ok(hex(self.chain_id))
        if method == "eth_blockNumber":
            return _ok(hex(self.head))
        if method == "eth_getBlockByNumber":
            return _ok(self._block(params))
        if method == "eth_getBalance":
            return self._balance(params)
        if method == "eth_getTransactionCount":
            return _ok(hex(self.nonce))
        if method == "eth_getLogs":
            return _ok([])
        if method == "nr_getAssetTransfers":
            return self._nodereal(params)
        return _ok([])

    def _block(self, params: list) -> dict | None:
        number = self._assigned_block()
        tag = params[0] if params else None
        if isinstance(tag, str) and tag not in {"finalized", "latest", "safe"}:
            try:
                asked = int(tag, 16)
            except ValueError:
                return None
            if asked != number:
                return {"hash": _hash(asked), "number": hex(asked), "timestamp": hex(1_700_000_000)}
        if self.barrier is not None:
            digest = _hash(number)
        else:
            digest = self.block_hash
        if self.after_mismatch and isinstance(tag, str) and tag.startswith("0x"):
            digest = _hash(number + 1)
        return {"hash": digest, "number": hex(number), "timestamp": hex(1_700_000_000)}

    def _assigned_block(self) -> int:
        if self.barrier is None:
            return self.block_number
        ident = threading.get_ident()
        if ident not in self._owners:
            self._owners[ident] = 10 if not self._owners else 30
        return self._owners[ident]

    def _balance(self, params: list) -> RawResponse:
        if self.fail_balance:
            return _err("read failed")
        spec = params[1] if len(params) > 1 else None
        if isinstance(spec, dict) and self.reject_eip1898:
            return _err("invalid params")
        number = self._assigned_block()
        amount = ONE if self.barrier is not None and number == 10 else NINE if self.barrier is not None else self.balance
        return _ok(hex(amount))

    def _nodereal(self, params: list) -> RawResponse:
        if self.nodereal == "missing_method":
            return _err("method not found", -32601)
        if self.nodereal == "rate_limited":
            return _err("rate limit")
        item = params[0] if params and isinstance(params[0], dict) else {}
        category = (item.get("category") or ["external"])[0]
        page = item.get("pageKey")
        rows = list(self.by_category.get(str(category), []))
        if self._split and page is None and len(rows) > 1:
            result = {"pageKey": "next", "transfers": rows[:1]}
        elif page == "next":
            result = {"transfers": rows[1:]}
        else:
            result = {"transfers": rows}
        return _ok(result)


def _ok(result: object) -> RawResponse:
    return RawResponse(200, {}, json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode())


def _err(message: str, code: int = -32000) -> RawResponse:
    body = {"error": {"code": code, "message": message}, "id": 1, "jsonrpc": "2.0"}
    return RawResponse(200, {}, json.dumps(body).encode())


def _hash(number: int) -> str:
    return "0x" + f"{number:064x}"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _desk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    network: str = "bsc-mainnet",
    threshold: str | None = "1.0",
    prices: bool = False,
    nodereal_key: bool = False,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = []
    document["enabled_networks"] = [network]
    document["routing"] = [route for route in document["routing"] if route.get("network") == network]
    document["polling"]["bootstrap_origin"] = 1
    if threshold is None:
        document["alerts"].pop("native_unit_thresholds", None)
    else:
        document["alerts"]["native_unit_thresholds"] = {network: threshold}
    if prices:
        document["sources"]["prices"]["enabled"] = True
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    secrets = {
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "COINGECKO_API_KEY": "synthetic-price-key",
        "GOPLUS_API_BASE": "https://goplus.example.invalid/api",
        "NODEREAL_BSC_URL": "https://nodereal.example.invalid/bsc",
        "OFAC_SLS_FEED": "https://ofac.example.invalid/feed",
        "RPC_BSC_MAINNET_URL": "https://bsc.example.invalid/rpc",
        "RPC_ETHEREUM_MAINNET_URL": "https://ethereum.example.invalid/rpc",
    }
    if nodereal_key:
        secrets["NODEREAL_API_KEY"] = "synthetic-nodereal-key"
    accounts = tmp_path / "accounts.json"
    secret_path = tmp_path / "secrets.json"
    _write_private(accounts, json.dumps({
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
        "accounts": [{
            "address": ADDRESS,
            "family": "evm",
            "label": "Wallet A",
            "networks": [network],
            "own_group": "desk",
            "relationship": "owned_user_declared",
            "wallet_id": WALLET,
        }],
    }) + "\n")
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("NODEREAL_API_KEY", raising=False)
    return config


def _run(
    config: Path,
    backend: ScriptBackend,
    capsys: pytest.CaptureFixture[str],
    *,
    no_send: bool = True,
    sender: Sink | None = None,
    expect_ok: bool = True,
) -> dict:
    argv = ["run-once", "--mcm-config", str(config)]
    if no_send:
        argv.append("--no-send")
    code = main(argv, backend=backend, sender=sender, clock=lambda: CLOCK)
    payload = json.loads(capsys.readouterr().out)
    assert code in ({0} if expect_ok else {0, 1}), payload
    blob = json.dumps(payload)
    assert ADDRESS.lower() not in blob.lower()
    return payload


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state" / "monitor.sqlite")


def _baseline(tmp_path: Path, network: str = "bsc-mainnet", asset: str = ASSET) -> dict | None:
    database = _db(tmp_path)
    try:
        return database.native_balance_baseline(TENANT, WALLET, network, asset)
    finally:
        database.close()


def _deltas(tmp_path: Path) -> list[dict]:
    database = _db(tmp_path)
    try:
        rows = database.outbox_for(TENANT)
    finally:
        database.close()
    return [row for row in rows if row["payload"].get("kind") == "balance_delta"]


def _kinds(tmp_path: Path) -> list[str]:
    database = _db(tmp_path)
    try:
        rows = database.outbox_for(TENANT)
    finally:
        database.close()
    return [row["payload"].get("kind") for row in rows]


def _events(tmp_path: Path) -> list[dict]:
    database = _db(tmp_path)
    try:
        return database.chain_events_for(TENANT)
    finally:
        database.close()


def _internal(tx: str, amount: int, block: int, trace: str, *, failed: bool = False, token: bool = False, sender: str = LISTA) -> dict:
    row = {
        "blockHash": _hash(block),
        "blockNumber": str(block),
        "chainId": 56,
        "from": sender,
        "hash": tx,
        "status": "1",
        "to": ADDRESS,
        "traceId": trace,
        "value": str(amount),
    }
    if failed:
        row["isError"] = "1"
    if token:
        row["contractAddress"] = TOKEN
    return row


def test_bal_01_increase_without_nonce_or_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """BAL-01. 1.0 to 9.421285 BNB, nonce unchanged, no history key: one increase."""
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(10, ONE, nonce=7)
    first = _run(config, backend, capsys)
    assert _deltas(tmp_path) == []
    assert first["baseline"] is True
    stored = _baseline(tmp_path)
    assert stored is not None
    assert int(stored["raw_balance"]) == ONE
    assert stored["nonce"] == hex(7)
    backend.at(20, NINE, nonce=7)
    second = _run(config, backend, capsys)
    rows = _deltas(tmp_path)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["alert"] is True
    assert payload["kind"] == "balance_delta"
    assert payload["direction"] == "increase"
    assert payload["cause"] == "unknown"
    assert payload["signed_delta_raw"] == str(DELTA)
    assert payload["threshold_decision"] == "above"
    assert payload["threshold"]["basis"] == "native_unit"
    assert payload["threshold"]["native_unit_threshold"] == "1.0"
    assert payload["binding"] == "eip1898_hash"
    assert payload["confirmation_policy"] == "bsc_bep126_fast_finality"
    assert payload["nonce"]["changed"] is False
    assert payload["attribution"] == "unavailable"
    assert "Lista" not in json.dumps(payload)
    assert "8.421285 BNB" in payload["detail"]
    assert "Transaction attribution is unavailable." in payload["detail"]
    assert "tx_id" not in payload
    decisions = [item for item in second["decisions"] if item.get("kind") == "balance_delta"]
    assert len(decisions) == 1 and decisions[0]["alert"] is True
    assert ADDRESS.lower() not in json.dumps(second).lower()


def test_bal_02_thresholds_boundaries_and_prices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """BAL-02. Decrease, exact floor, large integers, stale price, and no universal 0.5."""
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(10, NINE)
    _run(config, backend, capsys)
    backend.at(20, ONE)
    _run(config, backend, capsys)
    decreased = _deltas(tmp_path)
    assert len(decreased) == 1
    assert decreased[0]["payload"]["direction"] == "decrease"
    assert decreased[0]["payload"]["signed_delta_raw"] == str(ONE - NINE)
    assert "8.421285 BNB" in decreased[0]["payload"]["detail"]

    exact = tmp_path / "exact"
    exact_config = _desk(exact, monkeypatch)
    exact_backend = ScriptBackend()
    exact_backend.at(10, 2 * ONE)
    _run(exact_config, exact_backend, capsys)
    exact_backend.at(11, ONE)
    _run(exact_config, exact_backend, capsys)
    assert len(_deltas(exact)) == 1

    small = tmp_path / "small"
    small_config = _desk(small, monkeypatch)
    small_backend = ScriptBackend()
    small_backend.at(10, 15 * ONE // 10)
    _run(small_config, small_backend, capsys)
    small_backend.at(11, ONE)
    _run(small_config, small_backend, capsys)
    assert _deltas(small) == []
    assert int(_baseline(small)["raw_balance"]) == ONE
    small_backend.at(12, 14 * ONE // 10)
    _run(small_config, small_backend, capsys)
    assert _deltas(small) == []

    huge = tmp_path / "huge"
    huge_config = _desk(huge, monkeypatch)
    huge_backend = ScriptBackend()
    huge_backend.at(10, 2**200)
    _run(huge_config, huge_backend, capsys)
    huge_backend.at(11, 2**200 + ONE)
    _run(huge_config, huge_backend, capsys)
    huge_row = _deltas(huge)[0]["payload"]
    assert huge_row["signed_delta_raw"] == str(ONE)
    assert int(_baseline(huge)["raw_balance"]) == 2**200 + ONE

    bare = tmp_path / "bare"
    bare_config = _desk(bare, monkeypatch, threshold=None)
    bare_backend = ScriptBackend()
    bare_backend.at(10, ONE)
    _run(bare_config, bare_backend, capsys)
    bare_backend.at(11, ONE + (6 * ONE // 10))
    bare_report = _run(bare_config, bare_backend, capsys)
    assert _deltas(bare) == []
    unavailable = [item for item in bare_report["decisions"] if item.get("kind") == "balance_delta"]
    assert len(unavailable) == 1
    assert unavailable[0]["threshold_decision"] == "threshold_unavailable"
    assert unavailable[0]["alert"] is False

    eth = tmp_path / "eth"
    eth_config = _desk(eth, monkeypatch, network="ethereum-mainnet", threshold=None)
    eth_backend = ScriptBackend()
    eth_backend.chain_id = 0x1
    eth_backend.at(10, 0)
    _run(eth_config, eth_backend, capsys)
    eth_backend.at(11, 5 * ONE // 10)
    _run(eth_config, eth_backend, capsys)
    eth_rows = _deltas(eth)
    assert len(eth_rows) == 1
    assert eth_rows[0]["payload"]["threshold"]["basis"] == "native_eth"
    eth_low = tmp_path / "eth-low"
    eth_low_config = _desk(eth_low, monkeypatch, network="ethereum-mainnet", threshold=None)
    eth_low_backend = ScriptBackend()
    eth_low_backend.chain_id = 0x1
    eth_low_backend.at(10, 0)
    _run(eth_low_config, eth_low_backend, capsys)
    eth_low_backend.at(11, 4 * ONE // 10)
    _run(eth_low_config, eth_low_backend, capsys)
    assert _deltas(eth_low) == []

    priced = tmp_path / "priced"
    priced_config = _desk(priced, monkeypatch, threshold="1.0", prices=True)
    priced_backend = ScriptBackend()
    priced_backend.price = {"binancecoin": {"usd": 3000, "last_updated_at": int(CLOCK)}}
    priced_backend.at(10, ONE)
    _run(priced_config, priced_backend, capsys)
    priced_backend.at(11, ONE + (4 * ONE // 10))
    priced_report = _run(priced_config, priced_backend, capsys)
    priced_rows = _deltas(priced)
    assert len(priced_rows) == 1
    assert priced_rows[0]["payload"]["threshold"]["basis"] == "usd"
    assert priced_rows[0]["payload"]["alert"] is True
    stale = tmp_path / "stale"
    stale_config = _desk(stale, monkeypatch, threshold="1.0", prices=True)
    stale_backend = ScriptBackend()
    stale_backend.price = {"binancecoin": {"usd": 3000, "last_updated_at": int(CLOCK) - 1000}}
    stale_backend.at(10, ONE)
    _run(stale_config, stale_backend, capsys)
    stale_backend.at(11, ONE + (4 * ONE // 10))
    _run(stale_config, stale_backend, capsys)
    assert _deltas(stale) == []
    missing = tmp_path / "missing-price"
    missing_config = _desk(missing, monkeypatch, threshold="1.0", prices=True)
    missing_backend = ScriptBackend()
    missing_backend.price = {}
    missing_backend.at(10, ONE)
    _run(missing_config, missing_backend, capsys)
    missing_backend.at(11, NINE)
    _run(missing_config, missing_backend, capsys)
    assert _deltas(missing)[0]["payload"]["threshold"]["basis"] == "native_unit"
    assert _deltas(missing)[0]["payload"]["threshold"]["usd_value"] is None
    assert Decimal("8.421285") * Decimal(ONE) == Decimal(DELTA)


def test_bal_03_failed_read_reorg_and_wrong_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """BAL-03. A bad read cannot invent a movement or replace the baseline with zero."""
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(100, ONE)
    _run(config, backend, capsys)
    backend.fail_balance = True
    backend.at(110, 0)
    _run(config, backend, capsys, expect_ok=False)
    kept = _baseline(tmp_path)
    assert int(kept["raw_balance"]) == ONE
    assert kept["block_number"] == 100
    assert kept["last_failed_at"]
    assert _deltas(tmp_path) == []
    backend.fail_balance = False
    backend.at(120, NINE)
    _run(config, backend, capsys)
    recovered = _deltas(tmp_path)[0]["payload"]
    assert recovered["continuity"] == "gap_disclosed"
    assert recovered["gap_disclosure"] == "longer_interval"
    assert "longer than one quiet round" in recovered["detail"]

    mismatch = tmp_path / "mismatch"
    mismatch_config = _desk(mismatch, monkeypatch)
    mismatch_backend = ScriptBackend()
    mismatch_backend.at(30, ONE)
    _run(mismatch_config, mismatch_backend, capsys)
    mismatch_backend.after_mismatch = True
    mismatch_backend.at(40, NINE)
    _run(mismatch_config, mismatch_backend, capsys, expect_ok=False)
    assert int(_baseline(mismatch)["raw_balance"]) == ONE
    assert _deltas(mismatch) == []

    same = tmp_path / "same"
    same_config = _desk(same, monkeypatch)
    same_backend = ScriptBackend()
    same_backend.at(20, ONE)
    _run(same_config, same_backend, capsys)
    same_backend.block_hash = _hash(99)
    same_backend.balance = NINE
    same_backend.head = 20
    same_backend.block_number = 20
    _run(same_config, same_backend, capsys)
    assert _baseline(same)["block_hash"] == _hash(20)
    assert int(_baseline(same)["raw_balance"]) == ONE
    assert _deltas(same) == []

    back = tmp_path / "back"
    back_config = _desk(back, monkeypatch)
    back_backend = ScriptBackend()
    back_backend.at(100, ONE)
    _run(back_config, back_backend, capsys)
    back_backend.block_number = 90
    back_backend.block_hash = _hash(90)
    back_backend.balance = NINE
    back_backend.head = 100
    _run(back_config, back_backend, capsys)
    assert _baseline(back)["block_number"] == 100
    assert _deltas(back) == []

    deep = tmp_path / "deep"
    deep_config = _desk(deep, monkeypatch)
    deep_backend = ScriptBackend()
    deep_backend.at(100, ONE)
    _run(deep_config, deep_backend, capsys)
    deep_backend.block_number = 70
    deep_backend.block_hash = _hash(70)
    deep_backend.balance = NINE
    deep_backend.head = 100
    _run(deep_config, deep_backend, capsys)
    assert _baseline(deep)["block_number"] == 70
    assert int(_baseline(deep)["raw_balance"]) == NINE
    assert _deltas(deep) == []
    database = _db(deep)
    try:
        old = database.conn.execute(
            "SELECT block_number FROM native_balance_checkpoints WHERE block_number=100"
        ).fetchall()
    finally:
        database.close()
    assert old == []

    inconsistent = tmp_path / "inconsistent"
    inconsistent_config = _desk(inconsistent, monkeypatch)
    inconsistent_backend = ScriptBackend()
    inconsistent_backend.at(20, ONE)
    _run(inconsistent_config, inconsistent_backend, capsys)
    inconsistent_backend.balance = NINE
    _run(inconsistent_config, inconsistent_backend, capsys)
    assert int(_baseline(inconsistent)["raw_balance"]) == ONE
    assert _deltas(inconsistent) == []

    wrong = tmp_path / "wrong"
    wrong_config = _desk(wrong, monkeypatch)
    wrong_backend = ScriptBackend()
    wrong_backend.chain_id = 0x1
    wrong_backend.at(10, NINE)
    _run(wrong_config, wrong_backend, capsys, expect_ok=False)
    assert _baseline(wrong) is None
    assert _deltas(wrong) == []

    fixed = tmp_path / "fixed"
    fixed_config = _desk(fixed, monkeypatch)
    fixed_backend = ScriptBackend()
    fixed_backend.reject_eip1898 = True
    fixed_backend.at(10, ONE)
    _run(fixed_config, fixed_backend, capsys)
    fixed_backend.at(11, NINE)
    _run(fixed_config, fixed_backend, capsys)
    assert _deltas(fixed)[0]["payload"]["binding"] == "fixed_height"


def test_bal_04_reconcile_once_and_keep_opposing_transfers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """BAL-04. One reconciled alert, late enrichment, token logs, and a zero net."""
    config = _desk(tmp_path, monkeypatch, nodereal_key=True)
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys)
    tx = "0x" + "11" * 32
    backend.by_category["internal"] = [_internal(tx, DELTA, 15, "0")]
    backend.at(20, NINE)
    _run(config, backend, capsys)
    rows = _deltas(tmp_path)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["cause"] == "unknown"
    assert payload["tx_id"] == tx
    assert payload["attribution"] == "reconciled"
    assert payload["explained_by"]
    label = payload["counterparty_label"]
    assert label["label"] == "Lista StakeManager"
    assert label["label_source"] == "static_local_registry"
    assert label["counterparty_identity"] == "unverified"
    assert label["safety_assessment"] == "not_established"
    assert payload["alert"] is True
    assert [row["payload"].get("kind") for row in _outbox(tmp_path)].count("balance_delta") == 1
    assert any(item["tx_id"] == tx for item in _events(tmp_path))
    asset_alerts = [kind for kind in _kinds(tmp_path) if kind in {"asset_usd", "native_eth"}]
    assert asset_alerts == []

    late = tmp_path / "late"
    late_config = _desk(late, monkeypatch, nodereal_key=True)
    late_backend = ScriptBackend()
    late_backend.at(10, ONE)
    _run(late_config, late_backend, capsys)
    late_backend.at(20, NINE)
    _run(late_config, late_backend, capsys)
    assert "Lista" not in json.dumps(_deltas(late)[0]["payload"])
    late_backend.by_category["internal"] = [_internal(tx, DELTA, 20, "0")]
    late_backend.at(22, NINE)
    _run(late_config, late_backend, capsys)
    enriched = _deltas(late)
    assert len(enriched) == 1
    assert enriched[0]["payload"]["tx_id"] == tx
    assert enriched[0]["payload"]["cause"] == "unknown"

    token = tmp_path / "token"
    token_config = _desk(token, monkeypatch, nodereal_key=True)
    token_backend = ScriptBackend()
    token_backend.at(10, ONE)
    _run(token_config, token_backend, capsys)
    token_backend.by_category["20"] = [_internal("0x" + "22" * 32, DELTA, 15, "0", token=True)]
    token_backend.at(20, NINE)
    _run(token_config, token_backend, capsys)
    token_payload = _deltas(token)[0]["payload"]
    assert token_payload["attribution"] == "unavailable"
    assert token_payload.get("explained_by") in (None, [])
    assert "Lista" not in json.dumps(token_payload)

    oppose = tmp_path / "oppose"
    oppose_config = _desk(oppose, monkeypatch, nodereal_key=True, prices=True)
    oppose_backend = ScriptBackend()
    oppose_backend.price = {"binancecoin": {"usd": 300, "last_updated_at": int(CLOCK)}}
    oppose_backend.at(10, 10 * ONE)
    _run(oppose_config, oppose_backend, capsys)
    incoming = _internal("0x" + "33" * 32, 5 * ONE, 15, "0")
    outgoing = _internal("0x" + "44" * 32, 5 * ONE, 16, "0", sender=ADDRESS)
    outgoing["to"] = OTHER
    oppose_backend.by_category["external"] = [incoming, outgoing]
    oppose_backend.at(20, 10 * ONE)
    _run(oppose_config, oppose_backend, capsys)
    assert _deltas(oppose) == []
    paid = [kind for kind in _kinds(oppose) if kind == "asset_usd"]
    assert len(paid) == 2
    assert lookup("bsc-mainnet", LISTA[:20]) is None
    assert lookup("ethereum-mainnet", LISTA) is None


def test_bal_05_internal_history_and_balance_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """BAL-05. Pagination, restart, failed children, identities, and a missing plan."""
    config = _desk(tmp_path, monkeypatch, nodereal_key=True)
    backend = ScriptBackend()
    backend._split = True
    backend.at(40, ONE)
    first_tx = "0x" + "55" * 32
    second_tx = "0x" + "66" * 32
    backend.by_category["internal"] = [
        _internal(first_tx, ONE, 12, "0"),
        _internal(first_tx, ONE, 12, "0"),
        _internal(first_tx, ONE, 13, "1"),
        _internal(second_tx, ONE, 14, "0", failed=True),
    ]
    _run(config, backend, capsys)
    txs = [item["tx_id"] for item in _events(tmp_path)]
    assert txs.count(first_tx) == 2
    assert second_tx not in txs
    internal_calls = [call for call in backend.calls if call["method"] == "nr_getAssetTransfers" and (call["params"][0].get("category") or [""])[0] == "internal"]
    assert any(call["params"][0].get("pageKey") == "next" for call in internal_calls)
    backend.calls.clear()
    backend._split = False
    backend.by_category["internal"] = []
    backend.at(48, ONE)
    _run(config, backend, capsys)
    restarted = [
        int(str(call["params"][0]["fromBlock"]), 16)
        for call in backend.calls
        if call["method"] == "nr_getAssetTransfers" and (call["params"][0].get("category") or [""])[0] == "internal"
    ]
    assert restarted
    assert min(restarted) > 10

    missing = tmp_path / "missing"
    missing_config = _desk(missing, monkeypatch, nodereal_key=False)
    missing_backend = ScriptBackend()
    missing_backend.at(10, ONE)
    _run(missing_config, missing_backend, capsys)
    missing_backend.at(20, NINE)
    report = _run(missing_config, missing_backend, capsys)
    assert len(_deltas(missing)) == 1
    assert not any(call["method"] == "nr_getAssetTransfers" for call in missing_backend.calls)
    internal = _coverage(report, "history.native_internal")
    assert "credential_missing" in internal["reason_codes"]

    refused = tmp_path / "refused"
    refused_config = _desk(refused, monkeypatch, nodereal_key=True)
    refused_backend = ScriptBackend()
    refused_backend.nodereal = "missing_method"
    refused_backend.at(10, ONE)
    _run(refused_config, refused_backend, capsys)
    refused_backend.at(20, NINE)
    refused_report = _run(refused_config, refused_backend, capsys)
    assert len(_deltas(refused)) == 1
    assert "plan_not_supported" in _coverage(refused_report, "history.native_internal")["reason_codes"]

    rated = tmp_path / "rated"
    rated_config = _desk(rated, monkeypatch, nodereal_key=True)
    rated_backend = ScriptBackend()
    rated_backend.nodereal = "rate_limited"
    rated_backend.at(10, ONE)
    _run(rated_config, rated_backend, capsys)
    rated_backend.at(20, NINE)
    rated_report = _run(rated_config, rated_backend, capsys)
    assert len(_deltas(rated)) == 1
    assert "rate_limited" in _coverage(rated_report, "history.native_internal")["reason_codes"]


def test_bal_06_atomic_concurrent_and_no_sender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """BAL-06. One baseline, no duplicate payment, and no sender call."""
    config = _desk(tmp_path, monkeypatch)
    quiet = Sink()
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys, no_send=False, sender=quiet)
    assert quiet.calls == 0
    flagged = Sink()
    backend.at(20, NINE)
    _run(config, backend, capsys, no_send=True, sender=flagged)
    assert flagged.calls == 0
    assert len(_deltas(tmp_path)) == 1
    stored = _baseline(tmp_path)
    assert stored is not None and int(stored["version"]) >= 1
    assert int(stored["raw_balance"]) == NINE

    race = tmp_path / "race"
    race_config = _desk(race, monkeypatch)
    race_backend = ScriptBackend()
    race_backend.barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _once() -> None:
        try:
            code = main(
                ["run-once", "--mcm-config", str(race_config), "--no-send"],
                backend=race_backend,
                sender=Sink(),
                clock=lambda: CLOCK,
            )
            if code != 0:
                errors.append(RuntimeError(f"exit {code}"))
        except BaseException as exc:  # noqa: BLE001 - the test records the thread failure
            errors.append(exc)

    first = threading.Thread(target=_once)
    second = threading.Thread(target=_once)
    first.start()
    second.start()
    first.join(30)
    second.join(30)
    assert not errors
    assert not first.is_alive() and not second.is_alive()
    final = _baseline(race)
    assert final is not None
    assert final["block_number"] == 30
    assert int(final["raw_balance"]) == NINE
    raced = _deltas(race)
    assert len(raced) <= 1
    if raced:
        assert int(raced[0]["payload"]["new_checkpoint"]["block_number"]) == 30
        assert int(raced[0]["payload"]["old_checkpoint"]["block_number"]) == 10


def _coverage(report: dict, capability: str) -> dict:
    return next(row for row in report["coverage"] if row["capability"] == capability and row["network"] == "bsc-mainnet")


def _outbox(tmp_path: Path) -> list[dict]:
    database = _db(tmp_path)
    try:
        return database.outbox_for(TENANT)
    finally:
        database.close()
