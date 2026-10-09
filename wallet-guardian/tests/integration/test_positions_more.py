"""MCM positions-more. Opt-in sDAI, Steakhouse USDC, and Ethereum USDC Comet.

Assertions go through ``guardian.cli.main`` (``run-once --no-send``) with an
offline eth_call transport. Wallet addresses are synthetic. Protocol pins are
the documented public contracts.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx
import pytest
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from guardian.cli import main
from guardian.protocols.compound_v3 import STALE_ACCRUAL_SECONDS, CompoundV3
from guardian.protocols.models import PositionLeg, PositionObservation
from guardian.protocols.pins import (
    COMET_IMPLEMENTATION,
    COMET_USDC,
    DAI,
    MORPHO,
    SDAI,
    STEAKHOUSE_USDC,
    USDC,
)
from guardian.protocols.portfolio import aggregate_portfolio
from guardian.protocols.views import is_write
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
ADDRESS = to_checksum_address("0x" + ("0" * 39) + "1")
OTHER = to_checksum_address("0x" + ("0" * 39) + "2")
KEY = "synthetic-etherscan-key"
SUSDE = "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
HEAD = 100
TIMESTAMP = 1_700_003_600
BLOCK = hex(HEAD)

SDAI_L = SDAI.lower()
DAI_L = DAI.lower()
STEAK_L = STEAKHOUSE_USDC.lower()
USDC_L = USDC.lower()
MORPHO_L = MORPHO.lower()
COMET_L = COMET_USDC.lower()
IMPL_L = COMET_IMPLEMENTATION.lower()

ASSET_A = "0x" + ("0" * 38) + "a1"
ASSET_B = "0x" + ("0" * 38) + "a2"
ASSET_C = "0x" + ("0" * 38) + "a3"
FEED_BASE = "0x" + ("0" * 38) + "b0"
FEED_A = "0x" + ("0" * 38) + "b1"
FEED_B = "0x" + ("0" * 38) + "b2"
FEED_C = "0x" + ("0" * 38) + "b3"
WRONG = "0x" + ("0" * 38) + "ff"

_SENDS = (
    "eth_sendRawTransaction",
    "eth_sendTransaction",
    "eth_sign",
    "eth_signTypedData",
    "eth_signTypedData_v4",
    "personal_sign",
    "wallet_sendCalls",
)
_WRITES = (
    "absorb(address,address[])",
    "accrueAccount(address)",
    "approve(address,uint256)",
    "buyCollateral(address,uint256,uint256,address)",
    "deposit(uint256,address)",
    "redeem(uint256,address,address)",
    "supply(address,uint256)",
    "transfer(address,uint256)",
    "withdraw(uint256,address,address)",
)


def _selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


def _word(types: list[str], values: list[object]) -> str:
    return "0x" + encode(types, values).hex()


SEL_ASSET = _selector("asset()")
SEL_BALANCE = _selector("balanceOf(address)")
SEL_CONVERT = _selector("convertToAssets(uint256)")
SEL_DECIMALS = _selector("decimals()")
SEL_MAX = _selector("maxWithdraw(address)")
SEL_MORPHO = _selector("MORPHO()")
SEL_NAME = _selector("name()")
SEL_SYMBOL = _selector("symbol()")
SEL_BASE = _selector("baseToken()")
SEL_FEED = _selector("baseTokenPriceFeed()")
SEL_COLLATERAL = _selector("collateralBalanceOf(address,address)")
SEL_INFO = _selector("getAssetInfo(uint8)")
SEL_PRICE = _selector("getPrice(address)")
SEL_COLLATERALIZED = _selector("isBorrowCollateralized(address)")
SEL_LIQUIDATABLE = _selector("isLiquidatable(address)")
SEL_NUM = _selector("numAssets()")
SEL_TOTALS = _selector("totalsBasic()")
SEL_USER = _selector("userBasic(address)")
SEL_ACCRUE = _selector("accrueAccount(address)")
SEL_BORROW = _selector("borrowBalanceOf(address)")
_FORBIDDEN = {_selector(name) for name in _WRITES}


def _addr_word(data: str, index: int) -> str:
    start = 10 + index * 64
    return "0x" + data[start:start + 64][-40:]


def _int_word(data: str, index: int = 0) -> int:
    start = 10 + index * 64
    return int(data[start:start + 64], 16)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class _Vault:
    def __init__(self, asset: str, share_decimals: int, asset_decimals: int, morpho: str | None) -> None:
        self.asset = asset
        self.share_decimals = share_decimals
        self.asset_decimals = asset_decimals
        self.morpho = morpho
        self.shares = 0
        self.quote = 0
        self.max_withdraw = 0
        self.symbol = "sDAI"
        self.name = "Savings Dai"


class _Asset:
    def __init__(self, asset: str, feed: str, decimals: int, balance: int) -> None:
        self.asset = asset
        self.feed = feed
        self.decimals = decimals
        self.balance = balance


class PositionsBackend:
    """Offline JSON-RPC for the pinned vaults and the USDC Comet."""

    def __init__(self) -> None:
        self.head = HEAD
        self.timestamp = TIMESTAMP
        self.calls: list[object] = []
        self.rpc: list[dict] = []
        self.fail: set[tuple[str, str]] = set()
        self.sdai = _Vault(DAI_L, 18, 18, None)
        self.steak = _Vault(USDC_L, 18, 6, MORPHO_L)
        self.steak.symbol = "steakUSDC"
        self.steak.name = "Steakhouse USDC"
        self.base = USDC_L
        self.base_decimals = 6
        self.base_feed = FEED_BASE
        self.assets = [
            _Asset(ASSET_A, FEED_A, 18, 5 * 10**18),
            _Asset(ASSET_B, FEED_B, 8, 0),
            _Asset(ASSET_C, FEED_C, 18, 250_000_000),
        ]
        self.num_assets = 3
        self.principal = 1_500_000
        self.assets_in = 1
        # Synthetic stored indices at 1.1x and 1.2x Comet's 10**15 index scale.
        self.supply_index = 1_100_000_000_000_000
        self.borrow_index = 1_200_000_000_000_000
        self.last_accrual = TIMESTAMP
        self.collateralized = True
        self.liquidatable = False
        self.prices = {FEED_BASE: 10**8, FEED_A: 10**8, FEED_B: 10**8, FEED_C: 10**8}
        self.oracle = "ok"
        self.pause_flags = 0

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        body = getattr(call, "body", b"") or b""
        if not body:
            payload = {"message": "No transactions found", "result": [], "status": "0"}
            return RawResponse(200, {}, json.dumps(payload).encode())
        message = json.loads(body.decode("utf-8"))
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), list) else []
        record: dict = {"method": method}
        if method == "eth_call" and params and isinstance(params[0], dict):
            data = str(params[0].get("data") or "")
            record["data"] = data
            record["selector"] = data[:10]
            record["to"] = str(params[0].get("to") or "").lower()
            record["block"] = params[1] if len(params) > 1 else None
        self.rpc.append(record)
        result = self._rpc(method, params, record)
        if result is None:
            encoded = {"error": {"code": -32000, "message": "fixture-failure"}, "id": 1, "jsonrpc": "2.0"}
        else:
            encoded = {"id": 1, "jsonrpc": "2.0", "result": result}
        return RawResponse(200, {}, json.dumps(encoded).encode())

    def _rpc(self, method: str, params: list, record: dict) -> object | None:
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            return {
                "hash": "0x" + "11" * 32,
                "number": hex(self.head),
                "timestamp": hex(self.timestamp),
            }
        if method == "eth_getBalance":
            return "0x1"
        if method == "eth_getLogs":
            return []
        if method == "eth_call":
            return self._call(str(record.get("to") or ""), str(record.get("data") or ""))
        return []

    def _call(self, to: str, data: str) -> str | None:
        selector = data[:10]
        if (to, selector) in self.fail:
            return None
        vault = self._vault(to)
        if vault is not None:
            return self._vault_call(vault, selector, data)
        if to == DAI_L and selector == SEL_DECIMALS:
            return _word(["uint8"], [self.sdai.asset_decimals])
        if to == USDC_L and selector == SEL_DECIMALS:
            return _word(["uint8"], [self.steak.asset_decimals])
        if to == COMET_L:
            return self._comet(selector, data)
        for asset in self.assets:
            if to == asset.asset and selector == SEL_DECIMALS:
                return _word(["uint8"], [asset.decimals])
        return "0x"

    def _vault(self, to: str) -> _Vault | None:
        if to == SDAI_L:
            return self.sdai
        if to == STEAK_L:
            return self.steak
        return None

    def _vault_call(self, vault: _Vault, selector: str, data: str) -> str | None:
        del data
        if selector == SEL_ASSET:
            return _word(["address"], [vault.asset])
        if selector == SEL_DECIMALS:
            return _word(["uint8"], [vault.share_decimals])
        if selector == SEL_MORPHO:
            if vault.morpho is None:
                return None
            return _word(["address"], [vault.morpho])
        if selector == SEL_SYMBOL:
            return _word(["string"], [vault.symbol])
        if selector == SEL_NAME:
            return _word(["string"], [vault.name])
        if selector == SEL_BALANCE:
            return _word(["uint256"], [vault.shares])
        if selector == SEL_CONVERT:
            return _word(["uint256"], [vault.quote])
        if selector == SEL_MAX:
            return _word(["uint256"], [vault.max_withdraw])
        return "0x"

    def _comet(self, selector: str, data: str) -> str | None:
        if selector == SEL_BASE:
            return _word(["address"], [self.base])
        if selector == SEL_FEED:
            return _word(["address"], [self.base_feed])
        if selector == SEL_DECIMALS:
            return _word(["uint8"], [self.base_decimals])
        if selector == SEL_NUM:
            return _word(["uint8"], [self.num_assets])
        if selector == SEL_TOTALS:
            return _word(
                ["(uint64,uint64,uint64,uint64,uint104,uint104,uint40,uint8)"],
                [(self.supply_index, self.borrow_index, 0, 0, 0, 0, self.last_accrual, self.pause_flags)],
            )
        if selector == SEL_USER:
            return _word(
                ["(int104,uint64,uint64,uint16,uint8)"],
                [(self.principal, 0, 0, self.assets_in, 0)],
            )
        if selector == SEL_INFO:
            index = _int_word(data)
            if index >= len(self.assets):
                return None
            asset = self.assets[index]
            return _word(
                ["(uint8,address,address,uint64,uint64,uint64,uint64,uint128)"],
                [(index, asset.asset, asset.feed, 10**asset.decimals, 0, 0, 0, 0)],
            )
        if selector == SEL_COLLATERAL:
            asset = _addr_word(data, 1)
            row = next((item for item in self.assets if item.asset == asset), None)
            if row is None:
                return None
            return _word(["uint128"], [row.balance])
        if selector == SEL_PRICE:
            if self.oracle == "revert":
                return None
            feed = _addr_word(data, 0)
            if self.oracle == "zero":
                return _word(["uint256"], [0])
            price = self.prices.get(feed)
            if price is None:
                return None
            return _word(["uint256"], [price])
        if selector == SEL_COLLATERALIZED:
            return _word(["bool"], [self.collateralized])
        if selector == SEL_LIQUIDATABLE:
            return _word(["bool"], [self.liquidatable])
        return "0x"


class _Desk:
    def __init__(self, config: Path, state: Path, backend: PositionsBackend) -> None:
        self.config = config
        self.state = state
        self.backend = backend


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugins: list[str]) -> _Desk:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = plugins
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
    _write_private(accounts_path, json.dumps(accounts) + "\n")
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts_path))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return _Desk(config, state, PositionsBackend())


def _cli(capsys: pytest.CaptureFixture[str], desk: _Desk) -> dict:
    code = main(["run-once", "--mcm-config", str(desk.config), "--no-send"], backend=desk.backend)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload.get("status") != "error"
    assert payload["sent"] == 0
    assert payload["delivery_status"] == "no_send"
    assert payload["live_verified"] is False
    assert payload.get("live_pass") is not True
    blob = json.dumps(payload)
    assert KEY not in blob
    assert ADDRESS.lower() not in blob.lower()
    assert OTHER.lower() not in blob.lower()
    for call in desk.backend.calls:
        body = getattr(call, "body", b"") or b""
        text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
        for name in _SENDS:
            assert name not in text
    assert _selectors(desk.backend).isdisjoint(_FORBIDDEN)
    return payload


def _position(report: dict, plugin_id: str) -> dict:
    found = [row for row in report["positions"] if row["plugin_id"] == plugin_id and row["wallet_id"] == WALLET]
    assert len(found) == 1
    return found[0]


def _leg(legs: list[dict], kind: str, identity: str) -> dict:
    found = [leg for leg in legs if leg["kind"] == kind and leg["identity"] == identity]
    assert len(found) == 1, (kind, identity, legs)
    return found[0]


def _calls(backend: PositionsBackend, to: str, selector: str) -> list[dict]:
    return [row for row in backend.rpc if row.get("to") == to and row.get("selector") == selector]


def _selectors(backend: PositionsBackend) -> set[str]:
    return {str(row["selector"]) for row in backend.rpc if row.get("selector")}


def _targets(backend: PositionsBackend) -> set[str]:
    return {str(row.get("to") or "") for row in backend.rpc if row.get("method") == "eth_call"}


def _stored(desk: _Desk, plugin_id: str) -> dict:
    database = Database(desk.state / "monitor.sqlite")
    try:
        rows = database.capability_state_for(TENANT)
    finally:
        database.close()
    for row in rows:
        if row["capability"] != "positions":
            continue
        for item in row["payload"].get("position_state") or []:
            if isinstance(item, dict) and item.get("plugin_id") == plugin_id:
                return item
    raise AssertionError(plugin_id)


def _portfolio(row: dict):
    legs = []
    for leg in row["legs"]:
        labels = leg["labels"]
        legs.append(PositionLeg(
            kind=leg["kind"],
            identity=leg["identity"],
            raw_amount=leg["raw_amount"],
            decimals=leg["decimals"],
            network=leg["network"],
            plugin_id=row["plugin_id"],
            duplicates=labels.get("duplicates"),
        ))
    return aggregate_portfolio([PositionObservation(
        plugin_id=row["plugin_id"],
        wallet_id=WALLET,
        network=row["network"],
        status=row["status"],
        legs=tuple(legs),
    )])


def _fund_vaults(backend: PositionsBackend) -> None:
    backend.sdai.shares = 2 * 10**18
    backend.sdai.quote = backend.sdai.shares - 1
    backend.sdai.max_withdraw = backend.sdai.quote - 50
    backend.steak.shares = 5 * 10**18
    backend.steak.quote = 1_234_565
    backend.steak.max_withdraw = 1_234_500


def _bound_block(backend: PositionsBackend, *targets: str) -> None:
    rows = [row for row in backend.rpc if row.get("method") == "eth_call" and row.get("to") in targets]
    assert rows
    assert {row["block"] for row in rows} == {BLOCK}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def test_mcm_pos_01_opt_in_pin_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Opt-in pins observe when they agree, and a disagreement is not a position."""
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert example["plugins"] == ["lista-slisbnb", "aave-v3", "erc4626-registered", "kinza", "uxlink-holdings"]
    assert "erc4626-pins" not in example["plugins"]
    assert "compound-v3" not in example["plugins"]
    example_text = json.dumps(example).lower()
    assert SDAI_L not in example_text
    assert STEAK_L not in example_text
    assert COMET_L not in example_text
    pins = (ROOT / "src" / "guardian" / "protocols" / "pins.py").read_text(encoding="utf-8").lower()
    assert SUSDE.lower() not in pins
    assert "accrueAccount(address)" not in CompoundV3.specs
    assert is_write("accrueAccount(address)") is True

    quiet = _desk(tmp_path / "quiet", monkeypatch, ["erc4626-registered"])
    quiet_report = _cli(capsys, quiet)
    quiet_row = _position(quiet_report, "erc4626")
    assert quiet_row["status"] == "unknown"
    assert "vaults_not_configured" in quiet_row["reason_codes"]
    assert _targets(quiet.backend).isdisjoint({SDAI_L, STEAK_L, COMET_L, IMPL_L})

    happy = _desk(tmp_path / "happy", monkeypatch, ["erc4626-pins", "compound-v3"])
    _fund_vaults(happy.backend)
    happy.backend.principal = 1_500_000
    report = _cli(capsys, happy)
    vaults = _position(report, "erc4626_pins")
    comet = _position(report, "compound_v3")
    assert vaults["status"] == "observed"
    assert comet["status"] == "observed"
    sdai = _leg(vaults["legs"], "receipt", SDAI_L)
    assert sdai["labels"]["safety_certificate"] == "false"
    assert sdai["labels"]["asset_agreement"] == "true"
    assert sdai["labels"]["agreement_is_not_safety_certificate"] == "true"
    assert sdai["labels"]["symbol"] == "sDAI"
    assert sdai["labels"]["symbol_untrusted"] == "true"
    steak = _leg(vaults["legs"], "receipt", STEAK_L)
    assert steak["labels"]["morpho_agreement"] == "true"
    assert steak["labels"]["exposure"] == "vault"
    assert MORPHO_L not in {leg["identity"] for leg in vaults["legs"]}
    assert IMPL_L not in _targets(happy.backend)
    supply = _leg(comet["legs"], "receipt", COMET_L)
    assert supply["labels"]["safety_certificate"] == "false"
    assert supply["labels"]["role"] == "base_supply"
    assert "health_factor" not in json.dumps(comet)
    _bound_block(happy.backend, SDAI_L, STEAK_L, COMET_L)

    mismatch = _desk(tmp_path / "asset", monkeypatch, ["erc4626-pins", "compound-v3"])
    _fund_vaults(mismatch.backend)
    mismatch.backend.sdai.asset = WRONG
    mismatched = _cli(capsys, mismatch)
    bad = _position(mismatched, "erc4626_pins")
    assert bad["status"] == "unknown"
    assert bad["legs"] == []
    assert "pin_mismatch" in bad["reason_codes"]
    assert bad["status"] != "none_observed"
    assert _position(mismatched, "compound_v3")["status"] == "observed"

    morpho = _desk(tmp_path / "morpho", monkeypatch, ["erc4626-pins"])
    _fund_vaults(morpho.backend)
    morpho.backend.steak.morpho = WRONG
    morpho_row = _position(_cli(capsys, morpho), "erc4626_pins")
    assert morpho_row["status"] == "unknown"
    assert morpho_row["legs"] == []
    assert "pin_mismatch" in morpho_row["reason_codes"]

    decimals = _desk(tmp_path / "decimals", monkeypatch, ["erc4626-pins"])
    _fund_vaults(decimals.backend)
    decimals.backend.sdai.share_decimals = 8
    decimals_row = _position(_cli(capsys, decimals), "erc4626_pins")
    assert decimals_row["status"] == "unknown"
    assert decimals_row["legs"] == []
    assert "pin_mismatch" in decimals_row["reason_codes"]

    base = _desk(tmp_path / "base", monkeypatch, ["compound-v3", "erc4626-pins"])
    _fund_vaults(base.backend)
    base.backend.base = WRONG
    base_report = _cli(capsys, base)
    base_row = _position(base_report, "compound_v3")
    assert base_row["status"] == "unknown"
    assert base_row["legs"] == []
    assert "pin_mismatch" in base_row["reason_codes"]
    assert _position(base_report, "erc4626_pins")["status"] == "observed"


