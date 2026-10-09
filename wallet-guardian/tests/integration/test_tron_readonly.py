"""MCM-SHARE-01 TRON through the real run-once CLI.

Fixture HTTP only: an unmocked call fails. Addresses are synthetic public
values (repeated-byte payloads), never a user wallet. This is not full TRON
support.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from guardian.chains.tron.abi import READ_SELECTORS, abi_address_word
from guardian.chains.tron.adapter import _constant_uint
from guardian.cli import main
from guardian.identities import parse_address
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry
from guardian.sources.tron_grid import PAGE_CAP, PAGE_LIMIT, TronGridClient
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.nokeys.json"
WALLET_ID = "66666666-6666-4666-8666-666666666666"
WATCH = "TA4Wt1DUCqz6YegbnsmqsWC5uUfbdBqPxm"
OTHER = "TA9pkx4DFxrEw8JZzUtyDrh2uAat1LDuJL"
TOKEN = "TAF8dttxK5iPKbvYC626aDBytrWANpLRXp"
SPENDER = "TALSWqjhNCaXi5YWPh9DvZgvtYRSfqVUwq"
KEY = "TARkPnaSRKSg6ZAUbJGMGvBstELj7VS3Br"
WATCH_HEX = "410101010101010101010101010101010101010101"
OTHER_HEX = "410202020202020202020202020202020202020202"
HOST = "https://trongrid.example.invalid"
SECRET = "synthetic-tron-key-not-real"
LARGE = 2**80 + 9
TRANSFER = 2**80 + 7
FAIL_AMOUNT = 50
SUCCESS_FEE = 265_000
FAIL_FEE = 2_800_000
TOKEN_BALANCE = 2**256 - 1
TOKEN_TRANSFER = 2**256 - 9
ALLOWANCE = 2**128 + 3
ALLOWANCE_NOW = 2**128 + 5
SOLID = 1000
LATEST = 1005
SOLID_ID = "ab" * 32
LATEST_ID = "cd" * 32
WITNESS = "WITNESS-SIG-SHOULD-NOT-LEAK"
SIGNATURE = "SIGNATURE-SHOULD-NOT-LEAK"
SUCCESS_TX = "11" * 32
FAIL_TX = "22" * 32
LAG_TX = "33" * 32
ACT_TX = "44" * 32
KEY_TX = "55" * 32
TRC_TRANSFER_TX = "66" * 32
TRC_APPROVAL_TX = "77" * 32
TRC_REVOKE_TX = "88" * 32
_WRITE_PARTS = ("broadcast", "createtransaction", "triggersmartcontract", "easytransfer", "gettransactionsign")
_LEAKS = (WATCH, OTHER, TOKEN, SPENDER, KEY, SECRET, WITNESS, SIGNATURE)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _now(number: int, block_id: str) -> dict:
    return {
        "blockID": block_id,
        "block_header": {"raw_data": {"number": number}},
        "witness_signature": WITNESS,
    }


def _account(balance: int) -> dict:
    return {
        "active_permission": [{"keys": [{"address": KEY, "weight": 1}], "threshold": 1}],
        "balance": balance,
        "owner_permission": {"keys": [{"address": KEY, "weight": 1}], "threshold": 1},
    }


def _contract(kind: str, value: dict) -> dict:
    return {"parameter": {"type_url": "type.googleapis.com/protocol." + kind, "value": value}, "type": kind}


def _tx(txid: str, kind: str, value: dict, ret: str) -> dict:
    return {
        "raw_data": {"contract": [_contract(kind, value)]},
        "ret": [{"contractRet": ret}],
        "signature": [SIGNATURE],
        "txID": txid,
    }


def _empty_page() -> dict:
    return {"data": [], "meta": {"links": {"next": "https://evil.example/steal"}}, "success": True}


def _vote_row(index: int) -> dict:
    return {"raw_data": {"contract": [_contract("VoteWitnessContract", {})]}, "txID": f"{index:064x}"}


class TronBackend:
    def __init__(self, mode: str = "happy", *, secret: str = "") -> None:
        self.mode = mode
        self.secret = secret
        self.calls: list[object] = []
        self.sleeps: list[float] = []
        self.now_tries = 0

    def record_sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        method = str(getattr(call, "method", ""))
        url = str(getattr(call, "url", ""))
        host = urlsplit(url).hostname or ""
        if host != "trongrid.example.invalid":
            raise AssertionError(host)
        path = urlsplit(url).path
        lowered = path.lower()
        if any(part in lowered for part in _WRITE_PARTS):
            raise AssertionError(path)
        if method not in {"GET", "POST"}:
            raise AssertionError(method)
        body = json.loads(getattr(call, "body") or b"{}")
        status, payload, headers = self._answer(method, path, urlsplit(url).query, body)
        if isinstance(payload, bytes):
            encoded = payload
        else:
            encoded = json.dumps(payload).encode()
        return RawResponse(status, headers, encoded)

    def _answer(self, method: str, path: str, query: str, body: dict) -> tuple[int, object, dict]:
        if path == "/walletsolidity/getnowblock":
            if self.mode == "rate" and self.now_tries == 0:
                self.now_tries += 1
                token = self.secret or SECRET
                return 429, f"TRON-PRO-API-KEY: {token}".encode(), {"Retry-After": "2"}
            return 200, _now(SOLID, SOLID_ID), {}
        if path == "/wallet/getnowblock":
            return 200, _now(LATEST, LATEST_ID), {}
        if path in {"/walletsolidity/getaccount", "/wallet/getaccount"}:
            return 200, self._account_body(), {}
        if path.endswith("/transactions/trc20"):
            return 200, self._trc20(query), {}
        if path.endswith("/transactions"):
            return 200, self._native_history(query), {}
        if path.endswith("/gettransactionbyid"):
            return 200, self._transaction(str(body.get("value", "")).lower()), {}
        if path.endswith("/gettransactioninfobyid"):
            return 200, self._info(str(body.get("value", "")).lower()), {}
        if path.endswith("/triggerconstantcontract"):
            return 200, self._constant(body), {}
        raise AssertionError(path)

    def _account_body(self) -> dict:
        if self.mode == "unactivated":
            return {}
        if self.mode == "malformed":
            return {"balance": True}
        if self.mode == "zero":
            return {"active_permission": [], "balance": 0, "owner_permission": {"keys": [], "threshold": 1}}
        return _account(LARGE)

    def _native_history(self, query: str) -> dict:
        if self.mode == "history_bad":
            return {"data": "nope", "success": True}
        if self.mode == "paginate":
            return _page(parse_qs(query).get("fingerprint", [None])[0])
        if self.mode == "repeat":
            return {
                "data": [_vote_row(1)],
                "meta": {"fingerprint": "loop1", "links": {"next": "https://evil.example/steal"}},
                "success": True,
            }
        if self.mode == "mixed":
            return {"data": _mixed_rows(), "meta": {"links": {"next": "https://evil.example/steal"}}, "success": True}
        return _empty_page()

    def _trc20(self, _query: str) -> dict:
        if self.mode != "typed":
            return _empty_page()
        return {
            "data": [
                {
                    "from": OTHER,
                    "to": WATCH,
                    "token_info": {"address": TOKEN, "decimals": 6, "symbol": "SYN"},
                    "transaction_id": TRC_TRANSFER_TX,
                    "type": "Transfer",
                    "value": str(TOKEN_TRANSFER),
                },
                {
                    "from": WATCH,
                    "to": SPENDER,
                    "token_info": {"address": TOKEN, "symbol": "SYN"},
                    "transaction_id": TRC_APPROVAL_TX,
                    "type": "Approval",
                    "value": str(ALLOWANCE),
                },
                {
                    "from": WATCH,
                    "to": SPENDER,
                    "token_info": {"address": TOKEN, "symbol": "SYN"},
                    "transaction_id": TRC_REVOKE_TX,
                    "type": "Approval",
                    "value": "0",
                },
            ],
            "success": True,
            "meta": {},
        }

    def _transaction(self, txid: str) -> dict:
        if txid in {ACT_TX, KEY_TX}:
            raise AssertionError(txid)
        if txid == FAIL_TX:
            return _tx(txid, "TransferContract", {"amount": FAIL_AMOUNT, "owner_address": WATCH_HEX, "to_address": OTHER_HEX}, "REVERT")
        if txid == LAG_TX:
            return {}
        if txid == SUCCESS_TX:
            return _tx(
                txid,
                "TransferContract",
                {"amount": TRANSFER, "owner_address": WATCH_HEX, "to_address": OTHER_HEX},
                "SUCCESS",
            )
        if txid in {TRC_TRANSFER_TX, TRC_APPROVAL_TX, TRC_REVOKE_TX}:
            return _tx(txid, "TriggerSmartContract", {"call_value": 0, "owner_address": WATCH_HEX}, "SUCCESS")
        return {}

    def _info(self, txid: str) -> dict:
        if txid in {ACT_TX, KEY_TX}:
            raise AssertionError(txid)
        if txid == LAG_TX:
            return {}
        if txid == FAIL_TX:
            return {"blockNumber": 900, "fee": FAIL_FEE, "id": txid, "receipt": {"result": "REVERT"}, "result": "FAILED"}
        if txid in {SUCCESS_TX, TRC_TRANSFER_TX, TRC_APPROVAL_TX, TRC_REVOKE_TX}:
            payload = {"blockNumber": 900, "id": txid, "receipt": {"result": "SUCCESS"}}
            if txid == SUCCESS_TX:
                payload["fee"] = SUCCESS_FEE
            return payload
        return {}

    def _constant(self, body: dict) -> dict:
        selector = body.get("function_selector")
        if selector not in READ_SELECTORS:
            raise AssertionError(selector)
        if selector == "balanceOf(address)":
            word = f"{TOKEN_BALANCE:064x}"
        elif selector == "allowance(address,address)":
            word = f"{ALLOWANCE_NOW:064x}"
        else:
            raise AssertionError(selector)
        return {"constant_result": [word], "result": {"result": True}}


def _page(fingerprint: str | None) -> dict:
    if fingerprint is None:
        index = 0
    else:
        index = int(fingerprint.split("-", 1)[1])
    if index >= PAGE_CAP:
        return {"data": [], "success": True, "meta": {}}
    return {
        "data": [_vote_row(number) for number in range(index * PAGE_LIMIT, (index + 1) * PAGE_LIMIT)],
        "meta": {"fingerprint": f"page-{index + 1}", "links": {"next": "https://evil.example/steal"}},
        "success": True,
    }


def _mixed_rows() -> list[dict]:
    return [
        {
            "blockNumber": 900,
            "raw_data": {"contract": [_contract("TransferContract", {"amount": TRANSFER, "owner_address": WATCH, "to_address": OTHER})]},
            "ret": [{"contractRet": "SUCCESS"}],
            "txID": SUCCESS_TX,
        },
        {
            "blockNumber": 900,
            "raw_data": {"contract": [_contract("TransferContract", {"amount": FAIL_AMOUNT, "owner_address": WATCH, "to_address": OTHER})]},
            "ret": [{"contractRet": "REVERT"}],
            "txID": FAIL_TX,
        },
        {
            "blockNumber": 900,
            "raw_data": {"contract": [_contract("TransferContract", {"amount": 1, "owner_address": WATCH, "to_address": OTHER})]},
            "txID": LAG_TX,
        },
        {"raw_data": {"contract": [_contract("AccountCreateContract", {"owner_address": WATCH})]}, "txID": ACT_TX},
        {"raw_data": {"contract": [_contract("AccountPermissionUpdateContract", {})]}, "txID": KEY_TX},
    ]


def _desk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    address: str = WATCH,
    *,
    contracts: list[str] | None = None,
    spenders: list[str] | None = None,
    secret: str | None = None,
) -> Path:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["enabled_networks"] = ["tron-mainnet"]
    document["plugins"] = []
    document["sources"]["tron-grid"] = {
        "adapter": "tron_grid",
        "budget_group": "tron-grid",
        "credential_env": "TRON_PRO_API_KEY",
        "enabled": True,
        "endpoint_env": "TRONGRID_API_URL",
    }
    document["budgets"]["tron-grid"] = {
        "max_concurrency": 1,
        "max_requests_per_day": 200,
        "max_rps": 1,
        "retry_attempts": 2,
    }
    document["routing"] = [
        {"capability": capability, "network": "tron-mainnet", "source_ids": ["tron-grid"]}
        for capability in (
            "balances.native",
            "balances.fungible",
            "history.native_external",
            "history.fungible",
            "permissions.tron_account",
            "permissions.erc20",
        )
    ]
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    account = {
        "address": address,
        "family": "tron",
        "label": "trx-a",
        "networks": ["tron-mainnet"],
        "relationship": "watch_only",
        "wallet_id": WALLET_ID,
    }
    if contracts is not None:
        account["trc20_contracts"] = contracts
    if spenders is not None:
        account["allowance_spenders"] = spenders
    accounts = {"schema_version": "guardian.accounts.v1", "tenant_id": "local", "accounts": [account]}
    secrets = {"TRONGRID_API_URL": HOST}
    if secret is not None:
        secrets["TRON_PRO_API_KEY"] = secret
    _private(tmp_path / "accounts.json", json.dumps(accounts))
    _private(tmp_path / "secrets.json", json.dumps(secrets))
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("TRONGRID_API_URL", raising=False)
    monkeypatch.delenv("TRON_PRO_API_KEY", raising=False)
    public = config.read_text(encoding="utf-8")
    assert WATCH not in public
    assert SECRET not in public
    return config


def _private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def _run(capsys: pytest.CaptureFixture[str], config: Path, backend: TronBackend) -> tuple[int, dict]:
    code = main(["run-once", "--mcm-config", str(config), "--no-send"], backend=backend)
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    for leak in _LEAKS:
        assert leak not in blob
    if payload.get("status") != "error":
        assert payload["live_verified"] is False
        assert payload.get("live_pass") is not True
    return code, payload


def _coverage(report: dict, capability: str) -> dict:
    found = [row for row in report["coverage"] if row["network"] == "tron-mainnet" and row["capability"] == capability]
    assert len(found) == 1
    return found[0]


def _stored(tmp_path: Path) -> list[dict]:
    database = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        return database.chain_events_for("local")
    finally:
        database.close()


def _paths(backend: TronBackend) -> list[str]:
    return [urlsplit(str(getattr(call, "url", ""))).path for call in backend.calls]


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _history_urls(backend: TronBackend, *, trc20: bool) -> list[str]:
    found = []
    for call in backend.calls:
        path = urlsplit(str(getattr(call, "url", ""))).path
        if not path.endswith("/transactions/trc20" if trc20 else "/transactions"):
            continue
        if trc20 and not path.endswith("/trc20"):
            continue
        if not trc20 and path.endswith("/trc20"):
            continue
        found.append(str(getattr(call, "url", "")))
    return found


def _assert_filters(url: str, fingerprint: str | None) -> None:
    query = _query(url)
    assert query["only_confirmed"] == ["true"]
    assert query["limit"] == [str(PAGE_LIMIT)]
    assert query["order_by"] == ["block_timestamp,desc"]
    if fingerprint is None:
        assert "fingerprint" not in query
    else:
        assert query["fingerprint"] == [fingerprint]


def _assert_readonly(backend: TronBackend) -> None:
    assert backend.calls
    for call in backend.calls:
        path = urlsplit(str(getattr(call, "url", ""))).path.lower()
        assert all(part not in path for part in _WRITE_PARTS)
        assert "evil.example" not in str(getattr(call, "url", ""))


def test_mcm_tron_01_address_network_and_large_integers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Base58 and hex are one identity. The sun balance stays a full integer."""
    config = _desk(tmp_path, monkeypatch)
    backend = TronBackend()
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    assert report["status"] == "ok"
    assert report["native_reads"] == [{"network": "tron-mainnet", "raw_amount": str(LARGE), "wallet_id": WALLET_ID}]
    assert str(LARGE) != str(int(float(LARGE)))
    balance = _coverage(report, "balances.native")
    assert balance["chain_uid"] == "tron-mainnet"
    assert balance["quality"] == "complete_in_declared_scope"
    assert balance["scope"]["declared"] == "solidified_liquid_trx_not_staked_or_resources"
    assert balance["checkpoint"]["number"] == str(SOLID)
    assert balance["checkpoint"]["hash"] == SOLID_ID
    assert balance["checkpoint"]["hash"] != LATEST_ID
    assert balance["checkpoint"]["status"] == "finalized"
    assert "identity_status:to_verify_by_probe" in balance["evidence_refs"]
    assert f"solidified_number:{SOLID}" in balance["evidence_refs"]
    assert f"latest_number:{LATEST}" in balance["evidence_refs"]
    assert "balance_source:walletsolidity" in balance["evidence_refs"]
    permissions = _coverage(report, "permissions.tron_account")
    assert permissions["quality"] == "partial"
    assert "owner_threshold:1" in permissions["evidence_refs"]
    assert "owner_key_count:1" in permissions["evidence_refs"]
    assert "active_permission_count:1" in permissions["evidence_refs"]
    assert "account_keys_not_handled" in permissions["scope"]["gaps"]
    assert "no_configured_tokens" in _coverage(report, "balances.fungible")["reason_codes"]
    assert _coverage(report, "balances.fungible")["quality"] != "complete_in_declared_scope"
    account = [
        json.loads(getattr(call, "body"))
        for call in backend.calls
        if urlsplit(str(getattr(call, "url", ""))).path == "/walletsolidity/getaccount"
    ]
    assert account[0]["address"] == WATCH
    assert account[0]["visible"] is True
    posts = [json.loads(getattr(call, "body")) for call in backend.calls if getattr(call, "method", "") == "POST"]
    assert posts
    assert all("visible" in item for item in posts)
    _assert_readonly(backend)


