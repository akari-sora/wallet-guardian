"""MCM-LIVE-01R. Free-tier indexers and catch-up, through the real CLI.

Every test uses ``guardian run-once --mcm-config --no-send`` or the shadow
tool entrypoint. The backend is a fixture. Sockets and HTTP clients are blocked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from guardian.chains.evm.accounting import TRANSFER_TOPIC, pad_address
from guardian.chains.evm.permissions import ERC20_APPROVAL_TOPIC
from guardian.chains.evm.permits import PERMIT2_PERMIT_TOPIC
from guardian.cli import main
from guardian.errors import GuardianError
from guardian.protocols.pins import KINZA_POOLS, KINZA_PROVIDERS
from guardian.runtime_binding import assert_worktree
from guardian.sources.fixtures import PreparedCall, RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET_A = "11111111-1111-4111-8111-111111111111"
WALLET_B = "33333333-3333-4333-8333-333333333333"
ADDRESS_A = to_checksum_address("0x" + ("0" * 39) + "1")
ADDRESS_B = to_checksum_address("0x" + ("0" * 39) + "2")
OTHER = to_checksum_address("0x" + ("1" * 40))
TOKEN = to_checksum_address("0x" + ("0" * 38) + "aa")
SPENDER = to_checksum_address("0x" + ("0" * 38) + "bb")
UNDERLYING = to_checksum_address("0x" + ("0" * 38) + "11")
ATOKEN = to_checksum_address("0x" + ("0" * 38) + "22")
DEBT = to_checksum_address("0x" + ("0" * 38) + "33")
KEY = "synthetic-etherscan-key"
NODEREAL_KEY = "synthetic-nodereal-key"
BLOCKSCOUT_KEY = "synthetic-blockscout-key"
WEI = 10**18
LARGE = 5 * WEI
UINT256_MAX = 2**256 - 1
CHAIN = {
    "arbitrum.example.invalid": "0xa4b1",
    "avalanche.example.invalid": "0xa86a",
    "base.example.invalid": "0x2105",
    "bsc.example.invalid": "0x38",
    "ethereum.example.invalid": "0x1",
    "optimism.example.invalid": "0xa",
    "polygon.example.invalid": "0x89",
}
BLOCK_HASH = "0x" + ("ab" * 32)


def _selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


SEL_ACCOUNT = _selector("getUserAccountData(address)")
SEL_ALLOWANCE = _selector("allowance(address,address)")
SEL_BALANCE = _selector("balanceOf(address)")
SEL_DECIMALS = _selector("decimals()")
SEL_POOL = _selector("getPool()")
SEL_PROVIDER = _selector("ADDRESSES_PROVIDER()")
SEL_RESERVES = _selector("getReservesList()")
SEL_RESERVE_DATA = _selector("getReserveData(address)")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _abi(types: list[str], values: list[object]) -> str:
    return "0x" + encode(types, values).hex()


def _tx(index: int) -> str:
    return "0x" + f"{index:064x}"


class FixtureBackend:
    """Offline JSON-RPC, NodeReal, Blockscout, and Etherscan answers."""

    def __init__(self) -> None:
        self.head = 20
        self.logs: list[dict] = []
        self.calls: list[dict] = []
        self.fail_from: int | None = None
        self.bisect = False
        self.bad_blocks: set[int] = set()
        self.log_error: dict | None = None
        self.rate_limit_once = False
        self._rated = False
        self.nodereal_refused = False
        self.etherscan_plan_chains: set[str] = set()
        self.pro_featured_chains: set[str] = set()
        self.pro_requires_key = False
        self.cloudflare_hosts: set[str] = set()
        self.rate_headers = {"x-credits-remaining": "80000"}
        self.http_log: list[dict] = []
        self.last_authorization = ""
        self.rest_rows: dict[tuple[str, str], list[dict]] = {}
        self.answers: dict[tuple, str] = {}
        self.balances: dict[tuple[str, str, str], int] = {}
        self.accounts: dict[tuple[str, str, str], tuple[int, ...]] = {}
        self.balance_hex = "0x0"

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        body = getattr(call, "body", b"") or b""
        host = urlsplit(url).hostname or ""
        self.last_authorization = _authorization(call)
        self.http_log.append({"authorization": self.last_authorization, "url": url})
        if not body:
            return self._rest(url, host)
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, list):
            self.calls.append({"kind": "batch", "host": host, "methods": [item.get("method") for item in payload]})
            encoded = [
                {"id": item.get("id", index), "jsonrpc": "2.0", "result": self._rpc_method(host, item)}
                for index, item in enumerate(payload, start=1)
            ]
            return RawResponse(200, {}, json.dumps(encoded).encode())
        return self._rpc_response(host, payload)

    def _rpc_response(self, host: str, payload: dict) -> RawResponse:
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        if method == "eth_getLogs" and self.rate_limit_once and not self._rated:
            self._rated = True
            self.calls.append({"kind": "log", "host": host, "rate_limited": True})
            return RawResponse(429, {"retry-after": "2"}, b"{}")
        if method == "nr_getAssetTransfers":
            return self._nodereal(params)
        result = self._rpc_method(host, payload)
        if self.log_error is not None:
            encoded = {"error": self.log_error, "id": 1, "jsonrpc": "2.0"}
            self.log_error = None
        elif result is None:
            encoded = {"error": {"code": -32000, "message": "fixture-log-failure"}, "id": 1, "jsonrpc": "2.0"}
        else:
            encoded = {"id": 1, "jsonrpc": "2.0", "result": result}
        return RawResponse(200, {}, json.dumps(encoded).encode())

    def _rpc_method(self, host: str, payload: dict) -> object | None:
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        if method == "eth_chainId":
            return CHAIN.get(host, "0x1")
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBalance":
            return self.balance_hex
        if method == "eth_getBlockByNumber":
            return {"hash": BLOCK_HASH, "number": hex(self.head), "timestamp": hex(1_700_000_000)}
        if method == "eth_call":
            return self._call(host, params)
        if method == "eth_getLogs":
            return self._logs(host, params)
        if method == "getGenesisHash":
            return "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
        if method == "getSlot":
            return 9
        if method == "getBalance":
            return {"context": {"slot": 9}, "value": 5}
        if method == "getTokenAccountsByOwner":
            return {"context": {"slot": 9}, "value": []}
        if method == "getSignaturesForAddress":
            return []
        if method == "getAccountInfo":
            return {"context": {"slot": 9}, "value": None}
        if method == "getMultipleAccounts":
            return {"context": {"slot": 9}, "value": []}
        return []

    def _logs(self, host: str, params: list) -> object | None:
        filt = params[0] if params and isinstance(params[0], dict) else {}
        start = int(str(filt.get("fromBlock", "0x0")), 16)
        end = int(str(filt.get("toBlock", "0x0")), 16)
        topics = filt.get("topics") if isinstance(filt.get("topics"), list) else []
        address = filt.get("address")
        self.calls.append({
            "address": address.lower() if isinstance(address, str) else None,
            "from": start,
            "host": host,
            "kind": "log",
            "to": end,
            "topics": topics,
        })
        if self.fail_from is not None and start >= self.fail_from:
            return None
        if self.bisect and end > start:
            self.log_error = {"code": -32000, "message": "block range is too large"}
            return None
        if start == end and start in self.bad_blocks:
            return None
        found = []
        for log in self.logs:
            if log.get("host") not in {None, host}:
                continue
            block = int(str(log["blockNumber"]), 16)
            if block < start or block > end:
                continue
            if not _topic_match(log, topics):
                continue
            found.append({key: value for key, value in log.items() if key != "host"})
        return found

    def _call(self, host: str, params: list) -> str | None:
        item = params[0] if params and isinstance(params[0], dict) else {}
        data = str(item.get("data") or "")
        target = str(item.get("to") or "").lower()
        selector = data[:10]
        arg = _call_arg(data)
        self.calls.append({"arg": arg, "host": host, "kind": "eth_call", "selector": selector, "to": target})
        specific = self.answers.get((target, selector, arg))
        if isinstance(specific, str):
            return specific
        generic = self.answers.get((target, selector, None))
        if isinstance(generic, str):
            return generic
        if selector == SEL_ALLOWANCE:
            return _abi(["uint256"], [UINT256_MAX])
        if selector == SEL_BALANCE:
            return _abi(["uint256"], [self.balances.get((host, target, arg or ""), 0)])
        if selector == SEL_DECIMALS:
            return _abi(["uint8"], [18])
        if selector == SEL_ACCOUNT:
            values = self.accounts.get((host, target, arg or ""), (0, 0, 0, 0, 0, 2 * WEI))
            return _abi(["uint256"] * 6, list(values))
        return None

    def _nodereal(self, params: list) -> RawResponse:
        item = params[0] if params and isinstance(params[0], dict) else {}
        category = (item.get("category") or ["external"])[0]
        side = "from" if "fromAddress" in item else "to"
        page_key = item.get("pageKey")
        self.calls.append({"category": category, "kind": "nodereal", "page_key": page_key, "side": side})
        if self.nodereal_refused:
            body = {"error": {"code": -32601, "message": "method not found"}, "id": 1, "jsonrpc": "2.0"}
            return RawResponse(200, {}, json.dumps(body).encode())
        if category != "external" or page_key:
            result = {"transfers": []}
        else:
            wallet = str(item.get("toAddress") or item.get("fromAddress") or "")
            result = {"pageKey": "page-2", "transfers": [_native_transfer(side, wallet)]}
        return RawResponse(200, {}, json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode())

    def _rest(self, url: str, host: str) -> RawResponse:
        query = parse_qs(urlsplit(url).query)
        action = (query.get("action") or [""])[0]
        page = (query.get("page") or ["1"])[0]
        chain = (query.get("chainid") or [""])[0]
        chain_id = (query.get("chain_id") or [""])[0]
        module = (query.get("module") or [""])[0]
        self.calls.append({
            "action": action,
            "chain_id": chain_id,
            "chainid": chain,
            "host": host,
            "kind": "rest",
            "module": module,
            "page": page,
        })
        if host in self.cloudflare_hosts:
            page_html = (
                "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
                "<body>Cloudflare</body></html>"
            )
            return RawResponse(403, {"content-type": "text/html"}, page_html.encode())
        if chain_id and chain_id in self.pro_featured_chains:
            payload = {
                "error": (
                    f"Featured chain {chain_id} requires one of the following plans: "
                    "Builder, Business, Pro"
                ),
            }
            return RawResponse(402, {"content-type": "application/json"}, json.dumps(payload).encode())
        if chain_id and self.pro_requires_key and not self.last_authorization:
            payload = {"error": "Proceed with API key or make a X402 payment"}
            return RawResponse(402, {"content-type": "application/json"}, json.dumps(payload).encode())
        if chain in self.etherscan_plan_chains:
            body = {
                "message": "NOTOK",
                "result": "Free API access is not supported on this chain",
                "status": "0",
            }
            return RawResponse(200, {}, json.dumps(body).encode())
        rows = self.rest_rows.get((host, action), [])
        offset = int((query.get("offset") or ["1000"])[0] or "1000")
        start = (int(page) - 1) * offset
        chunk = rows[start:start + offset]
        if chunk:
            body = {"message": "OK", "result": chunk, "status": "1"}
        else:
            body = {"message": "No transactions found", "result": [], "status": "0"}
        return RawResponse(200, dict(self.rate_headers), json.dumps(body).encode())


def _topic_match(log: dict, topics: list) -> bool:
    own = log.get("topics") or []
    if not topics:
        return True
    topic0 = topics[0]
    allowed = {item.lower() for item in topic0} if isinstance(topic0, list) else {str(topic0).lower()}
    if str(own[0]).lower() not in allowed:
        return False
    for index, topic in enumerate(topics):
        if index == 0 or topic is None:
            continue
        if index >= len(own):
            return False
        if isinstance(topic, list):
            if str(own[index]).lower() not in {item.lower() for item in topic}:
                return False
        elif str(own[index]).lower() != str(topic).lower():
            return False
    return True


def _call_arg(data: str) -> str | None:
    raw = data[10:]
    if len(raw) < 64:
        return None
    return "0x" + raw[:64][-40:]


def _native_transfer(side: str, wallet: str) -> dict:
    incoming = side == "to"
    return {
        "blockNum": "0x5",
        "from": OTHER if incoming else wallet,
        "hash": _tx(1 if incoming else 2),
        "isError": "0",
        "to": wallet if incoming else OTHER,
        "value": str(LARGE),
    }


def _transfer_log(tx: str, block: int, index: int, receiver: str, sender: str = OTHER) -> dict:
    return {
        "address": TOKEN,
        "blockHash": BLOCK_HASH,
        "blockNumber": hex(block),
        "data": "0x" + WEI.to_bytes(32, "big").hex(),
        "logIndex": hex(index),
        "timestamp": "1700000000",
        "topics": [TRANSFER_TOPIC, pad_address(sender), pad_address(receiver)],
        "transactionHash": tx,
    }


def _approval_log(tx: str, block: int, owner: str) -> dict:
    return {
        "address": TOKEN,
        "blockHash": BLOCK_HASH,
        "blockNumber": hex(block),
        "data": "0x" + UINT256_MAX.to_bytes(32, "big").hex(),
        "logIndex": "0x1",
        "timestamp": "1700000000",
        "topics": [ERC20_APPROVAL_TOPIC, pad_address(owner), pad_address(SPENDER)],
        "transactionHash": tx,
    }


def _permit_log(tx: str, block: int, owner: str) -> dict:
    return {
        "address": TOKEN,
        "blockHash": BLOCK_HASH,
        "blockNumber": hex(block),
        "data": "0x",
        "logIndex": "0x2",
        "timestamp": "1700000000",
        "topics": [PERMIT2_PERMIT_TOPIC, pad_address(owner)],
        "transactionHash": tx,
    }


def _rest_transfer(tx: str, receiver: str, sender: str, block: int) -> dict:
    return {
        "blockNumber": str(block),
        "from": sender,
        "hash": tx,
        "isError": "0",
        "nonce": "1",
        "to": receiver,
        "transactionIndex": "1" if receiver == ADDRESS_A else "2",
        "value": str(LARGE),
    }


class _Desk:
    def __init__(self, config: Path, state: Path, backend: FixtureBackend) -> None:
        self.config = config
        self.state = state
        self.backend = backend


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _authorization(call: object) -> str:
    for name, value in getattr(call, "credential_headers", ()) or ():
        if str(name).lower() != "authorization":
            continue
        return value.reveal() if hasattr(value, "reveal") else str(value)
    return ""


def _desk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accounts: list[dict],
    *,
    plugins: list[str] | None = None,
    mutate=None,
    secrets_extra: dict | None = None,
    omit_secrets: tuple[str, ...] = (),
    example: Path | None = None,
) -> _Desk:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = json.loads((example or EXAMPLE).read_text(encoding="utf-8"))
    document["plugins"] = [] if plugins is None else plugins
    if mutate is not None:
        mutate(document)
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    secrets = {
        "BLOCKSCOUT_BASE_API_KEY": BLOCKSCOUT_KEY,
        "BLOCKSCOUT_BASE_URL": "https://base.example.invalid",
        "BLOCKSCOUT_OPTIMISM_API_KEY": BLOCKSCOUT_KEY,
        "BLOCKSCOUT_OPTIMISM_URL": "https://optimism.example.invalid",
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "ETHERSCAN_API_BASE": "https://etherscan.example.invalid/v2",
        "ETHERSCAN_API_KEY": KEY,
        "GOPLUS_API_BASE": "https://goplus.example.invalid/api",
        "HELIUS_PARSED_EVENTS_URL": "https://helius.example.invalid/events",
        "NODEREAL_API_KEY": NODEREAL_KEY,
        "NODEREAL_BSC_URL": "https://nodereal.example.invalid/bsc",
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
    if secrets_extra:
        secrets.update(secrets_extra)
    for name in omit_secrets:
        secrets.pop(name, None)
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
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY", "NODEREAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return _Desk(config, state, FixtureBackend())


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


def _cli(
    capsys: pytest.CaptureFixture[str],
    desk: _Desk,
    argv: list[str] | None = None,
    sender: object | None = None,
) -> dict:
    code = main(
        argv or ["run-once", "--mcm-config", str(desk.config), "--no-send"],
        backend=desk.backend,
        clock=lambda: 1_700_000_000.0,
        sender=sender,
    )
    payload = json.loads(capsys.readouterr().out)
    expected = 3 if payload.get("status") == "degraded" else 0
    assert code == expected, payload
    blob = json.dumps(payload)
    assert KEY not in blob
    assert NODEREAL_KEY not in blob
    assert BLOCKSCOUT_KEY not in blob
    assert ADDRESS_A.lower() not in blob.lower()
    assert ADDRESS_B.lower() not in blob.lower()
    return payload


def _events(desk: _Desk, wallet: str | None = None, network: str | None = None) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.chain_events_for(TENANT, wallet_id=wallet, network=network)
    finally:
        database.close()


def _cursor(desk: _Desk, wallet: str, network: str, capability: str) -> str | None:
    database = Database(desk.state / "monitor.sqlite")
    try:
        rows = database.capability_cursors_for(TENANT, wallet_id=wallet, network=network)
    finally:
        database.close()
    found = [row["block_number"] for row in rows if row["capability"] == capability]
    return found[0] if found else None


def _coverage(report: dict, wallet: str, network: str, capability: str) -> dict:
    return next(
        row for row in report["coverage"]
        if row["wallet_id"] == wallet and row["network"] == network and row["capability"] == capability
    )


def _position(report: dict, wallet: str, plugin_id: str) -> dict:
    return next(row for row in report["positions"] if row["wallet_id"] == wallet and row["plugin_id"] == plugin_id)


def _install_kinza(backend: FixtureBackend, *, reserve_data: bool = True) -> None:
    host = "bsc.example.invalid"
    pool = KINZA_POOLS["bsc-mainnet"].lower()
    provider = KINZA_PROVIDERS["bsc-mainnet"].lower()
    backend.answers[(provider, SEL_POOL, None)] = _abi(["address"], [KINZA_POOLS["bsc-mainnet"]])
    backend.answers[(pool, SEL_PROVIDER, None)] = _abi(["address"], [KINZA_PROVIDERS["bsc-mainnet"]])
    backend.answers[(pool, SEL_RESERVES, None)] = _abi(["address[]"], [[UNDERLYING]])
    if reserve_data:
        words = [0] * 16
        words[8] = int(ATOKEN, 16)
        words[10] = int(DEBT, 16)
        backend.answers[(pool, SEL_RESERVE_DATA, UNDERLYING.lower())] = _abi(["uint256"] * 16, words)
    for token in (ATOKEN.lower(), DEBT.lower()):
        backend.answers[(token, _selector("POOL()"), None)] = _abi(["address"], [KINZA_POOLS["bsc-mainnet"]])
        backend.answers[(token, _selector("UNDERLYING_ASSET_ADDRESS()"), None)] = _abi(["address"], [UNDERLYING])
    del host


def _shadow_tool():
    name = "mcm_shadow_round"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "tools" / "mcm_shadow_round.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_mcm_r1_01_runtime_source_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-R1-01. The CLI reports the worktree it actually loaded, and rejects another tree."""
    desk = _desk(tmp_path, monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))])
    report = _cli(capsys, desk)
    runtime = report["runtime"]
    assert runtime["executable"] == sys.executable
    assert Path(runtime["guardian_file"]) == ROOT / "src" / "guardian" / "__init__.py"
    assert Path(runtime["kinza_file"]) == ROOT / "src" / "guardian" / "protocols" / "kinza.py"
    assert runtime["kinza_class"] == "guardian.protocols.kinza.Kinza"
    assert runtime["kinza_registry"] == runtime["kinza_class"]
    assert Path(runtime["worktree"]) == ROOT
    assert runtime["binding"] == "worktree"
    with pytest.raises(GuardianError, match="runtime binding does not match this worktree"):
        assert_worktree(tmp_path)


