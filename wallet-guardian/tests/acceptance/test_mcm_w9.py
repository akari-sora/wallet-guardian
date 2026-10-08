"""MCM-W9 acceptance. Offline fixtures only. No live endpoint is contacted."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from eth_utils import to_checksum_address

from guardian.cli import main
from guardian.configuration import load_public_config, resolve_private_inputs
from guardian.delivery import deliver, load_handoff
from guardian.identities import _b58encode
from guardian.legacy_state import import_legacy_state
from guardian.rules import AlertPolicy, Candidate
from guardian.scheduler import Watchdog
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
SECRET = "synthetic-secret-value-9f3a7c"
EVM_ID = "11111111-1111-4111-8111-111111111111"
SOL_ID = "22222222-2222-4222-8222-222222222222"
TENANT = "local"
LEGACY_TX = "0x" + ("ab" * 32)
FRESH_TX = "0x" + ("cd" * 32)
WEI_HALF = 500_000_000_000_000_000
WEI_BELOW = 499_000_000_000_000_000
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
CHAIN_HEX = {
    "ethereum.example.invalid": "0x1",
    "bsc.example.invalid": "0x38",
    "arbitrum.example.invalid": "0xa4b1",
    "base.example.invalid": "0x2105",
    "optimism.example.invalid": "0xa",
    "polygon.example.invalid": "0x89",
    "avalanche.example.invalid": "0xa86a",
}


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


@dataclass
class Desk:
    config: Path
    state: Path
    evm_address: str
    sol_address: str
    backend: FixtureRpc


class FixtureRpc:
    """Answers JSON-RPC by method. Host selects the public chain id."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, call: object) -> RawResponse:
        body = getattr(call, "body", b"") or b""
        payload = json.loads(body.decode("utf-8")) if body else {}
        method = payload.get("method") if isinstance(payload, dict) else None
        host = urlsplit(str(getattr(call, "url", ""))).hostname or ""
        self.calls.append(str(method))
        result = _rpc_result(str(method), host)
        encoded = json.dumps({"id": payload.get("id", 1) if isinstance(payload, dict) else 1, "jsonrpc": "2.0", "result": result}).encode()
        return RawResponse(200, {}, encoded)


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, message: dict) -> None:
        self.messages.append(dict(message))


class FlakySender:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.failed = False

    def send(self, message: dict) -> None:
        if not self.failed:
            self.failed = True
            raise TimeoutError("timeout")
        self.messages.append(dict(message))


def test_mcm_w9_01_first_run_no_code_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    desk = _desk(tmp_path, monkeypatch)
    assert _cli(capsys, ["config", "validate", "--mcm-config", str(desk.config)])["wallets"] == 2
    probe = _cli(capsys, ["sources", "probe", "--mcm-config", str(desk.config)], desk.backend)
    by_id = {row["id"]: row for row in probe["sources"]}
    assert by_id["rpc-ethereum-mainnet"]["identity"]["status"] == "matched"
    assert by_id["rpc-ethereum-mainnet"]["identity"]["probe"] == "untested"
    assert by_id["rpc-solana-mainnet"]["identity"]["status"] == "matched"
    assert by_id["rpc-solana-mainnet"]["identity"]["probe"] == "untested"
    assert by_id["etherscan"]["credential_present"] is False
    assert by_id["etherscan"]["identity"]["status"] == "blocked"
    assert probe["live_verified"] is False
    report = _cli(capsys, ["run-once", "--mcm-config", str(desk.config), "--no-send"], desk.backend)
    assert report["status"] == "ok"
    assert report["sent"] == 0
    assert report["positions"] == "unknown"
    assert report["live_verified"] is False
    assert report["outbox_written"] >= 1
    assert report["valuation_unknown"]
    assert all(int(item["raw_amount"]) > 0 for item in report["native_reads"])
    assert {item["network"] for item in report["native_reads"]} == {"ethereum-mainnet", "solana-mainnet"}
    coverage = _cli(capsys, ["coverage", "--mcm-config", str(desk.config), "--json"])
    rows = {(row["wallet_id"], row["network"], row["capability"]): row for row in coverage["rows"]}
    eth_native = rows[(EVM_ID, "ethereum-mainnet", "balances.native")]
    sol_native = rows[(SOL_ID, "solana-mainnet", "balances.native")]
    history = rows[(EVM_ID, "ethereum-mainnet", "history.fungible")]
    external = rows[(EVM_ID, "ethereum-mainnet", "history.native_external")]
    permits = rows[(EVM_ID, "ethereum-mainnet", "permits.offchain_unused")]
    risk = rows[(EVM_ID, "ethereum-mainnet", "address_risk")]
    assert eth_native["quality"] == "complete_in_declared_scope"
    assert sol_native["quality"] == "complete_in_declared_scope"
    assert eth_native["price_quality"] == "unknown"
    assert sol_native["price_quality"] == "unknown"
    assert history["quality"] == "partial"
    assert "not_queried" not in history["reason_codes"]
    assert external["quality"] == "error"
    assert "credential_missing" in external["reason_codes"]
    assert "blocked" in external["reason_codes"]
    assert permits["support"] == "not_observable"
    assert permits["quality"] == "unknown"
    assert "not_observable" in permits["reason_codes"]
    assert "third_party_denied" in risk["reason_codes"]
    assert any(row["support"] == "unsupported" or "unsupported" in row["reason_codes"] for row in coverage["rows"])
    blob = json.dumps({"coverage": coverage, "probe": probe, "report": report}, ensure_ascii=False)
    _assert_private_absent(blob, desk)


