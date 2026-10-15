"""WP-4 balance probe. Offline fixtures only. A failed read is not a zero balance."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import fcntl
import httpx
import pytest
from eth_abi import encode
from eth_utils import to_checksum_address

from guardian.chains.evm.accounting import TRANSFER_TOPIC, pad_address
from guardian.chains.evm.permissions import ERC20_APPROVAL_TOPIC
from guardian.cli import main
from guardian.configuration import load_public_config, resolve_private_inputs
from guardian.scan.probe import probe_guard, run_balance_probe
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
ADDRESS = to_checksum_address("0x" + ("0" * 39) + "1")
ONE = 10**18
TWO = 2 * 10**18
CLOCK = 1_700_000_000.0
BSC = "bsc-mainnet"
BASE = "base-mainnet"
ASSETS = {BSC: "evm-native:56", BASE: "evm-native:8453"}
HOSTS = {
    "bsc.example.invalid": BSC,
    "base.example.invalid": BASE,
    "nodereal.example.invalid": BSC,
    "bsc-logs.example.invalid": BSC,
}


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class Backend:
    """Scripted EVM endpoints. One HTTP send is one request."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.head = {BSC: (10, _hash(10)), BASE: (20, _hash(20))}
        self.balance = {BSC: ONE, BASE: ONE}
        self.code = {BSC: "0x01", BASE: "0x01"}
        self.transfer_logs: dict[str, list[dict]] = {BSC: [], BASE: []}
        self.approval_logs: dict[str, list[dict]] = {BSC: [], BASE: []}
        self.fail_head: set[str] = set()
        self.fail_multicall: set[str] = set()
        self.fail_logs: set[str] = set()
        self.fail_item: set[str] = set()
        self.refuse_logs_hosts: set[str] = set()

    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        body = json.loads((getattr(call, "body", b"") or b"{}").decode("utf-8"))
        method = str(body.get("method") or "")
        params = body.get("params") if isinstance(body.get("params"), list) else []
        self.calls.append({"url": url, "method": method, "params": params})
        network = _network(url)
        if method == "eth_getBlockByNumber":
            if network in self.fail_head:
                return _err()
            number, digest = self.head[network]
            return _ok({"hash": digest, "number": hex(number), "timestamp": "0x65000000"})
        if method == "eth_getCode":
            return _ok(self.code.get(network, "0x"))
        if method == "eth_call":
            if network in self.fail_multicall:
                return _err()
            if network in self.fail_item:
                word = (0).to_bytes(32, "big")
                return _ok("0x" + encode(["(bool,bytes)[]"], [[(False, word)]]).hex())
            return _ok("0x" + encode(["(bool,bytes)[]"], [[(True, self.balance[network].to_bytes(32, "big"))]]).hex())
        if method == "eth_getBalance":
            return _ok(hex(self.balance[network]))
        if method == "eth_getLogs":
            if any(host in url for host in self.refuse_logs_hosts):
                return _refuse()
            if network in self.fail_logs:
                return _err()
            topics = params[0].get("topics") if params and isinstance(params[0], dict) else None
            topic0 = topics[0] if isinstance(topics, list) and topics else None
            rows: list[dict] = []
            if topic0 == TRANSFER_TOPIC or (isinstance(topic0, list) and TRANSFER_TOPIC in topic0):
                rows.extend(self.transfer_logs.get(network, []))
            if isinstance(topic0, list):
                rows.extend(self.approval_logs.get(network, []))
            return _ok(rows)
        return _err()


