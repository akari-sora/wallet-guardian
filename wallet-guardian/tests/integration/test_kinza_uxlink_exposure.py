"""MCM-LIVE-L1. The public CLI drives Kinza and UXLINK exposure offline.

Assertions go through ``guardian.cli.main``. The test does not call the plugin
runner, a plugin class, or the exposure linker to produce the result it checks.
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

from guardian.chains.evm.accounting import TRANSFER_TOPIC, pad_address
from guardian.chains.evm.permissions import ERC20_APPROVAL_TOPIC
from guardian.cli import main
from guardian.protocols.pins import (
    KINZA_POOLS,
    KINZA_PROVIDERS,
    UXLINK_LEGACY_TOKEN,
    UXLINK_NEW_TOKEN,
    evm_contracts,
)
from guardian.protocols.uxlink import AssetManifestError, load_uxlink_file, load_uxlink_manifest
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
KEY = "synthetic-etherscan-key"
WEI = 10**18
UINT256_MAX = 2**256 - 1
UNDERLYING = to_checksum_address("0x" + ("11" * 20))
ATOKEN = to_checksum_address("0x" + ("a1" * 20))
DEBT = to_checksum_address("0x" + ("d2" * 20))
WRONG = to_checksum_address("0x" + ("ee" * 20))
TOKEN = to_checksum_address("0x" + ("ab" * 20))
HOST = {
    "ethereum-mainnet": "ethereum.example.invalid",
    "bsc-mainnet": "bsc.example.invalid",
    "arbitrum-one": "arbitrum.example.invalid",
}
CHAIN = {
    "ethereum.example.invalid": hex(1),
    "bsc.example.invalid": hex(56),
    "arbitrum.example.invalid": hex(42161),
}


def _addr(n: int) -> str:
    return to_checksum_address("0x" + f"{n:040x}")


def _wallet(n: int) -> str:
    return f"{n:08d}-1111-4111-8111-{n:012d}"


def _tx(n: int) -> str:
    return "0x" + f"{n:064x}"


def _selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


def _abi(kinds: list[str], values: list[object]) -> str:
    return "0x" + encode(kinds, values).hex()


SEL_ACCOUNT = _selector("getUserAccountData(address)")
SEL_RESERVE_DATA = _selector("getReserveData(address)")
SEL_BALANCE = _selector("balanceOf(address)")
SEL_ALLOWANCE = _selector("allowance(address,address)")
SEL_DECIMALS = _selector("decimals()")
SEL_SYMBOL = _selector("symbol()")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class ChainBackend:
    """JSON-RPC answers keyed by host, contract, selector, and the address argument."""

    def __init__(self) -> None:
        self.head = 100
        self.logs: list[dict] = []
        self.rpc: list[dict] = []
        self.answers: dict[tuple, str] = {}
        self.errors: set[tuple] = set()
        self.accounts: dict[tuple[str, str, str], tuple[int, ...]] = {}
        self.balances: dict[tuple[str, str, str], int] = {}
        self.symbols: dict[tuple[str, str], str] = {}

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
            topics = params[0].get("topics") or []
            topic0 = topics[0] if topics else None
            record["kind"] = "approval" if isinstance(topic0, list) else "transfer"
            record["owner_topic"] = topics[1] if len(topics) > 1 and isinstance(topics[1], str) else None
        if method == "eth_call" and params and isinstance(params[0], dict):
            data = str(params[0].get("data") or "")
            record["selector"] = data[:10]
            record["to"] = str(params[0].get("to") or "").lower()
            record["arg"] = _call_arg(data)
        self.rpc.append(record)
        result = self._rpc(method, record)
        if result is None:
            encoded = json.dumps({"error": {"code": -32000, "message": "fixture"}, "id": 1, "jsonrpc": "2.0"}).encode()
        else:
            encoded = json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode()
        return RawResponse(200, {}, encoded)

    def _rpc(self, method: str, record: dict) -> object | None:
        host = str(record.get("host") or "")
        if method == "eth_chainId":
            return CHAIN.get(host)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBalance":
            return "0x1"
        if method == "eth_call":
            return self._call(host, str(record.get("to") or ""), str(record.get("selector") or ""), record.get("arg"))
        if method == "eth_getLogs":
            start = int(record.get("from_block") or 0)
            end = int(record.get("to_block") or 0)
            kind = record.get("kind")
            owner = str(record.get("owner_topic") or "").lower()
            found = []
            for log in self.logs:
                if log["host"] != host:
                    continue
                block = int(str(log["blockNumber"]), 16)
                if block < start or block > end:
                    continue
                topic0 = log["topics"][0]
                if kind == "approval" and topic0 != ERC20_APPROVAL_TOPIC:
                    continue
                if kind == "transfer" and topic0 != TRANSFER_TOPIC:
                    continue
                if owner and str(log["topics"][1]).lower() != owner:
                    continue
                found.append({key: value for key, value in log.items() if key != "host"})
            return found
        return []

    def _call(self, host: str, to: str, selector: str, arg: object) -> str | None:
        if (host, to, selector) in self.errors:
            return None
        specific = self.answers.get((host, to, selector, arg))
        if isinstance(specific, str):
            return specific
        generic = self.answers.get((host, to, selector, None))
        if isinstance(generic, str):
            return generic
        if selector == SEL_ALLOWANCE:
            return _abi(["uint256"], [UINT256_MAX])
        if selector == SEL_BALANCE:
            amount = self.balances.get((host, to, str(arg or "")), 0)
            return _abi(["uint256"], [amount])
        if selector == SEL_DECIMALS:
            return _abi(["uint8"], [18])
        if selector == SEL_SYMBOL:
            return _abi(["string"], [self.symbols.get((host, to), "UXLINK")])
        if selector == SEL_ACCOUNT:
            values = self.accounts.get((host, to, str(arg or "")), (0, 0, 0, 0, 0, 2 * WEI))
            return _abi(["uint256"] * 6, list(values))
        return "0x"

    def _etherscan(self, url: str, host: str) -> RawResponse:
        query = parse_qs(urlsplit(url).query)
        self.rpc.append({"action": (query.get("action") or [""])[0], "host": host, "method": "etherscan"})
        body = {"message": "No transactions found", "result": [], "status": "0"}
        return RawResponse(200, {}, json.dumps(body).encode())


class _Desk:
    def __init__(self, config: Path, state: Path, backend: ChainBackend, addresses: tuple[str, ...]) -> None:
        self.config = config
        self.state = state
        self.backend = backend
        self.addresses = addresses


def test_mcm_live_l1_01_documented_pins_not_send_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    document = json.loads((ROOT / "config" / "protocols" / "kinza.json").read_text(encoding="utf-8"))
    pools = evm_contracts("kinza")
    assert document["execution_authorized"] is False
    assert document["onchain_independently_verified"] is False
    assert document["abi_checked_by_reviewer"] == "2026-10-09"
    assert set(document["networks"]) == {"bsc-mainnet", "ethereum-mainnet"}
    for network, pool in KINZA_POOLS.items():
        assert document["contracts"][network]["pool"]["address"] == pool
        assert document["contracts"][network]["pool"]["status"] == "documented"
        assert document["contracts"][network]["addresses_provider"]["address"] == KINZA_PROVIDERS[network]
        assert document["contracts"][network]["addresses_provider"]["status"] == "documented"
        assert pool.lower() in pools
        assert KINZA_PROVIDERS[network].lower() not in pools
    holdings = json.loads((ROOT / "config" / "protocols" / "uxlink-holdings.json").read_text(encoding="utf-8"))
    assert "mantle-mainnet" not in holdings["networks"]
    assert "opbnb-mainnet" not in document["networks"]
    client = _action_client_text()
    for pool in KINZA_POOLS.values():
        assert pool.lower() not in client

    pool_wallet = _wallet(1)
    provider_wallet = _wallet(2)
    pool_owner = _addr(1)
    provider_owner = _addr(2)
    network = "bsc-mainnet"
    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(pool_wallet, pool_owner, (network,)), _account(provider_wallet, provider_owner, (network,))],
        ["kinza"],
    )
    _install_kinza(desk.backend, network)
    news = _news_file(tmp_path, _incidents())
    first = _cli(capsys, desk, _argv(desk))
    assert first["live_verified"] is False
    assert first["live_pass"] is not True
    assert not _of_kind(_outbox(desk), "exposure")
    assert not _of_kind(_outbox(desk), "first_observed")
    before = {row["id"] for row in _outbox(desk)}
    desk.backend.head = 130
    desk.backend.logs = [
        _approval_log(network, TOKEN, pool_owner, KINZA_POOLS[network], _tx(11), 130, 1),
        _approval_log(network, TOKEN, provider_owner, KINZA_PROVIDERS[network], _tx(12), 130, 2),
    ]
    second = _cli(capsys, desk, _argv(desk, news))
    added = [row for row in _outbox(desk) if row["id"] not in before]
    pool_alerts = [
        row for row in added
        if row["payload"]["kind"] == "first_observed" and row["payload"]["wallet_id"] == pool_wallet
    ]
    provider_alerts = [
        row for row in added
        if row["payload"]["kind"] == "first_observed" and row["payload"]["wallet_id"] == provider_wallet
    ]
    assert len(pool_alerts) == 1
    assert KINZA_POOLS[network].lower() in pool_alerts[0]["payload"]["identity"]
    assert len(provider_alerts) == 1
    assert KINZA_PROVIDERS[network].lower() in provider_alerts[0]["payload"]["identity"]
    exposures = [row for row in added if row["payload"]["kind"] == "exposure"]
    assert len(exposures) == 1
    assert exposures[0]["payload"]["wallet_id"] == pool_wallet
    assert exposures[0]["payload"]["via"] == "approval"
    assert exposures[0]["payload"]["protocols"] == ["kinza"]
    assert exposures[0]["payload"]["loss_alert"] is False
    assert exposures[0]["payload"]["source"] == "caller_supplied"
    assert not [
        row for row in added
        if row["payload"]["wallet_id"] == provider_wallet and row["payload"]["kind"] in {"exposure", "exposure_unknown"}
    ]
    assert second["live_verified"] is False
    third_before = {row["id"] for row in _outbox(desk)}
    third = _cli(capsys, desk, _argv(desk, news))
    assert third["live_verified"] is False
    assert [row for row in _outbox(desk) if row["id"] not in third_before] == []


def test_mcm_live_l1_02_pool_provider_reciprocal_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    network = "ethereum-mainnet"
    wallet = _wallet(1)
    owner = _addr(1)
    matched = _desk(tmp_path / "matched", monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
    _install_kinza(matched.backend, network)
    quiet = _cli(capsys, matched, _argv(matched))
    assert _position(quiet, wallet, "kinza")["status"] == "none_observed"
    assert _required(quiet, wallet, "positions:kinza") == "complete_in_declared_scope"
    assert _called(matched.backend, SEL_ACCOUNT)
    matched.backend.rpc.clear()
    matched.backend.accounts[(HOST[network], KINZA_POOLS[network].lower(), owner.lower())] = (100, 0, 0, 0, 0, 2 * WEI)
    matched.backend.balances[(HOST[network], ATOKEN.lower(), owner.lower())] = 100
    seen = _cli(capsys, matched, _argv(matched))
    assert _position(seen, wallet, "kinza")["status"] == "observed"
    assert _required(seen, wallet, "positions:kinza") == "complete_in_declared_scope"
    assert _called(matched.backend, SEL_ACCOUNT)
    assert len(_of_kind(_outbox(matched), "position_change")) == 1

    for name, flags, reason in (
        ("pool", {"pool_mismatch": True}, "provider_mismatch"),
        ("provider", {"provider_mismatch": True}, "provider_mismatch"),
        ("unread", {"pool_unreadable": True}, "identity_unreadable"),
    ):
        desk = _desk(tmp_path / name, monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
        _install_kinza(desk.backend, network, **flags)
        report = _cli(capsys, desk, _argv(desk))
        row = _position(report, wallet, "kinza")
        assert row["status"] == "unknown"
        assert reason in row["reason_codes"]
        assert _required(report, wallet, "positions:kinza") == "unknown"
        assert not _called(desk.backend, SEL_ACCOUNT)
        assert not _of_kind(_outbox(desk), "position_change")


def test_mcm_live_l1_03_reserve_reconciliation_incomplete_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    network = "bsc-mainnet"
    wallet = _wallet(1)
    owner = _addr(1)
    incomplete = _desk(tmp_path / "incomplete", monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
    _install_kinza(incomplete.backend, network)
    incomplete.backend.accounts[(HOST[network], KINZA_POOLS[network].lower(), owner.lower())] = (100, 0, 0, 0, 0, 2 * WEI)
    report = _cli(capsys, incomplete, _argv(incomplete))
    row = _position(report, wallet, "kinza")
    assert row["status"] == "unknown"
    assert "reserves_incomplete" in row["reason_codes"]
    assert row["legs"] == []
    assert _required(report, wallet, "positions:kinza") == "unknown"
    assert not _of_kind(_outbox(incomplete), "position_change")

    mismatch = _desk(tmp_path / "mismatch", monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
    _install_kinza(mismatch.backend, network, token_pool_mismatch=True)
    mismatch.backend.accounts[(HOST[network], KINZA_POOLS[network].lower(), owner.lower())] = (100, 0, 0, 0, 0, 2 * WEI)
    mismatch.backend.balances[(HOST[network], ATOKEN.lower(), owner.lower())] = 100
    mismatched = _cli(capsys, mismatch, _argv(mismatch))
    bad = _position(mismatched, wallet, "kinza")
    assert bad["status"] == "unknown"
    assert "reserve_identity_mismatch" in bad["reason_codes"]
    assert _required(mismatched, wallet, "positions:kinza") == "unknown"

    underlying = _desk(tmp_path / "underlying", monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
    _install_kinza(underlying.backend, network, underlying_mismatch=True)
    underlying_report = _cli(capsys, underlying, _argv(underlying))
    assert "reserve_identity_mismatch" in _position(underlying_report, wallet, "kinza")["reason_codes"]

    bound = _desk(tmp_path / "bound", monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
    _install_kinza(bound.backend, network, reserve_count=41)
    bounded = _cli(capsys, bound, _argv(bound))
    bound_row = _position(bounded, wallet, "kinza")
    assert bound_row["status"] == "unknown"
    assert "reserve_bound" in bound_row["reason_codes"]
    assert bound_row["status"] != "observed"
    assert _required(bounded, wallet, "positions:kinza") == "unknown"
    assert not _called(bound.backend, SEL_RESERVE_DATA)

    happy = _desk(tmp_path / "happy", monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
    _install_kinza(happy.backend, network)
    first = _cli(capsys, happy, _argv(happy))
    assert _position(first, wallet, "kinza")["status"] == "none_observed"
    assert not _of_kind(_outbox(happy), "position_change")
    before = {row["id"] for row in _outbox(happy)}
    happy.backend.accounts[(HOST[network], KINZA_POOLS[network].lower(), owner.lower())] = (100, 0, 0, 0, 0, 2 * WEI)
    happy.backend.balances[(HOST[network], ATOKEN.lower(), owner.lower())] = 50
    _cli(capsys, happy, _argv(happy))
    added = [row for row in _outbox(happy) if row["id"] not in before]
    assert len(added) == 1
    assert added[0]["payload"]["kind"] == "position_change"
    assert "appeared" in added[0]["payload"]["identity"]
    again = {row["id"] for row in _outbox(happy)}
    _cli(capsys, happy, _argv(happy))
    assert [row for row in _outbox(happy) if row["id"] not in again] == []


def test_mcm_live_l1_04_uxlink_old_new_chain_price_separation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    wallet = _wallet(1)
    owner = _addr(1)
    networks = ("ethereum-mainnet", "arbitrum-one", "bsc-mainnet")
    desk = _desk(tmp_path, monkeypatch, [_account(wallet, owner, networks)], ["uxlink-holdings"])
    desk.backend.symbols[(HOST["arbitrum-one"], UXLINK_LEGACY_TOKEN.lower())] = "NOTUX"
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({
        "quotes": [
            {"network": "ethereum-mainnet", "address": UXLINK_NEW_TOKEN, "price_usd": "1.25", "symbol": "UXLINK"},
            {"network": "arbitrum-one", "address": UXLINK_LEGACY_TOKEN, "price_usd": "9.99", "symbol": "UXLINK"},
            {"network": "bsc-mainnet", "address": UXLINK_LEGACY_TOKEN, "price_usd": "9.99"},
            {"network": "bsc-mainnet", "address": UXLINK_NEW_TOKEN, "price_usd": "9.99", "symbol": "UXLINK"},
            {"symbol": "UXLINK", "price_usd": "9.99"},
        ],
    }), encoding="utf-8")
    argv = _argv(desk, None, prices)
    first = _cli(capsys, desk, argv)
    assert all(row["status"] == "none_observed" for row in _plugin_rows(first, wallet, "uxlink_holdings"))
    for network, token in (
        ("ethereum-mainnet", UXLINK_NEW_TOKEN),
        ("arbitrum-one", UXLINK_LEGACY_TOKEN),
        ("bsc-mainnet", UXLINK_LEGACY_TOKEN),
    ):
        desk.backend.balances[(HOST[network], token.lower(), owner.lower())] = 2 * WEI
    second = _cli(capsys, desk, argv)
    blob = json.dumps(second)
    assert "9.99" not in blob
    assert "NOTUX" not in blob
    assert '"value_usd": 0' not in blob
    assert '"value_usd": "0"' not in blob
    new_id = f"ethereum-mainnet:{UXLINK_NEW_TOKEN.lower()}"
    old_arb = f"arbitrum-one:{UXLINK_LEGACY_TOKEN.lower()}"
    old_bsc = f"bsc-mainnet:{UXLINK_LEGACY_TOKEN.lower()}"
    assert old_arb != old_bsc
    assert old_arb.split(":", 1)[1] == old_bsc.split(":", 1)[1]
    legs = {
        leg["identity"]: leg
        for row in _plugin_rows(second, wallet, "uxlink_holdings")
        for leg in row["legs"]
    }
    assert set(legs) == {new_id, old_arb, old_bsc}
    assert legs[new_id]["valuation"] == "valued"
    assert legs[new_id]["value_usd"] == "2.5"
    for identity in (old_arb, old_bsc):
        assert legs[identity]["valuation"] == "valuation_unknown"
        assert legs[identity]["value_usd"] is None
        assert legs[identity]["labels"]["status"] == "deprecated_legacy"
        assert legs[identity]["labels"]["price_mapping"] == "separate_from_new_token"
        assert identity in second["valuation_unknown"]
    assert legs[old_arb]["labels"]["symbol_observed"] == "not_uxlink"
    assert new_id not in second["valuation_unknown"]


def test_mcm_live_l1_05_claim_eligibility_unknown_not_redeemable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    book = load_uxlink_file()
    legacy = [token for token in book.tokens if token.status == "deprecated_legacy"]
    assert {token.network for token in legacy} == {"arbitrum-one", "bsc-mainnet"}
    assert len({token.address for token in legacy}) == 1
    assert all(token.migration_eligibility == "unknown" for token in legacy)
    assert all(token.claimability == "unknown" for token in legacy)
    assert all(token.price_mapping == "separate_from_new_token" for token in legacy)
    fresh = json.loads((ROOT / "config" / "assets" / "uxlink.json").read_text(encoding="utf-8"))
    for mutate in (_claim_true, _redeem_only, _one_to_one, _redeemable, _claimable_word):
        document = json.loads(json.dumps(fresh))
        mutate(document)
        with pytest.raises(AssetManifestError):
            load_uxlink_manifest(document)

    wallet = _wallet(1)
    owner = _addr(1)
    network = "arbitrum-one"
    desk = _desk(tmp_path, monkeypatch, [_account(wallet, owner, (network,))], ["uxlink-holdings"])
    first = _cli(capsys, desk, _argv(desk))
    assert _position(first, wallet, "uxlink_holdings")["status"] == "none_observed"
    desk.backend.balances[(HOST[network], UXLINK_LEGACY_TOKEN.lower(), owner.lower())] = WEI
    before = {row["id"] for row in _outbox(desk)}
    second = _cli(capsys, desk, _argv(desk))
    row = _position(second, wallet, "uxlink_holdings")
    assert row["status"] == "observed"
    labels = row["legs"][0]["labels"]
    assert labels["status"] == "deprecated_legacy"
    assert labels["migration_eligibility"] == "unknown"
    assert labels["claimability"] == "unknown"
    assert labels["price_mapping"] == "separate_from_new_token"
    assert labels["valuation"] == "valuation_unknown"
    added = [item for item in _outbox(desk) if item["id"] not in before]
    assert len(added) == 1
    assert added[0]["payload"]["kind"] == "position_change"
    out_labels = added[0]["payload"]["labels"][0]["labels"]
    assert out_labels["status"] == "deprecated_legacy"
    assert out_labels["migration_eligibility"] == "unknown"
    assert out_labels["claimability"] == "unknown"
    _forbid(json.dumps(second))
    _forbid(json.dumps(added[0]["payload"]))


def test_mcm_live_l1_06_position_or_permission_news_relevance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    pos = _wallet(1)
    approval = _wallet(2)
    holder = _wallet(3)
    empty = _wallet(4)
    token_approval = _wallet(5)
    owners = {name: _addr(index) for index, name in enumerate((pos, approval, holder, empty, token_approval), start=1)}
    desk = _desk(
        tmp_path / "linked",
        monkeypatch,
        [
            _account(pos, owners[pos], ("bsc-mainnet",)),
            _account(approval, owners[approval], ("bsc-mainnet",)),
            _account(holder, owners[holder], ("arbitrum-one",)),
            _account(empty, owners[empty], ("ethereum-mainnet",)),
            _account(token_approval, owners[token_approval], ("bsc-mainnet",)),
        ],
        ["kinza", "uxlink-holdings"],
    )
    _install_kinza(desk.backend, "bsc-mainnet")
    _install_kinza(desk.backend, "ethereum-mainnet")
    news = _news_file(tmp_path, _incidents())
    first = _cli(capsys, desk, _argv(desk))
    assert not _of_kind(_outbox(desk), "exposure")
    assert not _of_kind(_outbox(desk), "exposure_unknown")
    for wallet in (pos, approval, holder, empty, token_approval):
        rows = _coverage(first, wallet, "exposure_news")
        assert rows
        assert all(row["quality"] != "complete_in_declared_scope" for row in rows)
        assert all("news_not_supplied" in row["reason_codes"] for row in rows)
        assert all("unsupported" in row["reason_codes"] for row in rows)
        assert all("no news" not in " ".join(row["reason_codes"]) for row in rows)
    desk.backend.head = 130
    desk.backend.accounts[(HOST["bsc-mainnet"], KINZA_POOLS["bsc-mainnet"].lower(), owners[pos].lower())] = (
        100, 0, 0, 0, 0, 2 * WEI,
    )
    desk.backend.balances[(HOST["bsc-mainnet"], ATOKEN.lower(), owners[pos].lower())] = 40
    desk.backend.balances[(HOST["arbitrum-one"], UXLINK_LEGACY_TOKEN.lower(), owners[holder].lower())] = WEI
    desk.backend.logs = [
        _approval_log("bsc-mainnet", TOKEN, owners[approval], KINZA_POOLS["bsc-mainnet"], _tx(21), 130, 1),
        _approval_log(
            "bsc-mainnet", UXLINK_LEGACY_TOKEN, owners[token_approval], TOKEN, _tx(22), 130, 2,
        ),
    ]
    second = _cli(capsys, desk, _argv(desk, news))
    notices = _of_kind(_outbox(desk), "exposure")
    by_wallet = {row["payload"]["wallet_id"]: row["payload"] for row in notices}
    assert set(by_wallet) == {pos, approval, holder, token_approval}
    assert by_wallet[pos]["via"] == "position"
    assert by_wallet[pos]["protocols"] == ["kinza"]
    assert by_wallet[approval]["via"] == "approval"
    assert by_wallet[approval]["protocols"] == ["kinza"]
    assert by_wallet[holder]["via"] == "position"
    assert by_wallet[holder]["protocols"] == ["uxlink"]
    assert by_wallet[token_approval]["via"] == "approval"
    assert by_wallet[token_approval]["protocols"] == ["uxlink"]
    for payload in by_wallet.values():
        assert payload["loss_alert"] is False
        assert payload["source"] == "caller_supplied"
    assert not [row for row in notices if row["payload"]["wallet_id"] == empty]
    assert not _of_kind(_outbox(desk), "exposure_unknown")
    for wallet in (pos, holder):
        rows = _coverage(second, wallet, "exposure_news")
        assert rows
        assert all(row["quality"] != "complete_in_declared_scope" for row in rows)
        assert all("caller_supplied" in row["reason_codes"] for row in rows)
        assert all("not_fetched" in row["reason_codes"] for row in rows)
        assert all("caller_supplied" in row["sources"] for row in rows)
    before = {row["id"] for row in _outbox(desk)}
    _cli(capsys, desk, _argv(desk, news))
    assert [row for row in _outbox(desk) if row["id"] not in before] == []

    unknown_wallet = _wallet(6)
    unknown_owner = _addr(6)
    unknown = _desk(
        tmp_path / "unknown",
        monkeypatch,
        [_account(unknown_wallet, unknown_owner, ("ethereum-mainnet",))],
        ["kinza"],
    )
    _install_kinza(unknown.backend, "ethereum-mainnet", pool_mismatch=True)
    unknown_news = _news_file(tmp_path / "unknown-news", _incidents())
    unknown_report = _cli(capsys, unknown, _argv(unknown, unknown_news))
    assert _position(unknown_report, unknown_wallet, "kinza")["status"] == "unknown"
    unknown_rows = _of_kind(_outbox(unknown), "exposure_unknown")
    assert len(unknown_rows) == 1
    assert unknown_rows[0]["payload"]["wallet_id"] == unknown_wallet
    assert unknown_rows[0]["payload"]["protocols"] == ["kinza"]
    assert unknown_rows[0]["payload"]["loss_alert"] is False
    assert not _of_kind(_outbox(unknown), "exposure")
    again = {row["id"] for row in _outbox(unknown)}
    _cli(capsys, unknown, _argv(unknown, unknown_news))
    assert [row for row in _outbox(unknown) if row["id"] not in again] == []


def test_mcm_live_l1_06_earlier_approval_still_links_later_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-LIVE-L1-06. An approval from an earlier round still links news that arrives later."""
    wallet = _wallet(1)
    owner = _addr(1)
    network = "bsc-mainnet"
    desk = _desk(tmp_path, monkeypatch, [_account(wallet, owner, (network,))], ["kinza"])
    _install_kinza(desk.backend, network)
    _cli(capsys, desk, _argv(desk))
    desk.backend.head = 130
    desk.backend.logs = [_approval_log(network, TOKEN, owner, KINZA_POOLS[network], _tx(31), 125, 1)]
    _cli(capsys, desk, _argv(desk))
    assert len(_of_kind(_outbox(desk), "first_observed")) == 1
    assert not _of_kind(_outbox(desk), "exposure")
    desk.backend.head = 1000
    news = _news_file(tmp_path, _incidents())
    _cli(capsys, desk, _argv(desk, news))
    notices = _of_kind(_outbox(desk), "exposure")
    assert len(notices) == 1
    assert notices[0]["payload"]["wallet_id"] == wallet
    assert notices[0]["payload"]["via"] == "approval"
    assert notices[0]["payload"]["protocols"] == ["kinza"]
    assert len(_of_kind(_outbox(desk), "first_observed")) == 1
    desk.backend.head = 1100
    _cli(capsys, desk, _argv(desk, news))
    assert len(_of_kind(_outbox(desk), "exposure")) == 1
    revoke = _approval_log(network, TOKEN, owner, KINZA_POOLS[network], _tx(32), 1150, 1)
    revoke["data"] = "0x" + (0).to_bytes(32, "big").hex()
    desk.backend.logs.append(revoke)
    desk.backend.answers[(HOST[network], TOKEN.lower(), SEL_ALLOWANCE, None)] = _abi(["uint256"], [0])
    desk.backend.head = 1200
    later = [dict(_incidents()[0], url="https://news.example.invalid/pool-note-2", title="Second documented pool note")]
    _cli(capsys, desk, _argv(desk, _news_file(tmp_path / "later", later)))
    assert len(_of_kind(_outbox(desk), "exposure")) == 1


