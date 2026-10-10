"""EXPL-01 through EXPL-08. Real run-once and explain entrypoints. Synthetic chain only.

Amounts are integer wei. The packaged bytecode hashes are specification pins.
No live provider is called. An explanation does not change the alert.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
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
from guardian.explanations.engine import note_increase, recover_missing_jobs
from guardian.explanations.store import (
    claim_job,
    complete_job,
    enqueue,
    failure_reasons,
    force_pending,
    job_for,
    jobs_for,
    latest_revision,
    load_interval,
    load_lookback,
    load_requests,
    release_process_claims,
    revision_count,
    save_interval,
    save_lookback,
    save_requests,
    supersede_checkpoint,
)
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
SCHEMA = ROOT / "schemas" / "explanation.schema.json"
APPROVED = Path(__file__).resolve().parents[1] / "fixtures" / "explainers" / "lista-stakemanager-approved.json"
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
        self.report_printed = False
        self.deadline_on_status = False
        self.advance_on_storage = True
        self.alert_rows: list[tuple[str, ...]] | None = None
        manifest = load_manifest(json.loads(APPROVED.read_text(encoding="utf-8")))
        assert manifest is not None
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
        if self.scenario == "removed_upgrade" and topic == self.upgrade_topic:
            return _ok([
                row for row in self.logs
                if str(row["topics"][0]).lower() == topic
            ])
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
        if self.clock_box is not None and self.advance_on_storage:
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
        if selector == self.status_selector and self.deadline_on_status and self.clock_box is not None:
            self._snapshot_alerts()
            self.clock_box[0] = CLOCK + 31
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
        assert self.report_printed is True

    def mark_report_printed(self) -> None:
        self.report_printed = True

    def _snapshot_alerts(self) -> None:
        connection = sqlite3.connect(self.state / "state" / "monitor.sqlite", timeout=5)
        try:
            self.alert_rows = connection.execute("SELECT payload FROM alerts ORDER BY id").fetchall()
        finally:
            connection.close()

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
        if scenario == "wrong_amount_lookback":
            old, new = 5000, 5010
        elif scenario == "truncated":
            old, new = 10, 4010
        else:
            old, new = 100, 110
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
        elif scenario == "slot_conflict":
            impl = legacy_impl
            end_impl = legacy_impl
            code = stored_code
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
        elif scenario == "older_request":
            self._add_request(REQ, 95, 4, slis)
            self._add_claim(0, payment, status, beneficiary, tx_index=1)
            queue = [(42, slis, 1)]
        elif scenario == "wrong_amount_lookback":
            self._add_request(REQ_B, 4500, 4, slis)
            self._add_historical_claim(4600, 0, payment)
            self._add_claim(0, payment, status, beneficiary, tx_index=1)
            queue = [(42, slis, 1)]
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
        elif scenario == "slot_conflict":
            self._add_upgrade(stored_impl, 0)
        elif scenario == "removed_upgrade":
            self._add_upgrade(stored_impl, 0, removed=True, address=OTHER, block=self.new - 3)
        queued_at_old = scenario in {"older_request", "wrong_amount_lookback"}
        self.views[(self.queue_selector, _hash(old))] = _encode_queue(queue if queued_at_old else [])
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

    def _add_historical_claim(self, block: int, index: int, payment: int) -> None:
        """A claim that is already in history. It is not the transaction under explanation."""
        self.logs.append({
            "address": self.proxy,
            "blockHash": _hash(block),
            "blockNumber": hex(block),
            "data": "0x" + _word(index) + _word(payment),
            "logIndex": hex(8),
            "topics": [self.claim_topic, pad_address(OWNER)],
            "transactionHash": "0x" + "55" * 32,
            "transactionIndex": hex(1),
        })

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
            "blockHash": row["blockHash"],
            "blockNumber": row["blockNumber"],
            "from": SUBMITTER,
            "hash": CLAIM,
            "input": self.for_selector + payload.hex(),
            "to": self.proxy,
            "transactionIndex": hex(tx_index),
        }
        receipt_log = dict(row)
        self.receipt_log = receipt_log
        self.receipts[CLAIM] = {
            "blockHash": row["blockHash"],
            "blockNumber": row["blockNumber"],
            "logs": [receipt_log],
            "status": status,
            "transactionHash": CLAIM,
            "transactionIndex": hex(tx_index),
        }

    def _add_upgrade(
        self,
        implementation: str,
        tx_index: int,
        *,
        removed: bool = False,
        address: str | None = None,
        block: int | None = None,
    ) -> None:
        number = self.new if block is None else block
        self.logs.append({
            "address": self.proxy if address is None else address,
            "blockHash": _hash(number),
            "blockNumber": hex(number),
            "data": "0x",
            "logIndex": hex(1),
            "removed": removed,
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


def _approved_manifest():
    manifest = load_manifest(json.loads(APPROVED.read_text(encoding="utf-8")))
    assert manifest is not None
    return manifest


def _inject_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _approved_manifest()
    monkeypatch.setattr("guardian.explanations.engine.load_packaged", lambda: [manifest])


def _cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    *,
    approved: bool = True,
    mutate=None,
    **backend_flags,
) -> tuple[ListaBackend, dict]:
    if approved:
        _inject_approved(monkeypatch)
    config = _desk(tmp_path, monkeypatch)
    backend = ListaBackend(scenario, tmp_path)
    for name, value in backend_flags.items():
        setattr(backend, name, value)
    if mutate is not None:
        mutate(backend)
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
    _inject_approved(monkeypatch)
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
            assert _claim_calls(backend)
            assert _job(root)["state"] == "retry"
            seen = len(_claim_calls(backend))
            _run(_desk(root, monkeypatch), backend, capsys)
            assert len(_claim_calls(backend)) > seen
            continued, _count, _event = _stored(root)
            assert continued["verdict"] != "NO_MATCH"
            assert "request_identity_incomplete" not in continued["gaps"]
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
    _chain, unverified = _cycle(blinded, monkeypatch, capsys, "match", approved=False)
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


def _job(tmp_path: Path) -> dict:
    database = _db(tmp_path)
    try:
        found = jobs_for(database, TENANT)
        assert len(found) == 1
        return found[0]
    finally:
        database.close()


def _alerts(tmp_path: Path) -> list[tuple]:
    connection = sqlite3.connect(tmp_path / "state" / "monitor.sqlite", timeout=5)
    try:
        return connection.execute("SELECT payload FROM alerts ORDER BY id").fetchall()
    finally:
        connection.close()


def test_expl_fix_claim_evidence_is_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A receipt that disagrees with the discovered claim is not a verified payment."""

    def amount_lie(backend: ListaBackend) -> None:
        lied = dict(backend.receipt_log)
        lied["data"] = "0x" + _word(0) + _word(1)
        backend.receipts[CLAIM]["logs"] = [lied]

    def foreign_emitter(backend: ListaBackend) -> None:
        lied = dict(backend.receipt_log)
        lied["address"] = OTHER
        backend.receipts[CLAIM]["logs"] = [lied]

    def removed_log(backend: ListaBackend) -> None:
        lied = dict(backend.receipt_log)
        lied["removed"] = True
        backend.receipts[CLAIM]["logs"] = [lied]

    def missing_status(backend: ListaBackend) -> None:
        backend.receipts[CLAIM].pop("status")

    def wrong_index(backend: ListaBackend) -> None:
        payload = encode(["address", "uint256"], [OWNER, 7])
        backend.txs[CLAIM]["input"] = backend.for_selector + payload.hex()

    def duplicate(backend: ListaBackend) -> None:
        backend.logs.append(dict(backend.logs[-1]))

    def hash_conflict(backend: ListaBackend) -> None:
        backend.receipts[CLAIM]["transactionHash"] = "0x" + "99" * 32

    def missing_block(backend: ListaBackend) -> None:
        backend.receipts[CLAIM].pop("blockHash")

    cases = (
        ("amount", amount_lie, "claim_evidence_mismatch"),
        ("emitter", foreign_emitter, "claim_evidence_mismatch"),
        ("removed", removed_log, "claim_evidence_mismatch"),
        ("index", wrong_index, "request_index_mismatch"),
        ("hash", hash_conflict, "receipt_identity_mismatch"),
        ("checkpoint", missing_block, "receipt_checkpoint_unverified"),
    )
    for name, mutate, gap in cases:
        root = tmp_path / name
        root.mkdir()
        _backend, body = _cycle(root, monkeypatch, capsys, "match", mutate=mutate)
        assert body["verdict"] != "MATCHES_OWN_REQUEST"
        assert body["verdict"] != "NO_MATCH"
        assert gap in body["gaps"]
        assert body["amounts"]["actual_claim_payment"] != str(PAY)
    missing = tmp_path / "status"
    missing.mkdir()
    _backend, unpaid = _cycle(missing, monkeypatch, capsys, "match", mutate=missing_status)
    assert unpaid["verdict"] != "NO_MATCH"
    assert unpaid["verdict"] != "MATCHES_OWN_REQUEST"
    assert "receipt_status_unavailable" in unpaid["gaps"]
    assert "failed_payment" not in unpaid["contradiction_reasons"]
    copied = tmp_path / "duplicate"
    copied.mkdir()
    _backend, once = _cycle(copied, monkeypatch, capsys, "match", mutate=duplicate)
    assert once["verdict"] == "MATCHES_OWN_REQUEST"
    assert once["residual"]["explained_raw_amount"] == str(PAY)
    assert once["residual"]["raw_amount"] == "1"


