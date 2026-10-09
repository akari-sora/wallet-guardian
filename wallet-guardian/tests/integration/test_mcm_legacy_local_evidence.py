"""MCM-SHARE-S2. Same-host legacy evidence, offline.

Tests drive ``guardian legacy-capture`` and ``tools/mcm_shadow_round.py``.
Synthetic addresses only. No socket.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import threading
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from guardian.cli import main
from guardian.configuration import file_permission_bits
from guardian.identities import parse_address
from guardian.legacy_capture import address_hash, capture_writer_lock, scope_identity_hash
from guardian.legacy_state import convert_legacy_document

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
FIXTURE = ROOT / "tests" / "fixtures" / "legacy" / "monitor_state.synthetic.json"
TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
LABEL = "evm-a"
CLOCK = 1_700_000_000.0
INTERVAL = 10800
TARGET = "0x" + ("ab" * 32)
TOOL_HASH = "cd" * 32
TX_IN = "0x" + ("11" * 32)
OBSERVED = datetime.fromtimestamp(CLOCK, timezone.utc)
SLOT_END = OBSERVED
SLOT_START = OBSERVED - timedelta(seconds=INTERVAL)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)

    def resolve(host: str) -> list[str]:
        del host
        return ["1.1.1.1"]

    monkeypatch.setattr("guardian.pipeline._resolve", resolve)


def test_mcm_share_s2_01_immutable_sidecar(tmp_path: Path) -> None:
    """MCM-SHARE-S2-01. Hashes, COMPLETE last, rerun refused, source bytes unchanged."""
    runs = tmp_path / "runs-01"
    source = tmp_path / "legacy.json"
    source.write_bytes(FIXTURE.read_bytes())
    before = source.read_bytes()
    modified = source.stat().st_mtime_ns
    code, payload = _publish(runs, "s201", source, _stamp(SLOT_START), _stamp(SLOT_END), windows=[_window("history.fungible", "complete", 1, 2, SLOT_END)])
    assert code == 0
    assert payload["phase"] == "finished"
    assert source.read_bytes() == before
    assert source.stat().st_mtime_ns == modified
    run = runs / "s201"
    manifest_bytes = (run / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    assert manifest["schema_version"] == "guardian.legacy-run.v1"
    assert manifest["run_id"] == "s201"
    assert manifest["legacy_tool"]["hash"] == TOOL_HASH
    assert manifest["exit_code"] == 0
    names = {item["name"] for item in manifest["files"]}
    assert names == {"coverage.json", "events.jsonl", "state-after.json", "state-before.json"}
    for item in manifest["files"]:
        data = (run / item["name"]).read_bytes()
        assert item["size"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
    assert (run / "state-before.json").read_bytes() == before
    assert (run / "state-after.json").read_bytes() == before
    complete = (run / "COMPLETE").read_text(encoding="utf-8").strip()
    assert complete == hashlib.sha256(manifest_bytes).hexdigest()
    assert complete == payload["manifest_sha256"]
    complete_ns = (run / "COMPLETE").stat().st_mtime_ns
    for name in ("manifest.json", *names):
        assert (run / name).stat().st_mtime_ns <= complete_ns
    assert sorted(path.name for path in run.iterdir()) == [
        "COMPLETE",
        "coverage.json",
        "events.jsonl",
        "manifest.json",
        "state-after.json",
        "state-before.json",
    ]
    assert list(runs.glob("s201.tmp-*")) == []
    address = "0x0000000000000000000000000000000000000001"
    assert address not in manifest_bytes.decode("utf-8")
    _assert_mode(run, 0o700)
    _assert_mode(runs, 0o700)
    for name in ("COMPLETE", "manifest.json", *names):
        _assert_mode(run / name, 0o600)
    published = {path.name: path.read_bytes() for path in run.iterdir()}
    again, error = _cli(_begin_argv(runs, "s201", source, _stamp(SLOT_START)))
    assert again != 0
    assert "immutable" in error["error"]
    finish_again, finish_error = _cli(_finish_argv(runs, "s201", source, _stamp(SLOT_END)))
    assert finish_again != 0
    assert "immutable" in finish_error["error"]
    assert {path.name: path.read_bytes() for path in run.iterdir()} == published
    assert source.read_bytes() == before
    assert source.stat().st_mtime_ns == modified


def test_mcm_share_s2_02_real_window_recorded(tmp_path: Path) -> None:
    """MCM-SHARE-S2-02. Real from/to/target stay, and partial/error are not upgraded."""
    runs = tmp_path / "runs-02"
    source = tmp_path / "legacy.json"
    source.write_bytes(FIXTURE.read_bytes())
    windows = [
        _window("history.fungible", "partial", 10, 40, SLOT_END, network="ethereum-mainnet"),
        _window("history.native_external", "error", 10, 48, SLOT_END, network="ethereum-mainnet"),
        _window("permissions.erc20", "not_scanned", 1, 2, SLOT_END, network="bsc-mainnet"),
    ]
    code, _payload = _publish(runs, "s202", source, _stamp(SLOT_START), _stamp(SLOT_END), windows=windows)
    assert code == 0
    manifest = json.loads((runs / "s202" / "manifest.json").read_text(encoding="utf-8"))
    found = {(row["network"], row["capability"]): row for row in manifest["windows"]}
    partial = found[("ethereum-mainnet", "history.fungible")]
    assert partial["status"] == "partial"
    assert partial["from_block"] == 10
    assert partial["to_block"] == 40
    assert partial["target_hash"] == TARGET
    assert partial["target_timestamp"] == _stamp(SLOT_END)
    assert partial["source"] == "synthetic-rpc"
    errored = found[("ethereum-mainnet", "history.native_external")]
    assert errored["status"] == "error"
    assert errored["from_block"] == 10 and errored["to_block"] == 48
    skipped = found[("bsc-mainnet", "permissions.erc20")]
    assert skipped["status"] == "not_scanned"
    assert skipped["from_block"] == 1 and skipped["to_block"] == 2
    assert all(row["status"] != "complete" for row in manifest["windows"])
    coverage = json.loads((runs / "s202" / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["windows"] == manifest["windows"]


def test_mcm_share_s2_03_compare_valid_positive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-SHARE-S2-03. Complete windows, a quiet zero-event window, and one shared transfer."""
    desk = _desk(tmp_path, monkeypatch, with_transfer=True)
    runs = tmp_path / "runs-03"
    source = tmp_path / "legacy.json"
    source.write_bytes(FIXTURE.read_bytes())
    windows = [
        _window("balances.native", "complete", 100, 100, SLOT_END),
        _window("history.fungible", "complete", 0, 100, SLOT_END),
        _window("history.native_external", "complete", 0, 100, SLOT_END),
        _window("permissions.erc20", "complete", 0, 100, SLOT_END),
        _window("positions:aave_v3", "complete", 100, 100, SLOT_END),
    ]
    code, _payload = _publish(
        runs,
        "s203",
        source,
        _stamp(SLOT_START),
        _stamp(SLOT_END),
        windows=windows,
        events=[_transfer(_stamp(OBSERVED))],
    )
    assert code == 0
    result = _shadow(desk, source, runs)
    assert result["code"] == 0
    compared = result["compared"]
    assert compared["compare_valid"] is True
    assert compared["compare_reason"] == ""
    assert compared["legacy_run_id"] == "s203"
    fungible = next(row for row in compared["legacy_query_windows"] if row["capability"] == "history.fungible")
    assert fungible["status"] == "complete"
    assert fungible["from_block"] == 0 and fungible["to_block"] == 100
    lines = [json.loads(line) for line in (runs / "s203" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["capability"] == "history.native_external"
    assert lines[0]["kind"] == "asset_in"
    assert all(line["capability"] != "history.fungible" for line in lines)
    matched = [row for row in compared["matched"] if row["kind"] == "asset_in" and row["wallet_id"] == WALLET]
    assert matched
    assert matched[0]["network"] == "ethereum-mainnet"
    assert matched[0]["position"] == "1"
    assert matched[0]["subject"] == "evm-native:1"
    assert matched[0]["tx"].startswith("0x1111")
    assert matched[0]["tx"].endswith("1111:" + hashlib.sha256(TX_IN.encode("utf-8")).hexdigest()[:12])
    assert result["round"]["compare_valid"] is True
    assert TX_IN not in json.dumps(compared)


def test_mcm_share_s2_04_invalid_evidence_negative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-SHARE-S2-04. Each broken sidecar disqualifies with its own reason."""
    desk = _desk(tmp_path, monkeypatch, with_transfer=False)
    source = tmp_path / "legacy.json"
    source.write_bytes(FIXTURE.read_bytes())
    fresh = (_stamp(SLOT_START), _stamp(SLOT_END))
    good = [_window("history.fungible", "complete", 0, 100, SLOT_END)]

    missing = tmp_path / "runs-missing"
    missing_result = _shadow(desk, source, missing, evidence=tmp_path / "evidence-missing")
    _assert_rejected(missing_result, "legacy_evidence_missing")
    assert not missing.exists()

    tmp_only = tmp_path / "runs-tmp"
    begin_code, begin_payload = _cli(_begin_argv(tmp_only, "s2tmp", source, fresh[0]))
    assert begin_code == 0 and begin_payload["phase"] == "begun"
    record_code, record_payload = _cli(_record_argv(tmp_only, "s2tmp", good, []))
    assert record_code == 0 and record_payload["windows"] == 1
    tmp_result = _shadow(desk, source, tmp_only, evidence=tmp_path / "evidence-tmp")
    _assert_rejected(tmp_result, "legacy_incomplete_marker")
    assert not (tmp_only / "s2tmp" / "COMPLETE").exists()

    incomplete = tmp_path / "runs-incomplete"
    assert _publish(incomplete, "s2inc", source, *fresh, windows=good)[0] == 0
    (incomplete / "s2inc" / "COMPLETE").unlink()
    incomplete_result = _shadow(desk, source, incomplete, evidence=tmp_path / "evidence-incomplete")
    _assert_rejected(incomplete_result, "legacy_incomplete_marker")

    mismatched = tmp_path / "runs-hash"
    assert _publish(mismatched, "s2hash", source, *fresh, windows=good)[0] == 0
    state_after = mismatched / "s2hash" / "state-after.json"
    state_after.write_bytes(state_after.read_bytes() + b" ")
    hash_result = _shadow(desk, source, mismatched, evidence=tmp_path / "evidence-hash")
    _assert_rejected(hash_result, "legacy_hash_mismatch")

    stale_runs = tmp_path / "runs-stale"
    old_end = SLOT_END - timedelta(days=2)
    old_start = old_end - timedelta(seconds=INTERVAL)
    assert _publish(
        stale_runs,
        "s2stale",
        source,
        _stamp(old_start),
        _stamp(old_end),
        windows=[_window("history.fungible", "complete", 0, 100, old_end)],
    )[0] == 0
    stale_result = _shadow(desk, source, stale_runs, evidence=tmp_path / "evidence-stale", max_age=60)
    _assert_rejected(stale_result, "legacy_stale")

    foreign = tmp_path / "runs-identity"
    other = [{"address_hash": hashlib.sha256(b"synthetic-other").hexdigest(), "label": "other"}]
    assert _publish(foreign, "s2id", source, *fresh, windows=good, accounts=other)[0] == 0
    identity_result = _shadow(desk, source, foreign, evidence=tmp_path / "evidence-identity")
    _assert_rejected(identity_result, "legacy_identity_mismatch")

    gap_runs = tmp_path / "runs-gap"
    outside = SLOT_START - timedelta(days=1)
    assert _publish(
        gap_runs,
        "s2gap",
        source,
        *fresh,
        windows=[_window("history.fungible", "complete", 0, 100, outside)],
    )[0] == 0
    gap_result = _shadow(desk, source, gap_runs, evidence=tmp_path / "evidence-gap")
    _assert_rejected(gap_result, "legacy_window_gap")

    partial_runs = tmp_path / "runs-partial"
    assert _publish(
        partial_runs,
        "s2part",
        source,
        *fresh,
        windows=[_window("history.fungible", "partial", 0, 100, SLOT_END)],
    )[0] == 0
    partial_result = _shadow(desk, source, partial_runs, evidence=tmp_path / "evidence-partial")
    _assert_rejected(partial_result, "legacy_partial")

    quiet_runs = tmp_path / "runs-quiet"
    quiet = tmp_path / "quiet.json"
    quiet.write_text(json.dumps({
        "_note": "QUIET",
        "alerted": [f"eth:{LABEL}:{'0x' + ('33' * 32)} approval synthetic"],
        "latest_seen": {f"{LABEL}_eth_external": "2023-11-14"},
        "nonces": {"0x0000000000000000000000000000000000000001": {"eth": 4}},
    }), encoding="utf-8")
    assert _publish(quiet_runs, "s2quiet", quiet, *fresh, windows=[])[0] == 0
    quiet_result = _shadow(desk, quiet, quiet_runs, evidence=tmp_path / "evidence-quiet")
    _assert_rejected(quiet_result, "legacy_window_gap")
    assert quiet_result["compared"]["legacy_query_windows"] == []
    assert any(row["seen_on"] == "2023-11-14" and row["wallet_id"] == WALLET for row in quiet_result["compared"]["legacy_coverage_windows"])
    assert quiet_result["compared"]["compare_valid"] is False


def test_mcm_share_s2_05_single_writer_report_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-SHARE-S2-05. Second writer loses the lock. Compare does not write. Converter reads the fixture."""
    module = (ROOT / "src" / "guardian" / "legacy_capture.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(module)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "guardian.delivery" not in imported
    assert "guardian.rules" not in imported
    assert "guardian.storage" not in imported
    assert not any("sender" in name or "notification" in name for name in imported)

    calls = {"delivery": 0}

    def _boom(*_args: object, **_kwargs: object) -> None:
        calls["delivery"] += 1
        raise AssertionError("delivery was called")

    monkeypatch.setattr("guardian.delivery.deliver", _boom)
    runs = tmp_path / "runs-lock"
    runs.mkdir()
    started = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with capture_writer_lock(runs):
            started.set()
            release.wait(5)

    thread = threading.Thread(target=_hold)
    thread.start()
    assert started.wait(5)
    source = tmp_path / "legacy.json"
    source.write_bytes(FIXTURE.read_bytes())
    held, held_payload = _cli(_begin_argv(runs, "s2lock", source, _stamp(SLOT_START)))
    release.set()
    thread.join()
    assert held != 0
    assert held_payload["error"] == "capture_locked"
    assert calls["delivery"] == 0
    assert list(runs.glob("s2lock.tmp-*")) == []

    published = tmp_path / "runs-read"
    before_source = source.read_bytes()
    before_ns = source.stat().st_mtime_ns
    assert _publish(published, "s205", source, _stamp(SLOT_START), _stamp(SLOT_END), windows=[_window("history.fungible", "complete", 0, 100, SLOT_END)])[0] == 0
    before_tree = _tree(published)
    desk = _desk(tmp_path, monkeypatch, with_transfer=False)
    result = _shadow(desk, source, published, evidence=tmp_path / "evidence-read")
    assert result["code"] == 0
    assert _tree(published) == before_tree
    assert source.read_bytes() == before_source
    assert source.stat().st_mtime_ns == before_ns
    assert not any(path.suffix == ".sqlite" for path in published.rglob("*"))
    assert calls["delivery"] == 0

    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    converted = convert_legacy_document(document, {LABEL: WALLET})
    assert any(
        row["kind"] == "transfer_out" and row["wallet_id"] == WALLET and row["network"] == "ethereum-mainnet" and row["position"] == ""
        for row in converted["tx_rows"]
    )
    assert any(row["seen_on"] == "2023-11-14" and row["kind"] == "external" and row["wallet_id"] == WALLET for row in converted["coverage_windows"])
    assert any(row["status"] == "mapped" and "ethereum-mainnet" in row["networks"] for row in converted["scan_windows"])
    assert any(row["seen_on"] == "2023-11-14" and row["wallet_id"] == WALLET for row in result["compared"]["legacy_coverage_windows"])
    assert document["bsc_last_block"] == 50
    assert "nonces" in document and "alerted" in document and "latest_seen" in document


def _assert_rejected(result: dict, reason: str) -> None:
    assert result["code"] == 0
    assert result["compared"]["compare_valid"] is False
    assert result["compared"]["compare_reason"] == reason
    assert result["round"]["compare_valid"] is False
    assert result["round"]["compare_reason"] == reason
    assert result["cutover"]["qualified"] is False
    assert reason in result["cutover"]["reasons"]
    copied = dict(result["round"])
    copied["baseline"] = False
    copied["index"] = 1
    decision = _tool().evaluate_cutover([copied])
    assert decision["qualified"] is False
    assert decision["qualifying_windows"] == 0
    assert reason in decision["reasons"]


def _publish(
    runs: Path,
    run_id: str,
    source: Path,
    started: str,
    finished: str,
    *,
    windows: list[dict],
    events: list[dict] | None = None,
    accounts: list[dict[str, str]] | None = None,
) -> tuple[int, dict]:
    code, payload = _cli(_begin_argv(runs, run_id, source, started, accounts=accounts))
    if code != 0:
        return code, payload
    recorded, record_payload = _cli(_record_argv(runs, run_id, windows, events or []))
    if recorded != 0:
        return recorded, record_payload
    return _cli(_finish_argv(runs, run_id, source, finished))


def _begin_argv(runs: Path, run_id: str, source: Path, started: str, *, accounts: list[dict[str, str]] | None = None) -> list[str]:
    rows = accounts if accounts is not None else [_desk_account()]
    scope = scope_identity_hash(rows)
    argv = [
        "legacy-capture",
        "begin",
        "--runs-dir",
        str(runs),
        "--run-id",
        run_id,
        "--legacy-state",
        str(source),
        "--tool-name",
        "legacy-monitor",
        "--tool-version",
        "v1",
        "--tool-hash",
        TOOL_HASH,
        "--host-id",
        "synthetic-host",
        "--scope-hash",
        scope,
        "--started-at",
        started,
    ]
    for row in rows:
        argv.extend(["--account", f"{row['label']}={row['address_hash']}"])
    return argv


def _record_argv(runs: Path, run_id: str, windows: list[dict], events: list[dict]) -> list[str]:
    argv = ["legacy-capture", "record", "--runs-dir", str(runs), "--run-id", run_id]
    for window in windows:
        argv.extend(["--window-json", json.dumps(window)])
    for event in events:
        argv.extend(["--event-json", json.dumps(event)])
    return argv


def _finish_argv(runs: Path, run_id: str, source: Path, finished: str) -> list[str]:
    return [
        "legacy-capture",
        "finish",
        "--runs-dir",
        str(runs),
        "--run-id",
        run_id,
        "--legacy-state",
        str(source),
        "--exit-code",
        "0",
        "--finished-at",
        finished,
    ]


def _window(capability: str, status: str, start: int, end: int, moment: datetime, *, network: str = "ethereum-mainnet") -> dict:
    return {
        "capability": capability,
        "from_block": start,
        "network": network,
        "source": "synthetic-rpc",
        "status": status,
        "target_hash": TARGET,
        "target_timestamp": _stamp(moment),
        "to_block": end,
    }


def _transfer(observed: str) -> dict:
    return {
        "capability": "history.native_external",
        "chain": "eth",
        "finality": "confirmed",
        "kind": "asset_in",
        "label": LABEL,
        "network": "ethereum-mainnet",
        "observed_at": observed,
        "position": "1",
        "schema_version": "guardian.legacy-event.v1",
        "subject": "evm-native:1",
        "tx": TX_IN,
        "wallet_id": WALLET,
    }


def _desk_account() -> dict[str, str]:
    identity = parse_address("evm", _live().ADDRESS_A).identity_key
    return {"address_hash": address_hash(identity), "label": LABEL}


def _shadow(
    desk: object,
    legacy: Path,
    runs: Path,
    *,
    evidence: Path | None = None,
    max_age: int = 12600,
) -> dict:
    evidence_dir = evidence or Path(getattr(desk, "evidence"))
    argv = [
        "--mcm-config",
        str(getattr(desk, "config")),
        "--legacy-state",
        str(legacy),
        "--evidence-dir",
        str(evidence_dir),
        "--legacy-runs-dir",
        str(runs),
        "--legacy-max-age-seconds",
        str(max_age),
    ]
    code = _tool().main(argv, backend=getattr(desk, "backend"), sender=_Sink(), clock=lambda: CLOCK)
    compared_path = evidence_dir / "commands" / "compare-legacy.stdout"
    compared = json.loads(compared_path.read_text(encoding="utf-8")) if compared_path.is_file() else {}
    round_path = evidence_dir / "round.json"
    recorded = json.loads(round_path.read_text(encoding="utf-8")) if round_path.is_file() else {}
    cutover_path = evidence_dir / "cutover.json"
    cutover = json.loads(cutover_path.read_text(encoding="utf-8")) if cutover_path.is_file() else {}
    return {"code": code, "compared": compared, "cutover": cutover, "round": recorded}


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_transfer: bool) -> object:
    live = _live()
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = ["aave-v3"]
    document["polling"]["interval_seconds"] = INTERVAL
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    accounts = {
        "accounts": [
            {
                "address": live.ADDRESS_A,
                "family": "evm",
                "label": LABEL,
                "networks": ["ethereum-mainnet"],
                "own_group": "desk",
                "relationship": "owned_user_declared",
                "wallet_id": WALLET,
            }
        ],
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
    }
    secrets = {
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "ETHERSCAN_API_BASE": "https://etherscan.example.invalid/v2",
        "ETHERSCAN_API_KEY": "synthetic-shadow-key",
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
    evidence = tmp_path / "evidence"
    _write_private(accounts_path, json.dumps(accounts) + "\n")
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts_path))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    backend = live.ChainBackend()
    if with_transfer:
        backend.native_rows = [live._native_row(TX_IN, live.ADDRESS_A, 50, "1", live.WEI)]
    return _Desk(config, evidence, backend)


def _cli(argv: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = main(argv)
    text = stdout.getvalue()
    payload = json.loads(text) if text.strip() else {}
    return code, payload


def _tool():
    name = "mcm_shadow_round"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "tools" / "mcm_shadow_round.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _live():
    name = "mcm_share_s2_live"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("test_mcm_live_entrypoint.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> dict[str, str]:
    found = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def _assert_mode(path: Path, expected: int) -> None:
    bits = file_permission_bits(path)
    if bits is not None:
        assert bits == expected


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class _Sink:
    def send(self, message: dict) -> None:
        del message


class _Desk:
    def __init__(self, config: Path, evidence: Path, backend: object) -> None:
        self.config = config
        self.evidence = evidence
        self.backend = backend


def test_mcm_share_s2_04b_missing_compare_valid_fails_closed() -> None:
    """Review fix: a round record with no compare_valid field must disqualify, not pass."""
    from guardian import cutover

    good = {"no_send": True, "compare_valid": True}
    assert cutover.compare_valid_block(good) == []
    assert cutover.compare_valid_block({"no_send": True}) == ["evidence_missing:compare_valid"]
    assert cutover.compare_valid_block({"compare_valid": False, "compare_reason": "legacy_stale"}) == ["legacy_stale"]