def test_mcm_r1_02_native_indexer_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-R1-02. NodeReal and Blockscout transfers survive a net-zero balance. A refusal is not an empty history."""
    def _pages(document: dict) -> None:
        document["sources"]["blockscout-base"]["page_size"] = 1
        document["sources"]["blockscout-optimism"]["page_size"] = 1

    desk = _desk(
        tmp_path / "positive",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("bsc-mainnet", "base-mainnet", "optimism-mainnet", "avalanche-c"))],
        mutate=_pages,
    )
    desk.backend.etherscan_plan_chains.add("43114")
    desk.backend.rest_rows[("base.example.invalid", "txlist")] = [
        _rest_transfer(_tx(11), ADDRESS_A, OTHER, 4),
        _rest_transfer(_tx(12), OTHER, ADDRESS_A, 6),
    ]
    desk.backend.rest_rows[("optimism.example.invalid", "txlist")] = [
        _rest_transfer(_tx(13), ADDRESS_A, OTHER, 4),
        _rest_transfer(_tx(14), OTHER, ADDRESS_A, 6),
    ]
    report = _cli(capsys, desk)
    bsc = [row for row in _events(desk, WALLET_A, "bsc-mainnet") if row["kind"] in {"asset_in", "asset_out"}]
    assert {row["kind"] for row in bsc} == {"asset_in", "asset_out"}
    assert {int(row["raw_amount"]) for row in bsc} == {LARGE}
    signed = sum(int(row["raw_amount"]) if row["kind"] == "asset_in" else -int(row["raw_amount"]) for row in bsc)
    assert signed == 0
    native = next(row for row in report["native_reads"] if row["network"] == "bsc-mainnet")
    assert native["raw_amount"] == "0"
    assert _coverage(report, WALLET_A, "bsc-mainnet", "history.native_external")["quality"] == "complete_in_declared_scope"
    for network, host in (("base-mainnet", "base.example.invalid"), ("optimism-mainnet", "optimism.example.invalid")):
        rows = [row for row in _events(desk, WALLET_A, network) if row["kind"] in {"asset_in", "asset_out"}]
        assert {row["kind"] for row in rows} == {"asset_in", "asset_out"}
        assert {int(row["raw_amount"]) for row in rows} == {LARGE}
    nodereal = [row for row in desk.backend.calls if row.get("kind") == "nodereal" and row.get("category") == "external"]
    assert {row["side"] for row in nodereal} == {"from", "to"}
    assert any(row["page_key"] == "page-2" for row in nodereal)
    for host in ("base.example.invalid", "optimism.example.invalid"):
        pages = [row["page"] for row in desk.backend.calls if row.get("kind") == "rest" and row.get("host") == host and row.get("action") == "txlist"]
        assert "1" in pages and "2" in pages
    avalanche = _coverage(report, WALLET_A, "avalanche-c", "history.native_external")
    assert "expansion_pending" in avalanche["reason_codes"]
    assert avalanche["quality"] != "complete_in_declared_scope"
    assert not [row for row in _events(desk, WALLET_A, "avalanche-c") if row["kind"] in {"asset_in", "asset_out"}]
    disclosed = {row["adapter"] for row in report["indexer_disclosures"]}
    assert {"nodereal_transfers", "blockscout"} <= disclosed
    assert all("address" not in row for row in report["indexer_disclosures"])
    plan_calls = [row for row in desk.backend.calls if row.get("kind") == "rest" and row.get("chainid") == "43114"]
    assert plan_calls
    again = _cli(capsys, desk)
    assert _coverage(again, WALLET_A, "avalanche-c", "history.native_external")["reason_codes"]
    later = [row for row in desk.backend.calls if row.get("kind") == "rest" and row.get("chainid") == "43114"]
    assert len(later) == len(plan_calls)
    cache = json.loads((desk.state / "capability-failures.json").read_text(encoding="utf-8"))
    assert cache["etherscan|avalanche-c"] == "plan_not_supported"
    assert ADDRESS_A.lower() not in json.dumps(cache).lower()

    missing = _desk(
        tmp_path / "missing-key",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("base-mainnet",))],
        omit_secrets=("BLOCKSCOUT_BASE_API_KEY",),
    )
    missing_report = _cli(capsys, missing)
    row = _coverage(missing_report, WALLET_A, "base-mainnet", "history.native_external")
    assert "credential_missing" in row["reason_codes"]
    assert row["quality"] != "error"
    assert row["quality"] != "complete_in_declared_scope"
    assert not _events(missing, WALLET_A, "base-mainnet")

    refused = _desk(tmp_path / "refused", monkeypatch, [_account(WALLET_A, ADDRESS_A, ("bsc-mainnet",))])
    refused.backend.nodereal_refused = True
    refused_report = _cli(capsys, refused)
    refused_row = _coverage(refused_report, WALLET_A, "bsc-mainnet", "history.native_external")
    assert "method_not_found" in refused_row["reason_codes"]
    assert "blocked" in refused_row["reason_codes"]
    assert refused_row["quality"] != "complete_in_declared_scope"
    assert not [row for row in _events(refused, WALLET_A, "bsc-mainnet") if row["kind"] in {"asset_in", "asset_out"}]
    assert any(row.get("kind") == "nodereal" for row in refused.backend.calls)


def test_mcm_r1_03_bounded_log_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-R1-03. Known logs are read under 50, 1000, and 2000 block caps. A one-block failure stays a gap."""
    for limit, head, block in ((50, 120, 110), (1000, 1500, 1400), (2000, 2500, 2400)):
        def _limit(document: dict, limit=limit) -> None:
            document["sources"]["rpc-ethereum-mainnet"]["log_range_max"] = limit

        desk = _desk(tmp_path / f"cap-{limit}", monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))], mutate=_limit)
        desk.backend.head = head
        desk.backend.logs = [_transfer_log(_tx(block), block, 1, ADDRESS_A)]
        if limit == 50:
            desk.backend.rate_limit_once = True
        report = _cli(capsys, desk)
        found = [row for row in _events(desk, WALLET_A, "ethereum-mainnet") if row["tx_id"] == _tx(block)]
        assert found
        spans = [row for row in desk.backend.calls if row.get("kind") == "log" and "from" in row]
        assert spans
        assert max(row["to"] - row["from"] for row in spans) <= limit
        assert all(row["address"] != ADDRESS_A.lower() for row in spans)
        assert all(row["address"] is None for row in spans)
        if limit == 50:
            assert any(item["seconds"] >= 2 for item in report["budget"]["backoff"])

    def _contract(document: dict) -> None:
        document["sources"]["rpc-ethereum-mainnet"]["log_contract"] = TOKEN
        document["sources"]["rpc-ethereum-mainnet"]["log_range_max"] = 50

    contracted = _desk(
        tmp_path / "contract",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))],
        mutate=_contract,
    )
    contracted.backend.head = 20
    contracted.backend.logs = [_transfer_log(_tx(70), 10, 1, ADDRESS_A)]
    _cli(capsys, contracted)
    contracted_logs = [row for row in contracted.backend.calls if row.get("kind") == "log" and "from" in row]
    addresses = {row["address"] for row in contracted_logs}
    assert TOKEN.lower() in addresses
    assert None in addresses
    assert ADDRESS_A.lower() not in addresses

    def _gap(document: dict) -> None:
        document["sources"]["rpc-ethereum-mainnet"]["log_range_max"] = 50

    gap = _desk(tmp_path / "gap", monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))], mutate=_gap)
    gap.backend.head = 4
    gap.backend.bisect = True
    gap.backend.bad_blocks.add(2)
    gap.backend.logs = [
        _transfer_log(_tx(71), 1, 1, ADDRESS_A),
        _transfer_log(_tx(72), 4, 1, ADDRESS_A),
    ]
    gap_report = _cli(capsys, gap)
    history = _coverage(gap_report, WALLET_A, "ethereum-mainnet", "history.fungible")
    assert any(code.startswith("single_block_unreadable:2") for code in history["reason_codes"])
    assert history["quality"] != "complete_in_declared_scope"
    assert _cursor(gap, WALLET_A, "ethereum-mainnet", "history.fungible") == "1"
    assert any(row["tx_id"] == _tx(71) for row in _events(gap, WALLET_A, "ethereum-mainnet"))
    assert not any(row["tx_id"] == _tx(72) for row in _events(gap, WALLET_A, "ethereum-mainnet"))