def test_expl_fix_packaged_intervals_stay_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Synthetic deployment pins are a test fixture. The packaged manifest does not verify them."""
    packaged = load_packaged()
    assert len(packaged) == 1
    assert packaged[0].intervals() == []
    assert packaged[0].document["source"]["live_bytecode_verified"] is False
    root = tmp_path / "packaged"
    root.mkdir()
    _backend, body = _cycle(root, monkeypatch, capsys, "match", approved=False)
    assert body["verdict"] != "MATCHES_OWN_REQUEST"
    assert body["facts"]["implementation_at_execution"] != "verified"
    conflict = tmp_path / "slot"
    conflict.mkdir()
    _backend, slot = _cycle(conflict, monkeypatch, capsys, "slot_conflict")
    assert slot["verdict"] != "MATCHES_OWN_REQUEST"
    assert slot["facts"]["implementation_at_execution"] != "verified"
    removed = tmp_path / "removed-upgrade"
    removed.mkdir()
    _backend, upgrade = _cycle(removed, monkeypatch, capsys, "removed_upgrade")
    assert upgrade["verdict"] != "MATCHES_OWN_REQUEST"
    assert upgrade["facts"]["implementation_at_execution"] != "verified"


def test_expl_fix_older_request_is_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A request older than the balance interval keeps its transaction identity."""
    backend, body = _cycle(tmp_path, monkeypatch, capsys, "older_request")
    assert body["request_identity"] is not None
    assert body["request_identity"].endswith(f":{REQ}:4")
    assert "request_identity_incomplete" not in body["gaps"]
    assert body["verdict"] == "MATCHES_OWN_REQUEST"
    assert _job(tmp_path)["state"] == "done"
    database = _db(tmp_path)
    try:
        account = jobs_for(database, TENANT)[0]["account"]
        stored = load_requests(database, TENANT, "bsc-mainnet", account, backend.proxy)
    finally:
        database.close()
    assert any(row["tx_id"] == REQ and int(row["log_index"]) == 4 for row in stored)


