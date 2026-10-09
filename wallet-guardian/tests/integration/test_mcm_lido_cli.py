"""MCM-SHARE-02. The public CLI reads Lido with offline eth_call fixtures.

Assertions go through ``guardian.cli.main`` (``run-once --no-send``). No test
calls the plugin runner to produce the result it checks. Addresses are synthetic.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from guardian.cli import main
from guardian.news.catalog import ALLOWLISTED_FEED_URLS
from guardian.protocols.network import native_identity
from guardian.protocols.pins import LIDO_SOURCE, LIDO_STETH, LIDO_WITHDRAWAL_QUEUE, LIDO_WSTETH
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
ADDRESS = to_checksum_address("0x" + ("0" * 39) + "1")
OTHER = to_checksum_address("0x" + ("0" * 39) + "2")
KEY = "synthetic-etherscan-key"
WEI = 10**18
ETH = native_identity("ethereum-mainnet")
STETH = LIDO_STETH.lower()
WSTETH = LIDO_WSTETH.lower()
QUEUE = LIDO_WITHDRAWAL_QUEUE.lower()
LIDO_BLOG = "https://blog.lido.fi/rss/"
_SENDS = (
    "eth_sendRawTransaction",
    "eth_sendTransaction",
    "eth_sign",
    "eth_signTypedData",
    "eth_signTypedData_v4",
    "personal_sign",
    "wallet_sendCalls",
)
_EMPTY_RSS = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'


def _selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


def _u256(value: int) -> str:
    return "0x" + encode(["uint256"], [value]).hex()


def _id_list(values: list[int]) -> str:
    return "0x" + encode(["uint256[]"], [values]).hex()


def _status_payload(rows: list[dict], owner: str) -> str:
    encoded = [
        (row["steth"], row["shares"], owner, row["timestamp"], row["finalized"], row["claimed"])
        for row in rows
    ]
    return "0x" + encode(["(uint256,uint256,address,uint256,bool,bool)[]"], [encoded]).hex()


def _decode_ids(data: str) -> list[int]:
    (values,) = decode(["uint256[]"], bytes.fromhex(data[10:]))
    return [int(item) for item in values]


SEL_BALANCE = _selector("balanceOf(address)")
SEL_SHARES = _selector("sharesOf(address)")
SEL_POOLED = _selector("getPooledEthByShares(uint256)")
SEL_WSTETH = _selector("getStETHByWstETH(uint256)")
SEL_REQUESTS = _selector("getWithdrawalRequests(address)")
SEL_STATUS = _selector("getWithdrawalStatus(uint256[])")
_FORBIDDEN = {
    _selector(name)
    for name in (
        "approve(address,uint256)",
        "claimWithdrawal(uint256)",
        "claimWithdrawals(uint256[],uint256[])",
        "claimWithdrawalsTo(uint256[],uint256[],address)",
        "requestWithdrawals(uint256[],address)",
        "requestWithdrawalsWstETH(uint256[],address)",
        "transfer(address,uint256)",
    )
}


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class LidoBackend:
    """JSON-RPC answers for one synthetic ethereum wallet. News GETs are queued."""

    def __init__(self) -> None:
        self.head = 100
        self.calls: list[object] = []
        self.rpc: list[dict] = []
        self.fail: set[tuple[str, str]] = set()
        self.steth_balance = 0
        self.steth_shares = 0
        self.pooled = 0
        self.wsteth_balance = 0
        self.wsteth_steth = 0
        self.request_ids: list[int] = []
        self.requests: dict[int, dict] = {}
        self.status_owner = ADDRESS
        self.news: dict[str, list[RawResponse]] = {}

    def set_steth(self, amount: int, shares: int | None = None) -> None:
        self.steth_balance = amount
        self.pooled = amount
        if shares is not None:
            self.steth_shares = shares

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        url = str(getattr(call, "url", ""))
        http = str(getattr(call, "method", "") or "")
        body = getattr(call, "body", b"") or b""
        host = (urlsplit(url).hostname or "").lower()
        if http == "GET" and (host in _news_hosts() or url in self.news):
            queued = self.news.get(url)
            if not queued:
                raise AssertionError(f"missing news fixture for {url}")
            return queued.pop(0)
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
        self.rpc.append(record)
        result = self._rpc(method, record)
        if result is None:
            encoded = {"error": {"code": -32000, "message": "fixture-failure"}, "id": 1, "jsonrpc": "2.0"}
        else:
            encoded = {"id": 1, "jsonrpc": "2.0", "result": result}
        return RawResponse(200, {}, json.dumps(encoded).encode())

    def _rpc(self, method: str, record: dict) -> object | None:
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_blockNumber":
            return hex(self.head)
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
        if to == STETH and selector == SEL_BALANCE:
            return _u256(self.steth_balance)
        if to == STETH and selector == SEL_SHARES:
            return _u256(self.steth_shares)
        if to == STETH and selector == SEL_POOLED:
            return _u256(self.pooled)
        if to == WSTETH and selector == SEL_BALANCE:
            return _u256(self.wsteth_balance)
        if to == WSTETH and selector == SEL_WSTETH:
            return _u256(self.wsteth_steth)
        if to == QUEUE and selector == SEL_REQUESTS:
            return _id_list(self.request_ids)
        if to == QUEUE and selector == SEL_STATUS:
            asked = _decode_ids(data)
            rows = []
            for request_id in asked:
                row = self.requests.get(request_id)
                if row is None:
                    return None
                rows.append(row)
            return _status_payload(rows, self.status_owner)
        return "0x"


def _news_hosts() -> set[str]:
    return {(urlsplit(url).hostname or "").lower() for url in ALLOWLISTED_FEED_URLS}


def test_lido_pins_are_the_documented_mainnet_contracts() -> None:
    """The packaged manifest uses the checksummed public deployments and no live pass."""
    document = json.loads((ROOT / "config" / "protocols" / "lido.json").read_text(encoding="utf-8"))
    contracts = document["contracts"]["ethereum-mainnet"]
    assert contracts["steth"]["address"] == LIDO_STETH == "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
    assert contracts["wsteth"]["address"] == LIDO_WSTETH == "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
    assert contracts["withdrawal_queue"]["address"] == LIDO_WITHDRAWAL_QUEUE == "0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1"
    assert LIDO_SOURCE == "https://docs.lido.fi/deployed-contracts/"
    for entry in contracts.values():
        assert entry["source_url"] == LIDO_SOURCE
        assert entry["retrieved"] == "2026-10-09"
        assert entry["status"] == "documented"
        assert entry["notes"] == "name/symbol verified by eth_call 2026-10-09"
    text = json.dumps(document).lower()
    assert "live_pass" not in text
    assert "offline fixtures; live probe: see docs/coverage-matrix.md" in document["notes"]


def test_lido_cli_holdings_withdrawals_and_no_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """stETH and wstETH are observed, and each withdrawal request keeps its status."""
    desk = _desk(tmp_path, monkeypatch)
    balance = 10 * WEI
    shares = 8 * WEI
    pooled = balance - 2
    wrapped = 4 * WEI
    wrapped_steth = 5 * WEI
    desk.backend.steth_balance = balance
    desk.backend.steth_shares = shares
    desk.backend.pooled = pooled
    desk.backend.wsteth_balance = wrapped
    desk.backend.wsteth_steth = wrapped_steth
    desk.backend.request_ids = [11, 22, 33]
    desk.backend.requests = {
        11: {"claimed": False, "finalized": False, "shares": 4 * WEI, "steth": 5 * WEI, "timestamp": 1_700_000_011},
        22: {"claimed": False, "finalized": True, "shares": 6 * WEI, "steth": 7 * WEI, "timestamp": 1_700_000_022},
        33: {"claimed": True, "finalized": True, "shares": 2 * WEI, "steth": 3 * WEI, "timestamp": 1_700_000_033},
    }
    report = _cli(capsys, desk)
    assert report["status"] == "ok"
    row = _position(report)
    assert row["status"] == "observed"
    assert row["reason_codes"] == []
    legs = row["legs"]
    receipt = _leg(legs, "receipt", STETH)
    assert receipt["raw_amount"] == balance
    assert receipt["decimals"] == 18
    assert receipt["labels"]["shares"] == str(shares)
    assert ETH is not None
    quote = _leg(legs, "underlying", ETH)
    assert quote["raw_amount"] == pooled
    assert quote["labels"]["role"] == "quote"
    wrapped_receipt = _leg(legs, "receipt", WSTETH)
    assert wrapped_receipt["raw_amount"] == wrapped
    steth_quote = _leg(legs, "underlying", STETH)
    assert steth_quote["raw_amount"] == wrapped_steth
    assert steth_quote["labels"] == {"asset": "stETH", "role": "quote"}
    eth_quote = _leg(legs, "underlying", "lido:wsteth:eth")
    assert eth_quote["raw_amount"] == wrapped_steth
    assert eth_quote["labels"] == {"asset": "ETH", "role": "quote"}
    withdrawals = [leg for leg in legs if leg["labels"].get("role") == "withdrawal"]
    by_status = {leg["labels"]["status"]: leg for leg in withdrawals}
    assert set(by_status) == {"claimed", "finalized-claimable", "pending"}
    assert by_status["pending"]["raw_amount"] == 5 * WEI
    assert by_status["pending"]["labels"]["request_id"] == "11"
    assert by_status["pending"]["labels"]["is_finalized"] == "false"
    assert by_status["pending"]["labels"]["is_claimed"] == "false"
    assert by_status["pending"]["labels"]["amount_of_shares"] == str(4 * WEI)
    assert by_status["finalized-claimable"]["raw_amount"] == 7 * WEI
    assert by_status["finalized-claimable"]["labels"]["request_id"] == "22"
    assert by_status["claimed"]["raw_amount"] == 3 * WEI
    assert by_status["claimed"]["labels"]["request_id"] == "33"
    assert by_status["claimed"]["labels"]["is_claimed"] == "true"
    pooled_calls = _matching(desk.backend, SEL_POOLED, STETH)
    assert pooled_calls
    assert all(int(row["data"][10:74], 16) == shares for row in pooled_calls)
    wrapped_calls = _matching(desk.backend, SEL_WSTETH, WSTETH)
    assert wrapped_calls
    assert all(int(row["data"][10:74], 16) == wrapped for row in wrapped_calls)
    status_calls = _matching(desk.backend, SEL_STATUS, QUEUE)
    assert status_calls
    assert all(_decode_ids(row["data"]) == [11, 22, 33] for row in status_calls)
    assert _selectors(desk.backend).isdisjoint(_FORBIDDEN)


def test_lido_cli_empty_read_is_none_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful empty answer is none_observed. The status call is not required."""
    desk = _desk(tmp_path, monkeypatch)
    report = _cli(capsys, desk)
    row = _position(report)
    assert row["status"] == "none_observed"
    assert row["legs"] == []
    assert "read_failed" not in row["reason_codes"]
    assert "unknown" != row["status"]
    selectors = {row["selector"] for row in _matching(desk.backend, SEL_REQUESTS, QUEUE)}
    assert selectors == {SEL_REQUESTS}
    assert _matching(desk.backend, SEL_BALANCE, STETH)
    assert _matching(desk.backend, SEL_SHARES, STETH)
    assert _matching(desk.backend, SEL_POOLED, STETH)
    assert _matching(desk.backend, SEL_BALANCE, WSTETH)
    assert _matching(desk.backend, SEL_WSTETH, WSTETH)
    assert _matching(desk.backend, SEL_STATUS, QUEUE) == []


