"""BAL-01 through BAL-06. Real ``guardian run-once`` entrypoint, fixture transport.

Synthetic addresses only. Amounts stay in integer wei. No live provider is called.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
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
        self.canonical_reject = False
        self.historical_reject = False
        self.balance_both = False
        self.finalized_429 = False
        self.finalized_null = False
        self.finalized_unsupported = False
        self.exhaust_after_nonce = False
        self.clock_box: list[float] | None = None
        self.fail_price = False
        self.block_at_price: int | None = None
        self.price_urls: list[str] = []
        self.canonical_hashes: dict[int, str] = {}
        self.reply_number: dict[int, int] = {}
        self.null_heights: set[int] = set()
        self.stall_height: int | None = None
        self.clock_jump: list[object] | None = None
        self.price_retry = False
        self._price_attempts = 0

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
        if "simple/price" in url:
            self.price_urls.append(url)
            state = os.environ.get("MONITOR_STATE_DIR")
            if state:
                connection = sqlite3.connect(os.path.join(state, "monitor.sqlite"))
                try:
                    row = connection.execute(
                        "SELECT block_number FROM native_balance_baselines LIMIT 1"
                    ).fetchone()
                finally:
                    connection.close()
                self.block_at_price = None if row is None else int(row[0])
            if self.fail_price:
                raise RuntimeError("price down")
            self._price_attempts += 1
            if self.price_retry and self._price_attempts == 1:
                return RawResponse(429, {"retry-after": "1"}, b"")
            if isinstance(self.price, dict):
                return RawResponse(200, {}, json.dumps(self.price).encode())
        return RawResponse(200, {}, b"{}")

    def _rpc(self, method: str, params: list) -> RawResponse:
        self.calls.append({"method": method, "params": params})
        if method == "eth_chainId":
            return _ok(hex(self.chain_id))
        if method == "eth_blockNumber":
            return _ok(hex(self.head))
        if method == "eth_getBlockByNumber":
            tag = params[0] if params else None
            self._stall(tag)
            if self.clock_jump is not None and not self.clock_jump[0]:
                self.clock_jump[0] = True
            if tag == "finalized" and self.finalized_429:
                return RawResponse(429, {}, b"")
            if tag == "finalized" and self.finalized_null:
                return _ok(None)
            if tag == "finalized" and self.finalized_unsupported:
                return _err("unsupported block tag", -32602)
            return _ok(self._block(params))
        if method == "eth_getBalance":
            return self._balance(params)
        if method == "eth_getTransactionCount":
            if self.exhaust_after_nonce and self.clock_box is not None:
                self.clock_box[0] = CLOCK + 100
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
            if asked in self.null_heights:
                return None
            if asked in self.reply_number:
                lied = self.reply_number[asked]
                return {"hash": _hash(lied), "number": hex(lied), "timestamp": hex(1_700_000_000)}
            if asked in self.canonical_hashes:
                return {"hash": self.canonical_hashes[asked], "number": hex(asked), "timestamp": hex(1_700_000_000)}
            if asked != number:
                return {"hash": _hash(asked), "number": hex(asked), "timestamp": hex(1_700_000_000)}
        if self.barrier is not None:
            digest = _hash(number)
        else:
            digest = self.block_hash
        if self.after_mismatch and isinstance(tag, str) and tag.startswith("0x"):
            digest = _hash(number + 1)
        return {"hash": digest, "number": hex(number), "timestamp": hex(1_700_000_000)}

    def _stall(self, tag: object) -> None:
        if self.stall_height is None or self.clock_box is None or not isinstance(tag, str):
            return
        if tag in {"finalized", "latest", "safe"}:
            return
        try:
            asked = int(tag, 16)
        except ValueError:
            return
        if asked == self.stall_height:
            self.clock_box[0] = CLOCK + 6

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
        if isinstance(spec, dict) and self.canonical_reject:
            return _err("blockHash is not canonical")
        if isinstance(spec, dict) and self.historical_reject:
            return _err("historical state is not available")
        if isinstance(spec, dict) and self.balance_both:
            body = {
                "error": {"code": -32000, "message": "stale"},
                "id": 1,
                "jsonrpc": "2.0",
                "result": hex(self.balance),
            }
            return RawResponse(200, {}, json.dumps(body).encode())
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
    deadline: int | None = None,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = []
    document["enabled_networks"] = [network]
    document["routing"] = [route for route in document["routing"] if route.get("network") == network]
    document["polling"]["bootstrap_origin"] = 1
    if deadline is not None:
        document["polling"]["round_deadline_seconds"] = deadline
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
    clock=None,
) -> dict:
    argv = ["run-once", "--mcm-config", str(config)]
    if no_send:
        argv.append("--no-send")
    code = main(argv, backend=backend, sender=sender, clock=clock or (lambda: CLOCK))
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
    bare_rows = _deltas(bare)
    assert len(bare_rows) == 1
    assert bare_rows[0]["payload"]["threshold_decision"] == "threshold_unavailable"
    assert bare_rows[0]["payload"]["alert"] is True
    assert "unjudged" in bare_rows[0]["payload"]["detail"]
    bare_baseline = _baseline(bare)
    assert int(bare_baseline["raw_balance"]) == ONE
    assert bare_baseline["block_number"] == 10
    unavailable = [item for item in bare_report["decisions"] if item.get("kind") == "balance_delta"]
    assert len(unavailable) == 1
    assert unavailable[0]["threshold_decision"] == "threshold_unavailable"
    assert unavailable[0]["alert"] is True

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
    assert _baseline(deep)["block_number"] == 100
    assert int(_baseline(deep)["raw_balance"]) == ONE
    assert _deltas(deep) == []
    database = _db(deep)
    try:
        old = database.conn.execute(
            "SELECT block_number FROM native_balance_checkpoints WHERE block_number=100"
        ).fetchall()
    finally:
        database.close()
    assert len(old) == 1

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
    _run(race_config, race_backend, capsys, no_send=True, sender=Sink())
    race_backend.at(30, NINE)
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
    assert len(raced) == 1
    assert int(raced[0]["payload"]["new_checkpoint"]["block_number"]) == 30
    assert int(raced[0]["payload"]["old_checkpoint"]["block_number"]) == 10
    assert raced[0]["payload"]["continuity"] != "gap"


def _coverage(report: dict, capability: str) -> dict:
    return next(row for row in report["coverage"] if row["capability"] == capability and row["network"] == "bsc-mainnet")


def _pending_movements(tmp_path: Path) -> list[str]:
    kinds = {"asset_usd", "balance_delta", "native_eth", "native_unit"}
    return [
        row["payload"]["kind"]
        for row in _outbox(tmp_path)
        if row["status"] == "pending" and row["payload"].get("kind") in kinds
    ]


def _outbox(tmp_path: Path) -> list[dict]:
    database = _db(tmp_path)
    try:
        return database.outbox_for(TENANT)
    finally:
        database.close()


def _balance_params(backend: ScriptBackend) -> list[object]:
    return [call["params"][1] for call in backend.calls if call["method"] == "eth_getBalance" and len(call["params"]) > 1]


def _checkpoint(block: int, raw: int) -> dict:
    return {
        "binding": "eip1898_hash",
        "block_hash": _hash(block),
        "block_number": block,
        "block_timestamp": 1,
        "confirmation_policy": "bsc_bep126_fast_finality",
        "nonce": "0x1",
        "observed_at": "2023-11-14T22:13:20Z",
        "raw_balance": str(raw),
        "source_id": "rpc.bsc-mainnet",
    }


def test_a03_canonical_rejection_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A03. "not canonical" is not an unsupported-parameter error and is not eligible."""
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.canonical_reject = True
    backend.at(20, NINE)
    backend.calls.clear()
    _run(config, backend, capsys, expect_ok=False)
    stored = _baseline(tmp_path)
    assert stored is not None
    assert stored["block_number"] == 10
    assert int(stored["raw_balance"]) == ONE
    assert _deltas(tmp_path) == []
    specs = _balance_params(backend)
    assert specs
    assert all(isinstance(spec, dict) for spec in specs)


