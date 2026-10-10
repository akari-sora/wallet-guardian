"""EXPL-01 through EXPL-08. Real run-once and explain entrypoints. Synthetic chain only.

Amounts are integer wei. The packaged bytecode hashes are specification pins.
No live provider is called. An explanation does not change the alert.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from eth_abi import decode, encode
from eth_hash.auto import keccak
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from guardian.chains.evm.accounting import pad_address
from guardian.cli import main
from guardian.explanations.discovery import FoundLog, Slot, replay
from guardian.explanations.lista import ListaExplainer
from guardian.explanations.models import canonical, sha256_text
from guardian.explanations.registry import load_manifest, load_packaged
from guardian.explanations.store import claim_job, enqueue, jobs_for, latest_revision, revision_count
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
SCHEMA = ROOT / "schemas" / "explanation.schema.json"
TENANT = "local"
OTHER_TENANT = "other"
WALLET = "11111111-1111-4111-8111-111111111111"
OWNER = "0x" + ("0" * 39) + "1"
OTHER = "0x" + ("0" * 39) + "2"
SUBMITTER = "0x" + ("0" * 36) + "0b07"
ASSET = "evm-native:56"
ONE = 10**18
SLIS = 8107363680000000000
PAY = 8421285170186643000
LEGACY_SLIS = 2 * 10**18
LEGACY_BNB = 10 * 10**18
LEGACY_TOTAL = 3 * 10**18
FLOOR = (LEGACY_BNB * LEGACY_SLIS) // LEGACY_TOTAL
CLOCK = 1_700_000_000.0
REQ = "0x" + "22" * 32
REQ_B = "0x" + "33" * 32
CLAIM = "0x" + "11" * 32


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


def _hash(number: int) -> str:
    return "0x" + f"{number:064x}"


def _word(value: int) -> str:
    return f"{value:064x}"


def _address_word(address: str) -> str:
    return "0x" + address.lower()[2:].rjust(64, "0")


def _spec_code(label: str) -> str:
    return "0x" + (bytes.fromhex("60806040") + label.encode()).hex()


def _ok(result: object) -> RawResponse:
    return RawResponse(200, {}, json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode())


def _err(message: str) -> RawResponse:
    body = {"error": {"code": -32000, "message": message}, "id": 1, "jsonrpc": "2.0"}
    return RawResponse(200, {}, json.dumps(body).encode())


def _has_latest(value: object) -> bool:
    if value == "latest":
        return True
    if isinstance(value, dict):
        return any(_has_latest(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_latest(item) for item in value)
    return False


def _block_hash(spec: object) -> str:
    if isinstance(spec, dict):
        digest = spec.get("blockHash")
        return str(digest).lower() if isinstance(digest, str) else ""
    return ""


class Sink:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: object) -> None:
        del message
        self.calls += 1


class ListaBackend:
    """Balance reads plus one synthetic Lista chain. Unknown methods return an empty result."""

    def __init__(self, scenario: str, state: Path) -> None:
        self.scenario = scenario
        self.state = state
        self.head = 100
        self.balance = ONE
        self.nonce = 7
        self.calls: list[dict] = []
        self.saw_latest = False
        self.sender = Sink()
        self.clock_box: list[float] | None = None
        self.on_claim = False
        self.lie: dict[int, str] = {}
        self.logs: list[dict] = []
        self.txs: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.storage: dict[tuple[str, str], str] = {}
        self.code: dict[tuple[str, str], str] = {}
        self.views: dict[tuple[str, str], str] = {}
        self.storage_error = False
        self.claim_mode = ""
        self.role = True
        self.latest_code = "0x"
        manifest = load_packaged()[0]
        self.proxy = manifest.proxy()
        self.claim_topic = str(manifest.event("ClaimWithdrawal(address,uint256,uint256)")["topic"]).lower()
        self.request_topic = str(manifest.event("RequestWithdraw(address,uint256)")["topic"]).lower()
        self.upgrade_topic = "0x" + keccak(b"Upgraded(address)").hex()
        self.queue_selector = _selector("getUserWithdrawalRequests(address)")
        self.status_selector = _selector("getUserRequestStatus(address,uint256)")
        self.bot_selector = _selector("getBotUndelegateRequest(uint256)")
        self.role_selector = _selector("hasRole(bytes32,address)")
        self.for_selector = str(manifest.call("claimWithdrawFor(address,uint256)")["selector"]).lower()
        self._build(manifest)

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        body = getattr(call, "body", b"") or b""
        if not body:
            return RawResponse(200, {}, b"{}")
        payload = json.loads(body.decode("utf-8"))
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        self.calls.append({"method": method, "params": params, "url": url})
        if _has_latest(params):
            self.saw_latest = True
        if method == "eth_chainId":
            return _ok("0x38")
        if method == "eth_blockNumber":
            return _ok(hex(self.head))
        if method == "eth_getBlockByNumber":
            return _ok(self._block(params[0] if params else None))
        if method == "eth_getBalance":
            return _ok(hex(self.balance))
        if method == "eth_getTransactionCount":
            return _ok(hex(self.nonce))
        if method == "eth_getLogs":
            return self._logs(params[0] if params else {})
        if method == "eth_getTransactionByHash":
            return _ok(self.txs.get(str(params[0]).lower()) if params else None)
        if method == "eth_getTransactionReceipt":
            return _ok(self.receipts.get(str(params[0]).lower()) if params else None)
        if method == "eth_getStorageAt":
            return self._storage(params)
        if method == "eth_getCode":
            return self._code(params)
        if method == "eth_call":
            return self._call(params)
        if method == "nr_getAssetTransfers":
            return _ok({"transfers": []})
        return _ok([])

    def _block(self, tag: object) -> dict:
        if tag in {"finalized", "latest", "safe"}:
            number = self.head
        elif isinstance(tag, str) and tag.startswith("0x"):
            number = int(tag, 16)
        else:
            number = self.head
        return {
            "hash": self.lie.get(number, _hash(number)),
            "number": hex(number),
            "parentHash": _hash(max(number - 1, 0)),
            "timestamp": hex(1_700_000_000),
        }

    def _logs(self, filt: object) -> RawResponse:
        if not isinstance(filt, dict):
            return _ok([])
        topics = filt.get("topics") if isinstance(filt.get("topics"), list) else []
        topic = str(topics[0]).lower() if topics else ""
        if topic == self.claim_topic:
            if self.on_claim:
                self._assert_alert_before_explanation()
            if self.claim_mode == "500":
                return RawResponse(500, {}, b"")
            if self.claim_mode == "raise":
                raise RuntimeError("https://bsc.example.invalid/rpc exploded")
            if self.claim_mode == "pruned":
                return _err("history has been pruned")
            if self.claim_mode == "429":
                return RawResponse(429, {}, b"")
        if "fromBlock" not in filt or "toBlock" not in filt:
            return _ok([])
        start = int(str(filt["fromBlock"]), 16)
        end = int(str(filt["toBlock"]), 16)
        rows = []
        for row in self.logs:
            if int(row["blockNumber"], 16) < start or int(row["blockNumber"], 16) > end:
                continue
            if str(row["topics"][0]).lower() != topic:
                continue
            if len(topics) > 1 and str(row["topics"][1]).lower() != str(topics[1]).lower():
                continue
            rows.append(row)
        return _ok(rows)

    def _storage(self, params: list) -> RawResponse:
        if self.clock_box is not None:
            self.clock_box[0] += 31
        if self.storage_error or len(params) < 3:
            return _err("missing trie node")
        key = (str(params[0]).lower(), _block_hash(params[2]))
        word = self.storage.get(key)
        if word is None:
            return _err("missing trie node")
        return _ok(word)

    def _code(self, params: list) -> RawResponse:
        if len(params) < 2:
            return _ok("0x")
        if _has_latest(params):
            return _ok(self.latest_code)
        return _ok(self.code.get((str(params[0]).lower(), _block_hash(params[1])), "0x"))

    def _call(self, params: list) -> RawResponse:
        if len(params) < 2 or not isinstance(params[0], dict):
            return _err("historical state is not available")
        data = str(params[0].get("data") or "")
        selector = data[:10].lower()
        if selector == self.role_selector:
            flag = encode(["bool"], [self.role])
            return _ok("0x" + flag.hex())
        found = self.views.get((selector, _block_hash(params[1])))
        if found is None:
            return _err("historical state is not available")
        return _ok(found)

    def _assert_alert_before_explanation(self) -> None:
        connection = sqlite3.connect(self.state / "state" / "monitor.sqlite", timeout=5)
        try:
            alerts = connection.execute("SELECT payload FROM alerts").fetchall()
            outbox = connection.execute("SELECT payload FROM outbox").fetchall()
        finally:
            connection.close()
        assert alerts
        assert outbox
        assert self.sender.calls == 0

    def _build(self, manifest) -> None:
        stored = next(row for row in manifest.intervals() if row["calculation"] == "stored_bnb")
        legacy = next(row for row in manifest.intervals() if row["calculation"] == "legacy_share")
        stored_impl = str(stored["implementation"]).lower()
        legacy_impl = str(legacy["implementation"]).lower()
        stored_code = _spec_code("lista-explainer-stored-bnb-v1")
        legacy_code = _spec_code("lista-explainer-legacy-share-v1")
        proxy_code = _spec_code("lista-explainer-erc1967-proxy-v1")
        assert "0x" + keccak(bytes.fromhex(stored_code[2:])).hex() == str(stored["code_hash"]).lower()
        assert "0x" + keccak(bytes.fromhex(legacy_code[2:])).hex() == str(legacy["code_hash"]).lower()
        assert "0x" + keccak(bytes.fromhex(proxy_code[2:])).hex() == manifest.proxy_code_hash()
        scenario = self.scenario
        old, new = (10, 4010) if scenario == "truncated" else (100, 110)
        self.old = old
        self.new = new
        claim_block = new
        request_block = new if scenario == "same_block" else 105
        slis = LEGACY_SLIS if scenario == "legacy" else SLIS
        payment = FLOOR if scenario == "legacy" else PAY
        if scenario == "many":
            payment = PAY
        self.payment = payment
        self.extra = 1
        impl = legacy_impl if scenario == "legacy" else stored_impl
        code = legacy_code if scenario == "legacy" else stored_code
        parent = _hash(claim_block - 1)
        claim_hash = _hash(claim_block)
        end_impl = impl
        if scenario == "earlier_upgrade":
            impl = legacy_impl
            end_impl = stored_impl
            code = stored_code
        elif scenario == "unresolved":
            end_impl = legacy_impl
        elif scenario == "same_tx_upgrade":
            end_impl = stored_impl
        if scenario == "storage_fail":
            self.storage_error = True
        if scenario == "pruned":
            self.claim_mode = "pruned"
        elif scenario == "http_500":
            self.claim_mode = "500"
        elif scenario == "explode":
            self.claim_mode = "raise"
        elif scenario == "rate_limit":
            self.claim_mode = "429"
        if scenario == "wrong_code":
            code = "0x1234"
        self._pin_code(end_impl if scenario in {"earlier_upgrade", "same_tx_upgrade"} else impl, claim_hash, code)
        self._pin_code(self.proxy, claim_hash, proxy_code)
        self.storage[(self.proxy, parent)] = _address_word(impl)
        self.storage[(self.proxy, claim_hash)] = _address_word(end_impl)
        beneficiary = OTHER if scenario == "wrong_beneficiary" else OWNER
        status = "0x0" if scenario == "failed_payment" else "0x1"
        self.role = scenario != "role_false"
        if scenario == "two":
            self._add_request(REQ, request_block, 4, slis)
            self._add_request(REQ_B, 106, 5, slis)
            self._add_claim(1, payment, status, beneficiary, tx_index=1)
            queue = [(10, slis, 1), (11, slis, 2)]
        elif scenario == "many":
            queue = [(index + 1, slis, 1) for index in range(20)]
            self._add_claim(0, payment, status, beneficiary, tx_index=1)
        elif scenario in {"empty", "truncated", "no_claim", "pruned"}:
            queue = [] if scenario != "no_claim" else [(42, slis, 1)]
        else:
            if scenario != "same_block":
                self._add_request(REQ, request_block, 4, slis)
            else:
                self._add_request(REQ, claim_block, 3, slis, tx_index=0)
            self._add_claim(0, payment, status, beneficiary, tx_index=1)
            queue = [] if scenario == "same_block" else [(7 if scenario == "legacy" else 42, slis, 1)]
        if scenario == "earlier_upgrade":
            self._add_upgrade(stored_impl, 0)
        elif scenario == "same_tx_upgrade":
            self._add_upgrade(stored_impl, 1)
        self.views[(self.queue_selector, _hash(old))] = _encode_queue([])
        self.views[(self.queue_selector, parent)] = _encode_queue(queue if scenario != "many" else queue)
        self.views[(self.queue_selector, claim_hash)] = _encode_queue(queue if scenario == "no_claim" else [])
        if scenario == "many":
            self.views[(self.queue_selector, _hash(old))] = _encode_queue(queue)
            self.views[(self.queue_selector, parent)] = _encode_queue(queue)
        expected = PAY + 9 if scenario == "amount_mismatch" else payment
        if scenario == "legacy":
            self.views[(self.bot_selector, parent)] = "0x" + encode(
                ["uint256", "uint256", "uint256", "uint256"], [1, 2, LEGACY_BNB, LEGACY_TOTAL],
            ).hex()
        else:
            self.views[(self.status_selector, parent)] = "0x" + encode(["bool", "uint256"], [True, expected]).hex()

    def _pin_code(self, address: str, block_hash: str, code: str) -> None:
        self.code[(address.lower(), block_hash)] = code

    def _add_request(self, tx: str, block: int, log_index: int, slis: int, tx_index: int = 0) -> None:
        self.logs.append({
            "address": self.proxy,
            "blockHash": _hash(block),
            "blockNumber": hex(block),
            "data": "0x" + _word(slis),
            "logIndex": hex(log_index),
            "topics": [self.request_topic, pad_address(OWNER)],
            "transactionHash": tx,
            "transactionIndex": hex(tx_index),
        })

    def _add_claim(self, index: int, payment: int, status: str, beneficiary: str, tx_index: int) -> None:
        row = {
            "address": self.proxy,
            "blockHash": _hash(self.new if self.scenario != "truncated" else self.new),
            "blockNumber": hex(self.new),
            "data": "0x" + _word(index) + _word(payment),
            "logIndex": hex(7),
            "topics": [self.claim_topic, pad_address(OWNER)],
            "transactionHash": CLAIM,
            "transactionIndex": hex(tx_index),
        }
        if self.scenario not in {"empty", "truncated", "no_claim"}:
            self.logs.append(row)
        payload = encode(["address", "uint256"], [beneficiary, index])
        self.txs[CLAIM] = {
            "from": SUBMITTER,
            "hash": CLAIM,
            "input": self.for_selector + payload.hex(),
            "to": self.proxy,
        }
        self.receipts[CLAIM] = {"logs": [row], "status": status, "transactionHash": CLAIM}

    def _add_upgrade(self, implementation: str, tx_index: int) -> None:
        self.logs.append({
            "address": self.proxy,
            "blockHash": _hash(self.new),
            "blockNumber": hex(self.new),
            "data": "0x",
            "logIndex": hex(1),
            "topics": [self.upgrade_topic, pad_address(implementation)],
            "transactionHash": "0x" + "44" * 32,
            "transactionIndex": hex(tx_index),
        })


def _encode_queue(rows: list[tuple[int, int, int]]) -> str:
    return "0x" + encode(["(uint256,uint256,uint256)[]"], [rows]).hex()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = []
    document["enabled_networks"] = ["bsc-mainnet"]
    document["routing"] = [route for route in document["routing"] if route.get("network") == "bsc-mainnet"]
    document["polling"]["bootstrap_origin"] = 1
    document["alerts"]["native_unit_thresholds"] = {"bsc-mainnet": "1.0"}
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    _write_private(tmp_path / "accounts.json", json.dumps({
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
        "accounts": [{
            "address": to_checksum_address(OWNER),
            "family": "evm",
            "label": "Wallet A",
            "networks": ["bsc-mainnet"],
            "own_group": "desk",
            "relationship": "owned_user_declared",
            "wallet_id": WALLET,
        }],
    }) + "\n")
    _write_private(tmp_path / "secrets.json", json.dumps({
        "RPC_BSC_MAINNET_URL": "https://bsc.example.invalid/rpc",
        "RPC_ETHEREUM_MAINNET_URL": "https://ethereum.example.invalid/rpc",
        "NODEREAL_BSC_URL": "https://nodereal.example.invalid/bsc",
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "COINGECKO_API_KEY": "synthetic-price-key",
        "GOPLUS_API_BASE": "https://goplus.example.invalid/api",
        "OFAC_SLS_FEED": "https://ofac.example.invalid/feed",
    }) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("NODEREAL_API_KEY", raising=False)
    return config


def _run(config: Path, backend: ListaBackend, capsys: pytest.CaptureFixture[str], clock=None, sender=None) -> dict:
    code = main(
        ["run-once", "--mcm-config", str(config), "--no-send"],
        backend=backend,
        sender=sender if sender is not None else backend.sender,
        clock=clock or (lambda: CLOCK),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert OWNER not in json.dumps(payload).lower()
    return payload


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state" / "monitor.sqlite")


def _deltas(tmp_path: Path) -> list[dict]:
    database = _db(tmp_path)
    try:
        rows = database.outbox_for(TENANT)
    finally:
        database.close()
    return [row["payload"] for row in rows if row["payload"].get("kind") == "balance_delta"]


def _stored(tmp_path: Path) -> tuple[dict, int, str]:
    database = _db(tmp_path)
    try:
        found = jobs_for(database, TENANT)
        assert len(found) == 1
        event_id = found[0]["event_id"]
        body = latest_revision(database, TENANT, event_id)
        count = revision_count(database, TENANT, event_id)
    finally:
        database.close()
    assert body is not None
    return body, count, event_id


def _cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scenario: str, **backend_flags) -> tuple[ListaBackend, dict]:
    config = _desk(tmp_path, monkeypatch)
    backend = ListaBackend(scenario, tmp_path)
    for name, value in backend_flags.items():
        setattr(backend, name, value)
    backend.head = backend.old
    backend.balance = ONE
    first = _run(config, backend, capsys)
    assert first["baseline"] is True
    assert _deltas(tmp_path) == []
    backend.head = backend.new
    backend.balance = ONE + backend.payment + backend.extra
    _run(config, backend, capsys)
    body, _count, _event = _stored(tmp_path)
    return backend, body


def _claim_calls(backend: ListaBackend) -> list[dict]:
    found = []
    for call in backend.calls:
        if call["method"] != "eth_getLogs":
            continue
        params = call["params"]
        if not params or not isinstance(params[0], dict):
            continue
        topics = params[0].get("topics") or []
        if topics and str(topics[0]).lower() == backend.claim_topic:
            found.append(call)
    return found


def test_expl_01_alert_is_durable_before_claim_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-01. The alert and outbox exist before claim logs, and a failed read keeps the alert."""
    backend, body = _cycle(tmp_path, monkeypatch, capsys, "match", on_claim=True)
    assert body["verdict"] == "MATCHES_OWN_REQUEST", body["gaps"]
    assert backend.on_claim is True
    failed = tmp_path / "failed"
    failed.mkdir()
    config = _desk(failed, monkeypatch)
    broken = ListaBackend("explode", failed)
    broken.head = broken.old
    broken.balance = ONE
    _run(config, broken, capsys)
    broken.head = broken.new
    broken.balance = ONE + broken.payment + 1
    _run(config, broken, capsys)
    payload = _deltas(failed)[0]
    assert payload["attribution"] == "unavailable"
    assert payload["explained_by"] == []
    assert "Lista" not in json.dumps(payload)
    exploded, _count, _event = _stored(failed)
    assert exploded["verdict"] != "MATCHES_OWN_REQUEST"
    assert "bsc.example.invalid" not in json.dumps(exploded)