def test_expl_fix_internal_native_trigger(tmp_path: Path) -> None:
    """An evidenced internal native credit is queued beside balance-delta increases."""
    plugin = ListaExplainer()
    alert = {
        "asset": ASSET,
        "dedup_key": "internal-credit",
        "direction": "in",
        "kind": "native_unit",
        "network": "bsc-mainnet",
        "transfer_origin": "evm_internal",
        "wallet_id": WALLET,
    }
    assert plugin.matches(alert) is True
    assert plugin.matches({"kind": "native_eth", "direction": "out", "transfer_origin": "evm_internal", "network": "bsc-mainnet", "asset": ASSET}) is False
    database = Database(tmp_path / "internal.sqlite")
    try:
        note_increase(database, TENANT, {"account": OWNER}, alert)
        found = jobs_for(database, TENANT)
        assert len(found) == 1
        assert found[0]["event_id"] == "internal-credit"
        assert found[0]["account"] == OWNER
    finally:
        database.close()


def test_expl_fix_restart_after_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """An expired lease is abandonment. Clearing process memory is not."""
    config = _desk(tmp_path, monkeypatch)
    event_id = "restart-claim"
    database = _db(tmp_path)
    try:
        enqueue(
            database, TENANT, event_id,
            account=OWNER, network="bsc-mainnet", wallet_id=WALLET,
            payload={
                "asset": ASSET,
                "dedup_key": event_id,
                "direction": "increase",
                "kind": "balance_delta",
                "network": "bsc-mainnet",
                "wallet_id": WALLET,
            },
        )
        assert claim_job(database, TENANT, event_id) is True
        release_process_claims()
        database.conn.execute(
            "UPDATE explanation_job_leases SET expires_at=? WHERE tenant_id=? AND event_id=?",
            ("2000-01-01T00:00:00Z", TENANT, event_id),
        )
        database.conn.commit()
        assert job_for(database, TENANT, event_id)["state"] == "running"
    finally:
        database.close()
    code = main(
        ["explain", "--mcm-config", str(config), "--event-id", event_id, "--refresh"],
        backend=ListaBackend("match", tmp_path),
        clock=lambda: CLOCK,
    )
    shown = json.loads(capsys.readouterr().out)
    assert code == 0, shown
    database = _db(tmp_path)
    try:
        assert job_for(database, TENANT, event_id)["state"] != "running"
    finally:
        database.close()


