"""R-2 evidence, cross-network expand, private labels, and Markdown reports.

Synthetic addresses and fixture transport only. No live network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from guardian.errors import ConfigError
from guardian.relations.labels import SCHEMA, load_user_labels

_SPEC = importlib.util.spec_from_file_location(
    "relations_r1_helpers",
    Path(__file__).with_name("test_relations_r1.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
r1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r1)

ETH = "ethereum-mainnet"
BASE = "base-mainnet"
TRON = "tron-mainnet"
ETH_HOST = "eth.blockscout.example.invalid"
BASE_HOST = "base.blockscout.example.invalid"
TRON_HOST = "tron.example.invalid"
_EXTRA = (
    chr(0x5373) + chr(0x6642),
    chr(0x5168) + chr(0x93C8),
    chr(0x5B8C) + chr(0x6574),
    chr(0x80FD) + chr(0x627E) + chr(0x51FA),
)
_NEEDLES = r1._NEEDLES + _EXTRA
_KINDS = ("same_tx", "same_asset", "amount_band", "cadence")
_WEIGHTS = {"same_tx": 3, "same_asset": 1, "amount_band": 1, "cadence": 1}
_CLOSING = (
    "This report lists addresses and heuristic evidence only. "
    "Shared transactions, assets, amounts or timing do not show that two addresses "
    "are controlled by the same party."
)
_REGISTRY = {ETH: {"family": "evm"}, BASE: {"family": "evm"}, TRON: {"family": "tron"}}


def invoke(capsys, argv, backend):
    code = r1.main(argv, backend=backend, clock=lambda: r1.CLOCK)
    printed = capsys.readouterr().out
    body = json.loads(printed)
    scan(printed)
    if body.get("result_path"):
        scan(Path(body["result_path"]).read_text(encoding="utf-8"))
    if body.get("markdown_path"):
        scan(Path(body["markdown_path"]).read_text(encoding="utf-8"))
    for row in body.get("co_recipients") or []:
        assert_evidence(row)
    return code, body


def scan(text: str) -> None:
    lowered = text.lower()
    for needle in _NEEDLES:
        assert needle.lower() not in lowered


def assert_evidence(row: dict) -> None:
    evidence = row["evidence"]
    assert isinstance(evidence, list)
    kinds = [item["kind"] for item in evidence]
    assert kinds == sorted(kinds, key=_KINDS.index)
    assert len(kinds) == len(set(kinds))
    for item in evidence:
        reject_false_zero(item)
        assert item["kind"] in _WEIGHTS
    assert row["score"] == sum(_WEIGHTS[kind] for kind in kinds)
    assert 0 <= row["score"] <= 6
    if row["score"] >= 4:
        assert row["confidence"] == "high"
    elif row["score"] >= 2:
        assert row["confidence"] == "medium"
    else:
        assert row["confidence"] == "low"
    assert row["reasons"] == [] or len(row["reasons"]) == len(kinds)
    assert isinstance(row["reasons"], list)


def reject_false_zero(value) -> None:
    if isinstance(value, bool):
        assert value is not False
    elif isinstance(value, int):
        assert value != 0
    elif isinstance(value, dict):
        for item in value.values():
            reject_false_zero(item)
    elif isinstance(value, list):
        for item in value:
            reject_false_zero(item)


def write_labels(state: Path, payload, mode: int = 0o600) -> Path:
    path = state / "relations-labels.json"
    text = json.dumps(payload, separators=(",", ":"))
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if os.name == "posix":
        os.chmod(path, mode)
    return path


def label_row(address: str, network: str, label: str, kind: str) -> dict:
    return {"address": address, "kind": kind, "label": label, "network": network}


def labels_file(*rows: dict) -> dict:
    return {"labels": list(rows), "schema_version": SCHEMA}


def eth_base() -> tuple[dict, list, dict]:
    eth_sources, eth_routes, eth_secrets = r1.blockscout_profile(
        ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}",
    )
    base_sources, base_routes, base_secrets = r1.blockscout_profile(
        BASE, "blockscout-base", "BLOCKSCOUT_BASE_URL", f"https://{BASE_HOST}",
    )
    sources = {**eth_sources, **base_sources}
    secrets = {**eth_secrets, **base_secrets}
    return sources, eth_routes + base_routes, secrets


def three_chains() -> tuple[dict, list, dict]:
    sources, routes, secrets = eth_base()
    sources["tron-grid"] = r1._source("tron_grid", "TRONGRID_API_URL", "tron-public")
    routes = routes + [{"capability": "history.fungible", "network": TRON, "source_ids": ["tron-grid"]}]
    secrets["TRONGRID_API_URL"] = f"https://{TRON_HOST}"
    return sources, routes, secrets


def install_chains(tmp_path, monkeypatch, sources, routes, secrets, networks):
    return r1.install(
        tmp_path, monkeypatch, networks=networks, sources=sources, routes=routes, secrets=secrets,
        address=r1.SEED, family="evm", account_networks=[ETH],
    )


def route(backend, host: str, tail: str, response) -> None:
    def predicate(call) -> bool:
        parsed = urlsplit(call.url)
        return parsed.hostname == host and parsed.path.rstrip("/").endswith("/" + tail)

    backend.add(predicate, response)


def fill_host(backend, host: str, transfers, transactions) -> None:
    route(backend, host, "token-transfers", r1.scout(transfers))
    route(backend, host, "transactions", r1.scout(transactions))
    route(backend, host, "internal-transactions", r1.empty_scout())


def kinds_of(body: dict, address: str) -> list[str]:
    row = next(item for item in body["co_recipients"] if item["address"] == address)
    return [item["kind"] for item in row["evidence"]]


def evidence_of(body: dict, address: str, kind: str) -> dict:
    row = next(item for item in body["co_recipients"] if item["address"] == address)
    return next(item for item in row["evidence"] if item["kind"] == kind)


def token_asset() -> dict:
    return {
        "contract": r1.TOKEN,
        "decimals": 6,
        "kind": "token",
        "network": ETH,
        "symbol": "SYN",
    }


def test_inbound_reads_three_networks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = three_chains()
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH, BASE, TRON])
    fill_host(backend, ETH_HOST, [], [r1.native_item(r1.LEFT, r1.SEED, 100, r1.STAMP, r1.tx(3))])
    fill_host(backend, BASE_HOST, [r1.token_item(r1.LEFT, r1.SEED, 7, r1.STAMP + 10, r1.tx(4))], [])
    backend.add(
        lambda call: urlsplit(call.url).hostname == TRON_HOST,
        r1.tron_page([r1.tron_row(r1.TRON_PAYER, r1.TRON_SEED, "9", "ab" * 32)], None),
    )
    code, body = invoke(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.SEED, "--seed", r1.TRON_SEED,
    ), backend)
    assert code == 0, body
    rows = body["counterparties"]
    by_network = {row["network"]: row for row in rows}
    assert set(by_network) == {ETH, BASE, TRON}
    eth = by_network[ETH]
    assert eth["address"] == r1.LEFT
    assert eth["count"] == 1
    assert eth["assets"][0]["kind"] == "native"
    assert eth["assets"][0]["raw_amount"] == "100"
    assert eth["assets"][0]["decimals"] == 18
    assert eth["tx_hashes"] == [r1.tx(3)]
    assert eth["first_timestamp"] == r1.iso(r1.STAMP)
    assert eth["last_timestamp"] == r1.iso(r1.STAMP)
    base = by_network[BASE]
    assert base["address"] == r1.LEFT
    assert base["count"] == 1
    assert base["assets"][0]["kind"] == "token"
    assert base["assets"][0]["raw_amount"] == "7"
    assert base["assets"][0]["decimals"] == 6
    assert base["assets"][0]["contract"] == r1.TOKEN
    assert base["tx_hashes"] == [r1.tx(4)]
    tron = by_network[TRON]
    assert tron["address"] == r1.TRON_PAYER
    assert tron["count"] == 1
    assert tron["assets"][0]["raw_amount"] == "9"
    assert tron["assets"][0]["decimals"] == 6
    assert tron["assets"][0]["contract"] == r1.TRON_TOKEN
    assert tron["tx_hashes"] == ["ab" * 32]
    merged = body["merged_addresses"]
    assert len(merged) == 1
    assert merged[0]["address"] == r1.LEFT
    assert merged[0]["networks"] == [BASE, ETH]
    assert merged[0]["count"] == 2
    assert merged[0]["tx_hash_count"] == 2
    assert merged[0]["is_contract"] is False
    assert "score" not in merged[0]


def test_expand_ranks_evidence_and_keeps_one_network_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    addr_a, addr_b, addr_c, addr_d = r1.evm(11), r1.evm(12), r1.evm(13), r1.evm(14)
    moment = r1.STAMP
    transfers = [
        r1.token_item(r1.PAYER, r1.SEED, 100, moment, r1.tx(1)),
        r1.token_item(r1.PAYER, r1.SEED, 100, moment + 7200, r1.tx(2)),
        r1.token_item(r1.PAYER, addr_a, 10000, moment, r1.tx(1)),
        r1.token_item(r1.PAYER, addr_b, 100, moment + 100, r1.tx(3)),
        r1.token_item(r1.PAYER, addr_b, 100, moment + 100 + 7200, r1.tx(4)),
        r1.token_item(r1.PAYER, addr_c, 5000, moment + 200, r1.tx(5)),
    ]
    r1.fill_tails(backend, transfers, [r1.native_item(r1.PAYER, addr_d, 10**15, moment + 300, r1.tx(6))])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["network"] == ETH
    assert body["networks"] == [ETH]
    assert body["seed_receipts"]["count"] == 2
    assert body["seed_receipts"]["assets"][0]["symbol"] == "SYN"
    assert "evidence_note" not in body
    found = body["co_recipients"]
    assert [row["address"] for row in found] == [addr_a, addr_b, addr_c, addr_d]
    assert all(row["tx_hashes"] for row in found)
    by_address = {row["address"]: row for row in found}
    assert by_address[addr_a]["same_tx_as_seed"] is True
    assert by_address[addr_b]["same_tx_as_seed"] is False
    assert by_address[addr_a]["score"] == 4
    assert by_address[addr_a]["confidence"] == "high"
    assert by_address[addr_a]["evidence"] == [
        {"kind": "same_tx", "tx_hashes": [r1.tx(1)]},
        {"assets": [token_asset()], "kind": "same_asset"},
    ]
    assert by_address[addr_a]["reasons"] == [
        "paid in the same transaction as a seed address (1 transactions)",
        "received an asset the seed addresses also received from this payer",
    ]
    assert by_address[addr_b]["score"] == 3
    assert by_address[addr_b]["confidence"] == "medium"
    assert [item["kind"] for item in by_address[addr_b]["evidence"]] == ["same_asset", "amount_band", "cadence"]
    band = evidence_of(body, addr_b, "amount_band")
    assert band["low"] == "80"
    assert band["high"] == "120"
    assert band["decimals"] == 6
    assert band["matched_raw_amounts"] == ["100", "100"]
    assert band["asset"]["symbol"] == "SYN"
    cadence = evidence_of(body, addr_b, "cadence")
    assert cadence == {"kind": "cadence", "median_seconds": 7200, "seed_median_seconds": 7200}
    assert by_address[addr_b]["reasons"][1] == (
        "received 100 (decimals 6) of SYN, inside 80\u2013120 derived from the seed receipts"
    )
    assert by_address[addr_b]["reasons"][2] == "median interval 7200s is within 20% of the seed median 7200s"
    assert by_address[addr_c]["score"] == 1
    assert by_address[addr_c]["confidence"] == "low"
    assert [item["kind"] for item in by_address[addr_c]["evidence"]] == ["same_asset"]
    assert by_address[addr_d]["score"] == 0
    assert by_address[addr_d]["confidence"] == "low"
    assert by_address[addr_d]["evidence"] == []
    assert by_address[addr_d]["reasons"] == []


def test_expand_merges_evm_recipients_and_dedupes_the_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = eth_base()
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH, BASE])
    eth_left = r1.token_item(r1.PAYER, r1.LEFT, 2, r1.STAMP, r1.tx(2))
    eth_left["to"] = {"hash": r1.LEFT, "is_contract": False}
    base_left = r1.token_item(r1.PAYER, r1.LEFT, 4, r1.STAMP + 3600, r1.tx(4))
    base_left["to"] = {"hash": r1.LEFT, "is_contract": True}
    fill_host(backend, ETH_HOST, [
        r1.token_item(r1.PAYER, r1.SEED, 1, r1.STAMP, r1.tx(1)),
        eth_left,
        r1.token_item(r1.PAYER, r1.RIGHT, 3, r1.STAMP, r1.tx(3)),
    ], [])
    fill_host(backend, BASE_HOST, [
        base_left,
        r1.token_item(r1.PAYER, r1.OTHER, 5, r1.STAMP, r1.tx(5)),
    ], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--network", BASE,
        "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert "network" not in body
    assert body["networks"] == [ETH, BASE]
    assert body["recipient_count"] == 4
    assert body["recipient_count_complete"] is True
    hosts = {row["host"]: row["addresses"] for row in body["plan"]["queries"]}
    assert hosts == {ETH_HOST: [r1.PAYER], BASE_HOST: [r1.PAYER]}
    left_rows = [row for row in body["co_recipients"] if row["address"] == r1.LEFT]
    assert {row["network"] for row in left_rows} == {ETH, BASE}
    assert left_rows[0]["score"] == left_rows[1]["score"]
    assert left_rows[0]["evidence"] == left_rows[1]["evidence"]
    merged = body["merged_addresses"]
    assert len(merged) == 1
    assert merged[0]["address"] == r1.LEFT
    assert merged[0]["networks"] == [BASE, ETH]
    assert merged[0]["count"] == 2
    assert merged[0]["tx_hash_count"] == 2
    assert merged[0]["is_contract"] is True
    assert merged[0]["first_timestamp"] == r1.iso(r1.STAMP)
    assert merged[0]["last_timestamp"] == r1.iso(r1.STAMP + 3600)
    assert merged[0]["score"] == left_rows[0]["score"]
    assert merged[0]["confidence"] == left_rows[0]["confidence"]
    assert all(row["tx_hashes"] for row in body["co_recipients"])


def test_amount_band_includes_the_ends_and_skips_null_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    low, high, under, over, blank = (r1.evm(21 + index) for index in range(5))
    moment = r1.STAMP
    missing = r1.token_item(r1.PAYER, r1.SEED, 1, moment + 20, r1.tx(3))
    missing["total"].pop("value")
    blank_row = r1.token_item(r1.PAYER, blank, 1, moment + 40, r1.tx(8))
    blank_row["total"].pop("value")
    r1.fill_tails(backend, [
        r1.token_item(r1.PAYER, r1.SEED, 100, moment, r1.tx(1)),
        r1.token_item(r1.PAYER, r1.SEED, 200, moment + 10, r1.tx(2)),
        missing,
        r1.token_item(r1.PAYER, low, 80, moment + 30, r1.tx(4)),
        r1.token_item(r1.PAYER, high, 240, moment + 31, r1.tx(5)),
        r1.token_item(r1.PAYER, under, 79, moment + 32, r1.tx(6)),
        r1.token_item(r1.PAYER, over, 241, moment + 33, r1.tx(7)),
        blank_row,
    ], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert "amount_band" in kinds_of(body, low)
    assert "amount_band" in kinds_of(body, high)
    assert evidence_of(body, low, "amount_band")["low"] == "80"
    assert evidence_of(body, low, "amount_band")["high"] == "240"
    assert evidence_of(body, high, "amount_band")["matched_raw_amounts"] == ["240"]
    assert "amount_band" not in kinds_of(body, under)
    assert "amount_band" not in kinds_of(body, over)
    assert "amount_band" not in kinds_of(body, blank)
    assert "same_asset" in kinds_of(body, blank)
    assert "cadence" not in kinds_of(body, low)


def test_cadence_accepts_twenty_percent_and_rejects_the_neighbours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    one, exact, wide = r1.evm(31), r1.evm(32), r1.evm(33)
    moment = r1.STAMP
    r1.fill_tails(backend, [
        r1.token_item(r1.PAYER, r1.SEED, 100, moment, r1.tx(1)),
        r1.token_item(r1.PAYER, r1.SEED, 100, moment + 10000, r1.tx(2)),
        r1.token_item(r1.PAYER, one, 100, moment + 1, r1.tx(3)),
        r1.token_item(r1.PAYER, exact, 100, moment + 2, r1.tx(4)),
        r1.token_item(r1.PAYER, exact, 100, moment + 2 + 8000, r1.tx(5)),
        r1.token_item(r1.PAYER, wide, 100, moment + 3, r1.tx(6)),
        r1.token_item(r1.PAYER, wide, 100, moment + 3 + 7900, r1.tx(7)),
    ], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert "cadence" not in kinds_of(body, one)
    assert evidence_of(body, exact, "cadence") == {
        "kind": "cadence", "median_seconds": 8000, "seed_median_seconds": 10000,
    }
    assert "cadence" not in kinds_of(body, wide)
    for row in body["co_recipients"]:
        for item in row["evidence"]:
            assert False not in item.values()


def test_cadence_under_one_hour_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    short = r1.evm(34)
    moment = r1.STAMP
    r1.fill_tails(backend, [
        r1.token_item(r1.PAYER, r1.SEED, 100, moment, r1.tx(1)),
        r1.token_item(r1.PAYER, r1.SEED, 100, moment + 3000, r1.tx(2)),
        r1.token_item(r1.PAYER, short, 100, moment + 1, r1.tx(3)),
        r1.token_item(r1.PAYER, short, 100, moment + 1 + 3000, r1.tx(4)),
    ], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert "cadence" not in kinds_of(body, short)
    assert body["seed_receipts"]["count"] == 2


def test_cadence_is_absent_when_the_seed_has_one_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    other = r1.evm(35)
    moment = r1.STAMP
    r1.fill_tails(backend, [
        r1.token_item(r1.PAYER, r1.SEED, 100, moment, r1.tx(1)),
        r1.token_item(r1.PAYER, other, 100, moment, r1.tx(2)),
        r1.token_item(r1.PAYER, other, 100, moment + 7200, r1.tx(3)),
    ], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["seed_receipts"]["count"] == 1
    assert "evidence_note" not in body
    assert "cadence" not in kinds_of(body, other)


def test_no_seed_receipts_drop_every_evidence_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    r1.fill_tails(backend, [r1.token_item(r1.PAYER, r1.LEFT, 5, r1.STAMP, r1.tx(1))], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["seed_receipts"] == {"assets": [], "count": 0}
    assert body["evidence_note"] == "no_seed_receipts"
    row = body["co_recipients"][0]
    assert row["address"] == r1.LEFT
    assert row["score"] == 0
    assert row["confidence"] == "low"
    assert row["evidence"] == []
    assert row["reasons"] == []
    assert row["tx_hashes"] == [r1.tx(1)]


def test_family_mismatch_and_disabled_network_send_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    sources["tron-grid"] = r1._source("tron_grid", "TRONGRID_API_URL", "tron-public")
    routes = routes + [{"capability": "history.fungible", "network": TRON, "source_ids": ["tron-grid"]}]
    secrets["TRONGRID_API_URL"] = f"https://{TRON_HOST}"
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH, TRON])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--dry-run", "--payer", r1.PAYER, "--network", ETH, "--network", TRON,
        "--seed", r1.SEED,
    ), backend)
    assert code == 2, body
    assert body["error_type"] == "ConfigError"
    assert "network_family" in body["error"]
    assert backend.calls == []
    eth_sources, eth_routes, eth_secrets = r1.blockscout_profile(
        ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}",
    )
    backend = install_chains(tmp_path, monkeypatch, eth_sources, eth_routes, eth_secrets, [ETH])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", BASE, "--seed", r1.SEED,
    ), backend)
    assert code == 2, body
    assert "network_disabled" in body["error"]
    assert backend.calls == []


def test_an_incomplete_network_clears_the_count_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = eth_base()
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH, BASE])
    fill_host(backend, ETH_HOST, [r1.token_item(r1.PAYER, r1.LEFT, 1, r1.STAMP, r1.tx(1))], [])
    route(backend, BASE_HOST, "token-transfers", r1.scout([r1.token_item(r1.PAYER, r1.RIGHT, 1, r1.STAMP, r1.tx(2))]))
    route(backend, BASE_HOST, "transactions", r1.challenge())
    route(backend, BASE_HOST, "internal-transactions", r1.empty_scout())
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--network", BASE,
        "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["recipient_count_complete"] is False
    assert body["hub"] is None
    assert {row["address"] for row in body["co_recipients"]} == {r1.LEFT, r1.RIGHT}


def test_a_contract_on_any_network_is_a_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = eth_base()
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH, BASE])
    fill_host(backend, ETH_HOST, [], [r1.native_item(r1.PAYER, r1.LEFT, 4, r1.STAMP, r1.tx(1))])
    fill_host(backend, BASE_HOST, [], [r1.native_item(r1.PAYER, r1.RIGHT, 4, r1.STAMP, r1.tx(2), contract=True)])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--network", BASE,
        "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is True
    assert "contract" in body["hub_reasons"]
    assert "co_recipients" not in body


@pytest.mark.parametrize("kind", ["exchange", "protocol", "bridge"])
def test_hub_kinds_from_the_label_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    write_labels(tmp_path / "state", labels_file(label_row(r1.PAYER, ETH, "Listed Venue", kind)))
    r1.fill_tails(backend, [r1.token_item(r1.PAYER, r1.LEFT, 2, r1.STAMP, r1.tx(1))], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is True
    assert body["hub_reasons"] == ["label"]
    assert body["label_source"] == "user_private_file"
    assert body["label_kind"] == kind
    assert body["label"] == "Listed Venue"
    assert all("Listed Venue" not in call.url for call in backend.calls)


@pytest.mark.parametrize("kind", ["self", "other"])
def test_self_and_other_labels_do_not_make_a_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    write_labels(tmp_path / "state", labels_file(label_row(r1.PAYER, ETH, "Desk Mark", kind)))
    r1.fill_tails(backend, [r1.token_item(r1.PAYER, r1.LEFT, 2, r1.STAMP, r1.tx(1))], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is False
    assert "label" not in body["hub_reasons"]
    assert body["label_source"] == "user_private_file"
    assert body["label_kind"] == kind


def test_label_file_mode_symlink_schema_and_size_block_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    state = tmp_path / "state"
    path = write_labels(state, labels_file(), 0o644)
    assert stat.S_IMODE(path.stat().st_mode) & 0o077
    for extra in (("--dry-run",), ("--yes",)):
        code, body = invoke(capsys, r1.expand_argv(
            tmp_path / "monitor.json", *extra, "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
        ), backend)
        assert code == 2
        assert body["error"] == "labels_file_mode"
        assert backend.calls == []
    path.unlink()
    target = tmp_path / "outside-labels.json"
    target.write_text(json.dumps(labels_file()), encoding="utf-8")
    os.chmod(target, 0o600)
    (state / "relations-labels.json").symlink_to(target)
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--dry-run", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 2
    assert body["error"] == "labels_file"
    assert backend.calls == []
    (state / "relations-labels.json").unlink()
    write_labels(state, {"labels": [], "schema_version": "nope"})
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 2
    assert body["error"] == "labels_file"
    assert backend.calls == []
    (state / "relations-labels.json").unlink()
    huge = state / "relations-labels.json"
    descriptor = os.open(huge, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.write(descriptor, b" " * (1024 * 1024 + 1))
    os.close(descriptor)
    os.chmod(huge, 0o600)
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--dry-run", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 2
    assert body["error"] == "labels_file"
    assert backend.calls == []


def test_label_file_rejects_more_than_ten_thousand_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    state = tmp_path / "state"
    row = label_row(r1.SEED, "evm", "a", "other")
    write_labels(state, labels_file(*([row] * 10000)))
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--dry-run", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert backend.calls == []
    write_labels(state, labels_file(*([row] * 10001)))
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--dry-run", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 2
    assert body["error"] == "labels_file"
    assert backend.calls == []


def test_evm_wildcard_hits_both_networks_and_stays_out_of_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = eth_base()
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH, BASE])
    extra = r1.evm(99)
    write_labels(tmp_path / "state", labels_file(
        label_row(r1.LEFT, "evm", "Cross Desk", "other"),
        label_row(extra, "evm", "Unqueried Desk", "other"),
    ))
    fill_host(backend, ETH_HOST, [r1.token_item(r1.PAYER, r1.LEFT, 2, r1.STAMP, r1.tx(1))], [])
    fill_host(backend, BASE_HOST, [r1.token_item(r1.PAYER, r1.LEFT, 3, r1.STAMP, r1.tx(2))], [])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--network", BASE,
        "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert len(body["co_recipients"]) == 2
    assert {row["network"] for row in body["co_recipients"]} == {ETH, BASE}
    assert all(row["label"] == "Cross Desk" for row in body["co_recipients"])
    assert all(row["label_source"] == "user_private_file" for row in body["co_recipients"])
    assert all(row["label_kind"] == "other" for row in body["co_recipients"])
    rendered_plan = json.dumps(body["plan"])
    assert extra not in rendered_plan
    assert "Unqueried Desk" not in rendered_plan
    assert "Cross Desk" not in rendered_plan
    assert all("Unqueried Desk" not in call.url and extra not in call.url for call in backend.calls)


def test_user_label_keeps_the_static_label_beside_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    write_labels(tmp_path / "state", labels_file(label_row(r1.LEFT, ETH, "User Desk", "other")))

    def fake(network: str, address: str):
        if network == ETH and address.lower() == r1.LEFT.lower():
            return {"label": "Static Desk", "label_source": "static_local_registry"}
        return None

    monkeypatch.setattr("guardian.relations.report.lookup", fake)
    r1.fill_tails(backend, [], [r1.native_item(r1.LEFT, r1.SEED, 4, r1.STAMP, r1.tx(1))])
    code, body = invoke(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.SEED, "--network", ETH,
    ), backend)
    assert code == 0, body
    row = body["counterparties"][0]
    assert row["address"] == r1.LEFT
    assert row["label"] == "User Desk"
    assert row["label_source"] == "user_private_file"
    assert row["label_kind"] == "other"
    assert row["static_label"] == "Static Desk"


def test_markdown_report_and_its_absence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    rows = [
        r1.token_item(r1.PAYER, r1.evm(100 + index), 1, r1.STAMP, r1.tx(index))
        for index in range(25)
    ]
    rows.append(r1.token_item(r1.PAYER, r1.SEED, 1, r1.STAMP, r1.tx(900)))
    r1.fill_tails(backend, rows, [])
    code, plain = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, plain
    assert "markdown_path" not in plain
    assert list(Path(plain["result_path"]).parent.glob("*.md")) == []
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--markdown", "--max-nodes", "500",
        "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    md_path = Path(body["markdown_path"])
    assert md_path.suffix == ".md"
    assert stat.S_IMODE(md_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(md_path.parent.stat().st_mode) == 0o700
    text = md_path.read_text(encoding="utf-8")
    assert _CLOSING in text
    assert "read_status" in text
    assert "## Co-recipients" in text
    assert "graph LR" in text
    assert "http://" not in text
    assert "https://" not in text
    positions = [text.index(part) for part in (
        "# Relations expand",
        "Generated:",
        "## Window",
        "## Disclosure plan",
        "## Reads",
        "## Co-recipients",
        "graph LR",
        "## Merged addresses",
        "## Gaps",
        _CLOSING,
    )]
    assert positions == sorted(positions)
    block = text.split("graph LR", 1)[1].split("```", 1)[0]
    node_labels = re.findall(r'\["([^"]*)"\]', block)
    assert len(node_labels) <= min(20, 500) + 1 + 1
    assert len(node_labels) == 22
    allowed = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz\u2026: ")
    assert node_labels
    for label in node_labels:
        assert set(label) <= allowed
        assert "\u2026" in label


def test_dry_run_markdown_contains_the_plan_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = r1.blockscout_profile(ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", f"https://{ETH_HOST}")
    backend = install_chains(tmp_path, monkeypatch, sources, routes, secrets, [ETH])
    code, body = invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--dry-run", "--markdown", "--payer", r1.PAYER, "--network", ETH,
        "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert backend.calls == []
    text = Path(body["markdown_path"]).read_text(encoding="utf-8")
    assert "## Disclosure plan" in text
    assert r1.PAYER in text
    assert ETH_HOST in text
    assert "graph LR" not in text
    assert "## Reads" not in text
    assert "## Co-recipients" not in text
    assert "http://" not in text and "https://" not in text
    assert stat.S_IMODE(Path(body["markdown_path"]).stat().st_mode) == 0o600


def test_label_text_must_be_printable_and_short(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_labels(state, labels_file(label_row(r1.SEED, "evm", "a" * 64, "other")))
    found = load_user_labels(state, _REGISTRY)
    assert found[0]["label"] == "a" * 64
    write_labels(state, labels_file(label_row(r1.SEED, "evm", "a" * 65, "other")))
    with pytest.raises(ConfigError, match="labels_file"):
        load_user_labels(state, _REGISTRY)
    write_labels(state, labels_file(label_row(r1.SEED, "evm", "bad" + "\n" + "row", "other")))
    with pytest.raises(ConfigError, match="labels_file"):
        load_user_labels(state, _REGISTRY)