def test_mcm_r1_04_prefix_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-R1-04. A failed third chunk keeps the first two prefixes. Restart reaches the frozen target once."""
    def _wide(document: dict) -> None:
        document["sources"]["rpc-ethereum-mainnet"]["log_range_max"] = 2000

    desk = _desk(tmp_path, monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))], mutate=_wide)
    desk.backend.head = 6000
    desk.backend.fail_from = 4002
    desk.backend.logs = [
        _transfer_log(_tx(81), 100, 1, ADDRESS_A),
        _transfer_log(_tx(82), 3000, 1, ADDRESS_A),
        _transfer_log(_tx(84), 4000, 1, ADDRESS_A),
        _transfer_log(_tx(83), 5000, 1, ADDRESS_A),
    ]
    first = _cli(capsys, desk)
    checkpoint = next(row for row in first["checkpoints"] if row["network"] == "ethereum-mainnet")
    assert checkpoint["height"] == 6000
    assert checkpoint["hash"] == BLOCK_HASH
    assert checkpoint["timestamp"] == 1_700_000_000
    assert _cursor(desk, WALLET_A, "ethereum-mainnet", "history.fungible") == "4001"
    stored = {row["tx_id"] for row in _events(desk, WALLET_A, "ethereum-mainnet")}
    assert _tx(81) in stored
    assert _tx(82) in stored
    assert _tx(84) in stored
    assert _tx(83) not in stored
    history = _coverage(first, WALLET_A, "ethereum-mainnet", "history.fungible")
    assert history["quality"] != "complete_in_declared_scope"
    desk.backend.fail_from = None
    desk.backend.calls.clear()
    second = _cli(capsys, desk)
    assert _cursor(desk, WALLET_A, "ethereum-mainnet", "history.fungible") == "6000"
    resumed = [row for row in desk.backend.calls if row.get("kind") == "log" and "from" in row]
    assert resumed
    assert min(row["from"] for row in resumed) == 4000
    assert 0 not in {row["from"] for row in resumed}
    finished = _coverage(second, WALLET_A, "ethereum-mainnet", "history.fungible")
    assert finished["quality"] == "complete_in_declared_scope"
    assert "incremental_window_complete" in finished["reason_codes"]
    counts: dict[str, int] = {}
    for row in _events(desk, WALLET_A, "ethereum-mainnet"):
        counts[row["tx_id"]] = counts.get(row["tx_id"], 0) + 1
    assert counts[_tx(81)] == 1
    assert counts[_tx(82)] == 1
    assert counts[_tx(84)] == 1
    assert counts[_tx(83)] == 1


def test_mcm_r1_05_incremental_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-R1-05. An explicit origin can complete the incremental window while older history stays unknown."""
    def _origin(document: dict) -> None:
        document["polling"]["bootstrap_origin"] = 50

    desk = _desk(tmp_path, monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))], mutate=_origin)
    desk.backend.head = 80
    approval = _tx(91)
    permit = _tx(92)
    desk.backend.logs = [
        _approval_log(_tx(90), 10, ADDRESS_A),
        _approval_log(approval, 60, ADDRESS_A),
        _permit_log(permit, 70, ADDRESS_A),
    ]
    report = _cli(capsys, desk)
    row = _coverage(report, WALLET_A, "ethereum-mainnet", "permissions.erc20")
    assert row["quality"] == "complete_in_declared_scope"
    assert "incremental_window_complete" in row["reason_codes"]
    assert "pre_origin_history_unknown" in row["reason_codes"]
    notes = [item for item in report["permission_notes"] if item["kind"] == "relay_permit"]
    assert len(notes) == 1
    assert notes[0]["tx"] == permit
    assert notes[0]["not_current_allowance"] is True
    assert notes[0]["wallet_id"] == WALLET_A
    assert "address" not in notes[0]
    assert any(call.get("selector") == SEL_ALLOWANCE for call in desk.backend.calls)
    events = _events(desk, WALLET_A, "ethereum-mainnet")
    assert any(row["tx_id"] == approval and row["kind"] == "approval_set" for row in events)
    assert not any(row["tx_id"] == permit and row["kind"] == "approval_set" for row in events)
    assert not any(row["tx_id"] == _tx(90) for row in events)