def test_expl_fix_stale_completion_after_reorg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A worker holding the previous generation cannot restore a superseded match."""
    _backend, body = _cycle(tmp_path, monkeypatch, capsys, "match")
    assert body["verdict"] == "MATCHES_OWN_REQUEST"
    database = _db(tmp_path)
    try:
        event_id = jobs_for(database, TENANT)[0]["event_id"]
        force_pending(database, TENANT, event_id)
        assert claim_job(database, TENANT, event_id) is True
        owned = job_for(database, TENANT, event_id)
        assert owned is not None
        target = str(body["checkpoint_hashes"]["new"])
        assert supersede_checkpoint(database, TENANT, "bsc-mainnet", target) == 1
        result = complete_job(
            database, TENANT, event_id, body, "stale-worker",
            attempt=int(owned["attempt"]),
            event_revision=int(owned["event_revision"]),
            state="done",
            retry_reason=None,
            hold_token=None,
        )
        assert result == "stale"
        current = latest_revision(database, TENANT, event_id)
    finally:
        database.close()
    assert current is not None
    assert current["verdict"] == "UNVERIFIED"
    assert current["event_revision"] == 2
    assert "reorg" in current["gaps"]


def test_expl_fix_persistence_failure_is_reclaimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A revision write error leaves a visible retry, and the next refresh stores the verdict."""
    config = _desk(tmp_path, monkeypatch)
    _inject_approved(monkeypatch)
    backend = ListaBackend("match", tmp_path)
    backend.head = backend.old
    backend.balance = ONE
    _run(config, backend, capsys)
    backend.head = backend.new
    backend.balance = ONE + backend.payment + backend.extra
    _run(config, backend, capsys)
    body, _count, event_id = _stored(tmp_path)
    assert body["verdict"] == "MATCHES_OWN_REQUEST"
    import guardian.explanations.store as store

    original = store._insert_unlocked

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "_insert_unlocked", boom)
    failed = main(
        ["explain", "--mcm-config", str(config), "--event-id", event_id, "--refresh"],
        backend=backend,
        clock=lambda: CLOCK,
    )
    capsys.readouterr()
    assert failed == 0
    database = _db(tmp_path)
    try:
        row = job_for(database, TENANT, event_id)
        assert row is not None
        assert row["state"] == "retry"
        assert row["retry_reason"] == "persistence_failure"
        assert "persistence_failure" in failure_reasons(database, TENANT, event_id)
    finally:
        database.close()
    monkeypatch.setattr(store, "_insert_unlocked", original)
    recovered = main(
        ["explain", "--mcm-config", str(config), "--event-id", event_id, "--refresh"],
        backend=backend,
        clock=lambda: CLOCK,
    )
    shown = json.loads(capsys.readouterr().out)
    assert recovered == 0, shown
    assert shown["explanation"]["verdict"] == "MATCHES_OWN_REQUEST"
    assert _job(tmp_path)["state"] == "done"


