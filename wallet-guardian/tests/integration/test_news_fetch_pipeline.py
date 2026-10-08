"""MCM-LIVE-L2. The public CLI fetches allowlisted RSS through a fake transport.

No live network. Socket and HTTP clients are blocked. News bytes come from the
fixture queue. Assertions go through ``guardian.cli.main``.
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
from guardian.news.catalog import ALLOWLISTED_FEED_URLS
from guardian.comparison import _mcm_events
from guardian.news.fetch import NEWS_STATE_CAPABILITY, NEWS_STATE_WALLET
from guardian.protocols.pins import KINZA_POOLS, KINZA_PROVIDERS, UXLINK_LEGACY_TOKEN
from guardian.sources.capabilities import NEWS_USER_AGENT
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
KEY = "synthetic-etherscan-key"
NEWS_KEY = "synthetic-key-9f3a7c"
WEI = 10**18
UINT256_MAX = 2**256 - 1
UNDERLYING = to_checksum_address("0x" + ("11" * 20))
ATOKEN = to_checksum_address("0x" + ("a1" * 20))
DEBT = to_checksum_address("0x" + ("d2" * 20))
TOKEN = to_checksum_address("0x" + ("ab" * 20))
AAVE_URL = "https://governance.aave.com/latest.rss?order=created"
KINZA_URL = "https://medium.com/feed/@kinzafinance"
SOLANA_URL = "https://status.solana.com/history.rss"
NEWS_URLS = (AAVE_URL, KINZA_URL, SOLANA_URL)
NEWS_HOSTS = frozenset({"governance.aave.com", "medium.com", "status.solana.com"})
KINZA_LINK = "https://news.example.invalid/kinza/pool"
AAVE_LINK = "https://news.example.invalid/aave/note"
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
_DNS: dict[str, list[str]] = {}
_EMPTY = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
_DOCTYPE = (
    b'<?xml version="1.0"?>'
    b'<!DOCTYPE rss [<!ENTITY xxe "injected">]>'
    b'<rss version="2.0"><channel><item>'
    b"<title>Injected entity</title>"
    b"<link>https://news.example.invalid/xxe</link>"
    b"<description>injected</description>"
    b"</item></channel></rss>"
)


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
    _DNS.clear()

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)

    def resolve(host: str) -> list[str]:
        name = host.lower().rstrip(".")
        if name in _DNS:
            return list(_DNS[name])
        return ["1.1.1.1"]

    monkeypatch.setattr("guardian.pipeline._resolve", resolve)


class ChainBackend:
    """JSON-RPC answers plus a queue of RSS fixtures keyed by the exact feed URL."""

    def __init__(self) -> None:
        self.head = 100
        self.logs: list[dict] = []
        self.rpc: list[dict] = []
        self.calls: list[object] = []
        self.news: dict[str, list[RawResponse]] = {}
        self.answers: dict[tuple, str] = {}
        self.errors: set[tuple] = set()
        self.accounts: dict[tuple[str, str, str], tuple[int, ...]] = {}
        self.balances: dict[tuple[str, str, str], int] = {}
        self.symbols: dict[tuple[str, str], str] = {}

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        url = str(getattr(call, "url", ""))
        body = getattr(call, "body", b"") or b""
        host = (urlsplit(url).hostname or "").lower()
        method = str(getattr(call, "method", "") or "")
        if method == "GET" and host in NEWS_HOSTS:
            return self._news(url)
        if not body:
            return self._etherscan(url, host)
        payload = json.loads(body.decode("utf-8"))
        rpc_method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        record: dict = {"method": rpc_method, "host": host}
        if rpc_method == "eth_getLogs" and params and isinstance(params[0], dict):
            record["from_block"] = int(str(params[0].get("fromBlock", "0x0")), 16)
            record["to_block"] = int(str(params[0].get("toBlock", "0x0")), 16)
            topics = params[0].get("topics") or []
            topic0 = topics[0] if topics else None
            record["kind"] = "approval" if isinstance(topic0, list) else "transfer"
            record["owner_topic"] = topics[1] if len(topics) > 1 and isinstance(topics[1], str) else None
        if rpc_method == "eth_call" and params and isinstance(params[0], dict):
            data = str(params[0].get("data") or "")
            record["selector"] = data[:10]
            record["to"] = str(params[0].get("to") or "").lower()
            record["arg"] = _call_arg(data)
        self.rpc.append(record)
        result = self._rpc(rpc_method, record)
        if result is None:
            encoded = json.dumps({"error": {"code": -32000, "message": "fixture"}, "id": 1, "jsonrpc": "2.0"}).encode()
        else:
            encoded = json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode()
        return RawResponse(200, {}, encoded)

    def _news(self, url: str) -> RawResponse:
        queued = self.news.get(url)
        if not queued:
            raise AssertionError(f"missing news fixture for {url}")
        return queued.pop(0)

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
    def __init__(self, config: Path, state: Path, backend: ChainBackend, addresses: tuple[str, ...], tenant: str) -> None:
        self.config = config
        self.state = state
        self.backend = backend
        self.addresses = addresses
        self.tenant = tenant


def test_mcm_live_l2_01_bounded_allowlisted_get_ssrf_xxe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the three allowlisted URLs are fetched, and only with GET."""
    assert set(NEWS_URLS) == set(ALLOWLISTED_FEED_URLS)
    network = "bsc-mainnet"
    wallet = _wallet(1)
    owner = _addr(1)
    desk = _desk(tmp_path / "mixed", monkeypatch, [_account(wallet, owner, (network,))], ["kinza"], fetch=True)
    _install_kinza(desk.backend, network)
    _queue(desk.backend, _feeds(_EMPTY, _EMPTY, _EMPTY))
    _cli(capsys, desk, _argv(desk))
    desk.backend.head = 130
    desk.backend.logs = [_approval_log(network, TOKEN, owner, KINZA_POOLS[network], _tx(11), 125, 1)]
    _queue(
        desk.backend,
        _feeds(
            RawResponse(200, {"content-type": "application/rss+xml"}, _DOCTYPE),
            RawResponse(200, {"content-type": "text/xml"}, _rss("Documented Kinza pool note", KINZA_LINK, "A documented Kinza pool note.")),
            RawResponse(302, {"location": "https://evil.example/stolen.rss"}, b""),
        ),
    )
    report = _cli(capsys, desk, _argv(desk))
    news_calls = _news_calls(desk.backend)
    assert news_calls
    assert all(str(getattr(call, "method", "")) == "GET" for call in news_calls)
    assert {str(getattr(call, "url", "")) for call in news_calls} <= set(ALLOWLISTED_FEED_URLS)
    assert all(getattr(call, "connect_ip", None) == "1.1.1.1" for call in news_calls)
    assert all(not getattr(call, "body", b"") for call in news_calls)
    assert all("evil.example" not in str(getattr(call, "url", "")) for call in desk.backend.calls)
    state = _news_state(desk)
    assert state["feeds"][AAVE_URL]["status"] == "fetch_error"
    assert state["feeds"][AAVE_URL]["reason"] == "dtd_or_entity_rejected"
    assert state["feeds"][SOLANA_URL]["status"] == "fetch_error"
    assert state["feeds"][SOLANA_URL]["reason"] == "redirect_host"
    assert state["feeds"][KINZA_URL]["status"] == "ok"
    titles = [row["title"] for row in state["incidents"]]
    assert titles == ["Documented Kinza pool note"]
    assert "Injected entity" not in titles
    exposures = _of_kind(_outbox(desk), "exposure")
    assert len(exposures) == 1
    assert exposures[0]["payload"]["wallet_id"] == wallet
    assert exposures[0]["payload"]["via"] == "approval"
    assert exposures[0]["payload"]["protocols"] == ["kinza"]
    assert exposures[0]["payload"]["source"] == "allowlisted_get"
    assert exposures[0]["payload"]["loss_alert"] is False
    rows = _coverage(report, wallet, "exposure_news")
    assert rows
    assert all(row["quality"] != "complete_in_declared_scope" for row in rows)
    assert all("fetch_error" in row["reason_codes"] for row in rows)
    assert all("kinza:ok" in row["evidence_refs"] for row in rows)

    poisoned = _desk(tmp_path / "dns", monkeypatch, [_account(wallet, owner, ("ethereum-mainnet",))], [], fetch=True)
    _DNS["governance.aave.com"] = ["127.0.0.1"]
    _DNS["medium.com"] = ["169.254.169.254"]
    _DNS["status.solana.com"] = ["10.0.0.8"]
    poisoned_report = _cli(capsys, poisoned, _argv(poisoned))
    assert _news_calls(poisoned.backend) == []
    poisoned_state = _news_state(poisoned)
    assert poisoned_state["incidents"] == []
    for url in NEWS_URLS:
        assert poisoned_state["feeds"][url]["status"] == "fetch_error"
        assert poisoned_state["feeds"][url]["reason"] == "private_endpoint"
    poisoned_rows = _coverage(poisoned_report, wallet, "exposure_news")
    assert poisoned_rows
    assert all(row["quality"] == "error" for row in poisoned_rows)
    assert all("fetch_error" in row["reason_codes"] for row in poisoned_rows)
    _DNS.clear()

    sized = _desk(tmp_path / "size", monkeypatch, [_account(wallet, owner, ("ethereum-mainnet",))], [], fetch=True)
    _queue(
        sized.backend,
        _feeds(
            RawResponse(200, {}, _EMPTY),
            RawResponse(200, {}, _rss("Kinza size note", "https://news.example.invalid/kinza/size", "Still parsed.")),
            RawResponse(200, {}, b"x" * 131073),
        ),
    )
    _cli(capsys, sized, _argv(sized))
    sized_state = _news_state(sized)
    assert sized_state["feeds"][SOLANA_URL]["reason"] == "response_too_large"
    assert sized_state["feeds"][SOLANA_URL]["status"] == "fetch_error"
    assert [row["title"] for row in sized_state["incidents"]] == ["Kinza size note"]
    assert sized_state["feeds"][KINZA_URL]["status"] == "ok"

    monkeypatch.setattr("guardian.news.fetch.NEWS_GETS_PER_RUN", 1)
    budget = _desk(tmp_path / "budget", monkeypatch, [_account(wallet, owner, ("ethereum-mainnet",))], [], fetch=True)
    _queue(budget.backend, {AAVE_URL: RawResponse(200, {}, _rss("Aave budget note", AAVE_LINK, "Fetched inside the cap."))})
    budget_report = _cli(capsys, budget, _argv(budget))
    budget_calls = _news_calls(budget.backend)
    assert [str(getattr(call, "url", "")) for call in budget_calls] == [AAVE_URL]
    assert all(str(getattr(call, "method", "")) == "GET" for call in budget_calls)
    budget_state = _news_state(budget)
    assert budget_state["feeds"][AAVE_URL]["status"] == "ok"
    assert [row["title"] for row in budget_state["incidents"]] == ["Aave budget note"]
    assert budget_state["feeds"][KINZA_URL]["status"] == "budget_exhausted"
    assert budget_state["feeds"][SOLANA_URL]["status"] == "budget_exhausted"
    budget_rows = _coverage(budget_report, wallet, "exposure_news")
    assert budget_rows
    assert all("budget_exhausted" in row["reason_codes"] for row in budget_rows)
    assert all(row["quality"] != "complete_in_declared_scope" for row in budget_rows)
    monkeypatch.setattr("guardian.news.fetch.NEWS_GETS_PER_RUN", 8)