def test_quiet_round_stays_within_budget_and_does_not_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    backend = Backend()
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    assert payload["status"] == "ok"
    assert payload["sent"] is False
    assert payload["delivery"] == "no_send"
    assert payload["probe_alerts_written"] == 0
    assert payload["run_once"] == "not_called"
    for chain in payload["chains"]:
        assert chain["triggered"] is False
        assert chain["failure"] is None
        assert chain["http"] <= 5
        assert chain["methods"] <= 4 + chain["wallets"]
        assert chain["cursor_advanced"] is True
        assert chain["balance_source"] == "aggregate3"
    http, methods = _counts(backend)
    assert http[BSC] <= 5 and http[BASE] <= 5
    assert methods[BSC] <= 5 and methods[BASE] <= 5
    assert [call["method"] for call in backend.calls if _network(call["url"]) == BSC] == [
        "eth_getBlockByNumber",
        "eth_getCode",
        "eth_call",
        "eth_getLogs",
        "eth_getLogs",
    ]
    logged = [call for call in backend.calls if call["method"] == "eth_getLogs"]
    assert logged
    assert all("address" not in call["params"][0] for call in logged)
    assert all("bsc.example.invalid" not in call["url"] for call in logged if "bsc" in call["url"] or "nodereal" in call["url"])
    assert any("nodereal.example.invalid" in call["url"] for call in logged)
    called = next(call for call in backend.calls if call["method"] == "eth_call")
    assert called["params"][0]["to"] == "0xcA11bde05977b3631167028862bE2a173976CA11"
    _assert_history_unchanged(tmp_path)
    _assert_no_zero_and_no_alert(tmp_path)
    bsc = _baseline(tmp_path, BSC)
    assert bsc["raw_balance"] == str(ONE)
    assert int(bsc["block_number"]) == 10
    probe = Database(tmp_path / "probe.sqlite")
    try:
        assert probe.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert probe.probe_cursor(TENANT, BSC)["block_hash"] == _hash(10)
        assert probe.probe_trigger_rows(TENANT) == []
    finally:
        probe.close()
    assert not list(tmp_path.rglob(".shadow-round.lock"))
    assert not list((tmp_path / "state").glob("*.run-once.lock"))


def test_balance_transfer_and_approval_trigger_only_the_matching_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    balance = Backend()
    balance.balance[BSC] = TWO
    payload = _run(config, tmp_path, balance, capsys, trigger="none")
    assert [chain["network"] for chain in payload["chains"] if chain["triggered"]] == [BSC]
    assert _reasons(tmp_path) == [(BSC, "balance_change")]
    assert _baseline(tmp_path, BSC)["raw_balance"] == str(ONE)

    transfer = Backend()
    transfer.transfer_logs[BASE] = [_log(TRANSFER_TOPIC, 1)]
    _run(config, tmp_path, transfer, capsys, trigger="none")
    assert (BASE, "transfer_log") in _reasons(tmp_path)
    assert (BSC, "transfer_log") not in _reasons(tmp_path)

    approval = Backend()
    approval.approval_logs[BSC] = [_log(ERC20_APPROVAL_TOPIC, 2)]
    _run(config, tmp_path, approval, capsys, trigger="none")
    assert (BSC, "approval_log") in _reasons(tmp_path)
    assert (BASE, "approval_log") not in _reasons(tmp_path)
    _assert_history_unchanged(tmp_path)
    _assert_no_zero_and_no_alert(tmp_path)


def test_head_multicall_or_logs_failure_does_not_trigger_advance_or_write_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    before = _baseline(tmp_path, BSC)
    for failure in ("head", "multicall", "logs"):
        backend = Backend()
        getattr(backend, f"fail_{failure}").add(BSC)
        payload = _run(config, tmp_path, backend, capsys, trigger="none")
        bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
        assert bsc["triggered"] is False
        assert bsc["cursor_advanced"] is False
        assert bsc["failure"] == failure
        assert _baseline(tmp_path, BSC) == before
    probe = Database(tmp_path / "probe.sqlite")
    try:
        assert probe.probe_cursor(TENANT, BSC) is None
        assert [row for row in probe.probe_trigger_rows(TENANT) if row["status"] == "triggered"] == []
    finally:
        probe.close()
    _assert_no_zero_and_no_alert(tmp_path)
    _assert_history_unchanged(tmp_path)