def test_expl_fix_missing_job_is_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A committed movement without a job is enqueued again, including after a failed insert."""
    import guardian.explanations.engine as engine

    database = Database(tmp_path / "gap.sqlite")
    alert = {
        "asset": ASSET,
        "dedup_key": "enqueue-gap",
        "direction": "increase",
        "kind": "balance_delta",
        "network": "bsc-mainnet",
        "wallet_id": WALLET,
    }
    real = engine.enqueue

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(engine, "enqueue", boom)
    try:
        note_increase(database, TENANT, {"account": OWNER}, alert)
        assert jobs_for(database, TENANT) == []
        assert "enqueue_failure" in failure_reasons(database, TENANT, "enqueue-gap")
    finally:
        monkeypatch.setattr(engine, "enqueue", real)
    recover_missing_jobs(database, TENANT)
    found = job_for(database, TENANT, "enqueue-gap")
    assert found is not None
    assert found["account"] == OWNER
    database.upsert_tenant(TENANT, TENANT)
    database.upsert_wallet_identity(TENANT, WALLET, "evm", OWNER, "Wallet A")
    database.add_alert(TENANT, "orphan-alert", {
        "asset": ASSET,
        "dedup_key": "orphan-alert",
        "direction": "increase",
        "kind": "balance_delta",
        "network": "bsc-mainnet",
        "wallet_id": WALLET,
    })
    recover_missing_jobs(database, TENANT)
    orphan = job_for(database, TENANT, "orphan-alert")
    assert orphan is not None
    assert orphan["account"] == OWNER
    database.close()


def test_expl_fix_late_status_does_not_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A status read that returns after the event budget is not a verified match, and the alert stays."""
    config = _desk(tmp_path, monkeypatch)
    _inject_approved(monkeypatch)
    box = [CLOCK]
    backend = ListaBackend("match", tmp_path)
    backend.clock_box = box
    backend.deadline_on_status = True
    backend.advance_on_storage = False

    def clock() -> float:
        return box[0]

    backend.head = backend.old
    backend.balance = ONE
    _run(config, backend, capsys, clock=clock)
    backend.head = backend.new
    backend.balance = ONE + backend.payment + backend.extra
    box[0] = CLOCK
    _run(config, backend, capsys, clock=clock)
    body, _count, _event = _stored(tmp_path)
    assert body["verdict"] != "MATCHES_OWN_REQUEST"
    assert "deadline" in body["gaps"]
    assert backend.alert_rows is not None
    assert _alerts(tmp_path) == list(backend.alert_rows)


def test_expl_queue_roundtrip() -> None:
    """The empty and populated queue encodings are what the explainer decodes."""
    empty = _encode_queue([])
    populated = _encode_queue([(42, SLIS, 7)])
    assert list(decode(["(uint256,uint256,uint256)[]"], bytes.fromhex(empty[2:]))[0]) == []
    assert list(decode(["(uint256,uint256,uint256)[]"], bytes.fromhex(populated[2:]))[0]) == [(42, SLIS, 7)]


def _mark_request_removed(backend: ListaBackend) -> None:
    for row in backend.logs:
        if str(row["topics"][0]).lower() == backend.request_topic:
            row["removed"] = True


def _conflict_request_hash(backend: ListaBackend) -> None:
    original = next(row for row in backend.logs if str(row["topics"][0]).lower() == backend.request_topic)
    twin = dict(original)
    twin["blockHash"] = _hash(999)
    backend.logs.append(twin)


def _split_transaction_index(backend: ListaBackend) -> None:
    """Discovery records index 2. The transaction and the receipt record index 1."""
    for row in backend.logs:
        if str(row["topics"][0]).lower() == backend.claim_topic:
            row["transactionIndex"] = hex(2)
    backend.txs[CLAIM]["transactionIndex"] = hex(1)
    receipt = backend.receipts[CLAIM]
    receipt["transactionIndex"] = hex(1)
    receipt["logs"][0]["transactionIndex"] = hex(1)


def _request_rows(tmp_path: Path) -> list[dict]:
    database = _db(tmp_path)
    try:
        job = jobs_for(database, TENANT)[0]
        return load_requests(
            database, TENANT, str(job["network"]), str(job["account"]), _approved_manifest().proxy(),
        )
    finally:
        database.close()


