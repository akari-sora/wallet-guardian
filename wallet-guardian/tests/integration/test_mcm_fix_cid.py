"""Chain-id retry and a shared NodeReal key budget. Offline CLI only.

Sockets stay blocked. Addresses and keys are synthetic. Each test calls
``guardian run-once --mcm-config --no-send`` with an injected fixture backend.
"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from guardian.cli import main
from guardian.sources.fixtures import RawResponse

_SPEC = importlib.util.spec_from_file_location(
    "mcm_fix_cid_support",
    Path(__file__).with_name("test_mcm_free_tier_catchup.py"),
)
_FREE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_FREE)

ADDRESS_A = _FREE.ADDRESS_A
ADDRESS_B = _FREE.ADDRESS_B
BLOCKSCOUT_KEY = _FREE.BLOCKSCOUT_KEY
KEY = _FREE.KEY
NODEREAL_KEY = _FREE.NODEREAL_KEY
WALLET_A = _FREE.WALLET_A
WALLET_B = _FREE.WALLET_B
_account = _FREE._account
_coverage = _FREE._coverage
_desk = _FREE._desk

ROOT = Path(__file__).resolve().parents[2]
ETH = "ethereum-mainnet"
OP = "optimism-mainnet"
ETH_HOST = "ethereum.example.invalid"
OP_HOST = "optimism.example.invalid"
RPC_ETH = "rpc-ethereum-mainnet"
RPC_OP = "rpc-optimism-mainnet"
EXAMPLES = (
    ROOT / "examples" / "monitor.json",
    ROOT / "examples" / "monitor.nokeys.json",
    ROOT / "src" / "guardian" / "_resources" / "examples" / "monitor.json",
    ROOT / "src" / "guardian" / "_resources" / "examples" / "monitor.nokeys.json",
)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class ChainBackend(_FREE.FixtureBackend):
    """Scripted eth_chainId answers. Anything else uses the shared fixture."""

    def __init__(self) -> None:
        super().__init__()
        self.chain_script: dict[str, list[object]] = {}
        self.repeat_chain: set[str] = set()
        self.rpc_log: list[dict[str, object]] = []
        self.wait_log: list[float] = []

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        host = urlsplit(url).hostname or ""
        source = str(getattr(call, "source_id", "") or "")
        body = getattr(call, "body", b"") or b""
        method = _method(body)
        self.rpc_log.append({
            "host": host,
            "method": method,
            "source": source,
            "waits": len(self.wait_log),
        })
        if method == "eth_chainId":
            scripted = self._scripted(host)
            if scripted is not None:
                self.http_log.append({"authorization": "", "url": url})
                return scripted
        return super().send(call)

    def _scripted(self, host: str) -> RawResponse | None:
        queue = self.chain_script.get(host)
        if not queue:
            return None
        item = queue[0]
        if not (len(queue) == 1 and host in self.repeat_chain):
            queue.pop(0)
        if item == "timeout":
            raise TimeoutError("timeout")
        if item == "http_5xx":
            return RawResponse(500, {}, b"{}")
        if isinstance(item, tuple) and item[0] == "429":
            return RawResponse(429, {"retry-after": str(item[1])}, b"{}")
        payload = {"id": 1, "jsonrpc": "2.0", "result": item}
        return RawResponse(200, {}, json.dumps(payload).encode())


class Sleeper:
    def __init__(self, clock: dict[str, float]) -> None:
        self.calls: list[float] = []
        self.clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        self.clock["now"] += float(seconds)


def _method(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("method") or "")
    return ""


def _quiet(document: dict, *, attempts: int, deadline: int) -> None:
    document["polling"]["bootstrap_origin"] = 20
    document["polling"]["round_deadline_seconds"] = deadline
    for spec in document["budgets"].values():
        spec["max_rps"] = 100
        spec["retry_attempts"] = attempts


def _use(desk) -> ChainBackend:
    backend = ChainBackend()
    backend.head = 20
    backend.balance_hex = "0x2a"
    desk.backend = backend
    return backend


def _run(capsys: pytest.CaptureFixture[str], desk, *, clock) -> tuple[int, dict]:
    code = main(
        ["run-once", "--mcm-config", str(desk.config), "--no-send"],
        backend=desk.backend,
        clock=clock,
    )
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    for secret in (KEY, NODEREAL_KEY, BLOCKSCOUT_KEY):
        assert secret not in blob
    assert ADDRESS_A.lower() not in blob.lower()
    assert ADDRESS_B.lower() not in blob.lower()
    return code, payload


def _chain_rows(backend: ChainBackend, host: str) -> list[dict[str, object]]:
    return [row for row in backend.rpc_log if row["method"] == "eth_chainId" and row["host"] == host]


def _has_method(backend: ChainBackend, host: str, method: str) -> bool:
    return any(row["method"] == method and row["host"] == host for row in backend.rpc_log)


def test_mcm_fix_cid_01_chain_id_429_then_ok_reads_balance_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """CID-01. One Retry-After 429 on eth_chainId is retried. The chain stays readable.

    Fails on the parent commit: open() sends eth_chainId once, so the 429 becomes
    identity_unreadable and balance plus history are never read.
    """
    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ETH,))],
        mutate=lambda document: _quiet(document, attempts=2, deadline=30),
    )
    backend = _use(desk)
    backend.chain_script[ETH_HOST] = [("429", "2"), "0x1"]
    clock = {"now": 1_700_000_000.0}
    sleeper = Sleeper(clock)
    backend.sleeper = sleeper
    code, report = _run(capsys, desk, clock=lambda: clock["now"])
    assert code == 0, report
    assert sleeper.calls == [2.0]
    assert len(_chain_rows(backend, ETH_HOST)) == 2
    native = _coverage(report, WALLET_A, ETH, "balances.native")
    history = _coverage(report, WALLET_A, ETH, "history.fungible")
    assert "identity_unreadable" not in native["reason_codes"]
    assert "identity_unreadable" not in history["reason_codes"]
    assert native["quality"] == "complete_in_declared_scope"
    assert _has_method(backend, ETH_HOST, "eth_getBalance")
    assert _has_method(backend, ETH_HOST, "eth_getLogs")
    assert any(
        row["network"] == ETH and int(row["raw_amount"]) == 42
        for row in report["native_reads"]
    )


def test_mcm_fix_cid_02_persistent_429_names_rate_limited_and_stays_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """CID-02. A 429 that outlasts the attempts is identity_unreadable plus rate_limited.

    The other chain still reads. Waits honor Retry-After and stay inside the deadline.
    Fails on the parent commit: the reason is only identity_unreadable and the call
    is not retried.
    """
    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ETH, OP))],
        mutate=lambda document: _quiet(document, attempts=1, deadline=10),
    )
    backend = _use(desk)
    backend.chain_script[ETH_HOST] = [("429", "1")]
    backend.repeat_chain.add(ETH_HOST)
    clock = {"now": 1_700_000_000.0}
    sleeper = Sleeper(clock)
    backend.sleeper = sleeper
    code, report = _run(capsys, desk, clock=lambda: clock["now"])
    assert code == (3 if report.get("status") == "degraded" else 0), report
    eth_calls = _chain_rows(backend, ETH_HOST)
    assert len(eth_calls) == 2
    assert sleeper.calls == [1.0]
    assert sum(sleeper.calls) <= 10
    native = _coverage(report, WALLET_A, ETH, "balances.native")
    history = _coverage(report, WALLET_A, ETH, "history.fungible")
    assert native["quality"] == "error"
    assert "identity_unreadable" in native["reason_codes"]
    assert "rate_limited" in native["reason_codes"]
    assert "identity_unreadable" in history["reason_codes"]
    assert "rate_limited" in history["reason_codes"]
    assert history["quality"] != "complete_in_declared_scope"
    assert not any(row["network"] == ETH for row in report["native_reads"])
    other = _coverage(report, WALLET_A, OP, "balances.native")
    assert "identity_unreadable" not in other["reason_codes"]
    assert other["quality"] == "complete_in_declared_scope"
    assert any(row["network"] == OP and int(row["raw_amount"]) == 42 for row in report["native_reads"])

    late = _desk(
        tmp_path / "deadline",
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ETH, OP))],
        mutate=lambda document: _quiet(document, attempts=3, deadline=2),
    )
    late_backend = _use(late)
    late_backend.chain_script[ETH_HOST] = [("429", "30")]
    late_backend.repeat_chain.add(ETH_HOST)
    late_clock = {"now": 1_700_000_000.0}
    late_sleeper = Sleeper(late_clock)
    late_backend.sleeper = late_sleeper
    late_code, late_report = _run(capsys, late, clock=lambda: late_clock["now"])
    assert late_code == (3 if late_report.get("status") == "degraded" else 0), late_report
    assert late_sleeper.calls == []
    assert len(_chain_rows(late_backend, ETH_HOST)) == 1
    late_native = _coverage(late_report, WALLET_A, ETH, "balances.native")
    assert "identity_unreadable" in late_native["reason_codes"]
    assert "rate_limited" in late_native["reason_codes"]
    assert "deadline" in late_native["reason_codes"]
    assert _coverage(late_report, WALLET_A, OP, "balances.native")["quality"] == "complete_in_declared_scope"


def test_mcm_fix_cid_03_wrong_chain_id_is_blocked_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """CID-03. A wrong chain id blocks immediately. A later correct answer is not fetched."""
    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ETH,))],
        mutate=lambda document: _quiet(document, attempts=3, deadline=30),
    )
    backend = _use(desk)
    backend.chain_script[ETH_HOST] = ["0x539", "0x1"]
    clock = {"now": 1_700_000_000.0}
    sleeper = Sleeper(clock)
    backend.sleeper = sleeper
    code, report = _run(capsys, desk, clock=lambda: clock["now"])
    assert code == 1, report
    assert report["status"] == "failed"
    assert len(_chain_rows(backend, ETH_HOST)) == 1
    assert sleeper.calls == []
    native = _coverage(report, WALLET_A, ETH, "balances.native")
    assert native["quality"] == "error"
    assert "chain_id_mismatch" in native["reason_codes"]
    assert "blocked" in native["reason_codes"]
    assert "identity_unreadable" not in native["reason_codes"]
    assert not _has_method(backend, ETH_HOST, "eth_getBalance")
    assert not any(row["network"] == ETH for row in report["native_reads"])


def test_mcm_fix_cid_04_successful_identity_is_cached_for_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """CID-04. Two wallets and several capabilities share one successful eth_chainId.

    Fails on the parent commit: each wallet calls open(), so the host is asked twice.
    """
    desk = _desk(
        tmp_path,
        monkeypatch,
        [
            _account(WALLET_A, ADDRESS_A, (ETH,)),
            _account(WALLET_B, ADDRESS_B, (ETH,)),
        ],
        mutate=lambda document: _quiet(document, attempts=2, deadline=30),
    )
    backend = _use(desk)
    clock = {"now": 1_700_000_000.0}
    code, report = _run(capsys, desk, clock=lambda: clock["now"])
    assert code == 0, report
    assert len(_chain_rows(backend, ETH_HOST)) == 1
    for wallet in (WALLET_A, WALLET_B):
        native = _coverage(report, wallet, ETH, "balances.native")
        history = _coverage(report, wallet, ETH, "history.fungible")
        permissions = _coverage(report, wallet, ETH, "permissions.erc20")
        assert native["quality"] == "complete_in_declared_scope"
        assert "identity_unreadable" not in native["reason_codes"]
        assert "identity_unreadable" not in history["reason_codes"]
        assert "identity_unreadable" not in permissions["reason_codes"]
        assert _has_method(backend, ETH_HOST, "eth_getLogs")
    assert sum(1 for row in report["native_reads"] if row["network"] == ETH) == 2


def test_mcm_fix_cid_05_shared_budget_group_paces_both_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """CID-05. Two sources on one budget group share the token bucket.

    Fails on the parent commit: the shipped example still gives nodereal-bsc its
    own group, so the key is not shared with a documented nodereal-key budget.
    """
    def _share(document: dict) -> None:
        _quiet(document, attempts=0, deadline=60)
        document["budgets"]["nodereal-key"] = {
            "max_concurrency": 1,
            "max_requests_per_day": 10000,
            "max_rps": 1,
            "retry_attempts": 0,
        }
        document["sources"][RPC_ETH]["budget_group"] = "nodereal-key"
        document["sources"][RPC_OP]["budget_group"] = "nodereal-key"

    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ETH, OP))],
        mutate=_share,
    )
    backend = _use(desk)
    clock = {"now": 1_700_000_000.0}
    waits: list[float] = []

    def budget_sleeper(seconds: float) -> None:
        waits.append(float(seconds))
        backend.wait_log.append(float(seconds))
        clock["now"] += float(seconds)

    backend.budget_sleeper = budget_sleeper
    code, report = _run(capsys, desk, clock=lambda: clock["now"])
    assert code == 0, report
    eth_rows = [row for row in backend.rpc_log if row["source"] == RPC_ETH]
    op_rows = [row for row in backend.rpc_log if row["source"] == RPC_OP]
    assert eth_rows and op_rows
    op_index = next(index for index, row in enumerate(backend.rpc_log) if row is op_rows[0])
    last_eth_before = max(
        (index for index, row in enumerate(backend.rpc_log) if row["source"] == RPC_ETH and index < op_index),
        default=-1,
    )
    assert last_eth_before >= 0
    assert int(op_rows[0]["waits"]) > int(backend.rpc_log[last_eth_before]["waits"])
    assert waits
    assert all(wait > 0 for wait in waits)
    assert _coverage(report, WALLET_A, ETH, "balances.native")["quality"] == "complete_in_declared_scope"
    assert _coverage(report, WALLET_A, OP, "balances.native")["quality"] == "complete_in_declared_scope"

    for path in EXAMPLES:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["sources"]["nodereal-bsc"]["budget_group"] == "nodereal-key"
        group = document["budgets"]["nodereal-key"]
        assert group["max_rps"] == 1
        assert group["max_concurrency"] == 1
        assert "nodereal-bsc" not in document["budgets"]
        assert document["sources"][RPC_ETH]["budget_group"] == RPC_ETH
        assert document["sources"][RPC_OP]["budget_group"] == RPC_OP
    keys = (ROOT / "docs" / "api-keys.md").read_text(encoding="utf-8")
    live = (ROOT / "docs" / "mcm-live-01r.md").read_text(encoding="utf-8")
    assert "nodereal-key" in keys
    assert "429" in keys
    assert "nodereal-key" in live


def test_mcm_fix_cid_06_timeout_and_http_5xx_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """CID-06. A timeout and an HTTP 5xx on eth_chainId are retried into a real read.

    Fails on the parent commit: both answers become identity_unreadable on the first try.
    """
    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, (ETH, OP))],
        mutate=lambda document: _quiet(document, attempts=2, deadline=30),
    )
    backend = _use(desk)
    backend.chain_script[ETH_HOST] = ["timeout", "0x1"]
    backend.chain_script[OP_HOST] = ["http_5xx", "0xa"]
    clock = {"now": 1_700_000_000.0}
    sleeper = Sleeper(clock)
    backend.sleeper = sleeper
    code, report = _run(capsys, desk, clock=lambda: clock["now"])
    assert code == 0, report
    assert len(_chain_rows(backend, ETH_HOST)) == 2
    assert len(_chain_rows(backend, OP_HOST)) == 2
    for network, host in ((ETH, ETH_HOST), (OP, OP_HOST)):
        native = _coverage(report, WALLET_A, network, "balances.native")
        history = _coverage(report, WALLET_A, network, "history.fungible")
        assert "identity_unreadable" not in native["reason_codes"]
        assert "identity_unreadable" not in history["reason_codes"]
        assert native["quality"] == "complete_in_declared_scope"
        assert _has_method(backend, host, "eth_getBalance")
        assert _has_method(backend, host, "eth_getLogs")
    assert {row["network"] for row in report["native_reads"]} >= {ETH, OP}