def test_mcm_w9_02_rules_boundary() -> None:
    alerts = json.loads(EXAMPLE.read_text(encoding="utf-8"))["alerts"]
    policy = AlertPolicy.from_public(alerts)
    assert policy.native_eth_wei == WEI_HALF
    assert policy.usd == Decimal("1000")
    assert policy.apply([]) == []

    below = policy.apply([_asset(raw=WEI_BELOW, native=True, network="ethereum-mainnet", usd="1")])
    exact = policy.apply([_asset(raw=WEI_HALF, native=True, network="ethereum-mainnet", usd="1")])
    unpriced = policy.apply([_asset(raw=WEI_HALF, native=True, network="ethereum-mainnet")])
    small_usd = policy.apply([_asset(raw=1, usd="999.99")])
    exact_usd = policy.apply([_asset(raw=1, usd="1000.00")])
    wrapped = policy.apply([_asset(raw=WEI_HALF, native=False, symbol="ETH", usd="1")])
    bnb = policy.apply([_asset(raw=WEI_HALF, native=True, network="bsc-mainnet", symbol="BNB", usd="1")])
    sol = policy.apply([_asset(raw=WEI_HALF, native=True, network="solana-mainnet", symbol="SOL", usd="1")])
    assert below[0].alert is False
    assert exact[0].alert is True
    assert exact[0].kind == "native_eth"
    assert unpriced[0].alert is True
    assert unpriced[0].valuation_unknown is True
    assert small_usd[0].alert is False
    assert small_usd[0].valuation_unknown is False
    assert exact_usd[0].alert is True
    assert exact_usd[0].kind == "asset_usd"
    assert wrapped[0].alert is False
    assert bnb[0].alert is False
    assert sol[0].alert is False

    missing = policy.apply([_asset(raw=10**30)])
    assert missing[0].alert is False
    assert missing[0].valuation_unknown is True

    increased = policy.apply([_permission("increased", raw=0)])
    first = policy.apply([_permission("new", prior_known=False, raw=0)])
    revoked = policy.apply([_permission("revoked", raw=WEI_HALF)])
    assert increased[0].alert is True
    assert increased[0].kind == "permission_increased"
    assert increased[0].valuation_unknown is False
    assert first[0].kind == "first_observed"
    assert first[0].alert is True
    assert "newly_added" not in first[0].kind
    assert revoked[0].kind == "revocation"
    assert revoked[0].informational is True

    history = [_asset(raw=WEI_HALF, native=True, network="ethereum-mainnet", usd="5000", identity=f"tx-{index}") for index in range(3)]
    risk = _permission("new", raw=0, prior_known=False, capability="history.fungible", identity="approval-1")
    folded = policy.apply([*history, risk], baseline_scopes=[(EVM_ID, "ethereum-mainnet", "history.fungible")])
    assert len(folded) == 1
    assert folded[0].kind == "baseline_summary"
    assert folded[0].alert is True
    assert dict(folded[0].counts) == {"events": 3, "explicit_risks": 1}
    assert policy.apply(history)[0].kind == "native_eth"
    again = policy.apply([_asset(raw=1, usd="1000", identity="priced")])
    repeat = policy.apply([_asset(raw=1, usd="1000", identity="priced")])
    assert again[0].dedup_key == repeat[0].dedup_key

    left = EVM_ID
    right = SOL_ID
    outgoing = _asset(raw=WEI_HALF, native=True, network="ethereum-mainnet", usd="5000", identity="move-1", direction="out", counterparty=right, wallet_id=left)
    incoming = _asset(raw=WEI_HALF, native=True, network="ethereum-mainnet", usd="5000", identity="move-1", direction="in", counterparty=left, wallet_id=right)
    internal = policy.apply([outgoing, incoming])
    assert len(internal) == 1
    assert internal[0].kind == "internal_transfer"
    assert internal[0].alert is False
    assert internal[0].net == "0"
    assert internal[0].gross == str(WEI_HALF * 2)