def test_mcm_live_l2_02_etag_304_cache_and_unchanged_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A 304 reuses the stored item set, and an identical body does not open a new incident."""
    network = "bsc-mainnet"
    approved = _wallet(1)
    later = _wallet(2)
    owners = {approved: _addr(1), later: _addr(2)}
    desk = _desk(
        tmp_path,
        monkeypatch,
        [
            _account(approved, owners[approved], (network,)),
            _account(later, owners[later], (network,)),
        ],
        ["kinza"],
        fetch=True,
    )
    _install_kinza(desk.backend, network)
    kinza_body = _rss("Documented Kinza pool note", KINZA_LINK, "A documented Kinza pool note.")
    aave_body = _rss("Aave governance note", AAVE_LINK, "A documented Aave governance note.")
    etag = 'W/"kinza-1"'
    _queue(desk.backend, _feeds(_EMPTY, _EMPTY, _EMPTY), _feeds(_EMPTY, _EMPTY, _EMPTY))
    _cli(capsys, desk, _argv(desk))
    desk.backend.head = 130
    desk.backend.logs = [_approval_log(network, TOKEN, owners[approved], KINZA_POOLS[network], _tx(21), 125, 1)]
    _cli(capsys, desk, _argv(desk))
    assert not _of_kind(_outbox(desk), "exposure")
    desk.backend.head = 1000
    _queue(
        desk.backend,
        _feeds(
            RawResponse(200, {}, aave_body),
            RawResponse(200, {"etag": etag}, kinza_body),
            RawResponse(200, {}, _EMPTY),
        ),
    )
    fetched = _cli(capsys, desk, _argv(desk))
    first = _of_kind(_outbox(desk), "exposure")
    assert len(first) == 1
    assert first[0]["payload"]["wallet_id"] == approved
    assert first[0]["payload"]["source"] == "allowlisted_get"
    assert first[0]["payload"]["via"] == "approval"
    assert first[0]["payload"]["protocols"] == ["kinza"]
    incident_id = first[0]["payload"]["incident_id"]
    assert incident_id
    stored_ids = _incident_ids(desk)
    assert _news_state(desk)["feeds"][KINZA_URL]["etag"] == etag
    assert len([row for row in _news_state(desk)["incidents"] if AAVE_LINK in row["urls"]]) == 1
    desk.backend.head = 1100
    desk.backend.logs.append(_approval_log(network, TOKEN, owners[later], KINZA_POOLS[network], _tx(22), 1050, 1))
    before = {row["id"] for row in _outbox(desk)}
    _queue(
        desk.backend,
        _feeds(
            RawResponse(200, {}, aave_body),
            RawResponse(304, {}, b""),
            RawResponse(200, {}, _EMPTY),
        ),
    )
    cached = _cli(capsys, desk, _argv(desk))
    kinza_calls = [call for call in _news_calls(desk.backend) if str(getattr(call, "url", "")) == KINZA_URL]
    assert _headers(kinza_calls[-1]).get("if-none-match") == etag
    assert "authorization" not in _headers(kinza_calls[-1])
    assert _news_state(desk)["feeds"][KINZA_URL]["status"] == "not_modified"
    assert _incident_ids(desk) == stored_ids
    assert len([row for row in _news_state(desk)["incidents"] if AAVE_LINK in row["urls"]]) == 1
    added = [row for row in _outbox(desk) if row["id"] not in before]
    added_exposure = [row for row in added if row["payload"]["kind"] == "exposure"]
    assert len(added_exposure) == 1
    assert added_exposure[0]["payload"]["wallet_id"] == later
    assert added_exposure[0]["payload"]["incident_id"] == incident_id
    assert added_exposure[0]["payload"]["source"] == "allowlisted_get"
    assert added_exposure[0]["payload"]["via"] == "approval"
    assert not [row for row in added if row["payload"]["wallet_id"] == approved and row["payload"]["kind"] == "exposure"]
    assert len(_of_kind(_outbox(desk), "exposure")) == 2
    for report in (fetched, cached):
        rows = _coverage(report, approved, "exposure_news")
        assert rows
        assert all(row["quality"] != "complete_in_declared_scope" for row in rows)
    cached_rows = _coverage(cached, later, "exposure_news")
    assert all("not_modified" in row["reason_codes"] for row in cached_rows)
    assert all("kinza:not_modified" in row["evidence_refs"] for row in cached_rows)


def test_mcm_live_l2_03_no_user_wallet_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """News requests carry the catalog URL and a fixed user agent, not the wallet."""
    tenant = "synthetic-tenant"
    label = "synthetic-label-9f3a"
    wallet = "9f3a9f3a-1111-4111-8111-9f3a9f3a9f3a"
    owner = to_checksum_address("0x0000000000000000000000000000000000009F3A")
    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(wallet, owner, ("ethereum-mainnet",), label=label)],
        [],
        fetch=True,
        tenant=tenant,
        extra_secrets={"SYNTHETIC_NEWS_TOKEN": NEWS_KEY},
    )
    monkeypatch.setenv("SYNTHETIC_NEWS_TOKEN", NEWS_KEY)
    _queue(desk.backend, _feeds(_EMPTY, _rss("Public Kinza note", KINZA_LINK, "Public body."), _EMPTY))
    report = _cli(capsys, desk, _argv(desk))
    news_calls = _news_calls(desk.backend)
    assert [str(getattr(call, "url", "")) for call in news_calls] == list(NEWS_URLS)
    markers = (owner, owner.lower(), wallet, tenant, label, NEWS_KEY, "SYNTHETIC_NEWS_TOKEN")
    for call in news_calls:
        text = _call_text(call)
        headers = _headers(call)
        assert str(getattr(call, "method", "")) == "GET"
        assert not getattr(call, "body", b"")
        assert headers.get("user-agent") == NEWS_USER_AGENT
        assert set(headers) <= {"accept", "user-agent", "if-none-match", "if-modified-since"}
        for marker in markers:
            assert marker not in text
    for call in desk.backend.calls:
        assert NEWS_KEY not in _call_text(call)
    rendered = json.dumps(report)
    assert NEWS_KEY not in rendered
    assert NEWS_KEY.encode() not in (desk.state / "monitor.sqlite").read_bytes()
    database_text = _database_text(desk)
    assert NEWS_KEY not in database_text
    assert label not in "\n".join(_call_text(call) for call in news_calls)
    chain_calls = [call for call in desk.backend.calls if call not in news_calls]
    assert any(owner[2:].lower() in _call_text(call).lower() for call in chain_calls)


def test_mcm_live_l2_04_incident_update_dedup_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """An earlier Kinza Pool approval links once, and a revised item updates once."""
    network = "bsc-mainnet"
    approved = _wallet(1)
    empty = _wallet(2)
    owners = {approved: _addr(1), empty: _addr(2)}
    desk = _desk(
        tmp_path,
        monkeypatch,
        [
            _account(approved, owners[approved], (network,)),
            _account(empty, owners[empty], (network,)),
        ],
        ["kinza"],
        fetch=True,
    )
    _install_kinza(desk.backend, network)
    original = _rss("Documented Kinza pool note", KINZA_LINK, "A documented Kinza pool note.")
    revised = _rss("Update: Documented Kinza pool note", KINZA_LINK, "A revised Kinza pool note.")
    _queue(
        desk.backend,
        _feeds(_EMPTY, _EMPTY, _EMPTY),
        _feeds(_EMPTY, _EMPTY, _EMPTY),
        _feeds(_EMPTY, RawResponse(200, {}, original), _EMPTY),
        _feeds(_EMPTY, RawResponse(200, {}, original), _EMPTY),
        _feeds(_EMPTY, RawResponse(200, {}, revised), _EMPTY),
        _feeds(_EMPTY, RawResponse(200, {}, revised), _EMPTY),
    )
    _cli(capsys, desk, _argv(desk))
    desk.backend.head = 130
    desk.backend.logs = [_approval_log(network, TOKEN, owners[approved], KINZA_POOLS[network], _tx(31), 125, 1)]
    _cli(capsys, desk, _argv(desk))
    assert len(_of_kind(_outbox(desk), "first_observed")) == 1
    assert not _of_kind(_outbox(desk), "exposure")
    desk.backend.head = 1000
    _cli(capsys, desk, _argv(desk))
    notices = _of_kind(_outbox(desk), "exposure")
    assert len(notices) == 1
    payload = notices[0]["payload"]
    assert payload["wallet_id"] == approved
    assert payload["via"] == "approval"
    assert payload["protocols"] == ["kinza"]
    assert payload["source"] == "allowlisted_get"
    assert payload["loss_alert"] is False
    incident_id = payload["incident_id"]
    assert incident_id
    assert not _for_wallet(_outbox(desk), empty, "exposure")
    assert not _for_wallet(_outbox(desk), empty, "exposure_unknown")
    desk.backend.head = 1100
    _cli(capsys, desk, _argv(desk))
    assert len(_of_kind(_outbox(desk), "exposure")) == 1
    assert not _of_kind(_outbox(desk), "exposure_update")
    assert _incident_ids(desk) == {incident_id}
    desk.backend.head = 1200
    _cli(capsys, desk, _argv(desk))
    updates = _of_kind(_outbox(desk), "exposure_update")
    assert len(updates) == 1
    assert updates[0]["payload"]["wallet_id"] == approved
    assert updates[0]["payload"]["incident_id"] == incident_id
    assert updates[0]["payload"]["source"] == "allowlisted_get"
    assert updates[0]["payload"]["loss_alert"] is False
    assert len(_of_kind(_outbox(desk), "exposure")) == 1
    assert _incident_ids(desk) == {incident_id}
    assert not _for_wallet(_outbox(desk), empty, "exposure")
    assert not _for_wallet(_outbox(desk), empty, "exposure_update")
    desk.backend.head = 1300
    _cli(capsys, desk, _argv(desk))
    assert len(_of_kind(_outbox(desk), "exposure_update")) == 1
    assert len(_of_kind(_outbox(desk), "exposure")) == 1


def test_mcm_live_l2_05_no_feed_gap_not_no_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A protocol without an allowlisted feed is a gap, including when fetch is off."""
    wallet = _wallet(1)
    owner = _addr(1)
    network = "arbitrum-one"
    disabled = _desk(
        tmp_path / "disabled",
        monkeypatch,
        [_account(wallet, owner, (network,))],
        ["uxlink-holdings"],
    )
    disabled.backend.balances[(HOST[network], UXLINK_LEGACY_TOKEN.lower(), owner.lower())] = WEI
    quiet = _cli(capsys, disabled, _argv(disabled))
    assert _news_calls(disabled.backend) == []
    assert all("governance.aave.com" not in str(getattr(call, "url", "")) for call in disabled.backend.calls)
    assert all("medium.com" not in str(getattr(call, "url", "")) for call in disabled.backend.calls)
    assert all("status.solana.com" not in str(getattr(call, "url", "")) for call in disabled.backend.calls)
    quiet_rows = _coverage(quiet, wallet, "exposure_news")
    assert quiet_rows
    assert all(row["quality"] != "complete_in_declared_scope" for row in quiet_rows)
    assert all("news_not_supplied" in row["reason_codes"] for row in quiet_rows)
    assert all("unsupported" in row["reason_codes"] for row in quiet_rows)
    assert all("no news" not in " ".join(row["reason_codes"]) for row in quiet_rows)

    enabled = _desk(
        tmp_path / "enabled",
        monkeypatch,
        [_account(wallet, owner, (network,))],
        ["uxlink-holdings"],
        fetch=True,
    )
    enabled.backend.balances[(HOST[network], UXLINK_LEGACY_TOKEN.lower(), owner.lower())] = WEI
    _queue(enabled.backend, _feeds(_EMPTY, _EMPTY, _EMPTY))
    opened = _cli(capsys, enabled, _argv(enabled))
    assert [str(getattr(call, "url", "")) for call in _news_calls(enabled.backend)] == list(NEWS_URLS)
    assert _position_status(opened, wallet, "uxlink_holdings") == "observed"
    opened_rows = _coverage(opened, wallet, "exposure_news")
    assert opened_rows
    assert all(row["quality"] != "complete_in_declared_scope" for row in opened_rows)
    assert all(row["quality"] != "ok" for row in opened_rows)
    assert all("no_allowlisted_feed" in row["reason_codes"] for row in opened_rows)
    assert all("uxlink" in row["reason_codes"] for row in opened_rows)
    assert all("uxlink" in row["scope"]["gaps"] for row in opened_rows)
    assert all("news_not_supplied" not in row["reason_codes"] for row in opened_rows)
    assert all("no news" not in " ".join(row["reason_codes"]) for row in opened_rows)
    assert any(ref.endswith(":empty") for row in opened_rows for ref in row["evidence_refs"])
    state = _news_state(enabled)
    assert state["feeds"][AAVE_URL]["status"] == "empty"
    assert state["incidents"] == []

    failed = _desk(
        tmp_path / "failed",
        monkeypatch,
        [_account(wallet, owner, ("ethereum-mainnet",))],
        [],
        fetch=True,
    )
    _queue(failed.backend, _feeds(RawResponse(500, {}, b"nope"), RawResponse(500, {}, b"nope"), RawResponse(500, {}, b"nope")))
    failed_report = _cli(capsys, failed, _argv(failed))
    failed_rows = _coverage(failed_report, wallet, "exposure_news")
    assert failed_rows
    assert all(row["quality"] == "error" for row in failed_rows)
    assert all("fetch_error" in row["reason_codes"] for row in failed_rows)
    assert all("empty" not in row["reason_codes"] for row in failed_rows)
    assert all(not ref.endswith(":empty") for row in failed_rows for ref in row["evidence_refs"])
    assert all("no news" not in " ".join(row["reason_codes"]) for row in failed_rows)
    assert _news_state(failed)["incidents"] == []
    assert all(item["status"] == "fetch_error" for item in _news_state(failed)["feeds"].values())