def _install_kinza(backend: ChainBackend, network: str, **flags: object) -> None:
    host = HOST[network]
    pool = KINZA_POOLS[network]
    provider = KINZA_PROVIDERS[network]
    pool_l = pool.lower()
    provider_l = provider.lower()
    count = int(flags.get("reserve_count", 1))
    assets = [UNDERLYING] if count == 1 else [_addr(0x1000 + index) for index in range(count)]
    if flags.get("pool_unreadable"):
        backend.errors.add((host, provider_l, _selector("getPool()")))
    else:
        returned = WRONG if flags.get("pool_mismatch") else pool
        backend.answers[(host, provider_l, _selector("getPool()"), None)] = _abi(["address"], [returned])
    if flags.get("provider_unreadable"):
        backend.errors.add((host, pool_l, _selector("ADDRESSES_PROVIDER()")))
    else:
        returned = WRONG if flags.get("provider_mismatch") else provider
        backend.answers[(host, pool_l, _selector("ADDRESSES_PROVIDER()"), None)] = _abi(["address"], [returned])
    backend.answers[(host, pool_l, _selector("getReservesList()"), None)] = _abi(["address[]"], [assets])
    pool_token = WRONG if flags.get("token_pool_mismatch") else pool
    for asset in assets:
        asset_l = asset.lower()
        words = [0] * 16
        words[8] = int(ATOKEN, 16)
        words[10] = int(DEBT, 16)
        backend.answers[(host, pool_l, SEL_RESERVE_DATA, asset_l)] = _abi(["uint256"] * 16, words)
        underlying = WRONG if flags.get("underlying_mismatch") else asset
        for token in (ATOKEN.lower(), DEBT.lower()):
            backend.answers[(host, token, _selector("POOL()"), None)] = _abi(["address"], [pool_token])
            backend.answers[(host, token, _selector("UNDERLYING_ASSET_ADDRESS()"), None)] = _abi(
                ["address"], [underlying],
            )


