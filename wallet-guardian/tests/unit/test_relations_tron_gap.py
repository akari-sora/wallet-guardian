"""TRON unread asset parts, and amounts that cannot be parsed.

Synthetic addresses and fixture transport only. No live network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from guardian.errors import SecurityError
from guardian.sources.budget import BudgetExhausted
from guardian.sources.fixtures import FixtureFailure

_SPEC = importlib.util.spec_from_file_location(
    "relations_r1_helpers",
    Path(__file__).with_name("test_relations_r1.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
r1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r1)

_UNREAD = "not_in_trc20_endpoint"
_BAD_AMOUNTS = ("1.5", "abc", -3, True)


def _tron_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> r1.PathBackend:
    sources = {"tron-grid": r1._source("tron_grid", "TRONGRID_API_URL", "tron-public")}
    routes = [{"network": "tron-mainnet", "capability": "history.fungible", "source_ids": ["tron-grid"]}]
    return r1.install(
        tmp_path, monkeypatch, networks=["tron-mainnet"], sources=sources, routes=routes,
        secrets={"TRONGRID_API_URL": "https://tron.example.invalid"},
        address=r1.TRON_PAYER, family="tron", account_networks=["tron-mainnet"],
    )


def _assert_trc20_only(backend: r1.PathBackend) -> None:
    assert backend.calls
    for call in backend.calls:
        split = urlsplit(call.url)
        assert split.hostname == "tron.example.invalid"
        assert split.path.rstrip("/").endswith("/transactions/trc20")


def _unread(part: str) -> dict[str, str]:
    return {"part": part, "read_status": "gap", "reason": _UNREAD}


def _blockscout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> r1.PathBackend:
    sources, routes, secrets = r1.blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    return r1.install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=r1.SEED, family="evm", account_networks=["ethereum-mainnet"],
    )


def test_tron_finished_walk_with_rows_is_gap_for_unread_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _tron_backend(tmp_path, monkeypatch)
    backend.add(lambda call: "transactions/trc20" in call.url, r1.tron_page([
        r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "9", "ab" * 32),
    ], None))
    code, body = r1.invoke(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.TRON_PAYER, "--network", "tron-mainnet",
    ), backend)
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == _UNREAD
    assert read["parts"] == [
        {"part": "trc20", "read_status": "complete", "reason": ""},
        _unread("trx"),
        _unread("trc10"),
    ]
    assert body["counterparties"] != []
    assert [row["address"] for row in body["counterparties"]] == [r1.TRON_SEED]
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "9"
    _assert_trc20_only(backend)


def test_tron_empty_trc20_is_not_an_empty_counterparty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _tron_backend(tmp_path, monkeypatch)
    backend.add(lambda call: "transactions/trc20" in call.url, r1.tron_page([], None))
    code, body = r1.invoke(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.TRON_PAYER, "--network", "tron-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "partial"
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == _UNREAD
    assert body["reads"][0]["parts"] == [
        {"part": "trc20", "read_status": "complete", "reason": ""},
        _unread("trx"),
        _unread("trc10"),
    ]
    assert body["counterparties"] is None
    _assert_trc20_only(backend)

    backend = _tron_backend(tmp_path, monkeypatch)
    backend.add(lambda call: "transactions/trc20" in call.url, r1.tron_page([], None))
    code, body = r1.invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.TRON_PAYER, "--network", "tron-mainnet",
        "--seed", r1.TRON_SEED,
    ), backend)
    assert code == 0, body
    assert body["status"] == "partial"
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == _UNREAD
    assert body["co_recipients"] is None
    assert body["recipient_count_complete"] is False
    assert body["recipient_count"] is None
    _assert_trc20_only(backend)


def test_tron_budget_redirect_and_walk_gap_keep_three_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    cases = (
        (BudgetExhausted("daily_cap"), "daily_cap"),
        (SecurityError("redirect_host", reason="redirect_host"), "redirect_host"),
        (FixtureFailure("connection_error"), "connection_error"),
    )
    for failure, reason in cases:
        backend = _tron_backend(tmp_path, monkeypatch)
        backend.add(lambda call: "transactions/trc20" in call.url, failure)
        code, body = r1.invoke(capsys, r1.inbound_argv(
            tmp_path / "monitor.json", "--yes", "--seed", r1.TRON_PAYER, "--network", "tron-mainnet",
        ), backend)
        assert code == 0, body
        read = body["reads"][0]
        assert read["read_status"] == "gap"
        assert read["reason"] == reason
        assert read["parts"] == [
            {"part": "trc20", "read_status": "gap", "reason": reason},
            _unread("trx"),
            _unread("trc10"),
        ]
        assert body["counterparties"] is None
        _assert_trc20_only(backend)


def test_blockscout_token_unparsed_amount_is_an_unreadable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _blockscout(tmp_path, monkeypatch)
    rows = [r1.token_item(r1.LEFT, r1.SEED, 4, r1.STAMP, r1.tx(1))]
    for index, value in enumerate(_BAD_AMOUNTS, start=2):
        item = r1.token_item(r1.OTHER, r1.SEED, 4, r1.STAMP, r1.tx(index))
        item["total"]["value"] = value
        rows.append(item)
    r1.fill_tails(backend, rows, [])
    code, body = r1.invoke(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "unreadable_row"
    parts = {part["part"]: part for part in read["parts"]}
    assert parts["token-transfers"] == {
        "part": "token-transfers", "read_status": "partial", "reason": "unreadable_row",
    }
    assert parts["transactions"]["read_status"] == "complete"
    assert parts["internal-transactions"]["read_status"] == "complete"
    assert [row["address"] for row in body["counterparties"]] == [r1.LEFT]
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "4"
    assert read["ignored_zero_value"] == 0


def test_blockscout_native_unparsed_amount_is_an_unreadable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _blockscout(tmp_path, monkeypatch)
    rows = [r1.native_item(r1.LEFT, r1.SEED, 4, r1.STAMP, r1.tx(1))]
    for index, value in enumerate(_BAD_AMOUNTS, start=2):
        item = r1.native_item(r1.OTHER, r1.SEED, 4, r1.STAMP, r1.tx(index))
        item["value"] = value
        rows.append(item)
    r1.fill_tails(backend, [], rows)
    code, body = r1.invoke(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "unreadable_row"
    parts = {part["part"]: part for part in read["parts"]}
    assert parts["transactions"] == {
        "part": "transactions", "read_status": "partial", "reason": "unreadable_row",
    }
    assert parts["token-transfers"]["read_status"] == "complete"
    assert parts["internal-transactions"]["read_status"] == "complete"
    assert [row["address"] for row in body["counterparties"]] == [r1.LEFT]
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "4"
    assert read["ignored_zero_value"] == 0


def test_tron_unparsed_amount_is_an_unreadable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _tron_backend(tmp_path, monkeypatch)
    rows = [r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "9", "aa" * 32)]
    for index, value in enumerate(_BAD_AMOUNTS, start=1):
        rows.append(r1.tron_row(r1.TRON_DEST, r1.TRON_PAYER, value, f"{index:064x}"))
    backend.add(lambda call: "transactions/trc20" in call.url, r1.tron_page(rows, None))
    code, body = r1.invoke(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.TRON_PAYER, "--network", "tron-mainnet",
    ), backend)
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == _UNREAD
    assert read["parts"] == [
        {"part": "trc20", "read_status": "partial", "reason": "unreadable_row"},
        _unread("trx"),
        _unread("trc10"),
    ]
    assert [row["address"] for row in body["counterparties"]] == [r1.TRON_SEED]
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "9"
    assert read["ignored_zero_value"] == 0
    _assert_trc20_only(backend)