def test_mcm_live_l2_04_first_fetch_is_quiet_baseline_for_old_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-LIVE-L2-04. Enabling fetch does not replay a feed's history; a later new item notifies once."""
    network = "bsc-mainnet"
    approved = _wallet(1)
    desk = _desk(tmp_path, monkeypatch, [_account(approved, _addr(1), (network,))], ["kinza"], fetch=True)
    _install_kinza(desk.backend, network)
    old_items = [(f"Old Kinza note {n}", f"https://kinzafinance.medium.com/old-{n}") for n in range(30)]
    history = _rss_many(old_items, "Sun, 01 Jan 2023 00:00:00 GMT")
    newer = _rss_many(old_items + [("New Kinza note", "https://kinzafinance.medium.com/new-1")], "Sun, 01 Jan 2023 00:00:00 GMT")
    down = RawResponse(503, {}, b"")
    _queue(
        desk.backend,
        _feeds(_EMPTY, down, _EMPTY),
        _feeds(_EMPTY, down, _EMPTY),
        _feeds(_EMPTY, RawResponse(200, {}, history), _EMPTY),
        _feeds(_EMPTY, RawResponse(200, {}, newer), _EMPTY),
        _feeds(_EMPTY, RawResponse(200, {}, newer), _EMPTY),
    )
    _cli(capsys, desk, _argv(desk))
    desk.backend.head = 130
    desk.backend.logs = [_approval_log(network, TOKEN, _addr(1), KINZA_POOLS[network], _tx(41), 125, 1)]
    _cli(capsys, desk, _argv(desk))
    desk.backend.head = 1000
    _cli(capsys, desk, _argv(desk))
    assert len(_news_state(desk)["incidents"]) == 30
    assert not _of_kind(_outbox(desk), "exposure")
    desk.backend.head = 1100
    _cli(capsys, desk, _argv(desk))
    notices = _of_kind(_outbox(desk), "exposure")
    assert len(notices) == 1
    assert notices[0]["payload"]["wallet_id"] == approved
    assert notices[0]["payload"]["via"] == "approval"
    desk.backend.head = 1200
    _cli(capsys, desk, _argv(desk))
    assert len(_of_kind(_outbox(desk), "exposure")) == 1
    database = Database(desk.state / "monitor.sqlite")
    try:
        assert _mcm_events(database, desk.tenant)[1] == {network}
    finally:
        database.close()