def test_expl_fix_removed_request_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A removed request is not a verified link, including after it is only in the cache."""
    backend, body = _cycle(tmp_path, monkeypatch, capsys, "older_request", mutate=_mark_request_removed)
    assert body["verdict"] != "MATCHES_OWN_REQUEST"
    assert body["facts"].get("request_linkage") != "verified"
    assert "request_linkage" not in body["established_facts"]
    assert body["request_identity"] is None
    cached = _request_rows(tmp_path)
    removed = [row for row in cached if row["tx_id"] == REQ]
    assert removed
    assert removed[0]["removed"] is True
    assert removed[0]["block_hash"] == _hash(95)
    backend.logs = [row for row in backend.logs if str(row["topics"][0]).lower() != backend.request_topic]
    config = _desk(tmp_path, monkeypatch)
    _event = _stored(tmp_path)[2]
    again = main(
        ["explain", "--mcm-config", str(config), "--event-id", _event, "--refresh"],
        backend=backend,
        clock=lambda: CLOCK,
    )
    shown = json.loads(capsys.readouterr().out)
    assert again == 0, shown
    stored = _stored(tmp_path)[0]
    assert stored["verdict"] != "MATCHES_OWN_REQUEST"
    assert stored["facts"].get("request_linkage") != "verified"
    assert stored["request_identity"] is None
    still = [row for row in _request_rows(tmp_path) if row["tx_id"] == REQ]
    assert still[0]["removed"] is True
    assert still[0]["block_hash"] == _hash(95)


def test_expl_fix_conflicting_request_hash_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two copies of one request log that disagree on the block hash are not a verified request."""
    _backend, body = _cycle(tmp_path, monkeypatch, capsys, "match", mutate=_conflict_request_hash)
    assert body["verdict"] != "MATCHES_OWN_REQUEST"
    assert body["facts"].get("request_linkage") != "verified"
    assert body["request_identity"] is None
    cached = [row for row in _request_rows(tmp_path) if row["tx_id"] == REQ]
    assert cached
    assert cached[0]["conflict"] is True
    assert cached[0]["block_hash"]


def test_expl_fix_amount_lookback_does_not_link_another_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A later same-amount request is not the pending request when the window does not reach genesis."""
    _backend, body = _cycle(tmp_path, monkeypatch, capsys, "wrong_amount_lookback")
    assert body["verdict"] != "MATCHES_OWN_REQUEST"
    assert body["facts"].get("request_linkage") != "verified"
    identity = body["request_identity"] or ""
    assert REQ_B not in identity
    assert "discovery_incomplete" in body["gaps"]


def _producer_internal(amount: int, identity: str, block: int) -> dict:
    """The alert dict the native-internal producer actually stores."""
    from decimal import Decimal

    from guardian.rules import AlertPolicy, Candidate

    decision = AlertPolicy(Decimal("0.5"), Decimal("1000"), {"bsc-mainnet": Decimal("1.0")}).apply([
        Candidate(
            tenant_id=TENANT,
            wallet_id=WALLET,
            network="bsc-mainnet",
            capability="history.native_internal",
            identity=identity,
            kind="asset",
            raw_amount=amount,
            is_native=True,
            direction="in",
            tx_id=CLAIM,
            transfer_origin="evm_internal",
            execution_evidence="evidenced",
            attribution="trace",
            block_number=block,
            asset=ASSET,
        )
    ])
    assert len(decision) == 1
    assert decision[0].kind == "native_unit"
    alert = decision[0].as_dict()
    assert alert["transfer_origin"] == "evm_internal"
    assert alert["direction"] == "in"
    assert alert["raw_amount"] == str(amount)
    assert alert["tx_id"] == CLAIM
    assert alert["block_number"] == str(block)
    assert "old_checkpoint" not in alert
    assert "new_checkpoint" not in alert
    assert "signed_delta_raw" not in alert
    return alert


def _refresh_internal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    alert: dict,
) -> tuple[ListaBackend, dict]:
    _inject_approved(monkeypatch)
    config = _desk(tmp_path, monkeypatch)
    backend = ListaBackend("match", tmp_path)
    database = _db(tmp_path)
    database.upsert_tenant(TENANT, TENANT)
    database.upsert_wallet_identity(TENANT, WALLET, "evm", OWNER, "Wallet A")
    assert database.add_alert(TENANT, alert["dedup_key"], alert) is True
    note_increase(database, TENANT, {"account": OWNER}, alert)
    database.close()
    code = main(
        ["explain", "--mcm-config", str(config), "--event-id", alert["dedup_key"], "--refresh"],
        backend=backend,
        clock=lambda: CLOCK,
    )
    shown = json.loads(capsys.readouterr().out)
    assert code == 0, shown
    assert shown["status"] == "ok"
    return backend, shown["explanation"]


def test_expl_fix_internal_native_uses_transfer_amount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The producer payload is explained from its transaction and amount, with no balance checkpoint."""
    backend = ListaBackend("match", tmp_path)
    alert = _producer_internal(PAY, "lista-internal", backend.new)
    backend, body = _refresh_internal(tmp_path, monkeypatch, capsys, alert)
    assert backend.calls
    assert body["verdict"] == "MATCHES_OWN_REQUEST", body["gaps"]
    assert "malformed_event" not in body["gaps"]
    assert body["residual"]["observed_raw_amount"] == str(PAY)
    assert body["checkpoint_hashes"] == {"new": "", "old": ""}
    assert body["request_identity"] is not None
    assert body["request_identity"].endswith(f"{REQ}:4")
    assert _job(tmp_path)["state"] == "done"
    stored = _stored(tmp_path)[0]
    assert stored["verdict"] == "MATCHES_OWN_REQUEST"
    assert stored["residual"]["observed_raw_amount"] == str(PAY)