def test_lido_cli_failed_call_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed shares read is unknown, not an empty wallet and not a zero balance."""
    desk = _desk(tmp_path, monkeypatch)
    desk.backend.steth_balance = 10 * WEI
    desk.backend.steth_shares = 8 * WEI
    desk.backend.pooled = 10 * WEI
    desk.backend.fail.add((STETH, SEL_SHARES))
    report = _cli(capsys, desk)
    row = _position(report)
    assert row["status"] == "unknown"
    assert row["status"] != "none_observed"
    assert row["legs"] == []
    assert "read_failed" in row["reason_codes"]
    assert "0" not in row["reason_codes"]


def test_lido_cli_share_gap_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """balanceOf and getPooledEthByShares may differ by 2 wei. 3 wei is unknown."""
    desk = _desk(tmp_path, monkeypatch)
    desk.backend.steth_balance = 1000 * WEI
    desk.backend.steth_shares = 800 * WEI
    desk.backend.pooled = 1000 * WEI + 3
    report = _cli(capsys, desk)
    row = _position(report)
    assert row["status"] == "unknown"
    assert row["legs"] == []
    assert "share_balance_mismatch" in row["reason_codes"]
    assert row["status"] != "none_observed"


def test_lido_cli_request_cap_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """More than 64 withdrawal ids is unknown. The partial list is not treated as complete."""
    desk = _desk(tmp_path, monkeypatch)
    desk.backend.request_ids = list(range(1, 66))
    report = _cli(capsys, desk)
    row = _position(report)
    assert row["status"] == "unknown"
    assert row["legs"] == []
    assert "result_exceeds_bound" in row["reason_codes"]
    assert _matching(desk.backend, SEL_STATUS, QUEUE) == []


def test_lido_cli_inconsistent_withdrawal_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Claimed-before-finalized, or a status owner that is not the wallet, is a failed read."""
    claimed = _desk(tmp_path / "claimed", monkeypatch)
    claimed.backend.request_ids = [4]
    claimed.backend.requests = {
        4: {"claimed": True, "finalized": False, "shares": 1, "steth": WEI, "timestamp": 1},
    }
    claimed_row = _position(_cli(capsys, claimed))
    assert claimed_row["status"] == "unknown"
    assert claimed_row["legs"] == []
    assert "read_failed" in claimed_row["reason_codes"]

    mismatch = _desk(tmp_path / "owner", monkeypatch)
    mismatch.backend.request_ids = [5]
    mismatch.backend.status_owner = OTHER
    mismatch.backend.requests = {
        5: {"claimed": False, "finalized": False, "shares": 1, "steth": WEI, "timestamp": 1},
    }
    mismatch_row = _position(_cli(capsys, mismatch))
    assert mismatch_row["status"] == "unknown"
    assert mismatch_row["legs"] == []
    assert "read_failed" in mismatch_row["reason_codes"]