def _desk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accounts: list[dict],
    plugins: list[str],
) -> _Desk:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = plugins
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
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
    _write_private(
        accounts_path,
        json.dumps({"schema_version": "guardian.accounts.v1", "tenant_id": TENANT, "accounts": accounts}) + "\n",
    )
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts_path))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    addresses = tuple(str(account["address"]) for account in accounts)
    return _Desk(config, state, ChainBackend(), addresses)


def _account(wallet_id: str, address: str, networks: tuple[str, ...]) -> dict:
    return {
        "address": address,
        "family": "evm",
        "label": "desk",
        "networks": list(networks),
        "own_group": "desk",
        "relationship": "owned_user_declared",
        "wallet_id": wallet_id,
    }


def _argv(desk: _Desk, news: Path | None = None, prices: Path | None = None) -> list[str]:
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send"]
    if news is not None:
        argv.extend(["--news-input", str(news)])
    if prices is not None:
        argv.extend(["--price-input", str(prices)])
    return argv


def _cli(capsys: pytest.CaptureFixture[str], desk: _Desk, argv: list[str]) -> dict:
    code = main(argv, backend=desk.backend, clock=lambda: 1_700_000_000.0)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    blob = json.dumps(payload)
    assert KEY not in blob
    for address in desk.addresses:
        assert address.lower() not in blob.lower()
    assert payload.get("live_verified") is False
    assert payload.get("live_pass") is not True
    _forbid(blob)
    return payload


