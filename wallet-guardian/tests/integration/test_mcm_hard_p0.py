"""HARDENING R1 part 1. P0-01, P0-02, and P0-03 through the real CLI.

Sockets stay blocked. Addresses and keys are synthetic. Each test calls
``guardian run-once --mcm-config --no-send`` with an injected fixture backend.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from guardian.chains.evm.accounting import TRANSFER_TOPIC
from guardian.cli import main
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

_SPEC = importlib.util.spec_from_file_location(
    "mcm_hard_p0_support",
    Path(__file__).with_name("test_mcm_free_tier_catchup.py"),
)
_FREE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_FREE)

ADDRESS_A = _FREE.ADDRESS_A
BLOCKSCOUT_KEY = _FREE.BLOCKSCOUT_KEY
KEY = _FREE.KEY
NODEREAL_KEY = _FREE.NODEREAL_KEY
TENANT = _FREE.TENANT
WALLET_A = _FREE.WALLET_A
_account = _FREE._account
_approval_log = _FREE._approval_log
_coverage = _FREE._coverage
_cursor = _FREE._cursor
_desk = _FREE._desk
_events = _FREE._events
_transfer_log = _FREE._transfer_log
_tx = _FREE._tx

ALCHEMY_KEY = "syntheticAlchemyKey99"
ALCHEMY_URL = f"https://alchemy.example.invalid/v2/{ALCHEMY_KEY}"
ETH = "ethereum-mainnet"
ARB = "arbitrum-one"
RPC_ETH = "rpc-ethereum-mainnet"
RPC_ARB = "rpc-arbitrum-one"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class DirectionBackend(_FREE.FixtureBackend):
    """One-sided range refusals and one-shot Retry-After, on top of the shared fixture."""

    def __init__(self) -> None:
        super().__init__()
        self.out_limit: int | None = None
        self.in_limit: int | None = None
        self.hard_out = False
        self.rate_sources: set[str] = set()
        self.rate_logs_only = False
        self.retry_after = "3"
        self._rated: set[str] = set()

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        source = str(getattr(call, "source_id", "") or "")
        body = getattr(call, "body", b"") or b""
        payload = _payload(body)
        if self._rate(source, payload):
            self._rated.add(source)
            self.http_log.append({"authorization": "", "url": url})
            return RawResponse(429, {"retry-after": self.retry_after}, b"{}")
        if isinstance(payload, dict) and payload.get("method") == "eth_getLogs":
            decision = self._decision(payload)
            if decision is not None:
                self._record(url, payload)
                message = "block range is too large" if decision == "range" else "fixture-log-failure"
                encoded = {
                    "error": {"code": -32000, "message": message},
                    "id": payload.get("id", 1),
                    "jsonrpc": "2.0",
                }
                return RawResponse(200, {}, json.dumps(encoded).encode())
        return super().send(call)

    def _rate(self, source: str, payload: object) -> bool:
        if source not in self.rate_sources or source in self._rated:
            return False
        if self.rate_logs_only:
            return isinstance(payload, dict) and payload.get("method") == "eth_getLogs"
        return True

    def _decision(self, payload: dict) -> str | None:
        start, end, topics = _span(payload)
        side = _transfer_side(topics)
        if side is None:
            return None
        if side == "out" and self.hard_out:
            return "hard"
        blocks = end - start + 1
        if side == "out" and self.out_limit is not None and blocks > self.out_limit:
            return "range"
        if side == "in" and self.in_limit is not None and blocks > self.in_limit:
            return "range"
        return None

    def _record(self, url: str, payload: dict) -> None:
        start, end, topics = _span(payload)
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        filt = params[0] if params and isinstance(params[0], dict) else {}
        address = filt.get("address")
        self.calls.append({
            "address": address.lower() if isinstance(address, str) else None,
            "from": start,
            "host": urlsplit(url).hostname or "",
            "kind": "log",
            "to": end,
            "topics": topics,
        })


class Sleeper:
    def __init__(self, clock: dict[str, float] | None = None) -> None:
        self.calls: list[float] = []
        self.clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        if self.clock is not None:
            self.clock["now"] += float(seconds)


def _payload(body: bytes) -> object:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _span(payload: dict) -> tuple[int, int, list]:
    params = payload.get("params") if isinstance(payload.get("params"), list) else []
    filt = params[0] if params and isinstance(params[0], dict) else {}
    start = int(str(filt.get("fromBlock", "0x0")), 16)
    end = int(str(filt.get("toBlock", "0x0")), 16)
    topics = filt.get("topics") if isinstance(filt.get("topics"), list) else []
    return start, end, topics


def _transfer_side(topics: list) -> str | None:
    if not topics or not isinstance(topics[0], str) or topics[0].lower() != TRANSFER_TOPIC.lower():
        return None
    if len(topics) > 1 and topics[1] is not None:
        return "out"
    if len(topics) > 2 and topics[2] is not None:
        return "in"
    return None


def _run(
    capsys: pytest.CaptureFixture[str],
    desk,
    *,
    clock=None,
    forbidden: tuple[str, ...] = (),
) -> dict:
    code = main(
        ["run-once", "--mcm-config", str(desk.config), "--no-send"],
        backend=desk.backend,
        clock=clock or (lambda: 1_700_000_000.0),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    blob = json.dumps(payload)
    for secret in (KEY, NODEREAL_KEY, BLOCKSCOUT_KEY, *forbidden):
        assert secret not in blob
    assert ADDRESS_A.lower() not in blob.lower()
    return payload


def _use(desk) -> DirectionBackend:
    backend = DirectionBackend()
    desk.backend = backend
    return backend


def _ethereum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate=None):
    return _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ETH,))],
        mutate=mutate,
    )


def _transfer_calls(backend) -> list[dict]:
    found = []
    for row in backend.calls:
        if row.get("kind") != "log":
            continue
        topics = row.get("topics") if isinstance(row.get("topics"), list) else []
        if _transfer_side(topics) is None:
            continue
        found.append(row)
    return found


def _spans(rows: list[dict], side: str) -> set[tuple[int, int]]:
    return {(int(row["from"]), int(row["to"])) for row in rows if _transfer_side(row.get("topics") or []) == side}


def _tx_counts(desk, network: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _events(desk, WALLET_A, network):
        tx = str(row["tx_id"])
        counts[tx] = counts.get(tx, 0) + 1
    return counts


def _outbox(desk) -> list[dict]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return database.outbox_for(TENANT)
    finally:
        database.close()


def _reopen(desk) -> None:
    database = Database(desk.state / "monitor.sqlite")
    database.close()


def _cap(document: dict, limit: int) -> None:
    document["polling"]["bootstrap_request_cap"] = limit


def _deadline(document: dict, seconds: int) -> None:
    document["polling"]["round_deadline_seconds"] = seconds


def test_mcm_hard_p0_01a_both_sides_one_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-01a. One leaf is read on both directions of the same span and committed once."""
    desk = _ethereum(tmp_path, monkeypatch)
    backend = _use(desk)
    backend.head = 30
    tx = _tx(11)
    backend.logs = [_transfer_log(tx, 10, 1, ADDRESS_A)]
    report = _run(capsys, desk)
    calls = _transfer_calls(backend)
    assert _spans(calls, "in") == {(0, 30)}
    assert _spans(calls, "out") == {(0, 30)}
    assert _cursor(desk, WALLET_A, ETH, "history.fungible") == "30"
    history = _coverage(report, WALLET_A, ETH, "history.fungible")
    assert history["quality"] == "complete_in_declared_scope"
    assert "unread_gap" not in history["reason_codes"]
    assert _tx_counts(desk, ETH)[tx] == 1