def test_mcm_r1_06_kinza_real_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-R1-06. Kinza discovers reserves at runtime for both wallets. A missing reserve is not an empty position."""
    desk = _desk(
        tmp_path / "kinza",
        monkeypatch,
        [
            _account(WALLET_A, ADDRESS_A, ("bsc-mainnet",)),
            _account(WALLET_B, ADDRESS_B, ("bsc-mainnet",)),
        ],
        plugins=["kinza", "erc4626-registered"],
    )
    _install_kinza(desk.backend)
    desk.backend.balances[("bsc.example.invalid", ATOKEN.lower(), ADDRESS_A.lower())] = 50
    desk.backend.accounts[("bsc.example.invalid", KINZA_POOLS["bsc-mainnet"].lower(), ADDRESS_A.lower())] = (0, 0, 0, 0, 0, 2 * WEI)
    report = _cli(capsys, desk)
    seen = _position(report, WALLET_A, "kinza")
    quiet = _position(report, WALLET_B, "kinza")
    assert seen["status"] == "observed"
    assert seen["legs"]
    assert quiet["status"] == "none_observed"
    assert quiet["legs"] == []
    kinza = report["budget"]["kinza"]
    assert kinza["wallets_read"] == 2
    assert kinza["balance_reads"] >= 4
    assert kinza["account_reads"] >= 2
    assert kinza["metadata_shared_hits"] > 0
    assert "0x" not in json.dumps(kinza)
    selectors = [row["selector"] for row in desk.backend.calls if row.get("kind") == "eth_call"]
    assert selectors.count(SEL_POOL) >= 2
    assert selectors.count(SEL_PROVIDER) >= 2
    assert selectors.count(SEL_RESERVES) >= 2
    assert SEL_RESERVE_DATA in selectors
    assert selectors.index(SEL_RESERVES) < selectors.index(SEL_BALANCE)
    for wallet in (WALLET_A, WALLET_B):
        vault = _position(report, wallet, "erc4626")
        assert vault["status"] == "unknown"
        assert "vaults_not_configured" in vault["reason_codes"]
        assert vault["status"] != "none_observed"
        assert f"{wallet}|bsc-mainnet|positions:erc4626" not in report["required"]
        assert f"{wallet}|bsc-mainnet|positions:kinza" in report["required"]

    missing = _desk(
        tmp_path / "missing-reserve",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("bsc-mainnet",))],
        plugins=["kinza"],
    )
    _install_kinza(missing.backend, reserve_data=False)
    missing_report = _cli(capsys, missing)
    row = _position(missing_report, WALLET_A, "kinza")
    assert row["status"] == "unknown"
    assert row["status"] != "none_observed"
    assert SEL_RESERVES in [call["selector"] for call in missing.backend.calls if call.get("kind") == "eth_call"]


def test_mcm_r1_07_fees_budget_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-R1-07. Logical method counts, credits, pages, and raw log metadata stay checkable."""
    def _batch(document: dict) -> None:
        document["sources"]["rpc-base-mainnet"]["batch_reads"] = True
        document["sources"]["blockscout-base"]["page_size"] = 1

    desk = _desk(
        tmp_path / "budget",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("base-mainnet",))],
        mutate=_batch,
    )
    desk.backend.head = 12
    desk.backend.logs = [_transfer_log(_tx(101), 12, 3, ADDRESS_A)]
    desk.backend.rest_rows[("base.example.invalid", "txlist")] = [
        _rest_transfer(_tx(102), ADDRESS_A, OTHER, 4),
    ]
    report = _cli(capsys, desk)
    provider = report["budget"]["providers"]["rpc-base-mainnet"]
    assert provider["logical_methods"] > provider["http_calls"]
    assert provider["methods"]["eth_getLogs"] >= 2
    blockscout = report["budget"]["providers"]["blockscout-base"]
    assert blockscout["credits"] == 20 * blockscout["logical_methods"]
    assert blockscout["pages"] >= 1
    assert blockscout["remaining_quota"] == "80000"
    assert any(row.get("kind") == "batch" and row.get("methods") == ["eth_getLogs", "eth_getLogs"] for row in desk.backend.calls)
    evidence = [row for row in report["log_evidence"] if row.get("tx") == _tx(101)]
    assert evidence
    assert evidence[0]["block"] == 12
    assert evidence[0]["hash"] == BLOCK_HASH
    assert evidence[0]["time"] == "1700000000"

    tool = _shadow_tool()
    meter = tool.CountingBackend(desk.backend)
    batch = PreparedCall(
        "rpc-base-mainnet",
        "POST",
        "https://base.example.invalid/rpc",
        json.dumps([
            {"id": 1, "jsonrpc": "2.0", "method": "eth_getLogs", "params": [{}]},
            {"id": 2, "jsonrpc": "2.0", "method": "eth_getBalance", "params": []},
        ]).encode(),
        "jsonrpc",
    )
    meter.send(batch)
    counted = meter.snapshot()
    assert counted["http_calls"] == 1
    assert counted["logical_methods"] == 2
    assert counted["credits"] == 2
    shadow = _desk(
        tmp_path / "shadow",
        monkeypatch,
        [
            _account(WALLET_A, ADDRESS_A, ("bsc-mainnet",)),
            _account(WALLET_B, ADDRESS_B, ("bsc-mainnet",)),
        ],
        plugins=["kinza"],
    )
    _install_kinza(shadow.backend)
    shadow.backend.balances[("bsc.example.invalid", ATOKEN.lower(), ADDRESS_A.lower())] = 50
    legacy = tmp_path / "legacy.json"
    _write_private(legacy, json.dumps({"alerted": []}) + "\n")
    evidence_dir = tmp_path / "evidence"
    code = tool.main(
        ["--mcm-config", str(shadow.config), "--legacy-state", str(legacy), "--evidence-dir", str(evidence_dir)],
        backend=shadow.backend,
        sender=type("Sink", (), {"send": lambda self, message: None})(),
        clock=lambda: 1_700_000_000.0,
    )
    assert code == 0
    budget = json.loads((evidence_dir / "budget.json").read_text(encoding="utf-8"))
    assert budget["kinza_eth_calls"] > 0
    assert isinstance(budget["etherscan_calls"], int)
    assert budget["run_once"]["kinza"]["wallets_read"] == 2
    rpc = budget["run_once"]["providers"]["rpc-bsc-mainnet"]
    assert rpc["logical_methods"] >= rpc["http_calls"]
    assert rpc["logical_methods"] > 0