def _rss_many(items: list[tuple[str, str]], published: str) -> bytes:
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link><pubDate>{published}</pubDate>"
        f"<description>{title}.</description></item>"
        for title, link in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{entries}</channel></rss>'.encode("utf-8")


def _rss(title: str, link: str, body: str) -> bytes:
    xml = (
        '<?xml version="1.0"?>'
        '<rss version="2.0"><channel><item>'
        f"<title>{title}</title><link>{link}</link>"
        "<pubDate>Thu, 01 Oct 2026 00:00:00 GMT</pubDate>"
        f"<description>{body}</description>"
        "</item></channel></rss>"
    )
    return xml.encode("utf-8")


def _feeds(aave: RawResponse | bytes, kinza: RawResponse | bytes, solana: RawResponse | bytes) -> dict[str, RawResponse]:
    return {AAVE_URL: _as_response(aave), KINZA_URL: _as_response(kinza), SOLANA_URL: _as_response(solana)}


def _as_response(value: RawResponse | bytes) -> RawResponse:
    if isinstance(value, RawResponse):
        return value
    return RawResponse(200, {}, value)


def _queue(backend: ChainBackend, *rounds: dict[str, RawResponse]) -> None:
    for round_ in rounds:
        for url, response in round_.items():
            backend.news.setdefault(url, []).append(response)