def test_expl_fix_internal_amount_disagreement_stays_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A transfer amount that is not the claim payment stays partial and keeps the real amount."""
    backend = ListaBackend("match", tmp_path)
    alert = _producer_internal(PAY + 9, "lista-internal-partial", backend.new)
    _backend, body = _refresh_internal(tmp_path, monkeypatch, capsys, alert)
    assert body["verdict"] == "PARTIAL", body
    assert "amount_inconsistent" in body["gaps"]
    assert "malformed_event" not in body["gaps"]
    assert body["residual"]["observed_raw_amount"] == str(PAY + 9)
    assert body["checkpoint_hashes"] == {"new": "", "old": ""}
    assert _backend.calls


def test_expl_fix_transaction_index_must_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Discovery index 2 against transaction and receipt index 1 is not a verified claim."""
    _backend, body = _cycle(tmp_path, monkeypatch, capsys, "match", mutate=_split_transaction_index)
    assert body["verdict"] != "MATCHES_OWN_REQUEST"
    assert "transaction_index_mismatch" in body["gaps"]


def _checkpoint_job(database: Database, event_id: str) -> None:
    enqueue(
        database, TENANT, event_id,
        account=OWNER, network="bsc-mainnet", wallet_id=WALLET,
        payload={
            "asset": ASSET,
            "dedup_key": event_id,
            "direction": "increase",
            "kind": "balance_delta",
            "network": "bsc-mainnet",
            "new_checkpoint": {"block_hash": _hash(110), "block_number": "110"},
            "old_checkpoint": {"block_hash": _hash(100), "block_number": "100"},
            "signed_delta_raw": str(PAY),
            "wallet_id": WALLET,
        },
    )


def test_expl_fix_reorg_before_first_revision(tmp_path: Path) -> None:
    """A reorg fences the worker before any revision exists, and does not bump twice."""
    database = Database(tmp_path / "early.sqlite")
    event_id = "early-reorg"
    try:
        _checkpoint_job(database, event_id)
        assert claim_job(database, TENANT, event_id) is True
        owned = job_for(database, TENANT, event_id)
        assert owned is not None
        assert int(owned["event_revision"]) == 1
        assert supersede_checkpoint(database, TENANT, "bsc-mainnet", _hash(110)) == 1
        fenced = job_for(database, TENANT, event_id)
        assert fenced is not None
        assert fenced["state"] == "retry"
        assert fenced["retry_reason"] == "reorg"
        assert int(fenced["event_revision"]) == 2
        assert latest_revision(database, TENANT, event_id) is None
        assert complete_job(
            database, TENANT, event_id, {"verdict": "MATCHES_OWN_REQUEST"}, "stale-before-revision",
            attempt=int(owned["attempt"]),
            event_revision=int(owned["event_revision"]),
            state="done",
            retry_reason=None,
            hold_token=None,
        ) == "stale"
        assert latest_revision(database, TENANT, event_id) is None
        assert int(job_for(database, TENANT, event_id)["event_revision"]) == 2
        assert supersede_checkpoint(database, TENANT, "bsc-mainnet", _hash(110)) == 0
        assert int(job_for(database, TENANT, event_id)["event_revision"]) == 2
    finally:
        database.close()