def test_a04_mixed_envelope_and_false_ok_are_not_eligible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A04. result+error, and a transport result that is not ok, never become a balance."""
    from guardian.balances.native import ConfirmationPolicy, read_definite_balance
    from guardian.sources.transport import TransportResult

    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.balance_both = True
    backend.at(20, NINE)
    backend.calls.clear()
    _run(config, backend, capsys, expect_ok=False)
    stored = _baseline(tmp_path)
    assert stored is not None and stored["block_number"] == 10
    assert _deltas(tmp_path) == []
    assert all(isinstance(spec, dict) for spec in _balance_params(backend))

    class Transport:
        def request(self, source_id: str, operation: object, priority: str = "high") -> TransportResult:
            del priority
            method = getattr(operation, "method", "")
            if method == "eth_getBlockByNumber":
                body = {
                    "id": 1,
                    "jsonrpc": "2.0",
                    "result": {"hash": _hash(16), "number": "0x10", "timestamp": "0x1"},
                }
                return TransportResult(source_id, 200, body, "ok", "ok", False, False, "", {})
            body = {"id": 1, "jsonrpc": "2.0", "result": "0x10"}
            return TransportResult(source_id, 200, body, "error", "error", False, False, "rpc_error", {})

    policy = ConfirmationPolicy("bsc_bep126_fast_finality", "finalized", 15, False)
    read = read_definite_balance(Transport(), "rpc", ADDRESS, policy)
    assert read.eligible is False
    assert read.raw is None
    assert read.failure == "malformed_response"


def test_a05_finalized_429_does_not_read_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A05. A rate-limited finalized tag is not an unsupported tag and does not become latest."""
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.finalized_429 = True
    backend.at(10, NINE)
    report = _run(config, backend, capsys, expect_ok=False)
    methods = [call["method"] for call in backend.calls]
    assert "eth_getBalance" not in methods
    assert "rate_limited" in _coverage(report, "balances.native")["reason_codes"]
    assert _baseline(tmp_path) is None
    assert _deltas(tmp_path) == []


def test_historical_state_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.historical_reject = True
    backend.at(20, NINE)
    backend.calls.clear()
    _run(config, backend, capsys, expect_ok=False)
    assert _baseline(tmp_path)["block_number"] == 10
    assert all(isinstance(spec, dict) for spec in _balance_params(backend))
    assert _deltas(tmp_path) == []


def test_c01_conflict_then_proven_rebaseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """C01. A same-height hash is retained. Rebaseline waits until that hash is not canonical."""
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(100, ONE)
    _run(config, backend, capsys)
    backend.block_number = 100
    backend.head = 100
    backend.block_hash = _hash(999)
    backend.balance = ONE + (8 * ONE)
    _run(config, backend, capsys)
    held = _baseline(tmp_path)
    assert held is not None
    assert held["block_hash"] == _hash(100)
    assert int(held["raw_balance"]) == ONE
    assert _deltas(tmp_path) == []
    database = _db(tmp_path)
    try:
        conflicts = database.native_conflicts_for(TENANT, WALLET, "bsc-mainnet", ASSET)
    finally:
        database.close()
    assert len(conflicts) == 1
    assert conflicts[0]["status"] == "open"
    assert conflicts[0]["observed_hash"] == _hash(999)

    backend.canonical_hashes[100] = _hash(999)
    backend.at(101, ONE + (8 * ONE))
    backend.canonical_hashes[100] = _hash(999)
    _run(config, backend, capsys)
    moved = _baseline(tmp_path)
    assert moved is not None
    assert moved["block_number"] == 101
    assert moved["continuity"] == "rebaseline"
    assert _deltas(tmp_path) == []
    database = _db(tmp_path)
    try:
        conflicts = database.native_conflicts_for(TENANT, WALLET, "bsc-mainnet", ASSET)
    finally:
        database.close()
    assert conflicts[0]["status"] == "rebaselined"