NOKEYS = ROOT / "examples" / "monitor.nokeys.json"
GUIDE = ROOT / "docs" / "api-keys.md"
SOL_ADDRESS = "11111111111111111111111111111111"
SOL_WALLET = "22222222-2222-4222-8222-222222222222"
PUBLIC_URLS = {
    "BLOCKSCOUT_ARBITRUM_URL": "https://arb-public.example.invalid",
    "BLOCKSCOUT_BASE_PUBLIC_URL": "https://base-public.example.invalid",
    "BLOCKSCOUT_ETH_URL": "https://eth-public.example.invalid",
    "BLOCKSCOUT_OPTIMISM_PUBLIC_URL": "https://op-public.example.invalid",
    "BLOCKSCOUT_POLYGON_URL": "https://polygon-public.example.invalid",
    "BLOCKSCOUT_PRO_API_BASE": "https://pro.example.invalid",
}
KEYLESS_HOSTS = {
    "arbitrum-one": "arb-public.example.invalid",
    "base-mainnet": "base-public.example.invalid",
    "ethereum-mainnet": "eth-public.example.invalid",
    "optimism-mainnet": "op-public.example.invalid",
    "polygon-pos": "polygon-public.example.invalid",
}


class _Sink:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, _message: object) -> None:
        self.calls += 1