def test_mcm_tron_01_unactivated_account_is_not_a_malformed_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """An empty account is unactivated. A bad balance is not stored as zero."""
    config = _desk(tmp_path, monkeypatch)
    code, report = _run(capsys, config, TronBackend("unactivated"))
    assert code == 0, report
    assert report["native_reads"] == [{"network": "tron-mainnet", "raw_amount": "0", "wallet_id": WALLET_ID}]
    balance = _coverage(report, "balances.native")
    assert balance["quality"] == "partial"
    assert balance["quality"] != "complete_in_declared_scope"
    assert "unactivated_account" in balance["reason_codes"]
    assert "owner_key_count:0" in _coverage(report, "permissions.tron_account")["evidence_refs"]

    zero_dir = tmp_path / "zero"
    zero_dir.mkdir()
    zero_config = _desk(zero_dir, monkeypatch)
    code, zero = _run(capsys, zero_config, TronBackend("zero"))
    assert code == 0, zero
    assert zero["native_reads"][0]["raw_amount"] == "0"
    zero_balance = _coverage(zero, "balances.native")
    assert zero_balance["quality"] == "complete_in_declared_scope"
    assert "unactivated_account" not in zero_balance["reason_codes"]
    assert "owner_threshold:1" in _coverage(zero, "permissions.tron_account")["evidence_refs"]

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_config = _desk(bad_dir, monkeypatch)
    code, bad = _run(capsys, bad_config, TronBackend("malformed"))
    assert code == 1, bad
    assert bad["status"] == "failed"
    assert bad["native_reads"] == []
    malformed = _coverage(bad, "balances.native")
    assert malformed["quality"] == "error"
    assert "malformed_account" in malformed["reason_codes"]
    assert "owner_threshold" not in json.dumps(_coverage(bad, "permissions.tron_account"))

    history_dir = tmp_path / "history"
    history_dir.mkdir()
    history_config = _desk(history_dir, monkeypatch)
    code, history = _run(capsys, history_config, TronBackend("history_bad"))
    assert code == 0, history
    assert history["native_reads"][0]["raw_amount"] == str(LARGE)
    row = _coverage(history, "history.native_external")
    assert row["quality"] == "error"
    assert "bad_history" in row["reason_codes"]
    assert row["scope"]["pagination_complete"] is False
    assert row["quality"] != "complete_in_declared_scope"