def _news_calls(backend: ChainBackend) -> list[object]:
    found = []
    for call in backend.calls:
        host = (urlsplit(str(getattr(call, "url", ""))).hostname or "").lower()
        if host in NEWS_HOSTS or str(getattr(call, "source_id", "")).startswith("news-"):
            found.append(call)
    return found


def _headers(call: object) -> dict[str, str]:
    found = {}
    for name, value in getattr(call, "public_headers", ()) or ():
        found[str(name).lower()] = str(value)
    return found


def _call_text(call: object) -> str:
    parts = [
        str(getattr(call, "url", "")),
        str(getattr(call, "method", "")),
        str(getattr(call, "body", b"")),
        str(getattr(call, "source_id", "")),
    ]
    parts.extend(f"{name}:{value}" for name, value in getattr(call, "public_headers", ()) or ())
    return "\n".join(parts)


def _install_kinza(backend: ChainBackend, network: str) -> None:
    host = HOST[network]
    pool = KINZA_POOLS[network]
    provider = KINZA_PROVIDERS[network]
    pool_l = pool.lower()
    provider_l = provider.lower()
    backend.answers[(host, provider_l, _selector("getPool()"), None)] = _abi(["address"], [pool])
    backend.answers[(host, pool_l, _selector("ADDRESSES_PROVIDER()"), None)] = _abi(["address"], [provider])
    backend.answers[(host, pool_l, _selector("getReservesList()"), None)] = _abi(["address[]"], [[UNDERLYING]])
    words = [0] * 16
    words[8] = int(ATOKEN, 16)
    words[10] = int(DEBT, 16)
    backend.answers[(host, pool_l, SEL_RESERVE_DATA, UNDERLYING.lower())] = _abi(["uint256"] * 16, words)
    for token in (ATOKEN.lower(), DEBT.lower()):
        backend.answers[(host, token, _selector("POOL()"), None)] = _abi(["address"], [pool])
        backend.answers[(host, token, _selector("UNDERLYING_ASSET_ADDRESS()"), None)] = _abi(["address"], [UNDERLYING])