def _nokeys_desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mutate=None) -> _Desk:
    def _quiet(document: dict) -> None:
        document["plugins"] = []
        if mutate is not None:
            mutate(document)

    desk = _desk(
        tmp_path,
        monkeypatch,
        [
            _account(WALLET_A, ADDRESS_A, (
                "arbitrum-one",
                "avalanche-c",
                "base-mainnet",
                "bsc-mainnet",
                "ethereum-mainnet",
                "optimism-mainnet",
                "polygon-pos",
            )),
            {
                "address": SOL_ADDRESS,
                "family": "solana",
                "label": "desk",
                "networks": ["solana-mainnet"],
                "own_group": "desk",
                "relationship": "owned_user_declared",
                "wallet_id": SOL_WALLET,
            },
        ],
        mutate=_quiet,
        secrets_extra=dict(PUBLIC_URLS),
        omit_secrets=(
            "BLOCKSCOUT_BASE_API_KEY",
            "BLOCKSCOUT_OPTIMISM_API_KEY",
            "ETHERSCAN_API_KEY",
            "NODEREAL_API_KEY",
        ),
        example=NOKEYS,
    )
    desk.backend.balance_hex = "0x1"
    desk.backend.pro_requires_key = True
    desk.backend.logs = [_transfer_log(_tx(40), 4, 1, ADDRESS_A)]
    for host in KEYLESS_HOSTS.values():
        desk.backend.rest_rows[(host, "txlist")] = [_rest_transfer(_tx(41), ADDRESS_A, OTHER, 4)]
    return desk