def test_mcm_pos_02_mixed_decimals_rounding_no_double_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Chain decimals differ, the quote rounds down, and the quote is not a second holding."""
    desk = _desk(tmp_path, monkeypatch, ["erc4626-pins"])
    _fund_vaults(desk.backend)
    report = _cli(capsys, desk)
    row = _position(report, "erc4626_pins")
    assert row["status"] == "observed"
    sdai = _leg(row["legs"], "receipt", SDAI_L)
    sdai_quote = _leg(row["legs"], "underlying", DAI_L)
    steak = _leg(row["legs"], "receipt", STEAK_L)
    steak_quote = _leg(row["legs"], "underlying", USDC_L)
    assert sdai["decimals"] == 18
    assert sdai_quote["decimals"] == 18
    assert steak["decimals"] == 18
    assert steak_quote["decimals"] == 6
    assert sdai["raw_amount"] == 2 * 10**18
    assert sdai_quote["raw_amount"] == sdai["raw_amount"] - 1
    assert sdai_quote["labels"]["rounding"] == "down"
    assert sdai_quote["labels"]["role"] == "quote"
    assert sdai_quote["labels"]["counted"] == "false"
    assert sdai_quote["labels"]["duplicates"] == SDAI_L
    assert int(sdai_quote["labels"]["max_withdraw"]) == sdai_quote["raw_amount"] - 50
    assert sdai_quote["labels"]["quote_is_withdrawal_promise"] == "false"
    assert sdai_quote["labels"]["redemption_guaranteed"] == "false"
    assert steak_quote["raw_amount"] == 1_234_565
    assert int(steak_quote["labels"]["max_withdraw"]) == 1_234_500
    assert steak_quote["raw_amount"] != steak["raw_amount"]
    amounts = {leg["raw_amount"] for leg in row["legs"]}
    assert sdai["raw_amount"] + sdai_quote["raw_amount"] not in amounts
    assert steak["raw_amount"] + steak_quote["raw_amount"] not in amounts
    assert all(int(row["data"][10:74], 16) == sdai["raw_amount"] for row in _calls(desk.backend, SDAI_L, SEL_CONVERT))
    assert all(int(row["data"][10:74], 16) == steak["raw_amount"] for row in _calls(desk.backend, STEAK_L, SEL_CONVERT))
    counted = _portfolio(row)
    assert counted.amount("ethereum-mainnet", SDAI_L) == sdai["raw_amount"]
    assert counted.amount("ethereum-mainnet", DAI_L) == 0
    assert counted.amount("ethereum-mainnet", STEAK_L) == steak["raw_amount"]
    assert counted.amount("ethereum-mainnet", USDC_L) == 0
    _bound_block(desk.backend, SDAI_L, STEAK_L, DAI_L, USDC_L)


def test_mcm_pos_03_collateral_enumeration_supply_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Every collateral slot is read. Supply, debt, and collateral stay different legs."""
    desk = _desk(tmp_path / "move", monkeypatch, ["compound-v3"])
    desk.backend.assets_in = 1
    supply = _cli(capsys, desk)
    row = _position(supply, "compound_v3")
    assert row["status"] == "observed"
    infos = _calls(desk.backend, COMET_L, SEL_INFO)
    assert [_int_word(item["data"]) for item in infos] == [0, 1, 2]
    balances = {_addr_word(item["data"], 1) for item in _calls(desk.backend, COMET_L, SEL_COLLATERAL)}
    assert balances == {ASSET_A, ASSET_B, ASSET_C}
    base = _leg(row["legs"], "receipt", COMET_L)
    assert base["labels"]["role"] == "base_supply"
    assert base["labels"]["principal"] == "1500000"
    assert base["raw_amount"] == 1_650_000
    assert base["raw_amount"] != int(base["labels"]["principal"])
    assert base["decimals"] == 6
    assert not any(leg["kind"] == "debt" for leg in row["legs"])
    first = _leg(row["legs"], "underlying", ASSET_A)
    third = _leg(row["legs"], "underlying", ASSET_C)
    assert first["labels"]["role"] == "collateral"
    assert first["kind"] != "debt"
    assert first["decimals"] == 18
    assert first["raw_amount"] == 5 * 10**18
    assert third["decimals"] == 18
    assert third["raw_amount"] == 250_000_000
    assert ASSET_B not in {leg["identity"] for leg in row["legs"]}
    assert base["raw_amount"] != first["raw_amount"]
    assert not any(leg["raw_amount"] == base["raw_amount"] + first["raw_amount"] for leg in row["legs"])
    _bound_block(desk.backend, COMET_L)

    desk.backend.rpc.clear()
    desk.backend.principal = -2_000_000
    debt_report = _cli(capsys, desk)
    debt = _position(debt_report, "compound_v3")
    assert debt["status"] == "observed"
    assert not any(leg["labels"].get("role") == "base_supply" for leg in debt["legs"])
    owed = _leg(debt["legs"], "debt", "compound-v3:base-debt")
    assert owed["labels"]["principal"] == "-2000000"
    assert owed["raw_amount"] == 2_400_000
    assert owed["raw_amount"] != 2_000_000
    assert owed["decimals"] == 6
    assert owed["labels"]["role"] == "base_debt"
    still = _leg(debt["legs"], "underlying", ASSET_A)
    assert still["labels"]["role"] == "collateral"
    assert still["kind"] != "debt"
    assert still["raw_amount"] == 5 * 10**18

    desk.backend.rpc.clear()
    desk.backend.principal = 0
    flat = _position(_cli(capsys, desk), "compound_v3")
    assert flat["status"] == "observed"
    assert {leg["labels"]["role"] for leg in flat["legs"]} == {"collateral"}
    assert not any(leg["kind"] == "debt" or leg["kind"] == "receipt" for leg in flat["legs"])

    bound = _desk(tmp_path / "bound", monkeypatch, ["compound-v3"])
    bound.backend.num_assets = 33
    bounded = _position(_cli(capsys, bound), "compound_v3")
    assert bounded["status"] == "unknown"
    assert bounded["legs"] == []
    assert "result_exceeds_bound" in bounded["reason_codes"]
    assert _calls(bound.backend, COMET_L, SEL_INFO) == []
    assert bounded["status"] != "none_observed"