def test_expl_fix_stale_discovery_is_not_overwritten(tmp_path: Path) -> None:
    """A superseded attempt cannot publish or replace the discovery it no longer owns."""
    database = Database(tmp_path / "discovery.sqlite")
    event_id = "stale-discovery"
    manager = "0x" + "ab" * 20
    request = {
        "address": manager,
        "block_hash": _hash(40),
        "block_number": 40,
        "data_words": [SLIS],
        "kind": "request",
        "log_index": 4,
        "topic": "0x" + "cd" * 32,
        "tx_id": REQ,
        "tx_index": 0,
    }
    try:
        _checkpoint_job(database, event_id)
        assert claim_job(database, TENANT, event_id) is True
        owned = job_for(database, TENANT, event_id)
        assert owned is not None
        save_interval(database, TENANT, event_id, 40, [request], attempt=1, generation=1)
        save_lookback(
            database, TENANT, "bsc-mainnet", OWNER, manager, 40,
            event_id=event_id, attempt=1, generation=1,
        )
        save_requests(
            database, TENANT, "bsc-mainnet", OWNER, manager, [request],
            event_id=event_id, attempt=1, generation=1,
        )
        assert supersede_checkpoint(database, TENANT, "bsc-mainnet", _hash(100)) == 1
        save_interval(database, TENANT, event_id, 1, [{"kind": "claim", "tx_id": CLAIM}], attempt=1, generation=1)
        save_lookback(
            database, TENANT, "bsc-mainnet", OWNER, manager, 1,
            event_id=event_id, attempt=1, generation=1,
        )
        later = dict(request)
        later["tx_id"] = REQ_B
        save_requests(
            database, TENANT, "bsc-mainnet", OWNER, manager, [later],
            event_id=event_id, attempt=1, generation=1,
        )
        cursor, logs = load_interval(database, TENANT, event_id)
        assert cursor == 40
        assert logs[0]["tx_id"] == REQ
        assert load_lookback(database, TENANT, "bsc-mainnet", OWNER, manager) == 40
        stored = load_requests(database, TENANT, "bsc-mainnet", OWNER, manager)
        assert [row["tx_id"] for row in stored] == [REQ]
        assert complete_job(
            database, TENANT, event_id, {"verdict": "MATCHES_OWN_REQUEST"}, "stale-discovery",
            attempt=int(owned["attempt"]),
            event_revision=int(owned["event_revision"]),
            state="done",
            retry_reason=None,
            hold_token=None,
        ) == "stale"
        assert latest_revision(database, TENANT, event_id) is None
        assert int(job_for(database, TENANT, event_id)["event_revision"]) == 2
    finally:
        database.close()


def _other_process_claim(path: Path, event_id: str) -> tuple[str, str]:
    script = (
        "import sys\n"
        "from guardian.storage import Database\n"
        "from guardian.explanations.store import claim_job, job_for\n"
        "database = Database(sys.argv[1])\n"
        "try:\n"
        "    claimed = claim_job(database, sys.argv[2], sys.argv[3])\n"
        "    row = job_for(database, sys.argv[2], sys.argv[3])\n"
        "    print('claimed' if claimed else 'refused')\n"
        "    print(0 if row is None else int(row['attempt']))\n"
        "finally:\n"
        "    database.close()\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path), TENANT, event_id],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert len(lines) == 2
    return lines[0], lines[1]


def test_expl_fix_unexpired_lease_is_not_stolen(tmp_path: Path) -> None:
    """Another process cannot take a running job until its lease expires."""
    path = tmp_path / "lease.sqlite"
    event_id = "leased-job"
    database = Database(path)
    try:
        enqueue(
            database, TENANT, event_id,
            account=OWNER, network="bsc-mainnet", wallet_id=WALLET,
            payload={"dedup_key": event_id, "kind": "balance_delta", "direction": "increase", "network": "bsc-mainnet"},
        )
        assert claim_job(database, TENANT, event_id) is True
        force_pending(database, TENANT, event_id)
        owned = job_for(database, TENANT, event_id)
        assert owned is not None
        assert owned["state"] == "running"
        assert int(owned["attempt"]) == 1
        database.conn.commit()
    finally:
        database.close()
    assert _other_process_claim(path, event_id) == ("refused", "1")
    database = Database(path)
    try:
        database.conn.execute(
            "UPDATE explanation_job_leases SET expires_at=? WHERE tenant_id=? AND event_id=?",
            ("2000-01-01T00:00:00Z", TENANT, event_id),
        )
        database.conn.commit()
    finally:
        database.close()
    assert _other_process_claim(path, event_id) == ("claimed", "2")