def test_mcm_w9_03_handover_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    desk = _desk(tmp_path, monkeypatch)
    sender = RecordingSender()
    first = _cli(capsys, ["run-once", "--mcm-config", str(desk.config), "--no-send"], desk.backend, sender)
    assert first["sent"] == 0
    assert first["delivery_status"] == "no_send"
    assert sender.messages == []
    database = Database(desk.state / "monitor.sqlite")
    pending = database.outbox_for(TENANT)
    assert pending
    assert {row["status"] for row in pending} == {"pending"}
    count = len(pending)
    database.close()
    second = _cli(capsys, ["run-once", "--mcm-config", str(desk.config), "--no-send"], desk.backend, sender)
    assert second["sent"] == 0
    assert second["outbox_written"] == 0
    assert sender.messages == []
    database = Database(desk.state / "monitor.sqlite")
    assert len(database.outbox_for(TENANT)) == count

    legacy_path = tmp_path / "monitor_state.json"
    legacy = {
        "alerted": [f"eth:desk:{LEGACY_TX}"],
        "latest_seen": {"desk_eth_native": desk.evm_address},
        "operator_note": SECRET,
    }
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    before = legacy_path.read_bytes()
    database.add_alert(TENANT, "legacy-shaped", {
        "dedup_key": "legacy-shaped",
        "identity": "legacy-tx",
        "kind": "asset_usd",
        "network": "ethereum-mainnet",
        "tx_id": LEGACY_TX,
        "wallet_id": EVM_ID,
    })
    database.add_alert(TENANT, "fresh-shaped", {
        "dedup_key": "fresh-shaped",
        "identity": "fresh-tx",
        "kind": "asset_usd",
        "network": "ethereum-mainnet",
        "tx_id": FRESH_TX,
        "wallet_id": EVM_ID,
    })
    public = load_public_config(desk.config)
    private = resolve_private_inputs(public, os.environ)
    compared = _compare_path(legacy_path, database)
    assert compared["counts"]["matched"] >= 1
    assert compared["counts"]["unmapped_legacy"] >= 1
    assert "solana-mainnet" in compared["coverage_differences"]["networks_legacy_never_checked"]
    imported = import_legacy_state(database, TENANT, json.loads(before.decode("utf-8")), tag_map={"desk": EVM_ID}, accounts=private.accounts)
    assert imported["suppressions_written"] == 1
    assert legacy_path.read_bytes() == before
    _assert_private_absent(json.dumps(compared, ensure_ascii=False), desk)
    assert LEGACY_TX not in json.dumps(compared)

    public.document["delivery"] = {"mode": "configured_sender", "profile_env": "MONITOR_DELIVERY_PROFILE"}
    withheld = tmp_path / "delivery-withheld.json"
    _write_private(withheld, json.dumps({"channel": "unwired"}) + "\n")
    monkeypatch.setenv("MONITOR_DELIVERY_PROFILE", str(withheld))
    withheld_gate = load_handoff(public, os.environ)
    withheld_report = deliver(database, TENANT, mode="send", sender=sender, handoff=withheld_gate, clock=lambda: 1_700_000_000)
    assert withheld_report.status == "send_disabled"
    assert withheld_report.misconfigured is False
    assert withheld_report.sent == 0
    assert sender.messages == []

    database.conn.execute(
        "INSERT INTO delivery_leases(tenant_id, owner_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (TENANT, "other-monitor", "1", "9999999999"),
    )
    database.conn.commit()
    ready = _ready_profile(tmp_path, monkeypatch, public)
    held = deliver(database, TENANT, mode="send", sender=sender, handoff=ready, clock=lambda: 1_700_000_000, owner_id="this-monitor")
    assert held.status == "lease_held"
    assert held.sent == 0
    assert sender.messages == []
    database.conn.execute("DELETE FROM delivery_leases")
    database.conn.commit()

    flaky = FlakySender()
    partial = deliver(database, TENANT, mode="send", sender=flaky, handoff=ready, clock=lambda: 1_700_000_100, owner_id="this-monitor")
    assert flaky.failed is True
    assert partial.sent >= 1
    attempts = dict(partial.attempts)
    assert attempts
    assert max(attempts.values()) >= 1
    still = [row for row in database.outbox_for(TENANT) if row["status"] == "pending"]
    assert len(still) == 1
    finished = deliver(database, TENANT, mode="send", sender=flaky, handoff=ready, clock=lambda: 1_700_000_200, owner_id="this-monitor")
    assert finished.sent == 1
    quiet = deliver(database, TENANT, mode="send", sender=flaky, handoff=ready, clock=lambda: 1_700_000_300, owner_id="this-monitor")
    assert quiet.sent == 0
    keys = [message["dedup_key"] for message in flaky.messages]
    assert len(keys) == len(set(keys))
    assert "fresh-shaped" in keys
    assert "legacy-shaped" not in keys
    assert all(LEGACY_TX not in json.dumps(message) for message in flaky.messages)
    assert legacy_path.read_bytes() == before
    database.close()