def test_allow_failure_false_item_is_a_failed_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    before = _baseline(tmp_path, BSC)
    backend = Backend()
    backend.fail_item.add(BSC)
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
    assert bsc["failure"] == "balance"
    assert bsc["triggered"] is False
    assert bsc["cursor_advanced"] is False
    assert _baseline(tmp_path, BSC) == before
    _assert_no_zero_and_no_alert(tmp_path)


def test_reorg_recomputes_without_a_duplicate_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    public, private, database = _open(config)
    calls: list[int] = []

    def run_once() -> None:
        calls.append(1)

    backend = Backend()
    backend.balance[BSC] = TWO
    backend.head[BSC] = (10, _hash(10))
    try:
        first = run_balance_probe(
            public, private, database,
            probe_state=tmp_path / "probe.sqlite",
            backend=backend,
            clock=lambda: CLOCK,
            trigger="run-once",
            run_once=run_once,
        )
        assert calls == [1]
        assert first["run_once"] == "called"
        seen = _reasons(tmp_path)
        backend.head[BSC] = (10, _hash(11))
        before = len(backend.calls)
        second = run_balance_probe(
            public, private, database,
            probe_state=tmp_path / "probe.sqlite",
            backend=backend,
            clock=lambda: CLOCK,
            trigger="run-once",
            run_once=run_once,
        )
    finally:
        database.close()
    assert len(backend.calls) > before
    assert calls == [1]
    assert second["run_once"] == "not_called"
    assert _reasons(tmp_path) == seen
    assert _baseline(tmp_path, BSC)["raw_balance"] == str(ONE)
    _assert_history_unchanged(tmp_path)


def test_second_probe_and_busy_run_once_record_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    probe_state = tmp_path / "probe.sqlite"
    release = threading.Event()
    holding = threading.Event()

    def _hold() -> None:
        with probe_guard(probe_state):
            holding.set()
            assert release.wait(5)

    thread = threading.Thread(target=_hold)
    thread.start()
    assert holding.wait(5)
    try:
        skipped = _run(config, tmp_path, Backend(), capsys, trigger="none")
    finally:
        release.set()
        thread.join(5)
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "balance_probe_locked"
    rows = _trigger_rows(tmp_path)
    assert any(row["reason"] == "balance_probe_locked" and row["status"] == "skipped" for row in rows)

    backend = Backend()
    backend.balance[BSC] = TWO
    lock_path = tmp_path / "state" / "monitor.sqlite.run-once.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        locked = _run(config, tmp_path, backend, capsys, trigger="run-once")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert locked["run_once"] == "locked"
    assert any(row["reason"] == "run_once_locked" and row["status"] == "skipped" for row in _trigger_rows(tmp_path))
    assert "eth_chainId" not in {call["method"] for call in backend.calls}
    _assert_history_unchanged(tmp_path)
    assert not list(tmp_path.rglob(".shadow-round.lock"))