def test_mcm_hard_p0_01b_one_side_bisect_common_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-01b. A wide outgoing range is split, and both directions use those same leaves.

    Three rounds must advance the cursor to head. The unread-gap loop this bug
    used to leave behind would still be requesting block 0 on round 3.
    """
    desk = _ethereum(tmp_path, monkeypatch)
    backend = _use(desk)
    backend.head = 80
    backend.out_limit = 50
    tx = _tx(12)
    backend.logs = [_transfer_log(tx, 10, 1, ADDRESS_A)]
    starts = []
    last = None
    for _round in range(3):
        backend.calls.clear()
        last = _run(capsys, desk)
        calls = _transfer_calls(backend)
        assert calls
        assert _spans(calls, "in") == _spans(calls, "out")
        starts.append(min(int(row["from"]) for row in calls))
    assert starts[0] == 0
    assert int(_cursor(desk, WALLET_A, ETH, "history.fungible") or -1) == 80
    assert starts[2] > 0
    history = _coverage(last, WALLET_A, ETH, "history.fungible")
    assert history["quality"] == "complete_in_declared_scope"
    assert "unread_gap" not in history["reason_codes"]
    assert _tx_counts(desk, ETH)[tx] == 1


def test_mcm_hard_p0_01c_both_sides_bisect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-01c. Different width limits still commit only spans both sides have read."""
    desk = _ethereum(tmp_path, monkeypatch)
    backend = _use(desk)
    backend.head = 80
    backend.in_limit = 30
    backend.out_limit = 45
    tx = _tx(13)
    backend.logs = [_transfer_log(tx, 70, 1, ADDRESS_A)]
    last = None
    for _round in range(3):
        backend.calls.clear()
        last = _run(capsys, desk)
        calls = _transfer_calls(backend)
        assert _spans(calls, "in") == _spans(calls, "out")
        assert _spans(calls, "in")
    assert _cursor(desk, WALLET_A, ETH, "history.fungible") == "80"
    history = _coverage(last, WALLET_A, ETH, "history.fungible")
    assert history["quality"] == "complete_in_declared_scope"
    assert "unread_gap" not in history["reason_codes"]
    assert _tx_counts(desk, ETH)[tx] == 1