def test_mcm_pos_04_stale_accrual_oracle_and_named_predicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Present value uses the stored index. Oracle failure is not a healthy predicate."""
    fresh = _desk(tmp_path / "fresh", monkeypatch, ["compound-v3"])
    fresh.backend.last_accrual = TIMESTAMP
    fresh.backend.collateralized = True
    fresh.backend.liquidatable = False
    row = _position(_cli(capsys, fresh), "compound_v3")
    supply = _leg(row["legs"], "receipt", COMET_L)
    assert supply["raw_amount"] == 1_650_000
    assert supply["labels"]["principal"] == str(fresh.backend.principal)
    assert supply["labels"]["accrual"] == "stored_index"
    assert supply["labels"]["last_accrual_time"] == str(TIMESTAMP)
    assert supply["labels"]["base_supply_index"] == str(fresh.backend.supply_index)
    assert supply["labels"]["base_borrow_index"] == str(fresh.backend.borrow_index)
    assert supply["labels"]["is_borrow_collateralized"] == "true"
    assert supply["labels"]["is_liquidatable"] == "false"
    assert supply["labels"]["block_number"] == str(HEAD)
    blob = json.dumps(row)
    assert "accrued_to_now" not in blob
    assert "health_factor" not in blob
    assert _calls(fresh.backend, COMET_L, SEL_ACCRUE) == []
    assert _calls(fresh.backend, COMET_L, SEL_BALANCE) == []
    assert _calls(fresh.backend, COMET_L, SEL_BORROW) == []
    _bound_block(fresh.backend, COMET_L)

    stale = _desk(tmp_path / "stale", monkeypatch, ["compound-v3"])
    stale.backend.last_accrual = TIMESTAMP - STALE_ACCRUAL_SECONDS - 1
    stale.backend.collateralized = False
    stale.backend.liquidatable = True
    stale_row = _position(_cli(capsys, stale), "compound_v3")
    stale_supply = _leg(stale_row["legs"], "receipt", COMET_L)
    assert stale_supply["raw_amount"] == 1_650_000
    assert stale_supply["labels"]["principal"] == str(stale.backend.principal)
    assert stale_supply["labels"]["accrual"] == "stale_stored_index"
    assert stale_supply["labels"]["is_borrow_collateralized"] == "false"
    assert stale_supply["labels"]["is_liquidatable"] == "true"
    assert "accrued_to_now" not in json.dumps(stale_row)
    assert "health_factor" not in json.dumps(stale_row)

    future = _desk(tmp_path / "future", monkeypatch, ["compound-v3"])
    future.backend.last_accrual = TIMESTAMP + 10
    future_row = _position(_cli(capsys, future), "compound_v3")
    assert future_row["status"] == "unknown"
    assert future_row["legs"] == []
    assert "accrual_inconsistent" in future_row["reason_codes"]
    assert _calls(future.backend, COMET_L, SEL_LIQUIDATABLE) == []
    assert "is_liquidatable" not in json.dumps(future_row)

    oracle = _desk(tmp_path / "oracle", monkeypatch, ["compound-v3"])
    oracle.backend.oracle = "revert"
    oracle.backend.rpc.clear()
    oracle_row = _position(_cli(capsys, oracle), "compound_v3")
    assert oracle_row["status"] == "unknown"
    assert oracle_row["legs"] == []
    assert "oracle_failed" in oracle_row["reason_codes"]
    assert oracle_row["status"] != "none_observed"
    assert _calls(oracle.backend, COMET_L, SEL_PRICE)
    assert _calls(oracle.backend, COMET_L, SEL_LIQUIDATABLE) == []
    assert _calls(oracle.backend, COMET_L, SEL_COLLATERALIZED) == []
    assert "is_liquidatable" not in json.dumps(oracle_row)
    assert "health_factor" not in json.dumps(oracle_row)

    zero = _desk(tmp_path / "zero-price", monkeypatch, ["compound-v3"])
    zero.backend.oracle = "zero"
    zero_row = _position(_cli(capsys, zero), "compound_v3")
    assert zero_row["status"] == "unknown"
    assert zero_row["legs"] == []
    assert "oracle_failed" in zero_row["reason_codes"]
    assert _calls(zero.backend, COMET_L, SEL_LIQUIDATABLE) == []


def test_mcm_pos_05_missing_reads_keep_last_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed reread is unknown and leaves the last observed amounts in state."""
    desk = _desk(tmp_path, monkeypatch, ["erc4626-pins", "compound-v3"])
    _fund_vaults(desk.backend)
    first = _cli(capsys, desk)
    vaults = _position(first, "erc4626_pins")
    comet = _position(first, "compound_v3")
    assert vaults["status"] == "observed"
    assert comet["status"] == "observed"
    kept_vaults = _stored(desk, "erc4626_pins")
    kept_comet = _stored(desk, "compound_v3")
    assert kept_vaults["status"] == "observed"
    assert kept_comet["status"] == "observed"
    assert any(leg["raw_amount"] == desk.backend.sdai.shares for leg in kept_vaults["legs"])
    assert any(leg["raw_amount"] == 1_650_000 for leg in kept_comet["legs"])
    assert kept_comet["metrics"]["principal"] == "1500000"
    assert kept_comet["metrics"]["is_borrow_collateralized"] == "true"
    assert kept_comet["metrics"]["accrual"] == "stored_index"

    desk.backend.fail.add((SDAI_L, SEL_BALANCE))
    desk.backend.fail.add((COMET_L, SEL_USER))
    desk.backend.sdai.shares = 0
    desk.backend.principal = 0
    second = _cli(capsys, desk)
    failed_vaults = _position(second, "erc4626_pins")
    failed_comet = _position(second, "compound_v3")
    assert failed_vaults["status"] == "unknown"
    assert failed_comet["status"] == "unknown"
    assert failed_vaults["legs"] == []
    assert failed_comet["legs"] == []
    assert failed_vaults["status"] != "none_observed"
    assert failed_comet["status"] != "none_observed"
    assert "read_failed" in failed_vaults["reason_codes"]
    assert "read_failed" in failed_comet["reason_codes"]
    assert _stored(desk, "erc4626_pins") == kept_vaults
    assert _stored(desk, "compound_v3") == kept_comet
    assert "is_liquidatable" not in json.dumps(failed_comet)


