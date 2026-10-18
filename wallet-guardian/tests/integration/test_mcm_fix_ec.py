"""FIX etherscan-catchup-slow. Offline runs through ``guardian run-once --mcm-config --no-send``."""

from __future__ import annotations

import importlib.util
import json
import math
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from guardian.cli import main
from guardian.errors import SecurityError
from guardian.sources.resolver import DohResponse, HostnameResolver
from guardian.storage import Database

_SPEC = importlib.util.spec_from_file_location(
    "mcm_free_tier_catchup",
    Path(__file__).with_name("test_mcm_free_tier_catchup.py"),
)
_FREE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_FREE)

ADDRESS_A = _FREE.ADDRESS_A
ADDRESS_B = _FREE.ADDRESS_B
KEY = _FREE.KEY
LARGE = _FREE.LARGE
OTHER = _FREE.OTHER
TENANT = _FREE.TENANT
WALLET_A = _FREE.WALLET_A
WALLET_B = _FREE.WALLET_B
_account = _FREE._account
_cli = _FREE._cli
_coverage = _FREE._coverage
_cursor = _FREE._cursor
_desk = _FREE._desk
_events = _FREE._events
_rest_transfer = _FREE._rest_transfer

HEAD = 26_000_000
ETH_BLOCK_SECONDS = 12
LOOKBACK_DAYS = 30
PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.0.0.8"
DOH_HOST = "dns.example.com"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _lookback_blocks(seconds_per_block: float = ETH_BLOCK_SECONDS, days: int = LOOKBACK_DAYS) -> int:
    return math.ceil(days * 86400 / seconds_per_block)


def _etherscan_windows(backend) -> list[tuple[int, int, str]]:
    """startblock/endblock from the fixture's recorded Etherscan URLs."""
    found = []
    for row in backend.http_log:
        parts = urlsplit(str(row.get("url") or ""))
        if parts.hostname != "etherscan.example.invalid":
            continue
        query = parse_qs(parts.query)
        if "startblock" not in query or "endblock" not in query:
            continue
        found.append((int(query["startblock"][0]), int(query["endblock"][0]), (query.get("action") or [""])[0]))
    return found


def test_mcm_fix_ec_01_fresh_db_bootstrap_is_bounded_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A fresh database starts at the lookback origin, in one indexer span, under the request cap."""
    desk = _desk(tmp_path, monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))])
    desk.backend.head = HEAD
    origin = max(0, HEAD - _lookback_blocks())
    started = time.perf_counter()
    report = _cli(capsys, desk)
    assert time.perf_counter() - started < 30
    windows = _etherscan_windows(desk.backend)
    assert windows
    assert windows[0][0] >= origin
    assert windows[0][0] != 0
    assert all(end - start != 2000 for start, end, _action in windows)
    assert all(start >= origin for start, end, _action in windows)
    etherscan_calls = [row for row in windows]
    assert len(etherscan_calls) <= 2000
    assert report["budget"]["bootstrap_request_cap"] == 2000
    assert report["budget"]["bootstrap_requests"]["etherscan"] <= 2000
    row = _coverage(report, WALLET_A, "ethereum-mainnet", "history.native_external")
    assert row["quality"] == "complete_in_declared_scope"
    assert row["scope"]["from"] == str(origin)
    assert row["scope"]["from"] != "0"
    assert "pre_origin_history_unknown" in row["reason_codes"]
    assert "incremental_window_complete" in row["reason_codes"]


def test_mcm_fix_ec_01_seconds_per_block_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """polling.seconds_per_block replaces the built-in block time for that network."""
    def _override(document: dict) -> None:
        document["polling"]["seconds_per_block"] = {"ethereum-mainnet": 86400}

    desk = _desk(tmp_path, monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))], mutate=_override)
    desk.backend.head = HEAD
    origin = max(0, HEAD - _lookback_blocks(86400))
    report = _cli(capsys, desk)
    windows = _etherscan_windows(desk.backend)
    assert windows[0][0] == origin
    assert _coverage(report, WALLET_A, "ethereum-mainnet", "history.native_external")["scope"]["from"] == str(origin)


def test_mcm_fix_ec_02_request_cap_checkpoints_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A tiny cap stops after a fully read prefix. The next round continues and does not alert twice."""
    def _cap(document: dict) -> None:
        document["polling"]["bootstrap_request_cap"] = 3

    desk = _desk(tmp_path, monkeypatch, [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))], mutate=_cap)
    desk.backend.head = 200
    kept = _FREE._tx(10)
    rows = [_rest_transfer(kept, ADDRESS_A, OTHER, 10)]
    rows.extend(_rest_transfer(_FREE._tx(1000 + index), ADDRESS_A, OTHER, 99) for index in range(2999))
    desk.backend.rest_rows[("etherscan.example.invalid", "txlist")] = rows
    first = _cli(capsys, desk)
    history = _coverage(first, WALLET_A, "ethereum-mainnet", "history.native_external")
    assert history["quality"] == "partial"
    assert "bootstrap_request_cap" in history["reason_codes"]
    checkpoint = _cursor(desk, WALLET_A, "ethereum-mainnet", "history.native_external")
    assert checkpoint == "10"
    assert first["budget"]["bootstrap_requests"]["etherscan"] == 3
    windows = _etherscan_windows(desk.backend)
    assert len(windows) == 3
    assert all(action == "txlist" for _start, _end, action in windows)
    desk.backend.rest_rows.clear()
    desk.backend.calls.clear()
    desk.backend.http_log.clear()
    second = _cli(capsys, desk)
    resumed = _etherscan_windows(desk.backend)
    assert resumed
    assert resumed[0][0] == int(checkpoint) + 1 - 2
    finished = _coverage(second, WALLET_A, "ethereum-mainnet", "history.native_external")
    assert finished["quality"] == "complete_in_declared_scope"
    stored = [row for row in _events(desk, WALLET_A, "ethereum-mainnet") if row["tx_id"] == kept]
    assert len(stored) == 1
    database = Database(desk.state / "monitor.sqlite")
    try:
        alerts = database.outbox_for(TENANT)
    finally:
        database.close()
    assert len([row for row in alerts if kept in json.dumps(row)]) == 1