def test_mcm_hard_p0_01d_partial_failure_does_not_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-01d. One direction failing outright does not commit the leaf or walk past it."""
    desk = _ethereum(tmp_path, monkeypatch)
    backend = _use(desk)
    backend.head = 40
    backend.hard_out = True
    counts = []
    for _round in range(3):
        backend.calls.clear()
        report = _run(capsys, desk)
        calls = _transfer_calls(backend)
        assert len(calls) == 2
        assert {int(row["from"]) for row in calls} == {0}
        assert _spans(calls, "in") == {(0, 40)}
        assert _spans(calls, "out") == {(0, 40)}
        counts.append(len(calls))
        history = _coverage(report, WALLET_A, ETH, "history.fungible")
        assert history["quality"] != "complete_in_declared_scope"
    assert counts == [2, 2, 2]
    assert _cursor(desk, WALLET_A, ETH, "history.fungible") is None


def test_mcm_hard_p0_01e_restart_resumes_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-01e. A second process reopens the database and continues from the committed prefix."""
    def _narrow(document: dict) -> None:
        document["sources"][RPC_ETH]["log_range_max"] = 100

    desk = _ethereum(tmp_path, monkeypatch, _narrow)
    backend = _use(desk)
    backend.head = 300
    backend.fail_from = 202
    early = _tx(14)
    late = _tx(15)
    backend.logs = [
        _transfer_log(early, 50, 1, ADDRESS_A),
        _transfer_log(late, 250, 1, ADDRESS_A),
    ]
    _run(capsys, desk)
    assert _cursor(desk, WALLET_A, ETH, "history.fungible") == "201"
    assert _tx_counts(desk, ETH).get(early) == 1
    assert late not in _tx_counts(desk, ETH)
    _reopen(desk)
    backend.fail_from = None
    backend.calls.clear()
    second = _run(capsys, desk)
    resumed = [row for row in backend.calls if row.get("kind") == "log" and "from" in row]
    assert resumed
    assert min(int(row["from"]) for row in resumed) == 200
    assert 0 not in {int(row["from"]) for row in resumed}
    assert _cursor(desk, WALLET_A, ETH, "history.fungible") == "300"
    assert _coverage(second, WALLET_A, ETH, "history.fungible")["quality"] == "complete_in_declared_scope"
    assert _tx_counts(desk, ETH)[early] == 1
    assert _tx_counts(desk, ETH)[late] == 1