def test_mcm_tron_01_rejects_malformed_address_before_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A bad checksum or an EVM address never reaches TronGrid."""
    samples = ("T1111111111111111111111111111111111", "0x0000000000000000000000000000000000000001")
    for index, address in enumerate(samples):
        desk = tmp_path / str(index)
        desk.mkdir()
        config = _desk(desk, monkeypatch, address=address)
        backend = TronBackend()
        code, report = _run(capsys, config, backend)
        assert code == 2, report
        assert report["status"] == "error"
        assert backend.calls == []


def test_mcm_tron_02_pagination_restart_and_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A full window keeps the next fingerprint and the same filters. links.next is not fetched."""
    config = _desk(tmp_path, monkeypatch)
    first = TronBackend("paginate")
    code, report = _run(capsys, config, first)
    assert code == 0, report
    assert report["native_reads"][0]["raw_amount"] == str(LARGE)
    history = _coverage(report, "history.native_external")
    assert history["quality"] == "partial"
    assert history["scope"]["pagination_complete"] is False
    assert history["scope"]["cursor"] == f"fp:page-{PAGE_CAP}"
    assert "page_cap" in history["reason_codes"]
    urls = _history_urls(first, trc20=False)
    assert len(urls) == PAGE_CAP
    _assert_filters(urls[0], None)
    _assert_filters(urls[1], "page-1")
    assert all("page-2" not in url for url in urls)
    assert not any("gettransaction" in path for path in _paths(first))
    _assert_readonly(first)

    second = TronBackend("paginate")
    code, again = _run(capsys, config, second)
    assert code == 0, again
    resumed = _history_urls(second, trc20=False)
    assert resumed
    _assert_filters(resumed[0], f"page-{PAGE_CAP}")
    _assert_readonly(second)