def test_c02_retention_prunes_without_meaning_a_missed_round(tmp_path: Path) -> None:
    """C02. One hundred and one checkpoints prune by retention. The distance is not a gap."""
    database = Database(tmp_path / "retention.sqlite")
    try:
        version = 0
        for block in range(1, 102):
            status = database.commit_native_observation(
                TENANT,
                wallet_id=WALLET,
                network="bsc-mainnet",
                asset=ASSET,
                expected_version=version,
                checkpoint=_checkpoint(block, block),
                continuity="continuous",
                retention_blocks=15,
                reset_checkpoints=False,
                alert=None,
                record_failure=None,
            )
            assert status == "committed"
            version += 1
        numbers = [
            row[0]
            for row in database.conn.execute(
                "SELECT block_number FROM native_balance_checkpoints WHERE tenant_id=? ORDER BY block_number",
                (TENANT,),
            )
        ]
        assert numbers == list(range(86, 102))
        stale = database.commit_native_observation(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=0,
            checkpoint=_checkpoint(10, 1),
            continuity="continuous",
            retention_blocks=15,
            reset_checkpoints=False,
            alert=None,
            record_failure=None,
        )
        assert stale == "conflict"
        assert database.native_balance_baseline(TENANT, WALLET, "bsc-mainnet", ASSET)["block_number"] == 101
        refused = database.rebaseline_native(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=101,
            checkpoint=_checkpoint(70, 1),
            proof={"block_number": 101, "canonical_hash": _hash(101), "stored_hash": _hash(101)},
        )
        assert refused == "rejected"
        assert database.native_balance_baseline(TENANT, WALLET, "bsc-mainnet", ASSET)["block_number"] == 101
    finally:
        database.close()


def test_polling_gap_is_not_retention_distance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.at(80, NINE)
    _run(config, backend, capsys)
    rows = _deltas(tmp_path)
    assert len(rows) == 1
    assert rows[0]["payload"]["continuity"] == "continuous"
    database = _db(tmp_path)
    try:
        numbers = [row[0] for row in database.conn.execute("SELECT block_number FROM native_balance_checkpoints")]
    finally:
        database.close()
    assert 10 not in numbers
    assert 80 in numbers


def test_e01_price_failure_keeps_the_native_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """E01. The observation is committed before the quote request."""
    config = _desk(tmp_path, monkeypatch, prices=True)
    backend = ScriptBackend()
    backend.price = {"binancecoin": {"usd": 300, "last_updated_at": int(CLOCK)}}
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.fail_price = True
    backend.at(20, NINE)
    report = _run(config, backend, capsys)
    assert report["status"] == "ok"
    assert backend.block_at_price == 20
    stored = _baseline(tmp_path)
    assert stored is not None and stored["block_number"] == 20
    assert len(_deltas(tmp_path)) == 1
    assert _outbox(tmp_path)


def test_e01_usd_only_failure_persists_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = _desk(tmp_path, monkeypatch, threshold=None, prices=True)
    backend = ScriptBackend()
    backend.price = {"binancecoin": {"usd": 300, "last_updated_at": int(CLOCK)}}
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.fail_price = True
    backend.at(20, NINE)
    _run(config, backend, capsys)
    stored = _baseline(tmp_path)
    assert stored is not None and stored["block_number"] == 10
    assert int(stored["raw_balance"]) == ONE
    held = _deltas(tmp_path)
    assert len(held) == 1
    assert held[0]["payload"]["threshold_decision"] == "threshold_unavailable"
    assert held[0]["payload"]["alert"] is True
    backend.at(21, NINE)
    _run(config, backend, capsys)
    assert len(_deltas(tmp_path)) == 1
    assert _baseline(tmp_path)["block_number"] == 10
    database = _db(tmp_path)
    try:
        decisions = database.native_decisions_for(TENANT, WALLET, "bsc-mainnet", ASSET)
    finally:
        database.close()
    assert any(row["payload"].get("threshold_decision") == "threshold_unavailable" for row in decisions)


def test_e02_balance_path_stops_at_the_round_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """E02. After identity, an expired round does not read a balance."""
    config = _desk(tmp_path, monkeypatch, deadline=5)
    backend = ScriptBackend()
    jump = [False]
    backend.clock_jump = jump

    def clock() -> float:
        return CLOCK + 10_000 if jump[0] else CLOCK

    report = _run(config, backend, capsys, clock=clock, expect_ok=False)
    methods = [call["method"] for call in backend.calls]
    assert "eth_chainId" in methods
    assert "eth_getBlockByNumber" in methods
    assert "eth_getBalance" not in methods
    assert "deadline" in _coverage(report, "balances.native")["reason_codes"]
    assert _baseline(tmp_path) is None


def test_e03_quote_uses_selected_credential_clock_and_sleeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch, prices=True)
    backend = ScriptBackend()
    backend.price = {"binancecoin": {"usd": 3000, "last_updated_at": int(CLOCK)}}
    backend.price_retry = True
    backend.slept: list[float] = []
    backend.budget_sleeper = lambda seconds: backend.slept.append(seconds)
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.at(11, ONE + (4 * ONE // 10))
    _run(config, backend, capsys)
    assert any("x_cg_demo_api_key=synthetic-price-key" in url for url in backend.price_urls)
    assert 1.0 in backend.slept or 1 in backend.slept
    row = _deltas(tmp_path)[0]["payload"]["threshold"]
    assert row["basis"] == "usd"
    assert row["quote_source"]
    assert row["quoted_at"] == int(CLOCK)
    assert row["last_updated_at"] == int(CLOCK)


def test_g03_limited_amount_match_stays_unreconciled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """G03. An indexer row with no success evidence does not explain the delta."""
    config = _desk(tmp_path, monkeypatch, nodereal_key=True)
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys)
    tx = "0x" + "77" * 32
    row = _internal(tx, DELTA, 15, "0")
    row.pop("status")
    row.pop("traceId")
    backend.by_category["internal"] = [row]
    backend.at(20, NINE)
    _run(config, backend, capsys)
    payload = _deltas(tmp_path)[0]["payload"]
    assert payload["attribution"] == "unavailable"
    assert payload.get("explained_by") in (None, [])
    assert tx not in {item["tx_id"] for item in _events(tmp_path) if item.get("execution_evidence") == "evidenced"}


def test_internal_rows_drop_failures_and_gap_on_ambiguity() -> None:
    from guardian.balances.internal import normalize_internal_batch

    owner = ADDRESS
    failed_receipt = _internal("0x" + "81" * 32, ONE, 15, "0")
    failed_receipt.pop("status")
    failed_receipt["receiptsStatus"] = "0"
    reverted = _internal("0x" + "82" * 32, ONE, 15, "0")
    reverted.pop("status")
    reverted["callError"] = "execution reverted"
    delegated = _internal("0x" + "83" * 32, ONE, 15, "0")
    delegated["type"] = "DELEGATECALL"
    ambiguous = _internal("0x" + "84" * 32, ONE, 15, "0")
    ambiguous["type"] = "NOT_A_CALL"
    contradictory = _internal("0x" + "86" * 32, ONE, 15, "0")
    contradictory["callError"] = "execution reverted"
    dropped = normalize_internal_batch([failed_receipt, reverted, delegated], chain_id=56, owner=owner)
    assert dropped.rows == ()
    assert dropped.gap is None
    conflicted = normalize_internal_batch([contradictory], chain_id=56, owner=owner)
    assert conflicted.gap == "parsing_gap"
    gapped = normalize_internal_batch([ambiguous], chain_id=56, owner=owner)
    assert gapped.rows == ()
    assert gapped.gap == "parsing_gap"
    page = normalize_internal_batch("not-a-list", chain_id=56, owner=owner)
    assert page.gap == "parsing_gap"


def test_ambiguous_internal_page_is_not_complete_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch, nodereal_key=True)
    backend = ScriptBackend()
    backend.at(10, ONE)
    row = _internal("0x" + "85" * 32, ONE, 12, "0")
    row["type"] = "NOT_A_CALL"
    backend.by_category["internal"] = [row]
    report = _run(config, backend, capsys)
    internal = _coverage(report, "history.native_internal")
    assert "parsing_gap" in internal["reason_codes"]
    assert internal.get("quality") != "complete"
    assert _events(tmp_path) == []