def test_mcm_hard_p0_01f_duplicate_events_not_double_alerted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-01f. The overlap re-read stores the transfer once and does not add another alert."""
    desk = _ethereum(tmp_path, monkeypatch)
    backend = _use(desk)
    backend.head = 100
    tx = _tx(16)
    backend.logs = [_transfer_log(tx, 99, 1, ADDRESS_A)]
    _run(capsys, desk)
    assert _tx_counts(desk, ETH)[tx] == 1
    alerts = len(_outbox(desk))
    backend.calls.clear()
    _run(capsys, desk)
    reread = _transfer_calls(backend)
    assert any(int(row["from"]) <= 99 <= int(row["to"]) for row in reread)
    assert _tx_counts(desk, ETH)[tx] == 1
    assert len(_outbox(desk)) == alerts
    mentioned = sum(1 for row in _outbox(desk) if tx in json.dumps(row))
    assert mentioned <= 1


def test_mcm_hard_p0_01g_unreadable_block_gap_is_finite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-01g. A single unreadable block is an explicit gap. The round ends and the cursor does not cross it."""
    desk = _ethereum(tmp_path, monkeypatch)
    backend = _use(desk)
    backend.head = 8
    backend.bisect = True
    backend.bad_blocks = {3}
    for _round in range(3):
        backend.calls.clear()
        report = _run(capsys, desk)
        calls = _transfer_calls(backend)
        assert 0 < len(calls) < 80
        assert any(int(row["from"]) == 3 and int(row["to"]) == 3 for row in calls)
        history = _coverage(report, WALLET_A, ETH, "history.fungible")
        assert "single_block_unreadable:3" in history["reason_codes"]
        assert history["quality"] != "complete_in_declared_scope"
        assert int(_cursor(desk, WALLET_A, ETH, "history.fungible") or -1) == 2


def test_mcm_hard_p0_02a_retry_after_waits_inside_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-02a. Retry-After 3 is waited, and the wait stays inside the round deadline."""
    def _bound(document: dict) -> None:
        _deadline(document, 30)

    desk = _ethereum(tmp_path, monkeypatch, _bound)
    backend = _use(desk)
    backend.head = 20
    backend.rate_sources = {RPC_ETH}
    backend.rate_logs_only = True
    backend.retry_after = "3"
    clock = {"now": 1_700_000_000.0}
    sleeper = Sleeper(clock)
    backend.sleeper = sleeper
    report = _run(capsys, desk, clock=lambda: clock["now"])
    assert sleeper.calls
    assert min(sleeper.calls) >= 3
    assert sum(sleeper.calls) <= 30
    assert _coverage(report, WALLET_A, ETH, "history.fungible")["quality"] == "complete_in_declared_scope"


def test_mcm_hard_p0_02b_wait_past_deadline_is_not_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-02b. A Retry-After that would pass the deadline is not slept. The capability ends on deadline."""
    def _bound(document: dict) -> None:
        _deadline(document, 2)

    desk = _ethereum(tmp_path, monkeypatch, _bound)
    backend = _use(desk)
    backend.head = 20
    backend.rate_sources = {RPC_ETH}
    backend.rate_logs_only = True
    backend.retry_after = "10"
    sleeper = Sleeper()
    backend.sleeper = sleeper
    report = _run(capsys, desk)
    assert sleeper.calls == []
    history = _coverage(report, WALLET_A, ETH, "history.fungible")
    assert "deadline" in history["reason_codes"]
    assert history["quality"] != "complete_in_declared_scope"
    refused = [item for item in report["budget"]["backoff"] if item.get("actual_wait_seconds") == 0]
    assert refused
    assert refused[0]["planned_wait_seconds"] >= 10