def test_mcm_tron_02_repeated_fingerprint_is_not_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The same fingerprint twice stops the walk and leaves it incomplete."""
    config = _desk(tmp_path, monkeypatch)
    backend = TronBackend("repeat")
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    assert report["native_reads"][0]["raw_amount"] == str(LARGE)
    history = _coverage(report, "history.native_external")
    assert history["scope"]["pagination_complete"] is False
    assert history["quality"] != "complete_in_declared_scope"
    assert "repeated_fingerprint" in history["reason_codes"]
    assert history["scope"]["cursor"] == "fp:loop1"
    urls = _history_urls(backend, trc20=False)
    assert len(urls) == 2
    _assert_filters(urls[0], None)
    _assert_filters(urls[1], "loop1")
    _assert_readonly(backend)


def test_mcm_tron_03_failed_versus_solidified_and_indexer_lag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Solidified success, a failed execution, and indexer lag stay different. Activation is not fetched."""
    config = _desk(tmp_path, monkeypatch)
    backend = TronBackend("mixed")
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    history = _coverage(report, "history.native_external")
    assert "indexer_lag" in history["reason_codes"]
    assert "activation_refused" in history["reason_codes"]
    assert "account_key_update_refused" in history["reason_codes"]
    assert history["quality"] != "complete_in_declared_scope"
    bodies = "\n".join(getattr(call, "body", b"").decode() for call in backend.calls)
    assert ACT_TX not in bodies
    assert KEY_TX not in bodies
    assert "/wallet/gettransactionbyid" not in bodies
    assert all(not path.endswith("/wallet/gettransactionbyid") for path in _paths(backend))
    events = _stored(tmp_path)
    by_tx: dict[str, list[dict]] = {}
    for event in events:
        by_tx.setdefault(event["tx_id"], []).append(event)
        assert event["chain_uid"] == "tron-mainnet"
    assert LAG_TX not in by_tx
    assert ACT_TX not in by_tx
    success = {item["kind"]: item for item in by_tx[SUCCESS_TX]}
    assert success["asset_out"]["raw_amount"] == str(TRANSFER)
    assert success["asset_out"]["tx_status"] == "success"
    assert success["asset_out"]["finality"] == "finalized"
    assert success["asset_out"]["asset"] == "tron-native:TRX"
    assert success["fee"]["raw_amount"] == str(SUCCESS_FEE)
    assert success["fee"]["kind"] == "fee"
    assert str(TRANSFER - SUCCESS_FEE) not in {item["raw_amount"] for item in events}
    failed = {item["kind"]: item for item in by_tx[FAIL_TX]}
    assert failed["tx_failed"]["tx_status"] == "failed"
    assert failed["tx_failed"]["finality"] == "finalized"
    assert failed["tx_failed"]["raw_amount"] == "0"
    assert failed["fee"]["raw_amount"] == str(FAIL_FEE)
    assert "asset_out" not in failed
    assert "asset_in" not in failed
    assert str(FAIL_AMOUNT) not in {item["raw_amount"] for item in events}
    _assert_readonly(backend)