def test_mcm_fix_ec_03_resolver_cache_skips_second_doh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A second request for the same host reuses the in-process DoH answer."""
    exchanges: list[str] = []
    clock = {"now": 0.0}
    _install_doh(monkeypatch, exchanges, clock, ttl=120, address=PUBLIC_IP)
    desk = _public_host_desk(tmp_path, monkeypatch)
    report = _cli(capsys, desk)
    assert report["status"] in {"ok", "degraded"}
    assert exchanges == ["A", "AAAA"]
    assert sum(1 for row in desk.backend.calls if row.get("host") == "monitor.example.com") > 2


def test_mcm_fix_ec_03_resolver_cache_ttl_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The cache lasts min(record TTL, 300s). After that the next resolve queries DoH again."""
    exchanges: list[str] = []
    clock = {"now": 0.0}
    _install_doh(monkeypatch, exchanges, clock, ttl=30, address=PUBLIC_IP)
    desk = _public_host_desk(tmp_path, monkeypatch)
    original = desk.backend.send

    def send(call):
        result = original(call)
        if clock["now"] == 0.0:
            clock["now"] = 31.0
        return result

    desk.backend.send = send
    _cli(capsys, desk)
    assert exchanges[:2] == ["A", "AAAA"]
    assert exchanges[2:4] == ["A", "AAAA"]
    assert len(exchanges) == 4