def test_mcm_hard_p0_02c_budget_records_planned_and_actual_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-02c. A wait that is taken records planned and actual seconds separately from each other."""
    def _bound(document: dict) -> None:
        _deadline(document, 30)

    desk = _ethereum(tmp_path, monkeypatch, _bound)
    backend = _use(desk)
    backend.head = 20
    backend.rate_sources = {RPC_ETH}
    backend.rate_logs_only = True
    backend.retry_after = "3"
    clock = {"now": 1_700_000_000.0}
    backend.sleeper = Sleeper(clock)
    report = _run(capsys, desk, clock=lambda: clock["now"])
    taken = [
        item for item in report["budget"]["backoff"]
        if item.get("source_id") == RPC_ETH and item.get("actual_wait_seconds", 0) >= 3
    ]
    assert taken
    assert taken[0]["planned_wait_seconds"] >= 3
    assert taken[0]["planned_wait_seconds"] == taken[0]["actual_wait_seconds"]
    assert taken[0]["seconds"] == taken[0]["actual_wait_seconds"]


@pytest.mark.parametrize(
    ("network", "source_id", "secrets", "host"),
    [
        ("ethereum-mainnet", "etherscan", {}, "etherscan.example.invalid"),
        ("bsc-mainnet", "nodereal-bsc", {}, "nodereal.example.invalid"),
        ("optimism-mainnet", "blockscout-optimism", {}, "optimism.example.invalid"),
        (
            "base-mainnet",
            "alchemy-base",
            {"ALCHEMY_API_KEY": ALCHEMY_KEY, "ALCHEMY_TRANSFERS_URL": ALCHEMY_URL},
            "alchemy.example.invalid",
        ),
    ],
)
def test_mcm_hard_p0_02d_indexer_uses_real_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    network: str,
    source_id: str,
    secrets: dict,
    host: str,
) -> None:
    """P0-02d. Etherscan, NodeReal, Blockscout, and Alchemy wait through the same sleeper."""
    def _bound(document: dict) -> None:
        _deadline(document, 30)

    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (network,))],
        mutate=_bound,
        secrets_extra=secrets or None,
    )
    backend = _use(desk)
    backend.head = 20
    backend.rate_sources = {source_id}
    backend.retry_after = "3"
    clock = {"now": 1_700_000_000.0}
    sleeper = Sleeper(clock)
    backend.sleeper = sleeper
    forbidden = (ALCHEMY_KEY,) if source_id == "alchemy-base" else ()
    _run(capsys, desk, clock=lambda: clock["now"], forbidden=forbidden)
    assert any(host in str(row.get("url") or "") for row in backend.http_log)
    assert sleeper.calls
    assert min(sleeper.calls) >= 3
    assert sum(sleeper.calls) <= 30


def test_mcm_hard_p0_03a_nokey_arbitrum_bootstrap_stays_under_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-03a. A fresh no-key Arbitrum lookback returns without exceeding the per-source cap."""
    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ARB,))],
        omit_secrets=("ETHERSCAN_API_KEY", "NODEREAL_API_KEY", "BLOCKSCOUT_BASE_API_KEY", "BLOCKSCOUT_OPTIMISM_API_KEY"),
    )
    backend = _use(desk)
    backend.head = 20_000_000
    started = time.perf_counter()
    report = _run(capsys, desk)
    assert time.perf_counter() - started < 60
    logs = [row for row in backend.calls if row.get("kind") == "log"]
    assert logs
    assert len(logs) <= 2000
    used = report["budget"]["bootstrap_requests"]
    assert 1 <= int(used[RPC_ARB]) <= 2000
    for source_id, count in used.items():
        assert int(count) <= 2000, source_id


def test_mcm_hard_p0_03b_quick_scan_before_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-03b. Round 1 already has the balance and the current approval while history is still pending."""
    def _tight(document: dict) -> None:
        _cap(document, 6)

    desk = _ethereum(tmp_path, monkeypatch, _tight)
    backend = _use(desk)
    backend.head = 5000
    backend.balance_hex = "0x10"
    approval = _tx(31)
    backend.logs = [_approval_log(approval, 5000, ADDRESS_A)]
    report = _run(capsys, desk)
    assert report["native_reads"] == [{"network": ETH, "raw_amount": "16", "wallet_id": WALLET_A}]
    events = _events(desk, WALLET_A, ETH)
    assert any(row["tx_id"] == approval and row["kind"] == "approval_set" for row in events)
    fungible = _coverage(report, WALLET_A, ETH, "history.fungible")
    permissions = _coverage(report, WALLET_A, ETH, "permissions.erc20")
    assert "historical_backfill_pending" in fungible["reason_codes"]
    assert "historical_backfill_pending" in permissions["reason_codes"]
    assert fungible["quality"] != "complete_in_declared_scope"
    assert permissions["quality"] != "complete_in_declared_scope"


def test_mcm_hard_p0_03c_backfill_pending_is_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-03c. A capped log bootstrap is partial and says the historical backfill is still pending."""
    def _tight(document: dict) -> None:
        _cap(document, 6)

    desk = _ethereum(tmp_path, monkeypatch, _tight)
    backend = _use(desk)
    backend.head = 5000
    report = _run(capsys, desk)
    for capability in ("history.fungible", "permissions.erc20"):
        row = _coverage(report, WALLET_A, ETH, capability)
        assert "historical_backfill_pending" in row["reason_codes"]
        assert "bootstrap_request_cap" in row["reason_codes"]
        assert row["quality"] != "complete_in_declared_scope"
        assert "incremental_window_complete" not in row["reason_codes"]