def test_mcm_tron_04_transfer_approval_typing_and_known_pair_allowance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Approval stays an approval. Allowance uses the 20-byte payload and a read selector."""
    exploded = _RejectingTransport()
    client = TronGridClient(exploded, "tron-grid")
    for selector in ("transfer(address,uint256)", "approve(address,uint256)", "transferFrom(address,address,uint256)", "deadbeef"):
        got = client.trigger_constant({"function_selector": selector, "visible": True}, solidity=True)
        assert got.ok is False
        assert got.reason == "selector_rejected"
    assert client.requests == 0
    assert exploded.calls == []
    watch = parse_address("tron", WATCH)
    raw = bytes.fromhex(watch.identity_key)
    assert raw[0] == 0x41
    word = raw[1:].rjust(32, b"\x00").hex()
    assert abi_address_word(WATCH) == word
    assert word == "00" * 12 + watch.identity_key[2:]
    assert word != ("00" * 11 + watch.identity_key)

    false_call = _FalseConstant()
    amount, solidity, ok = _constant_uint(TronGridClient(false_call, "tron-grid"), WATCH, TOKEN, "balanceOf(address)", (WATCH,))
    assert amount is None
    assert ok is False
    assert solidity is True
    assert false_call.paths == ["/walletsolidity/triggerconstantcontract"]

    config = _desk(tmp_path, monkeypatch, contracts=[TOKEN], spenders=[SPENDER])
    backend = TronBackend("typed")
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    fungible = _coverage(report, "balances.fungible")
    assert fungible["quality"] == "complete_in_declared_scope"
    assert fungible["scope"]["declared"] == "configured_tokens_only"
    assert str(TOKEN_BALANCE) in fungible["evidence_refs"][0]
    allowance = _coverage(report, "permissions.erc20")
    assert allowance["quality"] == "partial"
    assert allowance["scope"]["declared"] == "configured_pairs_only"
    assert allowance["scope"]["pagination_complete"] is False
    assert "approval_history_not_indexed" in allowance["scope"]["gaps"]
    assert "configured_pairs_only" in allowance["reason_codes"]
    assert str(ALLOWANCE_NOW) in json.dumps(allowance["evidence_refs"])
    assert str(ALLOWANCE) not in json.dumps(allowance["evidence_refs"])
    events = _stored(tmp_path)
    by_tx: dict[str, list[dict]] = {}
    for event in events:
        by_tx.setdefault(event["tx_id"], []).append(event)
    transfer = by_tx[TRC_TRANSFER_TX][0]
    assert transfer["kind"] == "asset_in"
    assert transfer["raw_amount"] == str(TOKEN_TRANSFER)
    assert transfer["finality"] == "finalized"
    assert transfer["tx_status"] == "success"
    assert transfer["asset"] == "tron-trc20:" + parse_address("tron", TOKEN).identity_key
    assert "SYN" not in transfer["asset"]
    approval = by_tx[TRC_APPROVAL_TX][0]
    assert approval["kind"] == "approval_set"
    assert approval["raw_amount"] == str(ALLOWANCE)
    assert approval["kind"] not in {"asset_in", "asset_out"}
    revoked = by_tx[TRC_REVOKE_TX][0]
    assert revoked["kind"] == "approval_revoked"
    assert revoked["raw_amount"] == "0"
    spender = parse_address("tron", SPENDER)
    spender_word = bytes.fromhex(spender.identity_key)[1:].rjust(32, b"\x00").hex()
    constants = [
        (urlsplit(str(getattr(call, "url", ""))).path, json.loads(getattr(call, "body")))
        for call in backend.calls
        if urlsplit(str(getattr(call, "url", ""))).path.endswith("/triggerconstantcontract")
    ]
    assert constants
    assert all(path == "/walletsolidity/triggerconstantcontract" for path, _body in constants)
    allowance_body = next(item for path, item in constants if item["function_selector"] == "allowance(address,address)")
    assert allowance_body["visible"] is True
    assert allowance_body["parameter"] == word + spender_word
    assert allowance_body["parameter"] != ("00" * 11 + watch.identity_key) + ("00" * 11 + spender.identity_key)
    balance_body = next(item for _path, item in constants if item["function_selector"] == "balanceOf(address)")
    assert balance_body["parameter"] == word
    _assert_readonly(backend)


def test_mcm_tron_05_keyless_429_redaction_and_no_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing key still reads. A keyed 429 retries, and the key stays out of the report."""
    config = _desk(tmp_path, monkeypatch)
    keyless = TronBackend("happy")
    code, report = _run(capsys, config, keyless)
    assert code == 0, report
    assert report["native_reads"][0]["raw_amount"] == str(LARGE)
    for call in keyless.calls:
        assert getattr(call, "credential_headers", ()) in ((), None)
        assert SECRET not in repr(call)
        assert SECRET not in str(getattr(call, "url", ""))
    _assert_readonly(keyless)

    keyed_dir = tmp_path / "keyed"
    keyed_dir.mkdir()
    keyed_config = _desk(keyed_dir, monkeypatch, secret=SECRET)
    keyed = TronBackend("rate", secret=SECRET)
    code, rated = _run(capsys, keyed_config, keyed)
    assert code == 0, rated
    assert rated["native_reads"][0]["raw_amount"] == str(LARGE)
    assert rated["native_reads"][0]["raw_amount"] != "0"
    assert 2.0 in keyed.sleeps
    assert keyed.now_tries == 1
    headers = [getattr(call, "credential_headers", ()) for call in keyed.calls]
    assert headers
    assert all(item and item[0][0] == "TRON-PRO-API-KEY" for item in headers)
    assert all(item[0][1].reveal() == SECRET for item in headers)
    assert all("Bearer" not in item[0][1].reveal() for item in headers)
    for call in keyed.calls:
        assert SECRET not in repr(call)
        assert SECRET not in str(getattr(call, "url", ""))
    _assert_readonly(keyed)
    _assert_header_redacted(SECRET)