def test_balance_probe_refuses_a_delivery_mode_other_than_no_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    code = main(["balance-probe", "--mcm-config", str(config), "--probe-state", str(tmp_path / "probe.sqlite")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "error"
    assert "no-send" in payload["error"]


def test_empty_code_falls_back_to_get_balance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    backend = Backend()
    backend.code[BSC] = "0x"
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
    assert bsc["balance_source"] == "eth_getBalance"
    assert bsc["triggered"] is False
    methods = [call["method"] for call in backend.calls if _network(call["url"]) == BSC]
    assert "eth_call" not in methods
    assert "eth_getBalance" in methods
    assert _baseline(tmp_path, BSC)["raw_balance"] == str(ONE)


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = []
    document["enabled_networks"] = [BSC, BASE]
    document["routing"] = [
        route for route in document["routing"] if route.get("network") in {BSC, BASE}
    ]
    document["alerts"]["native_unit_thresholds"] = {BSC: "1.0"}
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    secrets = {
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "GOPLUS_API_BASE": "https://goplus.example.invalid/api",
        "NODEREAL_BSC_URL": "https://nodereal.example.invalid/bsc",
        "OFAC_SLS_FEED": "https://ofac.example.invalid/feed",
        "RPC_BASE_MAINNET_URL": "https://base.example.invalid/rpc",
        "RPC_BSC_LOGS_URL": "https://bsc-logs.example.invalid/rpc",
        "RPC_BSC_MAINNET_URL": "https://bsc.example.invalid/rpc",
        "TENDERLY_BASE_URL": "https://base.example.invalid/rpc",
    }
    accounts = tmp_path / "accounts.json"
    secret_path = tmp_path / "secrets.json"
    _write_private(accounts, json.dumps({
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
        "accounts": [{
            "address": ADDRESS,
            "family": "evm",
            "label": "Wallet A",
            "networks": [BSC, BASE],
            "own_group": "desk",
            "relationship": "owned_user_declared",
            "wallet_id": WALLET,
        }],
    }) + "\n")
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(tmp_path / "state"))
    return config


def _seed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    database = Database(state / "monitor.sqlite")
    try:
        for network, asset in ASSETS.items():
            database.commit_native_observation(
                TENANT,
                wallet_id=WALLET,
                network=network,
                asset=asset,
                expected_version=0,
                checkpoint={
                    "binding": "eip1898_hash",
                    "block_hash": _hash(5),
                    "block_number": 5,
                    "block_timestamp": "1",
                    "confirmation_policy": "fixture",
                    "nonce": None,
                    "observed_at": "2026-01-01T00:00:00Z",
                    "raw_balance": str(ONE),
                    "source_id": f"rpc-{network}",
                },
                continuity="baseline",
                retention_blocks=64,
                reset_checkpoints=False,
                alert=None,
                record_failure=None,
            )
        for network in ASSETS:
            for capability in ("history.native_external", "history.native_internal"):
                database.conn.execute(
                    """
                    INSERT INTO capability_cursors(
                      tenant_id, wallet_id, network, capability, cursor, block_number
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (TENANT, WALLET, network, capability, "4", "4"),
                )
        database.conn.commit()
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        database.close()


def _open(config: Path):
    public = load_public_config(config)
    private = resolve_private_inputs(public, os.environ)
    return public, private, Database(private.state_dir / "monitor.sqlite")


def _run(
    config: Path,
    tmp_path: Path,
    backend: Backend,
    capsys: pytest.CaptureFixture[str],
    *,
    trigger: str,
) -> dict:
    code = main(
        [
            "balance-probe",
            "--mcm-config", str(config),
            "--probe-state", str(tmp_path / "probe.sqlite"),
            "--no-send",
            "--trigger", trigger,
        ],
        backend=backend,
        clock=lambda: CLOCK,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    return payload


def _counts(backend: Backend) -> tuple[dict[str, int], dict[str, int]]:
    http: dict[str, int] = {BSC: 0, BASE: 0}
    methods: dict[str, int] = {BSC: 0, BASE: 0}
    for call in backend.calls:
        network = _network(call["url"])
        http[network] += 1
        methods[network] += 1
    return http, methods


def _baseline(tmp_path: Path, network: str) -> dict:
    database = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        row = database.native_balance_baseline(TENANT, WALLET, network, ASSETS[network])
        assert row is not None
        return dict(row)
    finally:
        database.close()


def _reasons(tmp_path: Path) -> list[tuple[str | None, str]]:
    return [
        (row["network"], row["reason"])
        for row in _trigger_rows(tmp_path)
        if row["status"] == "triggered"
    ]


def _trigger_rows(tmp_path: Path) -> list[dict]:
    database = Database(tmp_path / "probe.sqlite")
    try:
        return database.probe_trigger_rows(TENANT)
    finally:
        database.close()


def _assert_history_unchanged(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        rows = database.conn.execute(
            "SELECT network, capability, cursor, block_number FROM capability_cursors ORDER BY network, capability"
        ).fetchall()
        assert [(row["network"], row["capability"], row["cursor"], row["block_number"]) for row in rows] == [
            (BASE, "history.native_external", "4", "4"),
            (BASE, "history.native_internal", "4", "4"),
            (BSC, "history.native_external", "4", "4"),
            (BSC, "history.native_internal", "4", "4"),
        ]
    finally:
        database.close()


def _assert_no_zero_and_no_alert(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        raws = [row[0] for row in database.conn.execute("SELECT raw_balance FROM native_balance_baselines")]
        assert raws
        assert "0" not in raws
        assert database.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
        assert database.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    finally:
        database.close()


def _log(topic: str, index: int) -> dict:
    return {
        "blockNumber": "0xa",
        "logIndex": hex(index),
        "topics": [topic, pad_address(ADDRESS), pad_address(ADDRESS)],
        "transactionHash": _hash(index),
    }


def _hash(number: int) -> str:
    return "0x" + f"{number:064x}"


def _network(url: str) -> str:
    for host, network in HOSTS.items():
        if host in url:
            return network
    raise AssertionError(url)


def _ok(result: object) -> RawResponse:
    body = json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode()
    return RawResponse(200, {"content-type": "application/json"}, body)


def _err() -> RawResponse:
    body = json.dumps({"error": {"code": -32000, "message": "down"}, "id": 1, "jsonrpc": "2.0"}).encode()
    return RawResponse(200, {"content-type": "application/json"}, body)


def _refuse() -> RawResponse:
    body = json.dumps({
        "error": {"code": -32701, "message": "please specify an address"},
        "id": 1,
        "jsonrpc": "2.0",
    }).encode()
    return RawResponse(200, {"content-type": "application/json"}, body)


def test_policy_refusal_moves_logs_to_the_next_dedicated_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """-32701 on the first log source is one request. The same chunk uses the next source."""
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    backend = Backend()
    backend.refuse_logs_hosts.add("nodereal.example.invalid")
    backend.transfer_logs[BSC] = [_log(TRANSFER_TOPIC, 7)]
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
    assert bsc["failure"] is None
    assert bsc["triggered"] is True
    assert bsc["reasons"]
    nodereal = [
        call for call in backend.calls
        if call["method"] == "eth_getLogs" and "nodereal.example.invalid" in call["url"]
    ]
    nxt = [
        call for call in backend.calls
        if call["method"] == "eth_getLogs" and "bsc-logs.example.invalid" in call["url"]
    ]
    family = [
        call for call in backend.calls
        if call["method"] == "eth_getLogs" and "bsc.example.invalid" in call["url"]
    ]
    assert len(nodereal) == 1
    assert len(nxt) == 2
    assert family == []
    _assert_no_zero_and_no_alert(tmp_path)


def test_every_log_source_refusal_is_a_gap_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    backend = Backend()
    backend.refuse_logs_hosts.update({"nodereal.example.invalid", "bsc-logs.example.invalid"})
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
    assert bsc["failure"] == "logs"
    assert bsc["triggered"] is False
    assert bsc["cursor_advanced"] is False
    probe = Database(tmp_path / "probe.sqlite")
    try:
        assert probe.probe_cursor(TENANT, BSC) is None
    finally:
        probe.close()
    assert _baseline(tmp_path, BSC)["raw_balance"] == str(ONE)
    _assert_no_zero_and_no_alert(tmp_path)
    family = [
        call for call in backend.calls
        if call["method"] == "eth_getLogs" and "bsc.example.invalid" in call["url"]
    ]
    assert family == []


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def test_log_gap_records_a_changed_native_balance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A log gap still records one balance_change. The baseline and probe cursor stay put."""
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    before = _baseline(tmp_path, BSC)
    probe_path = tmp_path / "probe.sqlite"
    for mode in ("refusal", "read_error"):
        if probe_path.exists():
            probe_path.unlink()
        backend = Backend()
        backend.balance[BSC] = TWO
        if mode == "refusal":
            backend.refuse_logs_hosts.update({"nodereal.example.invalid", "bsc-logs.example.invalid"})
        else:
            backend.fail_logs.add(BSC)
        payload = _run(config, tmp_path, backend, capsys, trigger="none")
        bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
        base = next(chain for chain in payload["chains"] if chain["network"] == BASE)
        assert _reasons(tmp_path) == [(BSC, "balance_change")]
        assert bsc["failure"] == "logs"
        assert bsc["triggered"] is True
        assert bsc["reasons"] == ["balance_change"]
        assert bsc["cursor_advanced"] is False
        assert base["failure"] is None
        assert base["triggered"] is False
        assert _baseline(tmp_path, BSC) == before
        probe = Database(probe_path)
        try:
            assert probe.probe_cursor(TENANT, BSC) is None
        finally:
            probe.close()
        _assert_no_zero_and_no_alert(tmp_path)
        _assert_history_unchanged(tmp_path)


def test_log_gap_failed_balance_item_does_not_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    before = _baseline(tmp_path, BSC)
    backend = Backend()
    backend.refuse_logs_hosts.update({"nodereal.example.invalid", "bsc-logs.example.invalid"})
    backend.fail_item.add(BSC)
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
    assert bsc["failure"] == "balance"
    assert bsc["triggered"] is False
    assert bsc["cursor_advanced"] is False
    assert _reasons(tmp_path) == []
    assert _baseline(tmp_path, BSC) == before
    assert [
        call for call in backend.calls
        if call["method"] == "eth_getLogs" and _network(call["url"]) == BSC
    ] == []
    _assert_no_zero_and_no_alert(tmp_path)


def test_log_gap_does_not_write_a_first_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    state = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        state.conn.execute(
            "DELETE FROM native_balance_baselines WHERE tenant_id=? AND wallet_id=? AND network=?",
            (TENANT, WALLET, BSC),
        )
        state.conn.commit()
        assert state.native_balance_baseline(TENANT, WALLET, BSC, ASSETS[BSC]) is None
    finally:
        state.close()
    backend = Backend()
    backend.refuse_logs_hosts.update({"nodereal.example.invalid", "bsc-logs.example.invalid"})
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
    assert bsc["failure"] == "logs"
    assert bsc["triggered"] is False
    assert _reasons(tmp_path) == []
    opened = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        assert opened.native_balance_baseline(TENANT, WALLET, BSC, ASSETS[BSC]) is None
    finally:
        opened.close()
    _assert_no_zero_and_no_alert(tmp_path)


def test_log_gap_balance_change_is_recorded_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    backend = Backend()
    backend.refuse_logs_hosts.update({"nodereal.example.invalid", "bsc-logs.example.invalid"})
    backend.balance[BSC] = TWO
    _run(config, tmp_path, backend, capsys, trigger="none")
    assert _reasons(tmp_path) == [(BSC, "balance_change")]
    payload = _run(config, tmp_path, backend, capsys, trigger="none")
    bsc = next(chain for chain in payload["chains"] if chain["network"] == BSC)
    assert bsc["triggered"] is False
    assert _reasons(tmp_path) == [(BSC, "balance_change")]


def test_log_gap_changed_balance_calls_run_once_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _desk(tmp_path, monkeypatch)
    _seed(tmp_path)
    public, private, database = _open(config)
    calls: list[int] = []

    def run_once() -> None:
        calls.append(1)

    backend = Backend()
    backend.refuse_logs_hosts.update({"nodereal.example.invalid", "bsc-logs.example.invalid"})
    backend.balance[BSC] = TWO
    try:
        first = run_balance_probe(
            public, private, database,
            probe_state=tmp_path / "probe.sqlite",
            backend=backend,
            clock=lambda: CLOCK,
            trigger="run-once",
            run_once=run_once,
        )
        assert calls == [1]
        assert first["run_once"] == "called"
        second = run_balance_probe(
            public, private, database,
            probe_state=tmp_path / "probe.sqlite",
            backend=backend,
            clock=lambda: CLOCK,
            trigger="run-once",
            run_once=run_once,
        )
    finally:
        database.close()
    assert calls == [1]
    assert second["run_once"] == "not_called"