def test_mcm_hard_p0_03d_backfill_converges_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-03d. Repeated capped rounds finish the backfill once, without a second copy of the events."""
    def _tight(document: dict) -> None:
        _cap(document, 8)
        document["sources"][RPC_ETH]["log_range_max"] = 50

    desk = _ethereum(tmp_path, monkeypatch, _tight)
    backend = _use(desk)
    backend.head = 400
    early = _tx(41)
    late = _tx(42)
    backend.logs = [
        _transfer_log(early, 10, 1, ADDRESS_A),
        _transfer_log(late, 399, 1, ADDRESS_A),
    ]
    finished = None
    rounds = 0
    for rounds in range(1, 21):
        report = _run(capsys, desk)
        fungible = _coverage(report, WALLET_A, ETH, "history.fungible")
        permissions = _coverage(report, WALLET_A, ETH, "permissions.erc20")
        if (
            fungible["quality"] == "complete_in_declared_scope"
            and permissions["quality"] == "complete_in_declared_scope"
        ):
            finished = report
            break
    assert finished is not None
    assert rounds > 1
    assert _tx_counts(desk, ETH)[early] == 1
    assert _tx_counts(desk, ETH)[late] == 1
    alerts = len(_outbox(desk))
    again = _run(capsys, desk)
    assert _tx_counts(desk, ETH)[early] == 1
    assert _tx_counts(desk, ETH)[late] == 1
    assert len(_outbox(desk)) == alerts
    assert _coverage(again, WALLET_A, ETH, "history.fungible")["quality"] == "complete_in_declared_scope"
    assert "historical_backfill_pending" not in _coverage(again, WALLET_A, ETH, "history.fungible")["reason_codes"]


def test_mcm_hard_p0_03e_log_reads_count_toward_bootstrap_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-03e. eth_getLogs counts toward bootstrap_request_cap.

    Fails on the parent commit: ``_LimitedTransport`` counted indexer HTTP only
    and left log reads outside ``bootstrap_request_cap``. This 5000-block window
    with 50-block chunks then issues more than 4 ``eth_getLogs`` calls, and
    ``budget.bootstrap_requests`` has no ``rpc-ethereum-mainnet`` entry. Both
    assertions below fail on that parent.
    """
    def _tight(document: dict) -> None:
        _cap(document, 4)
        document["sources"][RPC_ETH]["log_range_max"] = 50

    desk = _ethereum(tmp_path, monkeypatch, _tight)
    backend = _use(desk)
    backend.head = 5000
    report = _run(capsys, desk)
    logs = [row for row in backend.calls if row.get("kind") == "log"]
    assert 1 <= len(logs) <= 4
    used = report["budget"]["bootstrap_requests"]
    assert 1 <= int(used[RPC_ETH]) <= 4
    history = _coverage(report, WALLET_A, ETH, "history.fungible")
    assert "historical_backfill_pending" in history["reason_codes"]
    assert history["quality"] != "complete_in_declared_scope"


def test_mcm_hard_p0_02e_budget_limiter_really_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-02e. The max_rps token bucket waits through the budget sleeper, not just records.

    Before this fix the limiter was built with a no-op sleeper, so a 1 rps group was hit in a burst,
    the provider answered 429, and an identity read became ``identity_unreadable``.
    """
    desk = _ethereum(tmp_path, monkeypatch, lambda document: _deadline(document, 600))
    backend = _use(desk)
    backend.head = 20
    clock = {"now": 1_700_000_000.0}
    waits: list[float] = []

    def budget_sleeper(seconds: float) -> None:
        waits.append(seconds)
        clock["now"] += seconds

    backend.budget_sleeper = budget_sleeper
    report = _run(capsys, desk, clock=lambda: clock["now"])
    assert waits, "a burst on a 1 rps group must wait"
    assert all(0 < wait <= 10 for wait in waits)
    assert _coverage(report, WALLET_A, ETH, "history.fungible")["quality"] == "complete_in_declared_scope"