def test_mcm_r1_08_no_key_config_runs_end_to_end_with_honest_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-R1-08. No keys: keyless history is visible, and every other keyed gap stays named."""
    desk = _nokeys_desk(tmp_path, monkeypatch)
    sink = _Sink()
    report = _cli(capsys, desk, sender=sink)
    assert sink.calls == 0
    assert report["sent"] == 0
    assert report["status"] in {"ok", "degraded"}
    for network, host in KEYLESS_HOSTS.items():
        rows = [row for row in _events(desk, WALLET_A, network) if row["kind"] in {"asset_in", "asset_out"}]
        assert rows, network
        assert any(int(row["raw_amount"]) == LARGE for row in rows)
        assert any(item["host"] == host and item.get("action") == "txlist" for item in desk.backend.calls)
    bsc = _coverage(report, WALLET_A, "bsc-mainnet", "history.native_external")
    avalanche = _coverage(report, WALLET_A, "avalanche-c", "history.native_external")
    assert "credential_missing" in bsc["reason_codes"]
    assert "credential_missing" in avalanche["reason_codes"]
    assert bsc["quality"] != "complete_in_declared_scope"
    assert avalanche["quality"] != "complete_in_declared_scope"
    token = [row for row in _events(desk, WALLET_A, "bsc-mainnet") if row["tx_id"] == _tx(40)]
    assert token
    risk = _coverage(report, WALLET_A, "ethereum-mainnet", "address_risk")
    asset = _coverage(report, WALLET_A, "ethereum-mainnet", "asset_risk")
    assert "credential_missing" in risk["reason_codes"]
    assert "third_party_denied" in risk["reason_codes"]
    assert "credential_missing" in asset["reason_codes"]
    assert risk["quality"] != "complete_in_declared_scope"
    solana = [row for row in report["native_reads"] if row["network"] == "solana-mainnet"]
    assert solana and int(solana[0]["raw_amount"]) == 5
    for row in report["coverage"]:
        if row["quality"] != "complete_in_declared_scope":
            continue
        if row["capability"] == "history.native_external":
            assert _events(desk, row["wallet_id"], row["network"])
        assert "credential_missing" not in row["reason_codes"]
        assert "plan_not_supported" not in row["reason_codes"]
        assert "blocked" not in row["reason_codes"]
    blob = json.dumps(report)
    for secret in (KEY, NODEREAL_KEY, BLOCKSCOUT_KEY):
        assert secret not in blob
    assert not any(item.get("kind") == "nodereal" for item in desk.backend.calls)


def test_mcm_r1_09_blockscout_pro_plan_and_keyless_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-R1-09. PRO uses a bearer header. Base 402 falls back. A challenge page is blocked."""
    def _routes(document: dict) -> None:
        document["plugins"] = []

    desk = _desk(
        tmp_path / "pro",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("base-mainnet", "optimism-mainnet"))],
        mutate=_routes,
        secrets_extra={
            "BLOCKSCOUT_API_KEY": BLOCKSCOUT_KEY,
            "BLOCKSCOUT_BASE_PUBLIC_URL": "https://base-public.example.invalid",
            "BLOCKSCOUT_OPTIMISM_PUBLIC_URL": "https://op-public.example.invalid",
            "BLOCKSCOUT_PRO_API_BASE": "https://pro.example.invalid",
        },
    )
    desk.backend.pro_featured_chains.add("8453")
    desk.backend.rate_headers = {"x-credits-remaining": "80000", "x-ratelimit-limit": "5"}
    desk.backend.rest_rows[("pro.example.invalid", "txlist")] = [
        _rest_transfer(_tx(51), ADDRESS_A, OTHER, 4),
    ]
    desk.backend.rest_rows[("base-public.example.invalid", "txlist")] = [
        _rest_transfer(_tx(52), ADDRESS_A, OTHER, 5),
    ]
    report = _cli(capsys, desk)
    optimism = [row for row in _events(desk, WALLET_A, "optimism-mainnet") if row["tx_id"] == _tx(51)]
    base = [row for row in _events(desk, WALLET_A, "base-mainnet") if row["tx_id"] == _tx(52)]
    assert optimism and base
    pro_calls = [row for row in desk.backend.http_log if "pro.example.invalid" in row["url"]]
    assert pro_calls
    assert any("chain_id=10" in row["url"] and row["authorization"] == f"Bearer {BLOCKSCOUT_KEY}" for row in pro_calls)
    assert any("chain_id=8453" in row["url"] for row in pro_calls)
    assert all(BLOCKSCOUT_KEY not in row["url"] for row in desk.backend.http_log)
    assert all("apikey=" not in row["url"].lower() for row in desk.backend.http_log)
    featured = _coverage(report, WALLET_A, "base-mainnet", "history.native_external")
    assert featured["quality"] == "complete_in_declared_scope"
    cache = json.loads((desk.state / "capability-failures.json").read_text(encoding="utf-8"))
    assert cache["blockscout-pro|base-mainnet"] == "plan_not_supported"
    provider = report["budget"]["providers"]["blockscout-pro"]
    assert provider["rate_headers"]["x-credits-remaining"] == "80000"
    assert provider["rate_headers"]["x-ratelimit-limit"] == "5"
    assert provider["remaining_quota"] == "80000"
    before = sum(1 for row in desk.backend.http_log if "chain_id=8453" in row["url"])
    assert before >= 1
    _cli(capsys, desk)
    after = sum(1 for row in desk.backend.http_log if "chain_id=8453" in row["url"])
    assert after == before

    missing = _desk(
        tmp_path / "nokey",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("optimism-mainnet",))],
        mutate=_routes,
        secrets_extra={
            "BLOCKSCOUT_OPTIMISM_PUBLIC_URL": "https://op-public.example.invalid",
            "BLOCKSCOUT_PRO_API_BASE": "https://pro.example.invalid",
        },
        omit_secrets=("BLOCKSCOUT_API_KEY", "BLOCKSCOUT_OPTIMISM_API_KEY"),
    )
    missing.backend.pro_requires_key = True
    missing.backend.rest_rows[("op-public.example.invalid", "txlist")] = [
        _rest_transfer(_tx(53), ADDRESS_A, OTHER, 4),
    ]
    missing_report = _cli(capsys, missing)
    assert any(row["tx_id"] == _tx(53) for row in _events(missing, WALLET_A, "optimism-mainnet"))
    unpaid = [row for row in missing.backend.http_log if "pro.example.invalid" in row["url"]]
    assert unpaid
    assert all(row["authorization"] == "" for row in unpaid)
    assert all(BLOCKSCOUT_KEY not in row["url"] for row in unpaid)
    assert _coverage(missing_report, WALLET_A, "optimism-mainnet", "history.native_external")["quality"] == "complete_in_declared_scope"

    challenged = _desk(
        tmp_path / "challenge",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("base-mainnet",))],
        mutate=_routes,
        secrets_extra={
            "BLOCKSCOUT_BASE_PUBLIC_URL": "https://base-public.example.invalid",
            "BLOCKSCOUT_PRO_API_BASE": "https://pro.example.invalid",
        },
        omit_secrets=("BLOCKSCOUT_API_KEY", "BLOCKSCOUT_BASE_API_KEY"),
    )
    challenged.backend.pro_requires_key = True
    challenged.backend.cloudflare_hosts.add("base-public.example.invalid")
    challenged_report = _cli(capsys, challenged)
    row = _coverage(challenged_report, WALLET_A, "base-mainnet", "history.native_external")
    assert "bot_challenge" in row["reason_codes"]
    assert "blocked" in row["reason_codes"]
    assert row["quality"] != "complete_in_declared_scope"
    assert row["quality"] != "empty"
    assert not _events(challenged, WALLET_A, "base-mainnet")