def test_mcm_pos_06_stored_index_present_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Principal stays metadata. The counted amount is principal times index over 10**15.

    Equality of lastAccrualTime and the block timestamp does not skip that
    conversion. Division rounds down. The plugin does not accrue the account.
    """
    desk = _desk(tmp_path / "supply", monkeypatch, ["compound-v3"])
    for asset in desk.backend.assets:
        asset.balance = 0
    desk.backend.last_accrual = TIMESTAMP
    desk.backend.principal = 1_500_000
    desk.backend.supply_index = 1_100_000_000_000_000
    desk.backend.borrow_index = 1_200_000_000_000_000
    row = _position(_cli(capsys, desk), "compound_v3")
    assert row["status"] == "observed"
    supply = _leg(row["legs"], "receipt", COMET_L)
    assert supply["raw_amount"] == 1_650_000
    assert supply["labels"]["principal"] == "1500000"
    assert supply["labels"]["role"] == "base_supply"
    assert supply["labels"]["accrual"] == "stored_index"
    assert supply["labels"]["last_accrual_time"] == str(TIMESTAMP)
    assert supply["labels"]["block_timestamp"] == str(TIMESTAMP)
    assert supply["labels"]["base_supply_index"] == "1100000000000000"
    assert supply["raw_amount"] != 1_800_000
    assert _calls(desk.backend, COMET_L, SEL_ACCRUE) == []
    assert _calls(desk.backend, COMET_L, SEL_BALANCE) == []
    assert _calls(desk.backend, COMET_L, SEL_BORROW) == []
    _bound_block(desk.backend, COMET_L)

    desk.backend.rpc.clear()
    desk.backend.principal = -2_000_000
    debt = _position(_cli(capsys, desk), "compound_v3")
    owed = _leg(debt["legs"], "debt", "compound-v3:base-debt")
    assert owed["raw_amount"] == 2_400_000
    assert owed["labels"]["principal"] == "-2000000"
    assert owed["labels"]["role"] == "base_debt"
    assert owed["labels"]["base_borrow_index"] == "1200000000000000"
    assert owed["raw_amount"] != 2_200_000
    assert not any(leg["labels"].get("role") == "base_supply" for leg in debt["legs"])

    desk.backend.rpc.clear()
    desk.backend.principal = 5
    desk.backend.supply_index = 1_500_000_000_000_000
    truncated = _position(_cli(capsys, desk), "compound_v3")
    partial = _leg(truncated["legs"], "receipt", COMET_L)
    assert partial["raw_amount"] == 7
    assert partial["labels"]["principal"] == "5"
    assert partial["raw_amount"] != 8

    desk.backend.rpc.clear()
    desk.backend.principal = -5
    desk.backend.borrow_index = 1_500_000_000_000_000
    borrow_truncated = _position(_cli(capsys, desk), "compound_v3")
    borrow_partial = _leg(borrow_truncated["legs"], "debt", "compound-v3:base-debt")
    assert borrow_partial["raw_amount"] == 7
    assert borrow_partial["labels"]["principal"] == "-5"
    assert borrow_partial["raw_amount"] != 8