def _desk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accounts: list[dict],
    plugins: list[str],
    *,
    fetch: bool | None = None,
    tenant: str = TENANT,
    extra_secrets: dict[str, str] | None = None,
) -> _Desk:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = plugins
    if fetch is not None:
        document["news"] = {"fetch": fetch}
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
    if extra_secrets:
        secrets.update(extra_secrets)
    accounts_path = tmp_path / "accounts.json"
    secret_path = tmp_path / "secrets.json"
    state = tmp_path / "state"
    _write_private(
        accounts_path,
        json.dumps({"schema_version": "guardian.accounts.v1", "tenant_id": tenant, "accounts": accounts}) + "\n",
    )
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts_path))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    addresses = tuple(str(account["address"]) for account in accounts)
    return _Desk(config, state, ChainBackend(), addresses, tenant)


def _account(wallet_id: str, address: str, networks: tuple[str, ...], *, label: str = "desk") -> dict:
    return {
        "address": address,
        "family": "evm",
        "label": label,
        "networks": list(networks),
        "own_group": "desk",
        "relationship": "owned_user_declared",
        "wallet_id": wallet_id,
    }


def _argv(desk: _Desk) -> list[str]:
    return ["run-once", "--mcm-config", str(desk.config), "--no-send"]


def _cli(capsys: pytest.CaptureFixture[str], desk: _Desk, argv: list[str]) -> dict:
    code = main(argv, backend=desk.backend, clock=lambda: 1_700_000_000.0)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    blob = json.dumps(payload)
    assert KEY not in blob
    assert NEWS_KEY not in blob
    for address in desk.addresses:
        assert address.lower() not in blob.lower()
    assert payload.get("live_verified") is False
    assert payload.get("live_pass") is not True
    return payload