def test_mcm_r1_10_credential_hints_and_key_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-R1-10. Missing credentials name the env var and a real docs anchor. Values stay hidden."""
    desk = _nokeys_desk(tmp_path / "hints", monkeypatch)
    secret_path = Path(os.environ["MONITOR_SECRET_PROFILE"])
    profile = json.loads(secret_path.read_text(encoding="utf-8"))
    profile["ETHERSCAN_API_KEY"] = KEY
    _write_private(secret_path, json.dumps(profile) + "\n")
    validate = _cli(capsys, desk, ["config", "validate", "--mcm-config", str(desk.config)])
    probe = _cli(capsys, desk, ["sources", "probe", "--mcm-config", str(desk.config)])
    report = _cli(capsys, desk)
    guide = GUIDE.read_text(encoding="utf-8")
    for anchor in ("alchemy", "blockscout", "coingecko", "etherscan", "goplus", "helius", "nodereal", "ofac"):
        assert f'id="{anchor}"' in guide
    hints = []
    for row in validate["sources"]:
        if row.get("blocked_reason") == "credential_missing":
            assert "credential_hint" in row
            hints.append(row["credential_hint"])
    helius = next(row for row in probe["sources"] if row["id"] == "helius-parser")
    assert helius["identity"]["reason"] == "credential_missing"
    assert "HELIUS_API_KEY" in helius["credential_hint"]
    assert "docs/api-keys.md#helius" in helius["credential_hint"]
    hints.append(helius["credential_hint"])
    covered = [
        row for row in report["coverage"]
        if "credential_missing" in row["reason_codes"]
    ]
    assert covered
    for row in covered:
        assert "credential_hint" in row
        assert "see docs/api-keys.md#" in row["credential_hint"]
        hints.append(row["credential_hint"])
    for hint in hints:
        assert "see docs/api-keys.md#" in hint
        anchor = hint.split("#", 1)[1]
        assert f'id="{anchor}"' in guide
        assert KEY not in hint
    blob = json.dumps({"probe": probe, "report": report, "validate": validate})
    assert KEY not in blob
    assert BLOCKSCOUT_KEY not in blob
    assert NODEREAL_KEY not in blob


def test_mcm_r1_11_content_hash_normalizes_line_endings(tmp_path: Path) -> None:
    """MCM-R1-11. CRLF and LF of the same bytes share a content hash. A real edit does not."""
    tool = _shadow_tool()
    root = tmp_path / "tree"
    source = root / "src" / "guardian" / "mod.py"
    config = root / "config" / "sample.json"
    source.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    body = b"alpha\nbeta\n"
    source.write_bytes(body)
    config.write_bytes(b'{"ok": true}\n')
    first = tool.content_hash(root)
    source.write_bytes(body.replace(b"\n", b"\r\n"))
    config.write_bytes(b'{"ok": true}\r\n')
    assert tool.content_hash(root) == first
    source.write_bytes(b"alpha\rbeta\n")
    assert tool.content_hash(root) == first
    source.write_bytes(b"alpha\nbeta!\n")
    assert tool.content_hash(root) != first