def test_g04_transfer_then_delta_notifies_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """G04. A later balance delta links to the transfer and does not add a second pending row."""
    config = _desk(tmp_path, monkeypatch, nodereal_key=True)
    backend = ScriptBackend()
    backend.at(10, ONE)
    _run(config, backend, capsys)
    backend.fail_balance = True
    backend.at(16, ONE)
    backend.by_category["internal"] = [_internal("0x" + "91" * 32, 5 * ONE, 15, "1")]
    _run(config, backend, capsys, expect_ok=False)
    assert _baseline(tmp_path)["block_number"] == 10
    assert _pending_movements(tmp_path) == ["native_unit"]
    backend.fail_balance = False
    backend.by_category["internal"] = []
    backend.at(20, ONE + 5 * ONE)
    _run(config, backend, capsys)
    pending = _pending_movements(tmp_path)
    assert pending.count("native_unit") == 1
    assert "balance_delta" not in pending
    database = _db(tmp_path)
    try:
        delta = next(item for item in database.alerts_for(TENANT) if item.get("kind") == "balance_delta")
        links = database.conn.execute("SELECT COUNT(*) FROM balance_explanations").fetchone()[0]
    finally:
        database.close()
    assert delta["linked_transfers"]
    assert links >= 1


def test_sent_transfer_receipt_survives_the_later_delta(tmp_path: Path) -> None:
    database = Database(tmp_path / "sent.sqlite")
    try:
        assert database.commit_native_observation(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=0,
            checkpoint=_checkpoint(10, ONE),
            continuity="baseline",
            retention_blocks=15,
            reset_checkpoints=False,
            alert=None,
            record_failure=None,
        ) == "committed"
        transfer = {
            "alert": True,
            "asset": ASSET,
            "block_number": "15",
            "dedup_key": "transfer-key",
            "direction": "in",
            "execution_evidence": "evidenced",
            "identity": "tx-transfer",
            "kind": "native_unit",
            "network": "bsc-mainnet",
            "raw_amount": str(5 * ONE),
            "tenant_id": TENANT,
            "wallet_id": WALLET,
        }
        assert database.add_alert(TENANT, "transfer-key", transfer) is True
        with database.conn:
            database.conn.execute("UPDATE outbox SET status='sent' WHERE tenant_id=?", (TENANT,))
            database.conn.execute(
                "INSERT INTO deliveries(tenant_id, payload) VALUES(?, ?)",
                (TENANT, '{"receipt":"kept"}'),
            )
        delta = {
            "alert": True,
            "asset": ASSET,
            "dedup_key": "delta-key",
            "direction": "increase",
            "kind": "balance_delta",
            "network": "bsc-mainnet",
            "new_checkpoint": {"block_hash": _hash(20), "block_number": 20},
            "old_checkpoint": {"block_hash": _hash(10), "block_number": 10},
            "signed_delta_raw": str(5 * ONE),
            "wallet_id": WALLET,
        }
        assert database.commit_native_observation(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=1,
            checkpoint=_checkpoint(20, ONE + 5 * ONE),
            continuity="continuous",
            retention_blocks=15,
            reset_checkpoints=False,
            alert=delta,
            record_failure=None,
            decision=delta,
        ) == "committed"
        outbox = database.outbox_for(TENANT)
        assert len(outbox) == 1
        assert outbox[0]["status"] == "sent"
        assert outbox[0]["payload"]["kind"] == "native_unit"
        kinds = [item["kind"] for item in database.alerts_for(TENANT)]
        assert "native_unit" in kinds and "balance_delta" in kinds
        assert database.conn.execute("SELECT COUNT(*) FROM balance_explanations").fetchone()[0] >= 1
        assert "kept" in database.conn.execute("SELECT payload FROM deliveries").fetchone()[0]
    finally:
        database.close()


def test_d08_opposing_transfers_alert_while_net_delta_stays_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """D08. +5 and -5 BNB each meet the 1.0 floor when there is no price. The net delta does not."""
    config = _desk(tmp_path, monkeypatch, nodereal_key=True, prices=False)
    backend = ScriptBackend()
    backend.at(10, 10 * ONE)
    _run(config, backend, capsys)
    incoming = _internal("0x" + "a1" * 32, 5 * ONE, 15, "0")
    outgoing = _internal("0x" + "a2" * 32, 5 * ONE, 16, "0", sender=ADDRESS)
    outgoing["to"] = OTHER
    backend.by_category["external"] = [incoming, outgoing]
    backend.at(20, 10 * ONE)
    _run(config, backend, capsys)
    assert _deltas(tmp_path) == []
    assert _kinds(tmp_path).count("native_unit") == 2