def _outbox(desk: _Desk) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.outbox_for(desk.tenant)
    finally:
        database.close()


def _news_state(desk: _Desk) -> dict:
    database = Database(desk.state / "monitor.sqlite")
    try:
        for row in database.capability_state_for(desk.tenant):
            if row.get("wallet_id") != NEWS_STATE_WALLET or row.get("capability") != NEWS_STATE_CAPABILITY:
                continue
            payload = row.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("news_fetch"), dict):
                return payload["news_fetch"]
    finally:
        database.close()
    raise AssertionError("news fetch state is missing")


def _database_text(desk: _Desk) -> str:
    database = Database(desk.state / "monitor.sqlite")
    try:
        rows = database.capability_state_for(desk.tenant)
        return json.dumps({"outbox": database.outbox_for(desk.tenant), "state": rows})
    finally:
        database.close()


def _incident_ids(desk: _Desk) -> set[str]:
    return {str(row["incident_id"]) for row in _news_state(desk)["incidents"]}


def _coverage(report: dict, wallet: str, capability: str) -> list[dict]:
    return [
        row for row in report["coverage"]
        if row["wallet_id"] == wallet and row["capability"] == capability
    ]


def _position_status(report: dict, wallet: str, plugin_id: str) -> str:
    found = [
        row for row in report["positions"]
        if row["wallet_id"] == wallet and row["plugin_id"] == plugin_id
    ]
    assert len(found) == 1
    return str(found[0]["status"])


def _of_kind(rows: list[dict], kind: str) -> list[dict]:
    return [row for row in rows if row["payload"]["kind"] == kind]


def _for_wallet(rows: list[dict], wallet: str, kind: str) -> list[dict]:
    return [row for row in rows if row["payload"]["kind"] == kind and row["payload"]["wallet_id"] == wallet]


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


def _call_arg(data: str) -> str | None:
    if not isinstance(data, str) or len(data) < 74:
        return None
    return "0x" + data[10:74][-40:]


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