def _assert_header_redacted(secret: str) -> None:
    sources = {
        "tron-grid": {
            "adapter": "tron_grid",
            "budget_group": "tron-grid",
            "credential_env": "TRON_PRO_API_KEY",
            "enabled": True,
            "endpoint_env": "TRONGRID_API_URL",
        }
    }
    budgets = {"tron-grid": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 100, "retry_attempts": 0}}
    registry = build_endpoint_registry(
        {"sources": sources},
        secrets={"TRONGRID_API_URL": HOST, "TRON_PRO_API_KEY": secret},
    )
    book = BudgetBook.from_document(budgets, clock=lambda: 0.0, sleep=lambda _seconds: None)
    backend = FixtureBackend({})
    transport = ReadOnlyTransport(registry, book, backend, resolve=lambda _host: ["1.1.1.1"])
    operation = Operation(
        "rest",
        "tron_grid",
        http_method="POST",
        path="/walletsolidity/getnowblock",
        body={"visible": False},
        headers=("TRON-PRO-API-KEY",),
    )
    encoded = transport.encode("tron-grid", operation)
    backend.mapping[request_key(encoded.method, encoded.url, encoded.body)] = RawResponse(
        429,
        {"Retry-After": "2"},
        f"TRON-PRO-API-KEY: {secret}".encode(),
    )
    result = transport.request("tron-grid", operation)
    assert secret not in result.detail
    assert "TRON-PRO-API-KEY: ***" in result.detail
    sent = backend.calls[0]
    assert secret not in repr(sent)


class _RejectingTransport:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.endpoints = None

    def retry_attempts(self, _source_id: str) -> int:
        return 0

    def request(self, _source_id: str, operation: object) -> object:
        self.calls.append(operation)
        raise AssertionError("selector reached the transport")


class _FalseConstant:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.endpoints = None

    def retry_attempts(self, _source_id: str) -> int:
        return 0

    def request(self, _source_id: str, operation: object) -> object:
        self.paths.append(str(getattr(operation, "path", "")))
        return _Ok(False)


class _Ok:
    def __init__(self, accepted: bool) -> None:
        self.ok = True
        self.retryable = False
        self.reason = ""
        self.blocked = False
        self.headers: dict[str, str] = {}
        self.body = {"constant_result": ["0" * 64], "result": {"result": accepted}}