def test_mcm_fix_ec_03_resolver_private_ip_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A private answer is rejected, not cached, and a later resolve still asks DoH."""
    exchanges: list[str] = []
    clock = {"now": 0.0}
    created: list[HostnameResolver] = []
    _install_doh(monkeypatch, exchanges, clock, ttl=120, address=PRIVATE_IP, created=created)
    desk = _public_host_desk(tmp_path, monkeypatch)
    code = main(
        ["run-once", "--mcm-config", str(desk.config), "--no-send"],
        backend=desk.backend,
        clock=lambda: 1_700_000_000.0,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert "private" in json.dumps(payload).lower()
    assert ADDRESS_A.lower() not in json.dumps(payload).lower()
    before = len(exchanges)
    assert before >= 1
    with pytest.raises(SecurityError) as caught:
        created[-1].resolve("monitor.example.com")
    assert caught.value.reason == "private_endpoint"
    assert len(exchanges) > before


def test_mcm_fix_ec_04_plan_not_supported_one_request_then_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """One free-plan refusal covers both wallets and both indexer actions, including the next round."""
    def _route(document: dict) -> None:
        for route in document["routing"]:
            if route.get("network") == "bsc-mainnet" and route.get("capability") == "history.native_external":
                route["source_ids"] = ["etherscan", "nodereal-bsc"]

    desk = _desk(
        tmp_path,
        monkeypatch,
        [
            _account(WALLET_A, ADDRESS_A, ("bsc-mainnet",)),
            _account(WALLET_B, ADDRESS_B, ("bsc-mainnet",)),
        ],
        mutate=_route,
    )
    desk.backend.etherscan_plan_chains.add("56")
    report = _cli(capsys, desk)
    etherscan = [row for row in desk.backend.calls if row.get("kind") == "rest" and row.get("chainid") == "56"]
    assert len(etherscan) == 1
    assert etherscan[0]["action"] == "txlist"
    assert any(row.get("kind") == "nodereal" for row in desk.backend.calls)
    assert _coverage(report, WALLET_A, "bsc-mainnet", "history.native_external")["quality"] == "complete_in_declared_scope"
    assert _coverage(report, WALLET_B, "bsc-mainnet", "history.native_external")["quality"] == "complete_in_declared_scope"
    cache = json.loads((desk.state / "capability-failures.json").read_text(encoding="utf-8"))
    assert cache["etherscan|bsc-mainnet"] == "plan_not_supported"
    _cli(capsys, desk)
    later = [row for row in desk.backend.calls if row.get("kind") == "rest" and row.get("chainid") == "56"]
    assert len(later) == 1


def test_mcm_fix_ec_05_disabled_source_is_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """enabled false drops the source from use. History continues on the next route."""
    def _disable(document: dict) -> None:
        document["sources"]["etherscan"]["enabled"] = False
        # The assertion is the Blockscout host after Etherscan. Leave Routescan
        # off so the default origin does not complete history first.
        document["sources"]["routescan"]["enabled"] = False

    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))],
        mutate=_disable,
        secrets_extra={"BLOCKSCOUT_ETH_URL": "https://eth-public.example.invalid"},
    )
    desk.backend.rest_rows[("eth-public.example.invalid", "txlist")] = [
        _rest_transfer(_FREE._tx(7), ADDRESS_A, OTHER, 4),
    ]
    report = _cli(capsys, desk)
    assert not [row for row in desk.backend.calls if row.get("host") == "etherscan.example.invalid"]
    assert any(row.get("host") == "eth-public.example.invalid" and row.get("action") == "txlist" for row in desk.backend.calls)
    row = _coverage(report, WALLET_A, "ethereum-mainnet", "history.native_external")
    assert row["quality"] == "complete_in_declared_scope"
    assert any(int(item["raw_amount"]) == LARGE for item in _events(desk, WALLET_A, "ethereum-mainnet"))


def _public_host_desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _hosts(document: dict) -> None:
        document["sources"]["etherscan"]["enabled"] = False
        document["sources"]["routescan"]["enabled"] = False
        document["plugins"] = []

    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("ethereum-mainnet",))],
        mutate=_hosts,
        secrets_extra={
            "BLOCKSCOUT_ETH_URL": "https://monitor.example.com/api",
            "RPC_ETHEREUM_MAINNET_URL": "https://monitor.example.com/rpc",
        },
    )
    desk.backend.head = 20
    return desk


def _install_doh(monkeypatch, exchanges, clock, *, ttl: int, address: str, after=None, created=None) -> None:
    import guardian.sources.resolver as resolver_mod

    monkeypatch.setenv("GUARDIAN_RESOLVER_MODE", "doh")
    monkeypatch.setenv("GUARDIAN_DOH_ENDPOINTS", f"https://{DOH_HOST}/dns-query")
    monkeypatch.setenv("GUARDIAN_DOH_BOOTSTRAP", f"{DOH_HOST}=1.1.1.1")

    def exchange(request):
        exchanges.append(request.query_type)
        if request.query_type == "A":
            body = {"Answer": [{"TTL": ttl, "data": address, "name": request.query_name, "type": 1}], "Status": 0}
        else:
            body = {"Answer": [], "Status": 0}
        if after is not None:
            after()
        return DohResponse(200, (), json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(resolver_mod, "production_exchange", exchange)
    original = resolver_mod.HostnameResolver

    class _Clocked(original):
        def __init__(self, settings, hooks=None):
            super().__init__(settings, hooks, clock=lambda: clock["now"])
            if created is not None:
                created.append(self)

    monkeypatch.setattr(resolver_mod, "HostnameResolver", _Clocked)