def test_mcm_w9_04_health_and_privacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    desk = _desk(tmp_path, monkeypatch)
    report = _cli(capsys, ["run-once", "--mcm-config", str(desk.config), "--no-send"], desk.backend)
    exported = _cli(capsys, ["export", "--public", "--mcm-config", str(desk.config)])
    plan = _cli(capsys, ["monitor", "plan", "--mcm-config", str(desk.config)])
    assert plan["started"] is False
    assert plan["execution"]["auto_disable_schedule"] is False
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps({
        "alerted": [f"eth:desk:{FRESH_TX}"],
        "latest_seen": {"desk_eth_native": desk.evm_address},
        "operator_note": desk.sol_address,
    }), encoding="utf-8")
    database = Database(desk.state / "monitor.sqlite")
    compared = _compare_path(legacy_path, database)
    database.close()
    blob = json.dumps({"compared": compared, "exported": exported, "plan": plan, "report": report}, ensure_ascii=False)
    _assert_private_absent(blob, desk)
    assert exported["wallet_counts"]["by_family"]["evm"] == 1
    assert exported["wallet_counts"]["by_family"]["solana"] == 1
    assert plan["wallets"] == 2

    health = Database(tmp_path / "health.sqlite")
    health.upsert_tenant(TENANT, TENANT)
    health.conn.execute(
        "INSERT INTO capability_cursors(tenant_id, wallet_id, network, capability, cursor, block_number) VALUES (?, ?, ?, ?, ?, ?)",
        (TENANT, EVM_ID, "ethereum-mainnet", "balances.native", "kept", "1"),
    )
    health.conn.commit()
    public = load_public_config(desk.config)
    before = json.dumps(public.document["execution"])
    dog = Watchdog(health, TENANT, public)
    failed = SimpleNamespace(status="failed", required={})
    first = dog.evaluate(failed, "2026-10-09T00:00:00Z")
    assert first["emitted"][0]["kind"] == "monitor_blind"
    assert first["schedule_disabled"] is False
    assert first["auto_disable_schedule"] is False
    second = dog.evaluate(failed, "2026-10-09T00:10:00Z")
    assert second["emitted"] == []
    assert second["blind"] is True
    recovered = dog.evaluate(
        SimpleNamespace(status="ok", required={"desk|ethereum-mainnet|balances.native": "complete_in_declared_scope"}),
        "2026-10-09T00:20:00Z",
    )
    assert recovered["emitted"][0]["kind"] == "monitor_recovered"
    assert recovered["blind"] is False
    later = dog.evaluate(failed, "2026-10-09T03:00:00Z")
    assert later["emitted"][0]["kind"] == "monitor_blind"
    assert later["emitted"][0]["dedup_key"] != first["emitted"][0]["dedup_key"]
    assert json.dumps(public.document["execution"]) == before
    assert public.document["execution"]["auto_disable_schedule"] is False
    kept = health.conn.execute(
        "SELECT cursor FROM capability_cursors WHERE tenant_id=? AND capability=?",
        (TENANT, "balances.native"),
    ).fetchone()
    assert kept["cursor"] == "kept"

    other = Database(tmp_path / "health-shrink.sqlite")
    other.upsert_tenant(TENANT, TENANT)
    shrink = Watchdog(other, TENANT, public)
    shrink.evaluate(
        SimpleNamespace(status="ok", required={"desk|ethereum-mainnet|balances.native": "complete_in_declared_scope"}),
        "2026-10-09T00:00:00Z",
    )
    shrunk = shrink.evaluate(
        SimpleNamespace(status="ok", required={"desk|ethereum-mainnet|balances.native": "error"}),
        "2026-10-09T00:05:00Z",
    )
    assert shrunk["blind"] is True
    assert shrunk["emitted"][0]["kind"] == "monitor_blind"
    aged = Watchdog(Database(tmp_path / "health-age.sqlite"), TENANT, public)
    aged.database.upsert_tenant(TENANT, TENANT)
    aged.evaluate(
        SimpleNamespace(status="ok", required={"desk|ethereum-mainnet|balances.native": "partial"}),
        "2026-10-09T00:00:00Z",
    )
    late = aged.evaluate(
        SimpleNamespace(status="degraded", required={"desk|ethereum-mainnet|balances.native": "partial"}),
        "2026-10-09T07:00:00Z",
    )
    assert late["emitted"][0]["kind"] == "monitor_blind"
    assert late["schedule_disabled"] is False
    health.close()
    other.close()
    aged.database.close()


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Desk:
    config = tmp_path / "monitor.json"
    config.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    evm_address = to_checksum_address("0x" + ("0" * 39) + "1")
    sol_address = _b58encode(bytes([2]) * 32)
    accounts = {
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
        "accounts": [
            {
                "wallet_id": EVM_ID,
                "label": "evm-desk",
                "family": "evm",
                "address": evm_address,
                "networks": ["ethereum-mainnet"],
                "relationship": "owned_user_declared",
                "own_group": "desk",
            },
            {
                "wallet_id": SOL_ID,
                "label": "sol-desk",
                "family": "solana",
                "address": sol_address,
                "networks": ["solana-mainnet"],
                "relationship": "owned_user_declared",
                "own_group": "desk",
            },
        ],
    }
    secrets = {
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "COINGECKO_API_KEY": SECRET,
        "ETHERSCAN_API_BASE": "https://etherscan.example.invalid/v2",
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
    # Ambient machine keys must not leak into the desk: the profile intentionally omits them.
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return Desk(config, state, evm_address, sol_address, FixtureRpc())


def _cli(capsys: pytest.CaptureFixture[str], argv: list[str], backend: FixtureRpc | None = None, sender: RecordingSender | None = None) -> dict:
    code = main(argv, backend=backend, sender=sender)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0, payload
    assert payload.get("status") != "error"
    return payload


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _ready_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public: object) -> object:
    path = tmp_path / "delivery-ready.json"
    _write_private(path, json.dumps({"single_sender_handoff": True}) + "\n")
    monkeypatch.setenv("MONITOR_DELIVERY_PROFILE", str(path))
    return load_handoff(public, os.environ)


