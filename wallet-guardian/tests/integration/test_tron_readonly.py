"""MCM-SHARE-01 TRON through the real run-once CLI.

Fixture HTTP only: an unmocked call fails. Addresses are synthetic public
values (repeated-byte payloads), never a user wallet. This is not full TRON
support.
"""

from __future__ import annotations

import email.utils
import json
import os
import socket
import time
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
from guardian.sources.retry import Classification
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
UNMATCHED_TX = "a1" * 32
MATCHED_TX = "a2" * 32
STABLE_A = "b1" * 32
STABLE_B = "b2" * 32
INCOMING_TX = "c1" * 32
MISSING_OK = "d2" * 32
MISSING_TX = "d1" * 32
HEAD_TS = 1_700_000_000_000
FOREIGN_BALANCE = 424242
_TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_APPROVAL_TOPIC = "8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
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


def _now(number: int, block_id: str, timestamp: int = HEAD_TS) -> dict:
    return {
        "blockID": block_id,
        "block_header": {"raw_data": {"number": number, "timestamp": timestamp}},
        "witness_signature": WITNESS,
    }


def _event_log(token: str, sender: str, target: str, amount: int, topic: str) -> dict:
    token_id = parse_address("tron", token).identity_key[2:]
    sender_id = parse_address("tron", sender).identity_key[2:]
    target_id = parse_address("tron", target).identity_key[2:]
    return {
        "address": token_id,
        "data": f"{amount:064x}",
        "topics": [topic, sender_id.rjust(64, "0"), target_id.rjust(64, "0")],
    }


def _plain_transfer(txid: str, owner: str, target: str, amount: int) -> dict:
    value = {"amount": amount, "owner_address": owner, "to_address": target}
    return {
        "blockNumber": 900,
        "raw_data": {"contract": [_contract("TransferContract", value)]},
        "ret": [{"contractRet": "SUCCESS"}],
        "txID": txid,
    }


