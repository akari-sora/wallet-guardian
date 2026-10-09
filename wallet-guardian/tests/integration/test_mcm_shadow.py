"""MCM-LIVE-L3. Private shadow tooling against synthetic fixtures.

Every test drives ``tools/mcm_shadow_round.py`` or ``guardian.cli.main``.
No test opens a socket. Nothing here reads a real legacy state or a real address.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from eth_utils import to_checksum_address

from guardian.configuration import file_permission_bits
from guardian.errors import ConfigError
from guardian.sources.fixtures import PreparedCall, RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TENANT = "local"
WALLET = "11111111-1111-4111-8111-111111111111"
LABEL = "alpha"
ADDRESS = to_checksum_address("0x" + ("0" * 39) + "1")
TX_MATCH = "0x" + ("11" * 32)
TX_MISS = "0x" + ("22" * 32)
TX_ONLY = "0x" + ("33" * 32)
OBSERVED = "2026-10-08T03:00:00Z"
SECRET = "synthetic-shadow-key"
POISON_SENDER = "0x" + ("11" * 20)
INCOMPLETE_SENDER = "0x1234"
BSC_HEIGHT = 100
BSC_HASH = "0x" + ("ab" * 32)
BSC_BAD = "0x" + ("cd" * 32)
EVENT_A = "receipt-synthetic-a"
EVENT_B = "receipt-synthetic-b"


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


def test_mcm_live_l3_01_legacy_backup_readonly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-LIVE-L3-01. Byte-exact snapshot, and tx-level compare instead of all-uncomparable."""
    tool = _tool()
    desk = _desk(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_bytes(_legacy_bytes(_legacy_document()))
    before = legacy_path.read_bytes()
    mtime = legacy_path.stat().st_mtime_ns
    _seed_compare(desk)
    code = tool.main(_argv(desk, legacy_path), backend=_backend(), sender=_Sink(), clock=lambda: 1_700_000_000.0)
    assert code == 0
    assert legacy_path.read_bytes() == before
    assert legacy_path.stat().st_mtime_ns == mtime
    meta_path = next((desk.evidence).glob("legacy-*.snapshot.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    snapshot = desk.evidence / meta["snapshot_name"]
    copied = snapshot.read_bytes()
    assert copied == before
    assert meta["sha256"] == hashlib.sha256(copied).hexdigest()
    assert meta["size"] == len(copied)
    assert meta["mtime_ns"] == mtime
    assert meta["taken_at"]
    assert meta["mode"] == 0o600
    bits = file_permission_bits(snapshot)
    if bits is not None:
        assert bits == 0o600
    compared = json.loads((desk.evidence / "commands" / "compare-legacy.stdout").read_text(encoding="utf-8"))
    counts = compared["counts"]
    assert counts["matched"] == 0
    assert counts["matched_tx_level"] >= 1
    assert counts["legacy_only"] >= 1
    assert counts["mcm_only"] >= 1
    matched = compared["matched_tx_level"]
    assert any(row["wallet_id"] == WALLET and row["kind"] == "approval" and row["position"] == "" for row in matched)
    assert any(row["kind"] == "transfer_out" and row["position"] == "" for row in compared["legacy_only"])
    assert any(row["seen_on"] == "2026-10-08" and row["wallet_id"] == WALLET for row in compared["legacy_coverage_windows"])
    blob = json.dumps(compared)
    assert TX_MATCH not in blob and TX_MISS not in blob and TX_ONLY not in blob
    assert ADDRESS.lower() not in blob.lower()
    budget = json.loads((desk.evidence / "budget.json").read_text(encoding="utf-8"))
    assert budget["etherscan_calls"] > 0
    assert isinstance(budget["kinza_eth_calls"], int)
    assert isinstance(budget["news_gets"], int)
    assert isinstance(budget["rpc_calls_by_network"], dict)
    meter = tool.CountingBackend(_backend())
    meter.send(_kinza_call())
    assert meter.snapshot()["kinza_eth_calls"] == 1

    raced = tmp_path / "legacy-race.json"
    raced.write_bytes(b'{"alerted":[]}\n')
    evidence = tmp_path / "evidence-race"
    original = Path.read_bytes

    def wrapped(self: Path, *args: object, **kwargs: object) -> bytes:
        data = original(self, *args, **kwargs)
        if self.resolve() == raced.resolve():
            wrapped.calls += 1
            if wrapped.calls == 1:
                raced.write_bytes(data + b"\n")
        return data

    wrapped.calls = 0
    monkeypatch.setattr(Path, "read_bytes", wrapped)
    refused = tool.main(
        ["--mcm-config", str(desk.config), "--legacy-state", str(raced), "--evidence-dir", str(evidence)],
        backend=_backend(),
        clock=lambda: 1_700_000_000.0,
    )
    assert refused != 0
    assert list(evidence.glob("legacy-*.snapshot")) == []


def test_mcm_live_l3_02_poison_notes_no_blanket_ignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-LIVE-L3-02. Repeated dust is suppressed. Other activity from that sender still alerts."""
    tool = _tool()
    desk = _desk(tmp_path, monkeypatch)
    legacy = {
        "poison_events": [
            _poison_event(dust=True, kind="transfer_in", raw_amount=1),
            _poison_event(dust=True, kind="transfer_in", raw_amount=1),
            _poison_event(dust=False, kind="transfer_in", raw_amount=10**18),
            _poison_event(dust=True, kind="approval", raw_amount=0),
            _poison_event(dust=True, kind="transfer_in", raw_amount=1, sender=POISON_SENDER[:-4] + "abcd"),
        ],
        "poison_notes": [
            {
                "chain": "bsc",
                "confidence": "low",
                "sender": POISON_SENDER,
                "source": "legacy_heuristic",
                "spam_type": "address_poisoning",
            },
            {
                "chain": "ethereum",
                "confidence": "low",
                "sender": INCOMPLETE_SENDER,
                "source": "legacy_operator_note",
                "spam_type": "address_poisoning",
            },
        ],
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    code = tool.main(_argv(desk, path), backend=_backend(), sender=_Sink(), clock=lambda: 1_700_000_000.0)
    assert code == 0
    review = json.loads((desk.evidence / "poison-review.json").read_text(encoding="utf-8"))
    notes = review["notes"]
    assert [note["slot"] for note in notes] == ["poison-note-1", "poison-note-2"]
    assert notes[0]["sender"] == POISON_SENDER
    assert notes[0]["source"] == "legacy_heuristic"
    assert notes[0]["confidence"] == "low"
    assert notes[0]["label"] == "address_poisoning"
    assert notes[1]["sender"] is None
    assert notes[1]["address_status"] == "incomplete"
    decisions = review["decisions"]
    assert decisions[0]["alert"] is True and decisions[0]["suppress"] is False
    assert decisions[1]["suppress"] is True and decisions[1]["alert"] is False
    assert decisions[1]["reason"] == "spam_suppressed_rule"
    assert decisions[2]["alert"] is True and decisions[2]["suppress"] is False
    assert decisions[3]["alert"] is True and decisions[3]["suppress"] is False
    assert decisions[3]["reason"] == "approval_not_suppressed"
    assert decisions[4]["alert"] is True and decisions[4]["suppress"] is False
    assert review["safe"] is False
    assert all(decision["safe"] is False for decision in decisions)
    assert all(note["safe"] is False and note["label"] != "safe" for note in notes)
    assert INCOMPLETE_SENDER not in json.dumps(review)


def test_mcm_live_l3_03_freeform_cursor_archive_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-LIVE-L3-03. Prose stays unmapped. A checked BSC cursor can rebuild without duplicates."""
    tool = _tool()
    desk = _desk(tmp_path, monkeypatch)
    legacy = {
        "bsc_cursor": {"chain": "bsc", "hash": BSC_HASH, "height": BSC_HEIGHT},
        "bsc_last_block": "last time we were near the head",
        "cursor_candidates": [
            {"chain": "bsc", "hash": BSC_BAD, "height": BSC_HEIGHT},
            "0xabc123",
        ],
        "freeform_cursors": ["around the previous bsc scan", "0xdead"],
        "overlap": 20,
        "rebuild_events": [
            {"block": 70, "event_id": "below-floor", "tx": "0x" + "01" * 32},
            {"block": 90, "event_id": EVENT_A, "tx": "0x" + "02" * 32},
            {"block": 110, "event_id": EVENT_A, "tx": "0x" + "03" * 32},
            {"block": 120, "event_id": EVENT_B, "tx": "0x" + "04" * 32},
        ],
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    backend = _backend({BSC_HEIGHT: BSC_HASH})
    code = tool.main(_argv(desk, path), backend=backend, sender=_Sink(), clock=lambda: 1_700_000_000.0)
    assert code == 0
    assert backend.block_methods == ["eth_getBlockByNumber", "eth_getBlockByNumber"]
    report = json.loads((desk.evidence / "cursor-report.json").read_text(encoding="utf-8"))
    archived = report["archive"]
    assert archived
    assert all(item["status"] == "unmapped" and item["derived_block"] is None and item["used"] is False for item in archived)
    assert any(item["reason"] in {"prose", "prose_or_short_hash"} for item in archived)
    assert any(item["reason"] == "short_hash" for item in archived)
    verified = report["verified"]
    assert verified["status"] == "verified" and verified["used"] is True
    assert verified["height"] == BSC_HEIGHT and verified["hash"] == BSC_HASH
    assert verified["chain"] == "bsc-mainnet"
    refused = [item for item in report["checks"] if item["reason"] == "hash_mismatch"]
    assert len(refused) == 1 and refused[0]["used"] is False and refused[0]["status"] == "refused"
    rebuild = report["rebuild"]
    assert rebuild["duplicates_dropped"] >= 1
    identities = [item["event_id"] for item in rebuild["events"]]
    assert identities == [EVENT_A, EVENT_B]
    assert "below-floor" not in identities
    assert len(identities) == len(set(identities))


def test_mcm_live_l3_04_no_shadow_send_and_critical_relay_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-LIVE-L3-04. No delivery. One relay record for one critical MCM-only finding."""
    tool = _tool()
    desk = _desk(tmp_path, monkeypatch)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"alerted": []}), encoding="utf-8")
    _seed_compare(desk, only_critical=True)
    sender = _Sink()
    quiet = tool.main(_argv(desk, path), backend=_backend(), sender=sender, clock=lambda: 1_700_000_000.0)
    assert quiet == 0
    assert sender.messages == []
    assert not (desk.evidence / "relay").exists()
    argv = (desk.evidence / "commands" / "run-once.argv").read_text(encoding="utf-8")
    assert "--no-send" in argv
    report = json.loads((desk.evidence / "commands" / "run-once.stdout").read_text(encoding="utf-8"))
    assert report["delivery_status"] == "no_send"
    assert report["sent"] == 0
    assert _outbox_statuses(desk).isdisjoint({"sent"})
    once = tool.main(
        [*_argv(desk, path), "--relay"],
        backend=_backend(),
        sender=sender,
        clock=lambda: 1_700_000_100.0,
    )
    assert once == 0
    assert sender.messages == []
    relays = _relay_records(desk.evidence)
    critical = [item for item in relays if item["kind"] == "approval_set" and item["wallet_id"] == WALLET]
    assert len(critical) == 1
    assert critical[0]["origin"] == "mcm_shadow"
    dedup = critical[0]["dedup_id"]
    again = tool.main(
        [*_argv(desk, path), "--relay"],
        backend=_backend(),
        sender=sender,
        clock=lambda: 1_700_000_200.0,
    )
    assert again == 0
    assert sender.messages == []
    second = [item for item in _relay_records(desk.evidence) if item["dedup_id"] == dedup]
    assert len(second) == 1
    assert _outbox_statuses(desk).isdisjoint({"sent"})
    inside = ROOT / "mcm-shadow-evidence-must-not"
    with pytest.raises(ConfigError, match="inside the repo"):
        tool.prepare_evidence_dir(inside)
    assert not inside.exists()
    loose = tmp_path / "loose"
    loose.mkdir()
    monkeypatch.setattr(tool, "file_permission_bits", lambda _path: 0o755)
    with pytest.raises(ConfigError, match="0700"):
        tool.prepare_evidence_dir(loose)


def test_mcm_live_l3_05_overlap_restart_reorg_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-LIVE-L3-05. Restart keeps the committed cursor. A replaced tx counts once. Drills stay out of the 24."""
    tool = _tool()
    desk = _desk(tmp_path, monkeypatch)
    legacy = {
        "bsc_cursor": {"chain": "bsc", "hash": BSC_HASH, "height": BSC_HEIGHT},
        "overlap": 20,
        "rebuild_events": [
            {"block": 90, "event_id": EVENT_A, "tx": "0x" + "02" * 32},
            {"block": 110, "event_id": EVENT_A, "tx": "0x" + "03" * 32},
            {"block": 120, "event_id": EVENT_B, "tx": "0x" + "04" * 32},
        ],
        "restart_fixture": {
            "committed_height": 100,
            "events": [
                {"block": 90, "event_id": EVENT_A, "tx": "0x" + "02" * 32},
                {"block": 110, "event_id": EVENT_A, "tx": "0x" + "03" * 32},
                {"block": 120, "event_id": EVENT_B, "tx": "0x" + "04" * 32},
            ],
            "partial_height": 180,
        },
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    code = tool.main(
        [*_argv(desk, path), "--drill", "restart"],
        backend=_backend({BSC_HEIGHT: BSC_HASH}),
        sender=_Sink(),
        clock=lambda: 1_700_000_000.0,
    )
    assert code == 0
    proof = json.loads((desk.evidence / "restart-proof.json").read_text(encoding="utf-8"))
    assert proof["counts_toward_24"] is False
    resumed = proof["resumed"]
    assert resumed["cursor"] == 100
    assert resumed["partial_height"] == 180
    assert resumed["adopted_partial"] is False
    assert resumed["skipped"] is False
    assert [item["event_id"] for item in resumed["events"]] == [EVENT_A, EVENT_B]
    assert resumed["duplicate_alerts"] >= 1
    rebuild = json.loads((desk.evidence / "cursor-report.json").read_text(encoding="utf-8"))["rebuild"]
    assert [item["event_id"] for item in rebuild["events"]] == [EVENT_A, EVENT_B]
    assert rebuild["duplicates_dropped"] >= 1
    decision = json.loads((desk.evidence / "cutover.json").read_text(encoding="utf-8"))
    assert decision["drills"][0]["drill"] == "restart"
    assert decision["drills"][0]["counts_toward_24"] is False
    assert decision["qualifying_windows"] == 0
    series = _windows(3)
    mixed = tool.evaluate_cutover([series[0], series[1], {"drill": "restart", "round_id": "drill-restart"}, series[2]])
    assert mixed["qualifying_windows"] == 3
    assert "drill-restart" not in mixed["sequence_round_ids"]
    assert mixed["drills"][0]["counts_toward_24"] is False
    failed = tool.evaluate_cutover([{"drill": "source_failure", "round_id": "source-failure"}])
    assert failed["qualifying_windows"] == 0
    assert failed["drills"][0]["qualifying"] is False
    assert failed["drills"][0]["reported_as"] == "fetch_error"
    assert "source_failure" in failed["reasons"]
    rolled = tool.evaluate_cutover([{"drill": "rollback", "round_id": "rollback", "watermark": "synthetic-watermark"}])
    assert rolled["phase"] == "rolled_back"
    assert rolled["qualifying_windows"] == 0
    assert rolled["drills"][0]["both_senders"] is False
    assert rolled["drills"][0]["new_owner_stopped"] is True
    assert rolled["drills"][0]["inflight_confirmed"] is True
    assert rolled["drills"][0]["watermark"] == "synthetic-watermark"
    assert rolled["drills"][0]["counts_toward_24"] is False


def test_mcm_live_l3_06_24_round_72_hour_gate(tmp_path: Path) -> None:
    """MCM-LIVE-L3-06. 24 consecutive 3h windows spanning 72h qualify. Shorter or broken series do not."""
    del tmp_path
    tool = _tool()
    full = tool.evaluate_cutover(_windows(24))
    assert full["phase"] == "qualified"
    assert full["qualified"] is True
    assert full["qualifying_windows"] == 24
    assert full["span_seconds"] >= 72 * 60 * 60
    short_count = tool.evaluate_cutover(_windows(23))
    assert short_count["qualified"] is False
    assert short_count["qualifying_windows"] == 23
    assert "qualifying_windows_below_24" in short_count["reasons"]
    compressed = tool.evaluate_cutover(_windows(24, hours=2.5))
    assert compressed["qualified"] is False
    assert compressed["qualifying_windows"] == 24
    assert compressed["span_seconds"] < 72 * 60 * 60
    assert "span_below_72h" in compressed["reasons"]
    gapped = _windows(24)
    shift = timedelta(hours=2)
    for row in gapped[12:]:
        row["started_at"] = _shift(row["started_at"], shift)
        row["finished_at"] = _shift(row["finished_at"], shift)
    broken = tool.evaluate_cutover(gapped)
    assert broken["qualified"] is False
    assert "sequence_gap" in broken["reasons"]
    assert broken["qualifying_windows"] < 24
    wide = _windows(1, hours=4)
    assert "window_not_incremental" in tool.evaluate_cutover(wide)["reasons"]
    for field, value, reason in (
        ("unexplained_legacy_only", 1, "unexplained_legacy_only"),
        ("unexplained_mcm_only", 1, "unexplained_mcm_only"),
        ("duplicates", 1, "duplicate"),
        ("secret_leaks", 1, "secret_leak"),
    ):
        dirty = _windows(1)
        dirty[0][field] = value
        result = tool.evaluate_cutover(dirty)
        assert result["qualified"] is False
        assert result["qualifying_windows"] == 0
        assert reason in result["reasons"]
    base = _windows(24) + [{"action": "cutover", "round_id": "cutover-step"}]
    assert tool.evaluate_cutover(base)["phase"] == "cutover"
    seven = tool.evaluate_cutover(base + _reverse(7))
    assert seven["phase"] == "reverse_shadow"
    assert seven["reverse_rounds"] == 7
    assert seven["reverse_complete"] is False
    assert seven["qualifying_windows"] == 24
    eight = tool.evaluate_cutover(base + _reverse(8))
    assert eight["phase"] == "done"
    assert eight["reverse_complete"] is True
    assert eight["reverse_rounds"] == 8
    assert eight["reverse_span_seconds"] >= 24 * 60 * 60
    brief = tool.evaluate_cutover(base + _reverse(8, hours=2))
    assert brief["reverse_complete"] is False
    assert brief["reverse_rounds"] == 8
    assert brief["reverse_span_seconds"] < 24 * 60 * 60


def test_mcm_live_l3_07_baseline_fixture_do_not_count_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-LIVE-L3-07. Round 0 and fixture-shaped rounds do not qualify. Gap names stay verbatim."""
    tool = _tool()
    baseline = _windows(1)[0]
    baseline["baseline"] = True
    baseline["index"] = 0
    baseline["round_id"] = "round-0"
    followed = _windows(23, start=datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
    for index, row in enumerate(followed, start=1):
        row["round_id"] = f"after-{index}"
    counted = tool.evaluate_cutover([baseline, *followed])
    assert counted["baseline_counted"] is False
    assert counted["qualified"] is False
    assert counted["qualifying_windows"] == 23
    assert "round-0" not in counted["sequence_round_ids"]
    assert "baseline_not_counted" in counted["reasons"]
    for marker in ("native_balance_only", "http_200_only", "no_events", "mock_fixture_pass", "placeholder_feed"):
        sample = _windows(1)
        sample[0]["markers"] = [marker]
        result = tool.evaluate_cutover(sample)
        assert result["qualified"] is False
        assert result["qualifying_windows"] == 0
        assert marker in result["reasons"]
    declared = _windows(1)
    declared[0]["declared_gaps"] = ["no_allowlisted_feed", "news_not_supplied", "credential_missing"]
    shown = tool.evaluate_cutover(declared)
    assert shown["qualifying_windows"] == 1
    blob = json.dumps(shown)
    for token in ("no_allowlisted_feed", "news_not_supplied", "credential_missing"):
        assert token in blob
    assert "no news" not in blob.lower()
    for item in shown["gap_evidence"]:
        assert item["status"] == item["token"]
        assert item["status"] != "ok"
    required = _windows(1)
    required[0]["required_gaps"] = ["credential_missing", "private_endpoint", "fetch_error", "budget_exhausted"]
    blocked = tool.evaluate_cutover(required)
    assert blocked["qualifying_windows"] == 0
    for token in ("credential_missing", "private_endpoint", "fetch_error", "budget_exhausted"):
        assert token in blocked["reasons"]
        assert token in json.dumps(blocked["gap_evidence"])
    desk = _desk(tmp_path, monkeypatch)
    path = tmp_path / "legacy.json"
    path.write_text("{}", encoding="utf-8")
    code = tool.main(_argv(desk, path), backend=_backend(), sender=_Sink(), clock=lambda: 1_700_000_000.0)
    assert code == 0
    live = json.loads((desk.evidence / "cutover.json").read_text(encoding="utf-8"))
    recorded = json.loads((desk.evidence / "round.json").read_text(encoding="utf-8"))
    assert live["qualified"] is False
    assert live["baseline_counted"] is False
    assert recorded["baseline"] is True
    assert recorded["index"] == 0
    assert recorded["live_pass"] is False
    assert recorded["live_verified"] is False
    assert recorded["fake_ip_reported_as"] == ["fetch_error", "private_endpoint"]
    assert "ok" not in recorded["fake_ip_reported_as"]
    assert "no news" not in json.dumps(live).lower()
    planted = tmp_path / "public-copy.txt"
    planted.write_text("leaked " + SECRET, encoding="utf-8")
    leaks = tool.scan_leaks(
        [planted],
        evidence_dir=desk.evidence,
        secrets=[SECRET],
        addresses=[ADDRESS],
    )
    assert len(leaks) == 1 and leaks[0]["kind"] == "secret"
    assert SECRET not in json.dumps(leaks)
    leaked = _windows(1)
    leaked[0]["secret_leaks"] = 1
    assert tool.evaluate_cutover(leaked)["qualified"] is False
    assert "secret_leak" in tool.evaluate_cutover(leaked)["reasons"]


def test_mcm_live_l3_08_frozen_scope_no_denominator_shrink(tmp_path: Path) -> None:
    """MCM-LIVE-L3-08. Shrinking the frozen scope resets the count and keeps earlier evidence."""
    del tmp_path
    tool = _tool()
    base = _scope()
    original = _windows(5, scope=base)
    shrunk = _windows(1, scope=base, start=datetime(2026, 1, 1, 15, tzinfo=timezone.utc))
    shrunk[0]["round_id"] = "shrunk"
    shrunk[0]["scope"]["wallets"] = base["wallets"][:1]
    lost_wallet = tool.evaluate_cutover([*original, *shrunk])
    assert lost_wallet["qualified"] is False
    assert lost_wallet["qualifying_windows"] == 1
    assert "scope_changed" in lost_wallet["reasons"]
    assert lost_wallet["resets"][0]["reason"] == "scope_changed"
    assert "w0" in lost_wallet["history"]
    assert len(lost_wallet["history"]) == 6
    lost_network = _windows(1, scope=base, start=datetime(2026, 1, 1, 15, tzinfo=timezone.utc))
    lost_network[0]["scope"]["networks"] = ["ethereum-mainnet"]
    assert "scope_changed" in tool.evaluate_cutover([*_windows(2, scope=base), *lost_network])["reasons"]
    lost_required = _windows(1, scope=base, start=datetime(2026, 1, 1, 6, tzinfo=timezone.utc))
    lost_required[0]["scope"]["required"] = ["balances.native"]
    assert "scope_changed" in tool.evaluate_cutover([*_windows(2, scope=base), *lost_required])["reasons"]
    changed_config = _windows(1, scope=base, start=datetime(2026, 1, 1, 6, tzinfo=timezone.utc))
    changed_config[0]["scope"]["config_hash"] = "config-hash-changed"
    assert "scope_changed" in tool.evaluate_cutover([*_windows(2, scope=base), *changed_config])["reasons"]
    changed_src = _windows(1, scope=base, start=datetime(2026, 1, 1, 6, tzinfo=timezone.utc))
    changed_src[0]["scope"]["content_hash"] = "content-hash-changed"
    assert "scope_changed" in tool.evaluate_cutover([*_windows(2, scope=base), *changed_src])["reasons"]
    docs = _windows(4, scope=base)
    docs[-1]["scope"]["docs_only"] = True
    docs[-1]["scope"]["docs_sha"] = "docs-only-change"
    kept = tool.evaluate_cutover(docs)
    assert kept["qualifying_windows"] == 4
    assert "scope_changed" not in kept["reasons"]
    assert kept["history"] == ["w0", "w1", "w2", "w3"]


def test_mcm_live_l3_09_missing_evidence_fails_closed() -> None:
    """MCM-LIVE-L3-07. Missing evidence is not zero: the window does not qualify."""
    tool = _tool()
    good = tool.evaluate_cutover(_windows(3))
    assert good["qualifying_windows"] == 3
    for field in ("duplicates", "secret_leaks", "unexplained_legacy_only", "unexplained_mcm_only", "no_send"):
        rows = _windows(3)
        del rows[1][field]
        result = tool.evaluate_cutover(rows)
        assert f"evidence_missing:{field}" in result["reasons"], field
        assert result["qualifying_windows"] == 1, field
    for bad in (None, "0", True, -1):
        rows = _windows(2)
        rows[1]["secret_leaks"] = bad
        assert "evidence_missing:secret_leaks" in tool.evaluate_cutover(rows)["reasons"]
    rows = _windows(2)
    rows[1]["no_send"] = None
    unproven = tool.evaluate_cutover(rows)
    assert unproven["qualifying_windows"] == 0
    assert "evidence_missing:no_send" in unproven["reasons"]
    rows = _windows(2)
    del rows[1]["scope"]["content_hash"]
    assert "scope_missing" in tool.evaluate_cutover(rows)["reasons"]
    rows = [*_windows(24), {"action": "cutover"}, *_reverse(8, hours=3)]
    assert tool.evaluate_cutover(rows)["phase"] == "done"
    rows = [*_windows(24), {"action": "cutover"}, *_reverse(8, hours=3)]
    del rows[-1]["no_send"]
    reverse = tool.evaluate_cutover(rows)
    assert reverse["phase"] != "done"
    assert "evidence_missing:no_send" in reverse["reasons"]


def test_mcm_live_l3_10_scope_identity_is_content_hash(tmp_path: Path) -> None:
    """MCM-LIVE-L3-08. Scope identity hashes src/guardian and config/, not a commit SHA."""
    tool = _tool()
    root = tmp_path / "repo"
    for rel, text in {
        "src/guardian/a.py": "x = 1\n",
        "config/protocols/p.json": "{}\n",
        "docs/readme.md": "doc\n",
        "tests/t.py": "t\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    first = tool.content_hash(root)
    assert len(first) == 64
    (root / "docs/readme.md").write_text("doc changed\n", encoding="utf-8")
    (root / "tests/t.py").write_text("t changed\n", encoding="utf-8")
    (root / "src/guardian/__pycache__").mkdir()
    (root / "src/guardian/__pycache__/a.cpython-313.pyc").write_bytes(b"cache")
    assert tool.content_hash(root) == first
    (root / "config/protocols/p.json").write_text('{"x": 1}\n', encoding="utf-8")
    changed_config = tool.content_hash(root)
    assert changed_config != first
    (root / "src/guardian/a.py").write_text("x = 2\n", encoding="utf-8")
    assert tool.content_hash(root) not in {first, changed_config}


class _Sink:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, message: dict) -> None:
        self.messages.append(dict(message))


class _Desk:
    def __init__(self, config: Path, state: Path, evidence: Path) -> None:
        self.config = config
        self.state = state
        self.evidence = evidence


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


def _chain_backend():
    name = "mcm_l0_shadow_backend"
    if name in sys.modules:
        return sys.modules[name].ChainBackend
    path = Path(__file__).with_name("test_mcm_live_entrypoint.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.ChainBackend


def _backend(blocks: dict[int, str] | None = None):
    base = _chain_backend()

    class ShadowBackend(base):
        def __init__(self) -> None:
            super().__init__()
            self.blocks = dict(blocks or {})
            self.block_methods: list[str] = []

        def send(self, call: object) -> RawResponse:
            body = getattr(call, "body", b"") or b""
            if body:
                payload = json.loads(body.decode("utf-8"))
                if payload.get("method") == "eth_getBlockByNumber":
                    self.block_methods.append(str(payload["method"]))
                    height = int(str(payload["params"][0]), 16)
                    found = self.blocks.get(height)
                    result = None if found is None else {"hash": found, "number": hex(height)}
                    encoded = json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode()
                    return RawResponse(200, {}, encoded)
            return super().send(call)

    return ShadowBackend()


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Desk:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["plugins"] = ["aave-v3"]
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    accounts = {
        "accounts": [
            {
                "address": ADDRESS,
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
        "ETHERSCAN_API_KEY": SECRET,
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
    return _Desk(config, state, evidence)


def _argv(desk: _Desk, legacy: Path) -> list[str]:
    return [
        "--mcm-config", str(desk.config),
        "--legacy-state", str(legacy),
        "--evidence-dir", str(desk.evidence),
    ]


def _legacy_document() -> dict:
    return {
        "alerted": [
            f"eth:{LABEL}:{TX_MATCH} approval for synthetic",
            f"eth:{LABEL}:{TX_MISS} transfer_out block 99999",
        ],
        "latest_seen": {f"{LABEL}_eth_native": "2026-10-08"},
        "runs": [{"at": OBSERVED, "eth_since_0000": "scan"}],
    }


def _legacy_bytes(document: dict) -> bytes:
    return (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")


def _seed_compare(desk: _Desk, *, only_critical: bool = False) -> None:
    database = Database(desk.state / "monitor.sqlite")
    try:
        database.upsert_tenant(TENANT, TENANT)
        _insert_event(database, TX_MATCH if not only_critical else "0x" + "44" * 32, "approval_set")
        if not only_critical:
            _insert_event(database, TX_ONLY, "asset_in")
    finally:
        database.close()


def _insert_event(database: Database, tx: str, kind: str) -> None:
    payload = {
        "asset": "evm-native:1",
        "finality": "confirmed",
        "kind": kind,
        "network": "ethereum-mainnet",
        "observed_at": OBSERVED,
        "sub_index": "1",
        "tx_id": tx,
        "wallet_id": WALLET,
    }
    identity = "|".join((WALLET, "ethereum-mainnet", tx, "1", kind, "evm-native:1"))
    database.conn.execute(
        """INSERT INTO chain_events(
               tenant_id, identity, wallet_id, network, block_number, block_hash, finality, tx_status, payload
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (TENANT, identity, WALLET, "ethereum-mainnet", "10", None, "confirmed", "success", json.dumps(payload)),
    )
    database.conn.commit()


def _poison_event(*, dust: bool, kind: str, raw_amount: int, sender: str = POISON_SENDER) -> dict:
    return {
        "chain": "bsc",
        "dust": dust,
        "kind": kind,
        "raw_amount": raw_amount,
        "sender": sender,
        "spam_type": "address_poisoning",
    }


def _outbox_statuses(desk: _Desk) -> set[str]:
    database = Database(desk.state / "monitor.sqlite")
    try:
        return {str(row["status"]) for row in database.outbox_for(TENANT)}
    finally:
        database.close()


def _relay_records(evidence: Path) -> list[dict]:
    directory = evidence / "relay"
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def _kinza_call() -> PreparedCall:
    from guardian.protocols.pins import KINZA_POOLS

    body = json.dumps({
        "id": 1,
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"data": "0x", "to": KINZA_POOLS["bsc-mainnet"]}, "latest"],
    }).encode("utf-8")
    return PreparedCall("rpc-bsc-mainnet", "POST", "https://bsc.example.invalid/rpc", body, "json-rpc")


def _scope() -> dict:
    return {
        "config_hash": "config-hash-frozen",
        "docs_only": False,
        "networks": ["bsc-mainnet", "ethereum-mainnet"],
        "plugins": ["aave-v3"],
        "required": ["balances.native", "history.fungible"],
        "content_hash": "content-hash-frozen",
        "wallets": ["wallet-a", "wallet-b"],
    }


def _windows(
    count: int,
    *,
    hours: float = 3,
    start: datetime | None = None,
    scope: dict | None = None,
) -> list[dict]:
    origin = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    frozen = scope or _scope()
    rows = []
    for index in range(count):
        begin = origin + timedelta(hours=hours * index)
        end = begin + timedelta(hours=hours)
        rows.append({
            "baseline": False,
            "declared_gaps": [],
            "duplicates": 0,
            "finished_at": _stamp(end),
            "fixture": False,
            "index": index + 1,
            "markers": [],
            "no_send": True,
            "required_fresh": True,
            "required_gaps": [],
            "round_id": f"w{index}",
            "scope": json.loads(json.dumps(frozen)),
            "secret_leaks": 0,
            "started_at": _stamp(begin),
            "unexplained_legacy_only": 0,
            "unexplained_mcm_only": 0,
        })
    return rows


def _reverse(count: int, *, hours: float = 3) -> list[dict]:
    origin = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        begin = origin + timedelta(hours=hours * index)
        end = begin + timedelta(hours=hours)
        rows.append({
            "finished_at": _stamp(end),
            "index": 100 + index,
            "no_send": True,
            "reverse_shadow": True,
            "round_id": f"rev{index}",
            "started_at": _stamp(begin),
        })
    return rows


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift(stamp: str, delta: timedelta) -> str:
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return _stamp(parsed + delta)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