def _compare_path(path: Path, database: Database) -> dict:
    from guardian.comparison import compare_with_legacy

    return compare_with_legacy(path, database, TENANT)


def _assert_private_absent(blob: str, desk: Desk) -> None:
    lowered = blob.lower()
    assert desk.evm_address.lower() not in lowered
    assert desk.sol_address not in blob
    assert SECRET not in blob


def _asset(
    *,
    raw: int,
    native: bool = False,
    network: str = "ethereum-mainnet",
    usd: str | None = None,
    identity: str = "move",
    symbol: str | None = None,
    direction: str | None = None,
    counterparty: str | None = None,
    wallet_id: str = EVM_ID,
    capability: str = "history.fungible",
) -> Candidate:
    return Candidate(
        tenant_id=TENANT,
        wallet_id=wallet_id,
        network=network,
        capability=capability,
        identity=identity,
        kind="asset",
        raw_amount=raw,
        is_native=native,
        direction=direction,
        counterparty_wallet_id=counterparty,
        quote_usd=None if usd is None else Decimal(usd),
        quote_valuation=None if usd is None else "valued",
        quote_quality=None if usd is None else "available",
        symbol=symbol,
    )


def _permission(
    change: str,
    *,
    raw: int,
    prior_known: bool = True,
    capability: str = "permissions.erc20",
    identity: str = "approval",
) -> Candidate:
    return Candidate(
        tenant_id=TENANT,
        wallet_id=EVM_ID,
        network="ethereum-mainnet",
        capability=capability,
        identity=identity,
        kind="permission",
        raw_amount=raw,
        permission_change=change,
        prior_known=prior_known,
        explicit_risk=change != "revoked",
    )


def _rpc_result(method: str, host: str) -> object:
    if method == "eth_chainId":
        return CHAIN_HEX.get(host, "0x1")
    if method == "eth_blockNumber":
        return "0x10"
    if method == "eth_getBalance":
        return "0x1"
    if method == "eth_getLogs":
        return []
    if method == "getGenesisHash":
        return GENESIS
    if method == "getBalance":
        return {"context": {"slot": 1}, "value": 1}
    if method == "getTokenAccountsByOwner":
        return {"value": []}
    if method == "getSignaturesForAddress":
        return []
    return []