def _outbox(desk: _Desk) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.outbox_for(TENANT)
    finally:
        database.close()


def _position(report: dict, wallet: str, plugin_id: str) -> dict:
    found = _plugin_rows(report, wallet, plugin_id)
    assert len(found) == 1
    return found[0]


def _plugin_rows(report: dict, wallet: str, plugin_id: str) -> list[dict]:
    return [
        row for row in report["positions"]
        if row["wallet_id"] == wallet and row["plugin_id"] == plugin_id
    ]


def _required(report: dict, wallet: str, suffix: str) -> str:
    found = [
        value for key, value in report["required"].items()
        if key.startswith(wallet + "|") and key.endswith("|" + suffix)
    ]
    assert len(found) == 1
    return found[0]


def _coverage(report: dict, wallet: str, capability: str) -> list[dict]:
    return [
        row for row in report["coverage"]
        if row["wallet_id"] == wallet and row["capability"] == capability
    ]


def _of_kind(rows: list[dict], kind: str) -> list[dict]:
    return [row for row in rows if row["payload"]["kind"] == kind]


def _called(backend: ChainBackend, selector: str) -> bool:
    return any(row.get("selector") == selector for row in backend.rpc)


def _approval_log(network: str, token: str, owner: str, spender: str, tx: str, block: int, index: int) -> dict:
    return {
        "host": HOST[network],
        "address": token,
        "blockNumber": hex(block),
        "data": "0x" + UINT256_MAX.to_bytes(32, "big").hex(),
        "logIndex": hex(index),
        "topics": [ERC20_APPROVAL_TOPIC, pad_address(owner), pad_address(spender)],
        "transactionHash": tx,
    }