def test_own_wallet_pair_stays_net_even_with_a_native_floor() -> None:
    from guardian.rules import AlertPolicy, Candidate

    policy = AlertPolicy(Decimal("0.5"), Decimal("1000"), {"bsc-mainnet": Decimal("1.0")})

    def candidate(wallet: str, other: str, direction: str, identity: str) -> Candidate:
        return Candidate(
            tenant_id=TENANT,
            wallet_id=wallet,
            network="bsc-mainnet",
            capability="history.native_external",
            identity=identity,
            kind="asset",
            raw_amount=5 * ONE,
            is_native=True,
            direction=direction,
            counterparty_wallet_id=other,
            transfer_origin="evm_external",
        )

    paired = policy.apply([
        candidate("wallet-a", "wallet-b", "out", "same-tx"),
        candidate("wallet-b", "wallet-a", "in", "same-tx"),
    ])
    assert len(paired) == 1
    assert paired[0].kind == "internal_transfer"
    assert paired[0].alert is False
    separate = policy.apply([
        candidate("wallet-a", "", "in", "one"),
        candidate("wallet-a", "", "out", "two"),
    ])
    assert [item.kind for item in separate if item.alert] == ["native_unit", "native_unit"]


def test_a07_null_checkpoint_is_not_an_unsupported_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A07. result null is an absent checkpoint. An explicit tag rejection may still read head."""
    from guardian.balances.native import ConfirmationPolicy, read_definite_balance
    from guardian.sources.transport import TransportResult

    class TagTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def request(self, source_id: str, operation: object, priority: str = "high") -> TransportResult:
            del priority
            method = str(getattr(operation, "method", ""))
            params = getattr(operation, "params", None)
            tag = params[0] if isinstance(params, list) and params else None
            self.calls.append((method, tag))
            body = {"id": 1, "jsonrpc": "2.0", "result": None}
            return TransportResult(source_id, 200, body, "ok", "ok", False, False, "", {})

    for tag in ("finalized", "safe"):
        transport = TagTransport()
        policy = ConfirmationPolicy("checkpoint", tag, 15, tag == "finalized")
        read = read_definite_balance(transport, "rpc", ADDRESS, policy)
        assert read.eligible is False
        assert read.failure == "checkpoint_unavailable"
        assert [method for method, _used in transport.calls] == ["eth_getBlockByNumber"]
        assert transport.calls[0][1] == tag

    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.finalized_null = True
    backend.at(10, NINE)
    report = _run(config, backend, capsys, expect_ok=False)
    methods = [call["method"] for call in backend.calls]
    assert "eth_getBalance" not in methods
    balance_blocks = [
        call for call in backend.calls
        if call["method"] == "eth_getBlockByNumber" and call["params"][:1] == ["finalized"]
    ]
    assert balance_blocks
    assert "checkpoint_unavailable" in _coverage(report, "balances.native")["reason_codes"]
    assert _baseline(tmp_path) is None
    assert _deltas(tmp_path) == []

    allowed = tmp_path / "unsupported-tag"
    allowed_config = _desk(allowed, monkeypatch)
    allowed_backend = ScriptBackend()
    allowed_backend.finalized_unsupported = True
    allowed_backend.at(10, ONE)
    _run(allowed_config, allowed_backend, capsys)
    assert "eth_blockNumber" in [call["method"] for call in allowed_backend.calls]
    stored = _baseline(allowed)
    assert stored is not None
    assert stored["block_number"] == 10
    assert int(stored["raw_balance"]) == ONE


def test_c05_forward_fork_does_not_emit_continuous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """C05. Re-read the stored height before a forward delta, even with no open conflict."""
    config = _desk(tmp_path, monkeypatch)
    backend = ScriptBackend()
    backend.at(100, ONE)
    _run(config, backend, capsys)
    backend.canonical_hashes[100] = _hash(900)
    backend.at(101, NINE)
    backend.calls.clear()
    _run(config, backend, capsys)
    proofs = [
        call for call in backend.calls
        if call["method"] == "eth_getBlockByNumber" and call["params"][:1] == [hex(100)]
    ]
    assert proofs
    moved = _baseline(tmp_path)
    assert moved is not None
    assert moved["block_number"] == 101
    assert moved["continuity"] == "rebaseline"
    assert int(moved["raw_balance"]) == NINE
    assert _deltas(tmp_path) == []


def test_e04_revalidation_uses_the_round_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """E04. A nonce read that exhausts the deadline must not start the old-checkpoint proof."""
    config = _desk(tmp_path, monkeypatch, deadline=5)
    backend = ScriptBackend()
    backend.at(100, ONE)
    _run(config, backend, capsys)
    box = [CLOCK]
    backend.clock_box = box
    backend.exhaust_after_nonce = True
    backend.canonical_hashes[100] = _hash(900)
    backend.at(101, NINE)
    backend.calls.clear()

    def clock() -> float:
        return box[0]

    _run(config, backend, capsys, clock=clock, expect_ok=False)
    proofs = [
        call for call in backend.calls
        if call["method"] == "eth_getBlockByNumber" and call["params"][:1] == [hex(100)]
    ]
    methods = [call["method"] for call in backend.calls]
    assert "eth_getBalance" in methods
    assert "eth_getTransactionCount" in methods
    assert proofs == []
    stored = _baseline(tmp_path)
    assert stored is not None
    assert stored["block_number"] == 100
    assert stored["continuity"] != "rebaseline"
    assert _deltas(tmp_path) == []


def test_c07_e05_bad_checkpoint_proof_preserves_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """C07 and E05. A wrong height or a late proof does not move the baseline.

    An absent proof still defers. A matching proof inside the budget still advances.
    """

    def proofs(backend: ScriptBackend) -> list[dict]:
        return [
            call for call in backend.calls
            if call["method"] == "eth_getBlockByNumber" and call["params"][:1] == [hex(10)]
        ]

    def open_round(name: str) -> tuple[Path, Path, ScriptBackend]:
        path = tmp_path / name
        config = _desk(path, monkeypatch, deadline=5)
        backend = ScriptBackend()
        backend.at(10, ONE)
        _run(config, backend, capsys)
        backend.calls.clear()
        backend.at(20, NINE)
        return path, config, backend

    def preserved(path: Path, backend: ScriptBackend) -> None:
        assert proofs(backend)
        stored = _baseline(path)
        assert stored is not None
        assert int(stored["version"]) == 1
        assert stored["block_number"] == 10
        assert int(stored["raw_balance"]) == ONE
        assert stored["block_hash"] == _hash(10)
        assert stored["continuity"] != "rebaseline"
        assert _deltas(path) == []

    wrong_path, wrong_config, wrong = open_round("wrong-height")
    wrong.reply_number[10] = 9
    _run(wrong_config, wrong, capsys)
    preserved(wrong_path, wrong)

    late_path, late_config, late = open_round("late-proof")
    box = [CLOCK]
    late.clock_box = box
    late.stall_height = 10

    def clock() -> float:
        return box[0]

    _run(late_config, late, capsys, clock=clock, expect_ok=False)
    preserved(late_path, late)

    missing_path, missing_config, missing = open_round("unavailable-proof")
    missing.null_heights.add(10)
    _run(missing_config, missing, capsys)
    preserved(missing_path, missing)

    valid_path, valid_config, valid = open_round("within-budget")
    _run(valid_config, valid, capsys)
    assert proofs(valid)
    moved = _baseline(valid_path)
    assert moved is not None
    assert moved["block_number"] == 20
    assert int(moved["raw_balance"]) == NINE
    assert moved["continuity"] == "continuous"
    assert _deltas(valid_path)[0]["payload"]["signed_delta_raw"] == str(DELTA)


def test_g05_missing_execution_evidence_is_not_reconciled(tmp_path: Path) -> None:
    """G05. An external native candidate with no execution marker is not explained away."""
    from guardian.balances.reconcile import reconcile_native
    from guardian.rules import Candidate

    database = Database(tmp_path / "g05.sqlite")
    try:
        _seed_delta(database, signed=5 * ONE)
        candidate = Candidate(
            tenant_id=TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            capability="history.native_external",
            identity="external-without-evidence",
            kind="asset",
            raw_amount=5 * ONE,
            is_native=True,
            direction="in",
            tx_id="0x" + "51" * 32,
            transfer_origin="evm_external",
            block_number=15,
            asset=ASSET,
        )
        pending = [candidate]
        assert reconcile_native(database, TENANT, pending) == []
        assert pending == [candidate]
        stored = next(item for item in database.alerts_for(TENANT) if item.get("kind") == "balance_delta")
        assert stored.get("attribution") != "reconciled"
        assert stored.get("explained_by") in (None, [])
    finally:
        database.close()


def test_g06_orphaned_fee_does_not_complete_reconciliation(tmp_path: Path) -> None:
    """G06. An orphaned fee is excluded. A confirmed fee can still complete the sum."""
    from guardian.balances.reconcile import reconcile_native
    from guardian.events import ChainEvent
    from guardian.rules import Candidate

    def fee(tx: str, finality: str) -> ChainEvent:
        return ChainEvent(
            tenant_id=TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            chain_uid="eip155:56",
            tx_id=tx,
            sub_index="fee",
            kind="fee",
            asset=ASSET,
            raw_amount=str(ONE),
            block_number="15",
            block_hash=_hash(15),
            tx_status="success",
            finality=finality,
            source_ids=("nodereal",),
            observed_at="2023-11-14T22:13:20Z",
        )

    def candidate(identity: str) -> Candidate:
        return Candidate(
            tenant_id=TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            capability="history.native_internal",
            identity=identity,
            kind="asset",
            raw_amount=5 * ONE,
            is_native=True,
            direction="in",
            tx_id="0x" + "61" * 32,
            transfer_origin="evm_internal",
            execution_evidence="evidenced",
            attribution="trace",
            block_number=15,
            asset=ASSET,
        )

    orphaned_only = Database(tmp_path / "orphaned.sqlite")
    try:
        _seed_delta(orphaned_only, signed=4 * ONE)
        orphaned_only.commit_capability_cycle(
            TENANT, events=[fee("0x" + "62" * 32, "orphaned")], capability_rows=[], cursors=[], alerts=[],
        )
        pending = [candidate("orphaned-only")]
        assert reconcile_native(orphaned_only, TENANT, pending) == []
        assert len(pending) == 1
    finally:
        orphaned_only.close()

    mixed = Database(tmp_path / "mixed.sqlite")
    try:
        _seed_delta(mixed, signed=4 * ONE)
        mixed.commit_capability_cycle(
            TENANT,
            events=[fee("0x" + "63" * 32, "orphaned"), fee("0x" + "64" * 32, "confirmed")],
            capability_rows=[],
            cursors=[],
            alerts=[],
        )
        pending = [candidate("mixed-fees")]
        removed = reconcile_native(mixed, TENANT, pending)
        assert removed == ["mixed-fees"]
        assert pending == []
        stored = next(item for item in mixed.alerts_for(TENANT) if item.get("kind") == "balance_delta")
        assert stored["attribution"] == "reconciled"
        fee_rows = [item for item in stored["components"] if item.get("role") == "fee"]
        assert len(fee_rows) == 1
        assert fee_rows[0]["raw_amount"] == str(ONE)
    finally:
        mixed.close()


def test_f06_malformed_native_amount_is_a_parsing_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """F06. A relevant internal row with a bad native amount is not an empty history."""
    from guardian.balances.internal import normalize_internal_batch

    row = _internal("0x" + "71" * 32, ONE, 12, "1")
    row["value"] = "0xnothex"
    parsed = normalize_internal_batch([row], chain_id=56, owner=ADDRESS)
    assert parsed.rows == ()
    assert parsed.gap == "parsing_gap"
    raw_only = _internal("0x" + "72" * 32, ONE, 12, "1")
    raw_only.pop("value")
    raw_only["rawContract"] = {"value": "0xnothex"}
    raw_parsed = normalize_internal_batch([raw_only], chain_id=56, owner=ADDRESS)
    assert raw_parsed.rows == ()
    assert raw_parsed.gap == "parsing_gap"
    stranger = dict(row)
    stranger["from"] = OTHER
    stranger["to"] = OTHER
    ignored = normalize_internal_batch([stranger], chain_id=56, owner=ADDRESS)
    assert ignored.rows == ()
    assert ignored.gap is None

    config = _desk(tmp_path, monkeypatch, nodereal_key=True)
    backend = ScriptBackend()
    backend.at(10, ONE)
    backend.by_category["internal"] = [row]
    report = _run(config, backend, capsys)
    internal = _coverage(report, "history.native_internal")
    assert "parsing_gap" in internal["reason_codes"]
    assert internal.get("quality") != "complete"
    assert _events(tmp_path) == []


def test_g07_interleaved_commits_notify_once(tmp_path: Path) -> None:
    """G07. History judges first, balance commits, then history commits: one pending notification."""
    from guardian.balances.reconcile import reconcile_native
    from guardian.rules import AlertPolicy, Candidate

    path = tmp_path / "interleave.sqlite"
    history = Database(path)
    balance = Database(path)
    try:
        candidate = Candidate(
            tenant_id=TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            capability="history.native_internal",
            identity="interleaved-transfer",
            kind="asset",
            raw_amount=5 * ONE,
            is_native=True,
            direction="in",
            tx_id="0x" + "81" * 32,
            transfer_origin="evm_internal",
            execution_evidence="evidenced",
            attribution="trace",
            block_number=15,
            asset=ASSET,
        )
        judged_candidates = [candidate]
        assert reconcile_native(history, TENANT, judged_candidates) == []
        assert judged_candidates == [candidate]
        history.conn.rollback()
        decision = AlertPolicy(Decimal("0.5"), Decimal("1000"), {"bsc-mainnet": Decimal("1.0")}).apply(judged_candidates)
        assert len(decision) == 1
        assert decision[0].alert is True
        assert decision[0].kind == "native_unit"
        transfer = decision[0].as_dict()

        assert balance.commit_native_observation(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=0,
            checkpoint=_checkpoint(10, ONE),
            continuity="baseline",
            retention_blocks=15,
            reset_checkpoints=False,
            alert=None,
            record_failure=None,
        ) == "committed"
        delta = {
            "alert": True,
            "asset": ASSET,
            "attribution": "unavailable",
            "dedup_key": "delta-interleaved",
            "direction": "increase",
            "kind": "balance_delta",
            "network": "bsc-mainnet",
            "new_checkpoint": {"block_hash": _hash(20), "block_number": "20"},
            "old_checkpoint": {"block_hash": _hash(10), "block_number": "10"},
            "signed_delta_raw": str(5 * ONE),
            "wallet_id": WALLET,
        }
        assert balance.commit_native_observation(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=1,
            checkpoint=_checkpoint(20, ONE + 5 * ONE),
            continuity="continuous",
            retention_blocks=15,
            reset_checkpoints=False,
            alert=delta,
            record_failure=None,
            decision=delta,
        ) == "committed"
        with balance.conn:
            balance.conn.execute(
                "INSERT INTO deliveries(tenant_id, payload) VALUES(?, ?)",
                (TENANT, '{"receipt":"kept"}'),
            )

        history.commit_capability_cycle(
            TENANT, events=[], capability_rows=[], cursors=[], alerts=[transfer],
        )
        outbox = history.outbox_for(TENANT)
        movements = [
            row for row in outbox
            if row["payload"].get("kind") in {"asset_usd", "balance_delta", "native_eth", "native_unit"}
        ]
        assert len(movements) == 1
        assert movements[0]["status"] == "pending"
        assert movements[0]["payload"]["kind"] == "balance_delta"
        kinds = [item["kind"] for item in history.alerts_for(TENANT)]
        assert "balance_delta" in kinds
        assert "native_unit" in kinds
        assert history.conn.execute("SELECT COUNT(*) FROM balance_explanations").fetchone()[0] >= 1
        assert "kept" in history.conn.execute("SELECT payload FROM deliveries").fetchone()[0]
        sent = [row["status"] for row in outbox]
        assert sent == ["pending"]
    finally:
        history.close()
        balance.close()


def test_g09_multi_transfer_interleave_keeps_one_notice(tmp_path: Path) -> None:
    """G09. Two inflows committed together must not leave the partial pending notice."""
    from guardian.balances.reconcile import reconcile_native
    from guardian.rules import AlertPolicy, Candidate

    three = 3 * ONE
    rest = DELTA - three
    policy = AlertPolicy(Decimal("0.5"), Decimal("1000"), {"bsc-mainnet": Decimal("1.0")})

    def candidate(identity: str, raw: int, block: int, tx: str) -> Candidate:
        return Candidate(
            tenant_id=TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            capability="history.native_internal",
            identity=identity,
            kind="asset",
            raw_amount=raw,
            is_native=True,
            direction="in",
            tx_id=tx,
            transfer_origin="evm_internal",
            execution_evidence="evidenced",
            attribution="trace",
            block_number=block,
            asset=ASSET,
        )

    def alert_for(item: Candidate) -> dict:
        decided = policy.apply([item])
        assert len(decided) == 1 and decided[0].alert is True and decided[0].kind == "native_unit"
        return decided[0].as_dict()

    part_3 = candidate("part-3", three, 12, "0x" + "31" * 32)
    part_rest = candidate("part-rest", rest, 16, "0x" + "51" * 32)
    outside = candidate("outside-window", 4 * ONE, 25, "0x" + "91" * 32)

    same = Database(tmp_path / "same-run.sqlite")
    try:
        _seed_delta(same, signed=DELTA)
        pending = [part_3, part_rest, outside]
        removed = reconcile_native(same, TENANT, pending)
        assert set(removed) == {"part-3", "part-rest"}
        assert [item.identity for item in pending] == ["outside-window"]
        rows = same.outbox_for(TENANT)
        assert [row["status"] for row in rows] == ["pending"]
        assert rows[0]["payload"]["kind"] == "balance_delta"
    finally:
        same.close()

    path = tmp_path / "interleave.sqlite"
    history = Database(path)
    balance = Database(path)
    try:
        judged = [part_3, part_rest]
        assert reconcile_native(history, TENANT, judged) == []
        assert [item.identity for item in judged] == ["part-3", "part-rest"]
        history.conn.rollback()
        decided = policy.apply(judged)
        assert [item.kind for item in decided] == ["native_unit", "native_unit"]
        transfers = [item.as_dict() for item in decided]
        assert balance.commit_native_observation(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=0,
            checkpoint=_checkpoint(10, ONE),
            continuity="baseline",
            retention_blocks=15,
            reset_checkpoints=False,
            alert=None,
            record_failure=None,
        ) == "committed"
        delta = {
            "alert": True,
            "asset": ASSET,
            "attribution": "unavailable",
            "dedup_key": "delta-g09",
            "direction": "increase",
            "kind": "balance_delta",
            "network": "bsc-mainnet",
            "new_checkpoint": {"block_hash": _hash(20), "block_number": "20"},
            "old_checkpoint": {"block_hash": _hash(10), "block_number": "10"},
            "signed_delta_raw": str(DELTA),
            "wallet_id": WALLET,
        }
        assert balance.commit_native_observation(
            TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            asset=ASSET,
            expected_version=1,
            checkpoint=_checkpoint(20, ONE + DELTA),
            continuity="continuous",
            retention_blocks=15,
            reset_checkpoints=False,
            alert=delta,
            record_failure=None,
            decision=delta,
        ) == "committed"
        with balance.conn:
            balance.conn.execute(
                "INSERT INTO deliveries(tenant_id, payload) VALUES(?, ?)",
                (TENANT, '{"receipt":"kept"}'),
            )
            balance.conn.execute(
                "INSERT INTO outbox(tenant_id, status, payload) VALUES(?, 'sent', ?)",
                (TENANT, json.dumps({"dedup_key": "already-sent", "identity": "already-sent", "kind": "native_unit"})),
            )
        history.commit_capability_cycle(
            TENANT, events=[], capability_rows=[], cursors=[], alerts=transfers,
        )
        outbox = history.outbox_for(TENANT)
        pending = [
            row for row in outbox
            if row["status"] == "pending"
            and row["payload"].get("kind") in {"asset_usd", "balance_delta", "native_eth", "native_unit"}
        ]
        assert len(pending) == 1
        assert pending[0]["payload"]["kind"] == "balance_delta"
        assert pending[0]["payload"]["signed_delta_raw"] == str(DELTA)
        amounts = {
            item["raw_amount"]
            for item in pending[0]["payload"].get("components") or []
            if item.get("role") == "transfer"
        }
        assert amounts == {str(three), str(rest)}
        for row in outbox:
            if row["payload"].get("identity") in {"part-3", "part-rest"}:
                assert row["status"] == "superseded"
                assert row["payload"].get("superseded_by") == "delta-g09"
                assert row["status"] != "sent"
        assert [row["status"] for row in outbox if row["payload"].get("identity") == "already-sent"] == ["sent"]
        kinds = [item["kind"] for item in history.alerts_for(TENANT)]
        assert kinds.count("native_unit") == 2
        assert kinds.count("balance_delta") == 1
        assert history.conn.execute("SELECT COUNT(*) FROM balance_explanations").fetchone()[0] >= 2
        assert "kept" in history.conn.execute("SELECT payload FROM deliveries").fetchone()[0]

        history.commit_capability_cycle(
            TENANT, events=[], capability_rows=[], cursors=[], alerts=[alert_for(outside)],
        )
        outbox = history.outbox_for(TENANT)
        pending_kinds = [row["payload"]["kind"] for row in outbox if row["status"] == "pending"]
        assert pending_kinds.count("balance_delta") == 1
        assert pending_kinds.count("native_unit") == 1
        outside_rows = [row for row in outbox if row["payload"].get("identity") == "outside-window"]
        assert len(outside_rows) == 1
        assert outside_rows[0]["status"] == "pending"
        assert not any(
            row["status"] == "pending" and row["payload"].get("identity") in {"part-3", "part-rest"}
            for row in outbox
        )
        assert [row["status"] for row in outbox if row["payload"].get("identity") == "already-sent"] == ["sent"]
    finally:
        history.close()
        balance.close()

    later = Database(tmp_path / "already-pending.sqlite")
    try:
        _seed_delta(later, signed=DELTA)
        later.commit_capability_cycle(
            TENANT, events=[], capability_rows=[], cursors=[], alerts=[alert_for(part_3)],
        )
        pending_kinds = [row["payload"]["kind"] for row in later.outbox_for(TENANT) if row["status"] == "pending"]
        assert pending_kinds.count("balance_delta") == 1
        assert pending_kinds.count("native_unit") == 1
        later.commit_capability_cycle(
            TENANT, events=[], capability_rows=[], cursors=[], alerts=[alert_for(part_rest)],
        )
        outbox = later.outbox_for(TENANT)
        pending = [row for row in outbox if row["status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["payload"]["kind"] == "balance_delta"
        amounts = {
            item["raw_amount"]
            for item in pending[0]["payload"].get("components") or []
            if item.get("role") == "transfer"
        }
        assert amounts == {str(three), str(rest)}
        superseded = [row for row in outbox if row["status"] == "superseded"]
        assert len(superseded) == 1
        assert superseded[0]["payload"]["identity"] == "part-3"
        assert superseded[0]["payload"]["superseded_by"] == "delta-seed"
        assert superseded[0]["status"] != "sent"
        kinds = [item["kind"] for item in later.alerts_for(TENANT)]
        assert kinds.count("native_unit") == 2
        assert kinds.count("balance_delta") == 1
        assert later.conn.execute("SELECT COUNT(*) FROM balance_explanations").fetchone()[0] >= 2
    finally:
        later.close()


def _seed_delta(database: Database, *, signed: int) -> None:
    assert database.commit_native_observation(
        TENANT,
        wallet_id=WALLET,
        network="bsc-mainnet",
        asset=ASSET,
        expected_version=0,
        checkpoint=_checkpoint(10, ONE),
        continuity="baseline",
        retention_blocks=15,
        reset_checkpoints=False,
        alert=None,
        record_failure=None,
    ) == "committed"
    delta = {
        "alert": True,
        "asset": ASSET,
        "attribution": "unavailable",
        "dedup_key": "delta-seed",
        "direction": "increase",
        "kind": "balance_delta",
        "network": "bsc-mainnet",
        "new_checkpoint": {"block_hash": _hash(20), "block_number": "20"},
        "old_checkpoint": {"block_hash": _hash(10), "block_number": "10"},
        "signed_delta_raw": str(signed),
        "wallet_id": WALLET,
    }
    assert database.commit_native_observation(
        TENANT,
        wallet_id=WALLET,
        network="bsc-mainnet",
        asset=ASSET,
        expected_version=1,
        checkpoint=_checkpoint(20, ONE + signed),
        continuity="continuous",
        retention_blocks=15,
        reset_checkpoints=False,
        alert=delta,
        record_failure=None,
        decision=delta,
    ) == "committed"


def test_unknown_native_unit_key_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = _desk(tmp_path, monkeypatch)
    document = json.loads(config.read_text(encoding="utf-8"))
    document["alerts"]["native_unit_thresholds"] = {"not-a-network": "1.0"}
    config.write_text(json.dumps(document), encoding="utf-8")
    code = main(["run-once", "--mcm-config", str(config), "--no-send"], backend=ScriptBackend(), clock=lambda: CLOCK)
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["error_type"] == "ConfigError"
    assert "not-a-network" in payload["error"]
    assert "bsc-mainnet" in payload["error"]
    assert ADDRESS.lower() not in json.dumps(payload).lower()