def test_lido_cli_rebase_drift_does_not_realert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A small stETH rebase stays quiet. A 10 percent move from the reference alerts once."""
    desk = _desk(tmp_path, monkeypatch)
    base = 1000 * WEI
    desk.backend.set_steth(base, shares=800 * WEI)
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send"]

    def changes() -> list[dict]:
        return [row for row in _outbox(desk) if row["payload"]["kind"] == "position_change"]

    first = _cli(capsys, desk, argv)
    assert _position(first)["status"] == "observed"
    assert changes() == []
    assert _cli(capsys, desk, argv)["outbox_written"] == 0
    assert changes() == []
    desk.backend.set_steth(base + base // 100)
    assert _cli(capsys, desk, argv)["outbox_written"] == 0
    desk.backend.set_steth(base + (base * 9) // 100)
    assert _cli(capsys, desk, argv)["outbox_written"] == 0
    assert changes() == []
    desk.backend.set_steth(base + base // 10)
    moved = _cli(capsys, desk, argv)
    assert moved["outbox_written"] == 1
    assert len(changes()) == 1
    assert "lido" in changes()[0]["payload"]["identity"]
    assert _cli(capsys, desk, argv)["outbox_written"] == 0
    assert len(changes()) == 1


def test_lido_cli_news_links_an_observed_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """An allowlisted Lido item links through the lido position. It is not a loss alert."""
    monkeypatch.setattr("guardian.pipeline._resolve", lambda _host: ["1.1.1.1"])
    desk = _desk(tmp_path, monkeypatch, fetch=True)
    desk.backend.set_steth(10 * WEI, shares=8 * WEI)
    item = (
        '<?xml version="1.0"?><rss version="2.0"><channel><item>'
        "<title>Lido withdrawal queue incident</title>"
        "<link>https://news.example.invalid/lido/queue</link>"
        "<pubDate>Thu, 08 Oct 2026 12:00:00 GMT</pubDate>"
        "<description>A documented Lido queue note.</description>"
        "</item></channel></rss>"
    ).encode("utf-8")
    for url in ALLOWLISTED_FEED_URLS:
        body = item if url == LIDO_BLOG else _EMPTY_RSS
        desk.backend.news.setdefault(url, []).append(RawResponse(200, {"content-type": "application/rss+xml"}, body))
    clock = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
    report = _cli(capsys, desk, clock=lambda: clock)
    assert _position(report)["status"] == "observed"
    notices = [row for row in _outbox(desk) if row["payload"]["kind"] == "exposure"]
    assert len(notices) == 1
    payload = notices[0]["payload"]
    assert payload["wallet_id"] == WALLET
    assert payload["via"] == "position"
    assert payload["protocols"] == ["lido"]
    assert payload["source"] == "allowlisted_get"
    assert payload["loss_alert"] is False
    news_urls = [
        str(getattr(call, "url", ""))
        for call in desk.backend.calls
        if str(getattr(call, "method", "") or "") == "GET" and (urlsplit(str(getattr(call, "url", ""))).hostname or "") in _news_hosts()
    ]
    assert LIDO_BLOG in news_urls
    assert "https://research.lido.fi/latest.rss" in news_urls
    assert set(news_urls) <= set(ALLOWLISTED_FEED_URLS)
    for url in news_urls:
        assert ADDRESS.lower() not in url.lower()


class _Desk:
    def __init__(self, config: Path, state: Path, backend: LidoBackend) -> None:
        self.config = config
        self.state = state
        self.backend = backend


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fetch: bool = False) -> _Desk:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = ["lido"]
    if fetch:
        document["news"] = {"fetch": True}
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
    return _Desk(config, state, LidoBackend())


def _cli(
    capsys: pytest.CaptureFixture[str],
    desk: _Desk,
    argv: list[str] | None = None,
    clock=None,
) -> dict:
    command = argv or ["run-once", "--mcm-config", str(desk.config), "--no-send"]
    code = main(command, backend=desk.backend, clock=clock)
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
    return payload


def _position(report: dict) -> dict:
    found = [row for row in report["positions"] if row["plugin_id"] == "lido" and row["wallet_id"] == WALLET]
    assert len(found) == 1
    return found[0]


def _leg(legs: list[dict], kind: str, identity: str) -> dict:
    found = [leg for leg in legs if leg["kind"] == kind and leg["identity"] == identity]
    assert len(found) == 1
    return found[0]


def _matching(backend: LidoBackend, selector: str, to: str) -> list[dict]:
    return [row for row in backend.rpc if row.get("selector") == selector and row.get("to") == to]


def _selectors(backend: LidoBackend) -> set[str]:
    return {str(row["selector"]) for row in backend.rpc if row.get("selector")}


def _outbox(desk: _Desk) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.outbox_for(TENANT)
    finally:
        database.close()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