def _incidents() -> list[dict]:
    return [
        {
            "title": "Documented pool parameter note",
            "url": "https://news.example.invalid/pool-note",
            "body": "A documented lending-market parameter note.",
            "published_at": "2026-10-01T00:00:00Z",
            "protocols": ["kinza"],
        },
        {
            "title": "Documented token supply note",
            "url": "https://news.example.invalid/token-supply",
            "body": "A documented token supply distribution note.",
            "published_at": "2026-10-01T00:00:00Z",
            "protocols": ["uxlink"],
        },
    ]


def _news_file(directory: Path, items: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "news.json"
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    return path


def _call_arg(data: str) -> str | None:
    if not isinstance(data, str) or len(data) < 74:
        return None
    return "0x" + data[10:74][-40:]


def _forbid(blob: str) -> None:
    lowered = blob.lower()
    assert "redeemable" not in lowered
    assert "redeem-only" not in lowered
    assert "redeem_only" not in lowered
    assert "1:1" not in blob
    assert '"claimable": true' not in lowered
    assert '"claimable":true' not in lowered


def _claim_true(document: dict) -> None:
    document["tokens"][1]["claimability"] = True


def _redeem_only(document: dict) -> None:
    document["tokens"][1]["status"] = "redeem-only"


def _one_to_one(document: dict) -> None:
    document["tokens"][1]["price_mapping"] = "1:1"


def _redeemable(document: dict) -> None:
    document["tokens"][1]["status"] = "redeemable"


def _claimable_word(document: dict) -> None:
    document["tokens"][0]["claimability"] = "claimable"


def _action_client_text() -> str:
    chunks = []
    for path in (ROOT / "action-client").rglob("*"):
        if "node_modules" in path.parts or path.name == "package-lock.json":
            continue
        if path.is_file() and path.suffix in {".ts", ".js", ".json", ".md"}:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks).lower()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