def test_expl_02_parties_amounts_and_residual(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-02. Parties, wei strings, the settlement ratio, and a non-zero residual."""
    backend, body = _cycle(tmp_path, monkeypatch, capsys, "match")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert set(body) == set(schema["properties"])
    assert body["funds_marked_safe"] is False
    assert body["severity_changed"] is False
    assert body["labels"]["counterparty_identity"] == "unverified"
    assert body["operator_control"] == "not_established"
    assert body["parties"]["tx_submitter"] == SUBMITTER
    assert body["parties"]["effective_caller"] == SUBMITTER
    assert body["parties"]["request_account"] == OWNER
    assert body["parties"]["payout_recipient"] == OWNER
    assert body["parties"]["tx_submitter"] != body["parties"]["request_account"]
    assert body["amounts"]["requested_slis_amount"] == str(SLIS)
    assert body["amounts"]["actual_claim_payment"] == str(PAY)
    assert body["amounts"]["contract_expected_claim_amount"] == str(PAY)
    assert body["amounts"]["effective_settlement_ratio"] == f"{PAY}/{SLIS}"
    assert body["amounts"]["burned_slis_amount"] is None
    assert body["facts"]["burned_slis"] == "not_established"
    assert body["facts"]["caller_bot_role"] == "verified"
    assert body["facts"]["claim_time_rate"] == "not_queried"
    assert body["residual"]["raw_amount"] == "1"
    assert body["request_identity"] == f"eip155:56:{backend.proxy}:{OWNER}:{REQ}:4"
    assert ":0:" not in str(body["request_identity"])
    assert backend.saw_latest is False
    alert = _deltas(tmp_path)[0]
    assert alert["attribution"] == "unavailable"
    assert alert["explained_by"] == []
    assert OWNER not in json.dumps(alert).lower()


def test_expl_03_idempotent_refresh_reorg_and_single_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-03. A repeat is idle, refresh keeps one row, a reorg supersedes, and one claim wins."""
    config = _desk(tmp_path, monkeypatch)
    backend = ListaBackend("match", tmp_path)
    backend.head = backend.old
    backend.balance = ONE
    _run(config, backend, capsys)
    backend.head = backend.new
    backend.balance = ONE + backend.payment + backend.extra
    _run(config, backend, capsys)
    body, count, event_id = _stored(tmp_path)
    assert body["verdict"] == "MATCHES_OWN_REQUEST"
    assert count == 1
    code_calls = [call for call in backend.calls if call["method"] == "eth_getCode"]
    assert code_calls
    _run(config, backend, capsys)
    assert [call for call in backend.calls if call["method"] == "eth_getCode"] == code_calls
    quiet, quiet_count, _event = _stored(tmp_path)
    assert quiet["revision"] == body["revision"]
    assert quiet_count == 1
    offline = main(
        ["explain", "--db", str(tmp_path / "state" / "monitor.sqlite"), "--tenant", TENANT, "--event-id", event_id],
    )
    shown = json.loads(capsys.readouterr().out)
    assert offline == 0
    assert shown["explanation"]["verdict"] == "MATCHES_OWN_REQUEST"
    assert [call for call in backend.calls if call["method"] == "eth_getCode"] == code_calls
    missing = main(["explain", "--event-id", event_id, "--refresh"])
    assert missing == 2
    capsys.readouterr()
    refreshed = main(
        ["explain", "--mcm-config", str(config), "--event-id", event_id, "--refresh"],
        backend=backend,
        clock=lambda: CLOCK,
    )
    refresh_body = json.loads(capsys.readouterr().out)
    assert refreshed == 0
    assert refresh_body["explanation"]["verdict"] == "MATCHES_OWN_REQUEST"
    _body, after, _event = _stored(tmp_path)
    assert after == 1
    assert len([call for call in backend.calls if call["method"] == "eth_getCode"]) > len(code_calls)
    backend.lie[backend.new] = _hash(999)
    backend.head = backend.new + 10
    _run(config, backend, capsys)
    revised, revisions, _event = _stored(tmp_path)
    assert revisions >= 2
    assert revised["verdict"] == "UNVERIFIED"
    assert "reorg" in revised["gaps"]
    assert revised["supersedes"] == body["revision"]
    assert revised["funds_marked_safe"] is False

    race = tmp_path / "race.sqlite"
    database = Database(race)
    try:
        enqueue(database, TENANT, "event-a", account=OWNER, network="bsc-mainnet", wallet_id=WALLET, payload={"dedup_key": "event-a"})
        enqueue(database, OTHER_TENANT, "event-a", account=OTHER, network="bsc-mainnet", wallet_id=WALLET, payload={"dedup_key": "event-a"})
        assert [row["tenant_id"] for row in jobs_for(database, TENANT)] == [TENANT]
    finally:
        database.close()
    wins: list[bool] = []

    def _claim() -> None:
        own = Database(race)
        try:
            wins.append(claim_job(own, TENANT, "event-a"))
        finally:
            own.close()

    threads = [threading.Thread(target=_claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wins.count(True) == 1


def test_expl_04_swap_and_pop_and_legacy_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-04. Index 1 selects the second request. Legacy BNB uses floor division."""
    nearest = (LEGACY_BNB * LEGACY_SLIS + LEGACY_TOTAL // 2) // LEGACY_TOTAL
    assert FLOOR == 6666666666666666666
    assert FLOOR != nearest
    first = FoundLog("request", "0x1", OWNER, REQ, 1, 0, 1, (SLIS,), {})
    second = FoundLog("request", "0x1", OWNER, REQ_B, 2, 0, 2, (SLIS,), {})
    claim = FoundLog("claim", "0x1", OWNER, CLAIM, 3, 0, 3, (1, PAY), {})
    links, _queue = replay([], [first, second, claim])
    assert links[0].slot is not None
    assert links[0].slot.tx_id == REQ_B
    _backend, body = _cycle(tmp_path, monkeypatch, capsys, "two")
    assert body["verdict"] == "MATCHES_OWN_REQUEST"
    assert body["request_identity"].endswith(f":{REQ_B}:5")
    assert REQ not in body["request_identity"]
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    _legacy, legacy = _cycle(legacy_root, monkeypatch, capsys, "legacy")
    assert legacy["verdict"] == "MATCHES_OWN_REQUEST"
    assert legacy["amounts"]["contract_expected_claim_amount"] == str(FLOOR)
    assert legacy["amounts"]["actual_claim_payment"] == str(FLOOR)
    assert legacy["calculation_version"] == "legacy_share"
    same = tmp_path / "same"
    same.mkdir()
    _same, partial = _cycle(same, monkeypatch, capsys, "same_block")
    assert partial["verdict"] == "PARTIAL"
    assert partial["verdict"] != "MATCHES_OWN_REQUEST"


def test_expl_05_false_no_match_and_real_contradictions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-05. A missing read is unverified. A contradicted payment is NO_MATCH. A bad amount is partial."""
    expectations = {
        "empty": "empty_request_array",
        "truncated": "truncated_logs",
        "pruned": "pruned_logs",
        "no_claim": "no_claim_in_interval",
    }
    for scenario, gap in expectations.items():
        root = tmp_path / scenario
        root.mkdir()
        backend, body = _cycle(root, monkeypatch, capsys, scenario)
        assert body["verdict"] == "UNVERIFIED"
        assert body["verdict"] != "NO_MATCH"
        assert gap in body["gaps"]
        if scenario == "truncated":
            assert _claim_calls(backend) == []
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    _backend, rejected = _cycle(wrong, monkeypatch, capsys, "wrong_beneficiary")
    assert rejected["verdict"] == "NO_MATCH"
    assert "beneficiary_contradiction" in rejected["contradiction_reasons"]
    failed = tmp_path / "failed-pay"
    failed.mkdir()
    _backend, unpaid = _cycle(failed, monkeypatch, capsys, "failed_payment")
    assert unpaid["verdict"] == "NO_MATCH"
    assert "failed_payment" in unpaid["contradiction_reasons"]
    mismatch = tmp_path / "mismatch"
    mismatch.mkdir()
    _backend, partial = _cycle(mismatch, monkeypatch, capsys, "amount_mismatch")
    assert partial["verdict"] == "PARTIAL"
    assert "amount_inconsistent" in partial["gaps"]
    assert partial["verdict"] != "NO_MATCH"


def test_expl_06_execution_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-06. Historical code can match. A same-transaction upgrade, a gap, or a bad hash cannot."""
    backend, body = _cycle(tmp_path, monkeypatch, capsys, "match")
    assert body["verdict"] == "MATCHES_OWN_REQUEST"
    assert body["facts"]["implementation_at_execution"] == "verified"
    assert backend.saw_latest is False
    assert backend.latest_code == "0x"
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    _backend, upgraded = _cycle(earlier, monkeypatch, capsys, "earlier_upgrade")
    assert upgraded["verdict"] == "MATCHES_OWN_REQUEST"
    for scenario in ("same_tx_upgrade", "unresolved", "wrong_code", "storage_fail"):
        root = tmp_path / scenario
        root.mkdir()
        chain, document = _cycle(root, monkeypatch, capsys, scenario)
        assert document["verdict"] != "MATCHES_OWN_REQUEST"
        assert document["facts"]["implementation_at_execution"] != "verified"
        assert chain.saw_latest is False
        assert any(
            gap in document["gaps"]
            for gap in ("implementation_at_execution_unverified", "code_hash_mismatch", "proxy_code_mismatch")
        )
    packaged = load_packaged()[0]
    stale = dict(packaged.document)
    stale["abi_hash"] = "0" * 64
    assert load_manifest(stale) is None
    extra = json.loads(json.dumps(packaged.document))
    extra["abi"]["calls"].append({"selector": "0x8456cb59", "signature": "pause()"})
    extra["abi_hash"] = sha256_text(canonical(extra["abi"]))
    extra["manifest_hash"] = sha256_text(canonical({key: value for key, value in extra.items() if key != "manifest_hash"}))
    assert load_manifest(extra) is None
    monkeypatch.setattr("guardian.explanations.engine.load_packaged", lambda: [])
    blinded = tmp_path / "stale"
    blinded.mkdir()
    _chain, unverified = _cycle(blinded, monkeypatch, capsys, "match")
    assert unverified["verdict"] == "UNVERIFIED"
    assert "stale_abi" in unverified["gaps"]


def test_expl_07_budgets_and_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-07. Call and time limits stop the verdict. A 429 is not repeated until refresh."""
    _backend, limited = _cycle(tmp_path, monkeypatch, capsys, "many")
    assert limited["verdict"] != "MATCHES_OWN_REQUEST"
    assert "provider_call_budget" in limited["gaps"]
    timed = tmp_path / "timed"
    timed.mkdir()
    config = _desk(timed, monkeypatch)
    box = [CLOCK]
    chain = ListaBackend("match", timed)
    chain.clock_box = box

    def clock() -> float:
        return box[0]

    chain.head = chain.old
    chain.balance = ONE
    _run(config, chain, capsys, clock=clock)
    chain.head = chain.new
    chain.balance = ONE + chain.payment + chain.extra
    _run(config, chain, capsys, clock=clock)
    document, _count, _event = _stored(timed)
    assert document["verdict"] != "MATCHES_OWN_REQUEST"
    assert "deadline" in document["gaps"]
    held = tmp_path / "held"
    held.mkdir()
    held_config = _desk(held, monkeypatch)
    limited_chain = ListaBackend("rate_limit", held)
    limited_chain.head = limited_chain.old
    limited_chain.balance = ONE
    _run(held_config, limited_chain, capsys)
    limited_chain.head = limited_chain.new
    limited_chain.balance = ONE + limited_chain.payment + 1
    _run(held_config, limited_chain, capsys)
    first_logs = len(_claim_calls(limited_chain))
    assert first_logs == 1
    held_body, _count, event_id = _stored(held)
    assert held_body["verdict"] != "MATCHES_OWN_REQUEST"
    assert "rate_limited" in held_body["gaps"]
    limited_chain.head = limited_chain.new + 1
    limited_chain.claim_mode = ""
    _run(held_config, limited_chain, capsys)
    assert len(_claim_calls(limited_chain)) == first_logs
    main(
        ["explain", "--mcm-config", str(held_config), "--event-id", event_id, "--refresh"],
        backend=limited_chain,
        clock=lambda: CLOCK,
    )
    capsys.readouterr()
    assert len(_claim_calls(limited_chain)) > first_logs


def test_expl_08_explanation_does_not_touch_the_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """EXPL-08. Severity, attribution, and explained_by stay put. The secret URL is not stored."""
    _backend, body = _cycle(tmp_path, monkeypatch, capsys, "role_false")
    assert body["verdict"] == "MATCHES_OWN_REQUEST"
    assert body["facts"]["caller_bot_role"] == "contradicted"
    assert body["severity_changed"] is False
    assert body["funds_marked_safe"] is False
    blob = json.dumps(body)
    assert "bsc.example.invalid" not in blob
    assert "synthetic-price-key" not in blob
    alert = _deltas(tmp_path)[0]
    assert alert["explained_by"] == []
    assert alert["attribution"] == "unavailable"
    assert alert["cause"] == "unknown"
    assert "Lista" not in json.dumps(alert)
    plugin = ListaExplainer()
    assert plugin.matches({"kind": "balance_delta", "direction": "increase", "network": "bsc-mainnet", "asset": ASSET}) is True
    assert plugin.matches({"kind": "balance_delta", "direction": "decrease", "network": "bsc-mainnet", "asset": ASSET}) is False
    assert plugin.matches({"kind": "balance_delta", "direction": "increase", "network": "ethereum-mainnet", "asset": "evm-native:1"}) is False


def test_expl_queue_roundtrip() -> None:
    """The empty and populated queue encodings are what the explainer decodes."""
    empty = _encode_queue([])
    populated = _encode_queue([(42, SLIS, 7)])
    assert list(decode(["(uint256,uint256,uint256)[]"], bytes.fromhex(empty[2:]))[0]) == []
    assert list(decode(["(uint256,uint256,uint256)[]"], bytes.fromhex(populated[2:]))[0]) == [(42, SLIS, 7)]