def _trc20_row(txid: str, sender: str, target: str, amount: int, kind: str = "Transfer") -> dict:
    return {
        "from": sender,
        "to": target,
        "token_info": {"address": TOKEN, "decimals": 6, "symbol": "SYN"},
        "transaction_id": txid,
        "type": kind,
        "value": str(amount),
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
        self.prepend = False

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
        if self.mode == "wrong_account":
            return {
                "address": OTHER,
                "balance": FOREIGN_BALANCE,
                "owner_permission": {"keys": [{"address": KEY, "weight": 1}] * 9, "threshold": 9},
            }
        if self.mode == "default_balance":
            return {
                "active_permission": [
                    {"keys": [{"address": KEY, "weight": 1}], "threshold": 4},
                    {"keys": [{"address": KEY, "weight": 1}], "threshold": 5},
                ],
                "address": WATCH,
                "create_time": 1,
                "owner_permission": {"keys": [{"address": KEY, "weight": 1}], "threshold": 1},
            }
        if self.mode == "error_balance":
            return {"Error": "unavailable", "balance": 0}
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
        if self.mode == "verify_cap":
            if parse_qs(query).get("fingerprint"):
                return {"data": [], "meta": {"fingerprint": "even-further"}, "success": True}
            rows = [_plain_transfer(f"{index + 1:064x}", WATCH, OTHER, 1) for index in range(60)]
            return {
                "data": rows,
                "meta": {"fingerprint": "beyond60", "links": {"next": "https://evil.example/steal"}},
                "success": True,
            }
        if self.mode == "missing_receipt":
            rows = [
                _plain_transfer(MISSING_OK, WATCH, OTHER, 1),
                _plain_transfer(MISSING_TX, WATCH, OTHER, 2),
            ]
            return {"data": rows, "meta": {}, "success": True}
        if self.mode == "incoming":
            return {
                "data": [_plain_transfer(INCOMING_TX, OTHER, WATCH, TRANSFER)],
                "meta": {},
                "success": True,
            }
        return _empty_page()

    def _trc20(self, _query: str) -> dict:
        if self.mode == "unmatched":
            return {
                "data": [
                    _trc20_row(UNMATCHED_TX, OTHER, WATCH, TOKEN_TRANSFER),
                    _trc20_row(MATCHED_TX, OTHER, WATCH, TOKEN_TRANSFER),
                ],
                "meta": {},
                "success": True,
            }
        if self.mode == "stable":
            older = _trc20_row(STABLE_A, OTHER, WATCH, TOKEN_TRANSFER)
            newer = _trc20_row(STABLE_B, OTHER, WATCH, 1)
            data = [newer, older] if self.prepend else [older]
            return {"data": data, "meta": {}, "success": True}
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
        if self.mode == "verify_cap":
            return _tx(txid, "TransferContract", {"amount": 1, "owner_address": WATCH_HEX, "to_address": OTHER_HEX}, "SUCCESS")
        if self.mode == "missing_receipt" and txid in {MISSING_OK, MISSING_TX}:
            amount = 1 if txid == MISSING_OK else 2
            return _tx(txid, "TransferContract", {"amount": amount, "owner_address": WATCH_HEX, "to_address": OTHER_HEX}, "SUCCESS")
        if self.mode == "incoming" and txid == INCOMING_TX:
            return _tx(
                txid,
                "TransferContract",
                {"amount": TRANSFER, "owner_address": OTHER_HEX, "to_address": WATCH_HEX},
                "SUCCESS",
            )
        if self.mode in {"unmatched", "stable"} and txid in {UNMATCHED_TX, MATCHED_TX, STABLE_A, STABLE_B}:
            return _tx(txid, "TriggerSmartContract", {"call_value": 0, "owner_address": WATCH_HEX}, "SUCCESS")
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
        if self.mode == "verify_cap":
            return {"blockNumber": 900, "fee": 1000, "id": txid, "receipt": {"result": "SUCCESS"}}
        if self.mode == "missing_receipt":
            if txid == MISSING_TX:
                return {}
            if txid == MISSING_OK:
                return {"blockNumber": 900, "id": txid, "receipt": {"result": "SUCCESS"}}
        if self.mode == "incoming" and txid == INCOMING_TX:
            return {"blockNumber": 900, "fee": SUCCESS_FEE, "id": txid, "receipt": {"result": "SUCCESS"}}
        if self.mode == "unmatched" and txid == UNMATCHED_TX:
            return {"blockNumber": 900, "id": txid, "log": [], "receipt": {"result": "SUCCESS"}}
        if self.mode == "unmatched" and txid == MATCHED_TX:
            return {
                "blockNumber": 900,
                "id": txid,
                "log": [_event_log(TOKEN, OTHER, WATCH, TOKEN_TRANSFER, _TRANSFER_TOPIC)],
                "receipt": {"result": "SUCCESS"},
            }
        if self.mode == "stable" and txid in {STABLE_A, STABLE_B}:
            amount = TOKEN_TRANSFER if txid == STABLE_A else 1
            return {
                "blockNumber": 900,
                "id": txid,
                "log": [
                    {"address": "ab" * 20, "data": "00", "topics": ["11"]},
                    _event_log(TOKEN, OTHER, WATCH, amount, _TRANSFER_TOPIC),
                ],
                "receipt": {"result": "SUCCESS"},
            }
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
            if txid == TRC_TRANSFER_TX:
                payload["log"] = [_event_log(TOKEN, OTHER, WATCH, TOKEN_TRANSFER, _TRANSFER_TOPIC)]
            if txid == TRC_APPROVAL_TX:
                payload["log"] = [_event_log(TOKEN, WATCH, SPENDER, ALLOWANCE, _APPROVAL_TOPIC)]
            if txid == TRC_REVOKE_TX:
                payload["log"] = [_event_log(TOKEN, WATCH, SPENDER, 0, _APPROVAL_TOPIC)]
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


def test_mcm_tron_fix_02_frozen_window_and_unverified_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A frozen timestamp window is repeated, and unverified rows are not skipped."""
    page_dir = tmp_path / "page"
    page_dir.mkdir()
    page_config = _desk(page_dir, monkeypatch)
    first = TronBackend("paginate")
    code, report = _run(capsys, page_config, first)
    assert code == 0, report
    urls = _history_urls(first, trc20=False)
    assert len(urls) == PAGE_CAP
    assert _bounds(urls[0]) == _bounds(urls[1]) == ("0", str(HEAD_TS))
    second = TronBackend("paginate")
    code, again = _run(capsys, page_config, second)
    assert code == 0, again
    resumed = _history_urls(second, trc20=False)
    assert _query(resumed[0])["fingerprint"] == [f"page-{PAGE_CAP}"]
    assert _bounds(resumed[0]) == ("0", str(HEAD_TS))

    solid_dir = tmp_path / "solid"
    solid_dir.mkdir()
    solid_config = _desk(solid_dir, monkeypatch)
    code, solid = _run(capsys, solid_config, TronBackend())
    assert code == 0, solid
    native = _coverage(solid, "history.native_external")
    assert native["scope"]["cursor"] == f"solidified:{SOLID}"
    assert f"frozen_max_timestamp:{HEAD_TS}" in native["evidence_refs"]
    held = TronBackend()
    code, followed = _run(capsys, solid_config, held)
    assert code == 0, followed
    followed_urls = _history_urls(held, trc20=False)
    assert followed_urls
    assert "fingerprint" not in _query(followed_urls[0])
    assert _bounds(followed_urls[0]) == (str(HEAD_TS + 1), str(HEAD_TS + 1))

    cap_dir = tmp_path / "cap"
    cap_dir.mkdir()
    cap_config = _desk(cap_dir, monkeypatch)
    capped = TronBackend("verify_cap")
    code, capped_report = _run(capsys, cap_config, capped)
    assert code == 0, capped_report
    cap_history = _coverage(capped_report, "history.native_external")
    assert cap_history["scope"]["pagination_complete"] is False
    assert "beyond60" not in str(cap_history["scope"]["cursor"])
    assert not str(cap_history["scope"]["cursor"]).startswith("solidified")
    cap_urls = _history_urls(capped, trc20=False)
    assert len(cap_urls) == 1
    assert "fingerprint" not in _query(cap_urls[0])
    assert "beyond60" not in cap_urls[0]
    info_calls = [path for path in _paths(capped) if path.endswith("/gettransactioninfobyid")]
    assert 0 < len(info_calls) < 60
    outs = [event for event in _stored(cap_dir) if event["kind"] == "asset_out"]
    assert 0 < len(outs) < 60
    capped_again = TronBackend("verify_cap")
    code, _cap_again = _run(capsys, cap_config, capped_again)
    assert code == 0, _cap_again
    retry_urls = _history_urls(capped_again, trc20=False)
    assert "beyond60" not in retry_urls[0]
    assert "fingerprint" not in _query(retry_urls[0])
    assert _query(retry_urls[0])["max_timestamp"] == [str(HEAD_TS)]

    lag_dir = tmp_path / "lag"
    lag_dir.mkdir()
    lag_config = _desk(lag_dir, monkeypatch)
    lagging = TronBackend("missing_receipt")
    code, lag_report = _run(capsys, lag_config, lagging)
    assert code == 0, lag_report
    lag_history = _coverage(lag_report, "history.native_external")
    assert lag_history["scope"]["pagination_complete"] is False
    assert not str(lag_history["scope"]["cursor"]).startswith("solidified")
    stored = _stored(lag_dir)
    assert any(event["tx_id"] == MISSING_OK and event["kind"] == "asset_out" and event["raw_amount"] == "1" for event in stored)
    assert all(event["tx_id"] != MISSING_TX or event["tx_status"] != "success" for event in stored)
    lag_again = TronBackend("missing_receipt")
    code, _lag_again = _run(capsys, lag_config, lag_again)
    assert code == 0, _lag_again
    lag_urls = _history_urls(lag_again, trc20=False)
    assert "fingerprint" not in _query(lag_urls[0])
    assert _bounds(lag_urls[0]) == ("0", str(HEAD_TS))


def test_mcm_tron_fix_03_receipt_event_identity_and_fee_payer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A token event needs a matching receipt log, a stable log index, and a proven fee payer."""
    unmatched_dir = tmp_path / "unmatched"
    unmatched_dir.mkdir()
    config = _desk(unmatched_dir, monkeypatch)
    code, report = _run(capsys, config, TronBackend("unmatched"))
    assert code == 0, report
    events = _stored(unmatched_dir)
    assert all(event["tx_id"] != UNMATCHED_TX for event in events)
    matched = [event for event in events if event["tx_id"] == MATCHED_TX]
    assert len(matched) == 1
    assert matched[0]["kind"] == "asset_in"
    assert matched[0]["raw_amount"] == str(TOKEN_TRANSFER)
    assert matched[0]["finality"] == "finalized"
    assert matched[0]["tx_status"] == "success"
    assert "trc20_event_unmatched" in _coverage(report, "history.fungible")["reason_codes"]

    stable_dir = tmp_path / "stable"
    stable_dir.mkdir()
    stable_config = _desk(stable_dir, monkeypatch)
    backend = TronBackend("stable")
    code, _first = _run(capsys, stable_config, backend)
    assert code == 0, _first
    original = next(event for event in _stored(stable_dir) if event["tx_id"] == STABLE_A)
    assert original["sub_index"] == "log-1"
    assert original["kind"] == "asset_in"
    assert original["raw_amount"] == str(TOKEN_TRANSFER)
    identity = original["identity"]
    backend.prepend = True
    code, _second = _run(capsys, stable_config, backend)
    assert code == 0, _second
    again = [event for event in _stored(stable_dir) if event["tx_id"] == STABLE_A]
    assert len(again) == 1
    assert again[0]["sub_index"] == "log-1"
    assert again[0]["identity"] == identity
    assert any(event["tx_id"] == STABLE_B and event["kind"] == "asset_in" for event in _stored(stable_dir))

    incoming_dir = tmp_path / "incoming"
    incoming_dir.mkdir()
    incoming_config = _desk(incoming_dir, monkeypatch)
    code, incoming = _run(capsys, incoming_config, TronBackend("incoming"))
    assert code == 0, incoming
    received = [event for event in _stored(incoming_dir) if event["tx_id"] == INCOMING_TX]
    kinds = {event["kind"]: event for event in received}
    assert kinds["asset_in"]["raw_amount"] == str(TRANSFER)
    assert kinds["asset_in"]["tx_status"] == "success"
    assert "fee" not in kinds
    assert all(event["kind"] != "fee" or event["raw_amount"] != str(SUCCESS_FEE) for event in _stored(incoming_dir))


def test_mcm_tron_fix_01_account_envelope_and_allowance_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The account envelope is checked before protobuf defaults, and a dropped pair is named."""
    wrong_dir = tmp_path / "wrong"
    wrong_dir.mkdir()
    code, wrong = _run(capsys, _desk(wrong_dir, monkeypatch), TronBackend("wrong_account"))
    assert code == 1, wrong
    assert wrong["native_reads"] == []
    assert "account_mismatch" in _coverage(wrong, "balances.native")["reason_codes"]
    assert "owner_threshold:9" not in json.dumps(_coverage(wrong, "permissions.tron_account"))
    assert str(FOREIGN_BALANCE) not in json.dumps(wrong["native_reads"])

    zero_dir = tmp_path / "default-balance"
    zero_dir.mkdir()
    code, omitted = _run(capsys, _desk(zero_dir, monkeypatch), TronBackend("default_balance"))
    assert code == 0, omitted
    assert omitted["native_reads"] == [{"network": "tron-mainnet", "raw_amount": "0", "wallet_id": WALLET_ID}]
    balance = _coverage(omitted, "balances.native")
    assert balance["quality"] == "complete_in_declared_scope"
    assert "malformed_account" not in balance["reason_codes"]
    assert "unactivated_account" not in balance["reason_codes"]
    permissions = _coverage(omitted, "permissions.tron_account")
    assert "owner_threshold:1" in permissions["evidence_refs"]
    assert "owner_key_count:1" in permissions["evidence_refs"]
    assert "active_permission_count:2" in permissions["evidence_refs"]
    assert "threshold:4" not in json.dumps(permissions["evidence_refs"])
    assert "threshold:5" not in json.dumps(permissions["evidence_refs"])

    error_dir = tmp_path / "error-balance"
    error_dir.mkdir()
    code, errored = _run(capsys, _desk(error_dir, monkeypatch), TronBackend("error_balance"))
    assert code == 1, errored
    assert errored["native_reads"] == []
    assert "malformed_account" in _coverage(errored, "balances.native")["reason_codes"]

    tokens = [_synthetic(3), _synthetic(4), _synthetic(5)]
    spenders = [_synthetic(6), _synthetic(7), _synthetic(8)]
    pair_dir = tmp_path / "pairs"
    pair_dir.mkdir()
    pair_config = _desk(pair_dir, monkeypatch, contracts=tokens, spenders=spenders)
    backend = TronBackend()
    code, paired = _run(capsys, pair_config, backend)
    assert code == 0, paired
    allowance = _coverage(paired, "permissions.erc20")
    first = f"allowance:{_identity(3)}:{_identity(6)}:{ALLOWANCE_NOW}"
    omitted = f"omitted_pair:{_identity(5)}:{_identity(8)}"
    assert first in allowance["evidence_refs"]
    assert omitted in allowance["evidence_refs"]
    assert "allowance_scope_omitted" in allowance["reason_codes"]
    assert all(not ref.startswith(f"allowance:{_identity(5)}:{_identity(8)}:") for ref in allowance["evidence_refs"])
    allowance_calls = [
        json.loads(getattr(call, "body"))
        for call in backend.calls
        if urlsplit(str(getattr(call, "url", ""))).path.endswith("/triggerconstantcontract")
        and json.loads(getattr(call, "body")).get("function_selector") == "allowance(address,address)"
    ]
    assert len(allowance_calls) == 8


def test_mcm_tron_fix_05_retry_after_clock_and_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-After waits on the client clock. A no-hook transport still sleeps, and a deadline stops the retry."""
    clock = {"now": 1_700_000_000.0}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(float(seconds))
        clock["now"] += float(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)

    def open_client(responses: list[object], deadline: int) -> tuple[TronGridClient, _NoHook]:
        transport = _NoHook(responses, clock)
        assert not hasattr(transport, "record_sleep")
        assert not hasattr(transport, "sleeper")
        assert not hasattr(transport, "backend")
        reader = TronGridClient(
            transport,
            "tron-grid",
            clock=lambda: clock["now"],
            started_at=clock["now"],
            deadline=deadline,
        )
        return reader, transport

    clock["now"] = 1_700_000_000.0
    slept.clear()
    reader, transport = open_client([_rate("2"), _ok_block()], 30)
    result = reader.post("/walletsolidity/getnowblock", {"visible": False})
    assert result.ok is True
    assert result.body["blockID"] == SOLID_ID
    assert slept == [2.0]
    assert transport.attempts[1] - transport.attempts[0] == 2.0
    assert len(transport.calls) == 2

    clock["now"] = 1_700_000_000.0
    slept.clear()
    header = email.utils.formatdate(clock["now"] + 5, usegmt=True)
    reader, transport = open_client([_rate(header), _ok_block()], 30)
    result = reader.post("/walletsolidity/getnowblock", {"visible": False})
    assert result.ok is True
    assert result.body["blockID"] == SOLID_ID
    assert slept == [5.0]
    assert transport.attempts[1] - transport.attempts[0] == 5.0

    clock["now"] = 1_700_000_000.0
    slept.clear()
    started = clock["now"]
    reader, transport = open_client([_rate("10"), _ok_block()], 2)
    result = reader.post("/walletsolidity/getnowblock", {"visible": False})
    assert result.ok is False
    assert result.reason == "deadline"
    assert slept == []
    assert transport.calls
    assert len(transport.calls) == 1
    assert clock["now"] == started


def _bounds(url: str) -> tuple[str, str]:
    query = _query(url)
    return query["min_timestamp"][0], query["max_timestamp"][0]


def _synthetic(byte: int) -> str:
    from guardian.identities import _b58check_encode

    return _b58check_encode(bytes([0x41]) + bytes([byte]) * 20)


def _identity(byte: int) -> str:
    return "41" + f"{byte:02x}" * 20


def _rate(delay: str) -> object:
    return _Reply(
        ok=False,
        retryable=True,
        reason="rate_limited",
        body=None,
        headers={"Retry-After": delay},
        classification=Classification("rate_limited", "error", True, False, "rate_limited"),
    )


def _ok_block() -> object:
    return _Reply(
        ok=True,
        retryable=False,
        reason="",
        body=_now(SOLID, SOLID_ID),
        headers={},
        classification=Classification("ok", "partial", False, False, "ok"),
    )


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


class _NoHook:
    def __init__(self, responses: list[object], clock: dict[str, float]) -> None:
        self._responses = list(responses)
        self._clock = clock
        self.calls: list[object] = []
        self.attempts: list[float] = []

    def retry_attempts(self, _source_id: str) -> int:
        return 1

    def request(self, _source_id: str, operation: object) -> object:
        self.attempts.append(self._clock["now"])
        self.calls.append(operation)
        return self._responses.pop(0)


class _Reply:
    def __init__(self, *, ok: bool, retryable: bool, reason: str, body: object, headers: dict[str, str], classification: Classification) -> None:
        self.ok = ok
        self.retryable = retryable
        self.reason = reason
        self.blocked = False
        self.body = body
        self.headers = headers
        self.classification = classification


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
