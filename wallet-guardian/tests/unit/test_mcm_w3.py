"""Offline acceptance for MCM-W3. Synthetic fixtures only. No network."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from guardian.coverage import (
    CoverageObservation,
    build_coverage,
    portfolio_summary,
    render_coverage_md,
)
from guardian.events import (
    ChainEvent,
    FlowStatus,
    effects_from_tx,
    finality_for,
    flow_status,
    reconcile_events,
    rollback_to,
    success_alertable,
)
from guardian.jsonschema_lite import validate
from guardian.monitoring import CapabilityCycle, reconcile_capabilities
from guardian.network_registry import load_registry
from guardian.sources.capabilities import CAPABILITIES, load_source_capabilities
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
WALLET = "11111111-1111-4111-8111-111111111111"
NOW = "2026-10-08T00:00:00Z"
OLD_SUCCESS = "2020-01-01T00:00:00Z"
ADDR = "0x0000000000000000000000000000000000000001"
TOKEN = "0x0000000000000000000000000000000000000002"
HUGE = str(2**53 + 9)
HASH_A = "0x" + "aa" * 32
HASH_B = "0x" + "bb" * 32
TX = "0x" + "ab" * 32
TX_B = "0x" + "cd" * 32
TX_C = "0x" + "ef" * 32


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def _event(**overrides) -> ChainEvent:
    data = {
        "tenant_id": "local",
        "wallet_id": WALLET,
        "network": "ethereum-mainnet",
        "chain_uid": "eip155:1",
        "tx_id": TX,
        "sub_index": "0",
        "kind": "asset_in",
        "asset": "native",
        "raw_amount": "1000",
        "block_number": "10",
        "block_hash": HASH_A,
        "tx_status": "success",
        "finality": "observed",
        "source_ids": ("synthetic",),
        "observed_at": NOW,
    }
    data.update(overrides)
    return ChainEvent(**data)


def _tx(**overrides) -> dict:
    data = {
        "tenant_id": "local",
        "wallet_id": WALLET,
        "network": "ethereum-mainnet",
        "chain_uid": "eip155:1",
        "tx_id": TX,
        "block_number": "10",
        "block_hash": HASH_A,
        "observed_at": NOW,
        "source_ids": ["synthetic"],
        "finality": "observed",
        "effects": [
            {"kind": "asset_in", "asset": "native", "raw_amount": HUGE, "sub_index": "0"},
            {"kind": "approval_set", "asset": TOKEN, "raw_amount": "1", "sub_index": "1"},
        ],
    }
    data.update(overrides)
    return data


def _item(position: str, capability: str, amount: str, **extra) -> dict:
    row = {
        "position_id": position,
        "chain_id": 1,
        "network": "ethereum-mainnet",
        "wallet_label": "synthetic-a",
        "wallet_id": WALLET,
        "capability": capability,
        "raw_amount": amount,
        "quality": "COMPLETE",
        "fresh": True,
    }
    row.update(extra)
    return row


def _full_observation() -> CoverageObservation:
    return CoverageObservation(
        category="ok",
        pagination_complete=True,
        gap_detection_complete=True,
        gaps=(),
        cursor="10",
        declared="synthetic-scope",
        scope_from="0",
        scope_to="10",
        checkpoint_number="10",
        checkpoint_hash=HASH_A,
        checkpoint_status="observed",
        sources=("synthetic",),
    )


def _registry():
    return load_registry(ROOT / "config" / "chains")


def _catalog():
    return load_source_capabilities(ROOT / "config" / "source-capabilities")


def _schema():
    return json.loads((ROOT / "schemas" / "coverage.schema.json").read_text(encoding="utf-8"))


def test_mcm_w3_01_native_success_keeps_failed_token_stale():
    previous = [
        _item("native", "balances.native", "1000"),
        _item("token-a", "balances.fungible", "50"),
        _item("synthetic-a:lista:11111111-1111-4111-8111-111111111111", "positions", "7"),
    ]
    previous[2].pop("capability")
    incoming = [
        _item("native", "balances.native", "2000"),
        _item("token-a", "balances.fungible", "0"),
    ]
    rows = reconcile_capabilities(previous, incoming, {
        ("synthetic-a", 1, "balances.native"): "ok",
        ("synthetic-a", 1, "balances.fungible"): "error",
    })
    native = next(row for row in rows if row["position_id"] == "native")
    token = next(row for row in rows if row["position_id"] == "token-a")
    lista = next(row for row in rows if ":lista:" in row["position_id"])
    assert native["raw_amount"] == "2000"
    assert native["quality"] == "COMPLETE"
    assert token["raw_amount"] == "50"
    assert token["quality"] == "STALE"
    assert token["stale_reason"] == "capability_not_observed"
    assert token["retained_last_known_good"] is True
    assert lista["raw_amount"] == "7"
    assert lista["quality"] == "STALE"
    assert all(row["raw_amount"] != "0" for row in rows)


def test_mcm_w3_01_empty_error_and_partial_stay_distinct():
    previous = [
        _item("native", "balances.native", "1000"),
        _item("token-a", "balances.fungible", "50"),
        _item("token-b", "balances.fungible", "7"),
    ]

    def signature(category: str, incoming: list[dict]) -> list[tuple]:
        rows = reconcile_capabilities(previous, incoming, {
            ("synthetic-a", 1, "balances.native"): "ok",
            ("synthetic-a", 1, "balances.fungible"): category,
        })
        return sorted(
            (row["position_id"], row["raw_amount"], row.get("quality"), row.get("stale_reason"))
            for row in rows
            if row["position_id"] != "native"
        )

    empty = signature("empty", [_item("token-a", "balances.fungible", "1")])
    error = signature("error", [_item("token-a", "balances.fungible", "0")])
    partial = signature("partial", [_item("token-a", "balances.fungible", "60"), _item("token-c", "balances.fungible", "3")])
    assert empty == []
    assert error == [
        ("token-a", "50", "STALE", "capability_not_observed"),
        ("token-b", "7", "STALE", "capability_not_observed"),
    ]
    assert ("token-a", "60", "COMPLETE", None) in partial
    assert ("token-b", "7", "STALE", "partial_result") in partial
    assert ("token-c", "3", "COMPLETE", None) in partial
    assert len({tuple(empty), tuple(error), tuple(partial)}) == 3
    assert all(amount != "0" for _position, amount, _quality, _reason in error)


def test_mcm_w3_01_commit_retains_stale_token_amount(tmp_path):
    path = tmp_path / "monitor.sqlite"
    database = Database(path)
    try:
        first = CapabilityCycle(database, "local")
        first.finish(
            incoming=[
                _item("native", "balances.native", "1000"),
                _item("token-a", "balances.fungible", "50"),
            ],
            results={
                ("synthetic-a", 1, "balances.native"): "ok",
                ("synthetic-a", 1, "balances.fungible"): "ok",
            },
        )
    finally:
        database.close()
    database = Database(path)
    try:
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        second = CapabilityCycle(database, "local")
        result = second.finish(
            incoming=[
                _item("native", "balances.native", "2000"),
                _item("token-a", "balances.fungible", "0"),
            ],
            results={
                ("synthetic-a", 1, "balances.native"): "ok",
                ("synthetic-a", 1, "balances.fungible"): "error",
            },
        )
        token = next(row for row in result["items"] if row["position_id"] == "token-a")
        native = next(row for row in result["items"] if row["position_id"] == "native")
        assert native["raw_amount"] == "2000"
        assert token["raw_amount"] == "50"
        assert token["quality"] == "STALE"
        stored = database.capability_items("local")
        assert next(row for row in stored if row["position_id"] == "token-a")["raw_amount"] == "50"
    finally:
        database.close()


def test_mcm_w3_02_reorg_replaces_hash_without_duplicating_or_deleting():
    kept = _event(sub_index="0", kind="asset_in", block_hash=HASH_A)
    dropped = _event(sub_index="1", kind="asset_out", raw_amount="4", block_hash=HASH_A)
    reincluded = _event(sub_index="0", kind="asset_in", block_hash=HASH_B, finality="confirmed")
    canonical = {"10": HASH_B}
    forward = reconcile_events([kept, dropped], [reincluded], canonical)
    backward = reconcile_events([dropped, kept], [reincluded], canonical)
    assert forward.events == backward.events
    assert [event.identity for event in forward.events].count(kept.identity) == 1
    assert len(forward.events) == 2
    current = next(event for event in forward.events if event.identity == kept.identity)
    orphan = next(event for event in forward.events if event.identity == dropped.identity)
    assert current.block_hash == HASH_B
    assert current.finality == "confirmed"
    assert orphan.finality == "orphaned"
    assert orphan.raw_amount == "4"
    assert any(item.identity == dropped.identity and item.reason == "reorg" for item in forward.corrections)
    assert any(item.identity == kept.identity and item.previous_block_hash == HASH_A for item in forward.corrections)
    replay = reconcile_events(list(forward.events), list(forward.events), canonical)
    assert replay.added == ()
    assert replay.updated == ()
    assert [event.identity for event in replay.events] == [event.identity for event in forward.events]
    assert kept.identity == reincluded.identity
    assert kept.block_hash != reincluded.block_hash


def test_mcm_w3_02_out_of_order_matches_in_order_and_rollback_keeps_rows():
    low = _event(tx_id=TX, block_number="2", sub_index="0", block_hash=HASH_A)
    high = _event(tx_id=TX_B, block_number="4", sub_index="1", block_hash=HASH_B)
    canonical = {"2": HASH_A, "4": HASH_B}
    forward = reconcile_events([], [low, high], canonical)
    backward = reconcile_events([], [high, low], canonical)
    assert forward.events == backward.events
    assert [event.block_number for event in forward.events] == ["2", "4"]
    assert len(forward.added) == 2
    rolled = rollback_to(list(forward.events), "4")
    assert len(rolled.events) == 2
    assert next(event for event in rolled.events if event.block_number == "2").finality == "observed"
    assert next(event for event in rolled.events if event.block_number == "4").finality == "orphaned"
    assert any(item.reason == "rollback" for item in rolled.corrections)


def test_mcm_w3_02_crash_mid_commit_rolls_back_then_retry_is_once(tmp_path, monkeypatch):
    path = tmp_path / "monitor.sqlite"
    database = Database(path)
    seed = _event(tx_id=TX, block_number="10")
    cursor = {
        "wallet_id": WALLET,
        "network": "ethereum-mainnet",
        "capability": "history.native_external",
        "cursor": "10",
        "block_number": "10",
    }
    try:
        database.commit_capability_cycle(
            "local",
            events=[seed],
            capability_rows=[],
            cursors=[cursor],
            alerts=[{"dedup_key": "seed-alert", "kind": "seed"}],
        )
        original = Database._add_alert
        state = {"armed": True}

        def flaky(self, tenant_id, dedup_key, payload):
            if dedup_key == "crash-me" and state["armed"]:
                state["armed"] = False
                raise RuntimeError("injected crash")
            return original(self, tenant_id, dedup_key, payload)

        monkeypatch.setattr(Database, "_add_alert", flaky)
        crashed = _event(tx_id=TX_B, block_number="11", block_hash=HASH_B)
        next_cursor = dict(cursor, cursor="11", block_number="11")
        with pytest.raises(RuntimeError, match="injected crash"):
            database.commit_capability_cycle(
                "local",
                events=[crashed],
                capability_rows=[],
                cursors=[next_cursor],
                alerts=[{"dedup_key": "crash-me", "kind": "moved"}],
            )
        assert [row["identity"] for row in database.chain_events_for("local")] == [seed.identity]
        assert database.capability_cursors_for("local")[0]["cursor"] == "10"
        assert len(database.outbox_for("local")) == 1
        database.commit_capability_cycle(
            "local",
            events=[crashed],
            capability_rows=[],
            cursors=[next_cursor],
            alerts=[{"dedup_key": "crash-me", "kind": "moved"}],
        )
        database.commit_capability_cycle(
            "local",
            events=[crashed],
            capability_rows=[],
            cursors=[next_cursor],
            alerts=[{"dedup_key": "crash-me", "kind": "moved"}],
        )
        stored_events = database.chain_events_for("local")
        assert {row["tx_id"] for row in stored_events} == {TX, TX_B}
        assert len(stored_events) == 2
        assert database.capability_cursors_for("local")[0]["cursor"] == "11"
        assert len(database.outbox_for("local")) == 2
        with pytest.raises(ValueError, match="cursor_regressed"):
            database.commit_capability_cycle(
                "local",
                events=[],
                capability_rows=[],
                cursors=[dict(cursor, cursor="9", block_number="9")],
                alerts=[],
            )
        assert database.capability_cursors_for("local")[0]["cursor"] == "11"
    finally:
        database.close()
    reopened = Database(path)
    try:
        assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert len(reopened.chain_events_for("local")) == 2
        assert reopened.capability_cursors_for("local")[0]["cursor"] == "11"
        assert len(reopened.outbox_for("local")) == 2
        moved = reopened.rollback_reorg("local", network="ethereum-mainnet", block_number="11")
        assert moved["orphaned"] == 1
        assert moved["cursors"] == 1
        assert len(reopened.chain_events_for("local")) == 2
        assert any(row["finality"] == "orphaned" for row in reopened.chain_events_for("local"))
        assert reopened.capability_cursors_for("local")[0]["cursor"] == "10"
    finally:
        reopened.close()


def test_mcm_w3_03_coverage_denominator_lists_every_capability():
    registry = _registry()
    catalog = _catalog()
    schema = _schema()
    enabled = [(WALLET, "ethereum-mainnet"), (WALLET, "solana-mainnet")]
    rows = build_coverage(enabled, {}, catalog, registry, NOW)
    assert len(CAPABILITIES) == 30
    assert len(rows) == 60
    assert {(row["wallet_id"], row["network"], row["capability"]) for row in rows} == {
        (WALLET, network, capability)
        for network in ("ethereum-mainnet", "solana-mainnet")
        for capability in CAPABILITIES
    }
    for row in rows:
        validate(row, schema)
        assert row["probe"] != "live_pass"
        assert row["support"] != "implemented"
        assert row["quality"] == "unknown"
        assert "not_queried" in row["reason_codes"] or row["support"] in {"not_applicable", "not_observable"}
        assert row["scope"]["gap_detection_complete"] is False
        assert row["scope"]["gaps"] == []
        assert row["quality"] != "complete_in_declared_scope"
        assert row["price_quality"] == "unknown"
        assert row["price_quality"] != 0
    eth = next(row for row in rows if row["network"] == "ethereum-mainnet" and row["capability"] == "balances.native")
    sol = next(row for row in rows if row["network"] == "solana-mainnet" and row["capability"] == "balances.native")
    assert eth["chain_uid"] == "eip155:1"
    assert eth["checkpoint"]["kind"] == "block"
    assert sol["checkpoint"]["kind"] == "slot"
    assert sol["chain_uid"] == registry["solana-mainnet"]["identity"]["expected_value"]
    summary = portfolio_summary(rows)
    assert summary["complete"] is False
    assert summary["counts"]["unknown"] == 60
    rendered = render_coverage_md(rows)
    assert "沒有列出來的能力是 unknown，不是完整，也不是零。" in rendered
    assert "balances.native" in rendered


def test_mcm_w3_03_gaps_offchain_native_only_and_a_complete_row():
    registry = _registry()
    catalog = _catalog()
    schema = _schema()
    enabled = [(WALLET, "ethereum-mainnet")]
    gapped = build_coverage(enabled, {
        (WALLET, "ethereum-mainnet", "balances.native"): CoverageObservation(
            category="ok",
            pagination_complete=True,
            gap_detection_complete=False,
            gaps=(),
            declared="synthetic-scope",
            sources=("synthetic",),
        ),
    }, catalog, registry, NOW)
    native = next(row for row in gapped if row["capability"] == "balances.native")
    validate(native, schema)
    assert native["scope"]["gaps"] == []
    assert native["scope"]["gap_detection_complete"] is False
    assert native["quality"] == "partial"
    assert "gap_detection_incomplete" in native["reason_codes"]
    assert native["quality"] != "complete_in_declared_scope"

    observed = build_coverage(enabled, {
        (WALLET, "ethereum-mainnet", "balances.native"): _full_observation(),
        (WALLET, "ethereum-mainnet", "permits.offchain_unused"): _full_observation(),
    }, catalog, registry, NOW)
    complete = next(row for row in observed if row["capability"] == "balances.native")
    offchain = next(row for row in observed if row["capability"] == "permits.offchain_unused")
    validate(complete, schema)
    validate(offchain, schema)
    assert complete["quality"] == "complete_in_declared_scope"
    assert complete["support"] == "partial"
    assert complete["last_success_at"] == NOW
    assert complete["price_quality"] == "unknown"
    assert offchain["support"] == "not_observable"
    assert offchain["quality"] == "unknown"
    assert "not_observable" in offchain["reason_codes"]
    assert offchain["quality"] != "complete_in_declared_scope"
    summary = portfolio_summary(observed)
    assert summary["complete"] is False
    assert "native_only_not_portfolio" in summary["reason_codes"]

    errored = build_coverage(enabled, {
        (WALLET, "ethereum-mainnet", "balances.fungible"): CoverageObservation(
            category="error",
            last_success_at=OLD_SUCCESS,
            reason_codes=("read_error",),
        ),
    }, catalog, registry, NOW)
    fungible = next(row for row in errored if row["capability"] == "balances.fungible")
    assert fungible["quality"] == "error"
    assert fungible["last_success_at"] == OLD_SUCCESS
    assert fungible["last_success_at"] != NOW
    assert fungible["price_quality"] == "unknown"

    priced = build_coverage(enabled, {
        (WALLET, "ethereum-mainnet", "balances.native"): CoverageObservation(
            category="ok",
            pagination_complete=True,
            gap_detection_complete=True,
            declared="synthetic-scope",
            sources=("synthetic",),
            price_quality="available",
        ),
    }, catalog, registry, NOW)
    assert next(row for row in priced if row["capability"] == "balances.native")["price_quality"] == "available"


def test_mcm_w3_03_not_applicable_is_unknown_and_full_portfolio_can_complete():
    registry = _registry()

    class _Status:
        def __init__(self, support: str, probe: str = "untested") -> None:
            self.support = support
            self.probe = probe

    class _Adapter:
        def __init__(self) -> None:
            self.capabilities = {name: _Status("not_applicable") for name in CAPABILITIES}
            self.capabilities["balances.native"] = _Status("partial", "documented")
            self.capabilities["balances.fungible"] = _Status("partial", "documented")
            self.capabilities["permits.offchain_unused"] = _Status("partial", "documented")

        def declares_support(self, network: str, capability: str) -> str | None:
            if network != "ethereum-mainnet":
                return None
            return self.capabilities[capability].support

    class _Catalog:
        def __init__(self) -> None:
            self.adapters = {"fake": _Adapter()}

    full = _full_observation()
    rows = build_coverage([(WALLET, "ethereum-mainnet")], {
        (WALLET, "ethereum-mainnet", "balances.native"): full,
        (WALLET, "ethereum-mainnet", "balances.fungible"): full,
        (WALLET, "ethereum-mainnet", "permissions.cosmos_authz"): full,
        (WALLET, "ethereum-mainnet", "permits.offchain_unused"): full,
    }, _Catalog(), registry, NOW)
    native = next(row for row in rows if row["capability"] == "balances.native")
    fungible = next(row for row in rows if row["capability"] == "balances.fungible")
    cosmos = next(row for row in rows if row["capability"] == "permissions.cosmos_authz")
    offchain = next(row for row in rows if row["capability"] == "permits.offchain_unused")
    assert native["quality"] == "complete_in_declared_scope"
    assert fungible["quality"] == "complete_in_declared_scope"
    assert cosmos["support"] == "not_applicable"
    assert cosmos["quality"] == "unknown"
    assert "not_applicable" in cosmos["reason_codes"]
    assert offchain["quality"] == "unknown"
    assert offchain["support"] == "not_observable"
    summary = portfolio_summary(rows)
    assert summary["complete"] is True
    assert "native_only_not_portfolio" not in summary["reason_codes"]


def test_mcm_w3_03_error_keeps_previous_success_time_in_storage(tmp_path):
    database = Database(tmp_path / "monitor.sqlite")
    try:
        database.commit_capability_cycle(
            "local",
            events=[],
            capability_rows=[{
                "wallet_id": WALLET,
                "network": "ethereum-mainnet",
                "capability": "balances.fungible",
                "quality": "partial",
                "last_success_at": OLD_SUCCESS,
                "valid_until": None,
                "cursor": None,
                "payload": {"raw_amount": "50"},
            }],
            cursors=[],
            alerts=[],
        )
        database.commit_capability_cycle(
            "local",
            events=[],
            capability_rows=[{
                "wallet_id": WALLET,
                "network": "ethereum-mainnet",
                "capability": "balances.fungible",
                "quality": "error",
                "last_success_at": NOW,
                "valid_until": None,
                "cursor": None,
                "payload": {"raw_amount": "50", "last_success_at": NOW},
            }],
            cursors=[],
            alerts=[],
        )
        stored = database.capability_state_for("local")[0]
        assert stored["quality"] == "error"
        assert stored["last_success_at"] == OLD_SUCCESS
        assert stored["payload"]["raw_amount"] == "50"
        assert stored["payload"]["last_success_at"] == OLD_SUCCESS
    finally:
        database.close()


def test_mcm_w3_04_failed_transactions_keep_only_the_fee():
    failed_evm = effects_from_tx(_tx(status="0x0", gas_used="21000", effective_gas_price="1000000000"))
    kinds = [event.kind for event in failed_evm]
    assert "asset_in" not in kinds
    assert "approval_set" not in kinds
    assert kinds.count("fee") == 1
    assert kinds.count("tx_failed") == 1
    fee = next(event for event in failed_evm if event.kind == "fee")
    assert fee.raw_amount == str(21000 * 1_000_000_000)
    assert fee.tx_status == "failed"
    assert success_alertable(fee) is False
    assert all(success_alertable(event) is False for event in failed_evm)

    failed_solana = effects_from_tx(_tx(
        network="solana-mainnet",
        chain_uid="solana-mainnet",
        status=None,
        meta={"err": {"InstructionError": [0, "Custom"]}, "fee": 5000},
    ))
    solana_kinds = [event.kind for event in failed_solana]
    assert "asset_in" not in solana_kinds
    assert "approval_set" not in solana_kinds
    assert next(event for event in failed_solana if event.kind == "fee").raw_amount == "5000"
    assert any(event.kind == "tx_failed" for event in failed_solana)

    succeeded = effects_from_tx(_tx(status="0x1"))
    incoming = next(event for event in succeeded if event.kind == "asset_in")
    approval = next(event for event in succeeded if event.kind == "approval_set")
    assert incoming.raw_amount == HUGE
    assert incoming.raw_amount == "9007199254741001"
    assert int(incoming.raw_amount) == 2**53 + 9
    assert isinstance(incoming.raw_amount, str)
    assert incoming.tx_status == "success"
    assert success_alertable(incoming) is True
    assert success_alertable(approval) is True
    unknown = effects_from_tx(_tx())
    assert all(event.tx_status == "unknown" for event in unknown)
    assert any(event.kind == "asset_in" and event.raw_amount == HUGE for event in unknown)
    assert all(success_alertable(event) is False for event in unknown)
    with pytest.raises(ValueError, match="raw amount"):
        effects_from_tx(_tx(status="0x1", effects=[{"kind": "asset_in", "asset": "native", "raw_amount": 1.5, "sub_index": "0"}]))


def test_mcm_w3_04_finality_differs_by_network_and_bridge_completion_is_separate():
    registry = _registry()
    ethereum = dict(registry["ethereum-mainnet"])
    solana = dict(registry["solana-mainnet"])
    bitcoin = dict(registry["bitcoin-mainnet"])
    solana["commitment"] = "processed"
    bitcoin["confirmation_threshold"] = 6
    ethereum_with_depth = dict(ethereum)
    ethereum_with_depth["confirmation_threshold"] = 12
    assert finality_for("ethereum-mainnet", "100", "111", "50", ethereum_with_depth) == "observed"
    assert finality_for("ethereum-mainnet", "100", "130", "120", ethereum) == "finalized"
    assert finality_for("solana-mainnet", "100", "130", "120", solana) == "observed"
    assert finality_for("bitcoin-mainnet", "100", "130", "120", bitcoin) == "confirmed"
    assert finality_for("bitcoin-mainnet", "1", "1000", "1000", bitcoin) != "finalized"
    solana_final = dict(solana)
    solana_final["commitment"] = "finalized"
    assert finality_for("solana-mainnet", "100", "130", None, solana_final) == "finalized"
    assert finality_for("solana-mainnet", "100", "130", None, dict(registry["solana-mainnet"])) == "unknown"
    shallow = dict(bitcoin)
    shallow["confirmation_threshold"] = 6
    assert finality_for("bitcoin-mainnet", "100", "101", None, shallow) == "observed"

    source = _event(kind="asset_out", finality="finalized", tx_status="success", network="ethereum-mainnet")
    destination = _event(
        kind="asset_in", tx_status="success", network="arbitrum-one", chain_uid="eip155:42161", tx_id=TX_B,
    )
    refund = _event(kind="asset_in", tx_status="success", tx_id=TX_C, sub_index="2")
    assert flow_status(source, None) == FlowStatus.source_finalized
    assert flow_status(source, None) != FlowStatus.completed
    assert flow_status(source, destination) == FlowStatus.completed
    assert flow_status(source, None, refund_event=refund) == FlowStatus.refunded
    failed_destination = _event(
        kind="asset_in", tx_status="failed", network="arbitrum-one", chain_uid="eip155:42161", tx_id=TX_B,
    )
    assert flow_status(source, failed_destination) == FlowStatus.source_finalized
