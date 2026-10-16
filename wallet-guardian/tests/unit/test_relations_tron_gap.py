"""TRON unread asset parts, and amounts that cannot be parsed.

Synthetic addresses and fixture transport only. No live network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from guardian.errors import SecurityError
from guardian.relations.evidence import evidence_for, seed_summary
from guardian.relations.fetch import Edge, read_tron
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


def _gap_parts(reason: str) -> list[dict[str, str]]:
    return [
        {"part": "trc20", "read_status": "gap", "reason": reason},
        _unread("trx"),
        _unread("trc10"),
    ]


def _page_one(sender: str, recipient: str, good: str, bad: str) -> list[dict]:
    return [
        r1.tron_row(sender, recipient, "9", good),
        r1.tron_row(sender, recipient, "0", "22" * 32),
        r1.tron_row(sender, recipient, "abc", bad),
    ]


def test_tron_second_page_failure_keeps_the_first_page_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A later trc20 page can fail and the rows already read still stay."""
    inbound_good = "11" * 32
    expand_good = "66" * 32
    cases = (
        ("connection_error", FixtureFailure("connection_error"), FixtureFailure("connection_error")),
        (
            "redirect_host",
            SecurityError("redirect_host", reason="redirect_host"),
            SecurityError("redirect_host", reason="redirect_host"),
        ),
        (
            "repeated_fingerprint",
            r1.tron_page([r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "4", "44" * 32)], "pageTwo"),
            r1.tron_page([r1.tron_row(r1.TRON_PAYER, r1.TRON_DEST, "4", "99" * 32)], "pageTwo"),
        ),
    )
    for reason, inbound_next, expand_next in cases:
        backend = _tron_backend(tmp_path, monkeypatch)
        backend.add(lambda call: "transactions/trc20" in call.url, [
            r1.tron_page(_page_one(r1.TRON_SEED, r1.TRON_PAYER, inbound_good, "33" * 32), "pageTwo"),
            inbound_next,
        ])
        code, body = r1.invoke(capsys, r1.inbound_argv(
            tmp_path / "monitor.json", "--yes", "--seed", r1.TRON_PAYER, "--network", "tron-mainnet",
        ), backend)
        assert code == 0, body
        read = body["reads"][0]
        assert read["read_status"] == "gap"
        assert read["reason"] == reason
        assert read["parts"] == _gap_parts(reason)
        assert read["ignored_zero_value"] == 1
        assert [row["address"] for row in body["counterparties"]] == [r1.TRON_SEED]
        assert body["counterparties"][0]["count"] == (2 if reason == "repeated_fingerprint" else 1)
        assert body["counterparties"][0]["assets"][0]["raw_amount"] == (
            "13" if reason == "repeated_fingerprint" else "9"
        )
        assert inbound_good in body["counterparties"][0]["tx_hashes"]
        assert "33" * 32 not in body["counterparties"][0]["tx_hashes"]
        assert "22" * 32 not in body["counterparties"][0]["tx_hashes"]
        assert len(backend.calls) == 2
        _assert_trc20_only(backend)

        backend = _tron_backend(tmp_path, monkeypatch)
        backend.add(lambda call: "transactions/trc20" in call.url, [
            r1.tron_page([
                r1.tron_row(r1.TRON_PAYER, r1.TRON_SEED, "5", "55" * 32),
                *_page_one(r1.TRON_PAYER, r1.TRON_DEST, expand_good, "88" * 32),
            ], "pageTwo"),
            expand_next,
        ])
        code, body = r1.invoke(capsys, r1.expand_argv(
            tmp_path / "monitor.json", "--yes", "--payer", r1.TRON_PAYER, "--network", "tron-mainnet",
            "--seed", r1.TRON_SEED,
        ), backend)
        assert code == 0, body
        read = body["reads"][0]
        assert read["read_status"] == "gap"
        assert read["reason"] == reason
        assert read["parts"] == _gap_parts(reason)
        assert read["ignored_zero_value"] == 1
        assert [row["address"] for row in body["co_recipients"]] == [r1.TRON_DEST]
        assert r1.TRON_SEED not in [row["address"] for row in body["co_recipients"]]
        assert body["co_recipients"][0]["count"] == (2 if reason == "repeated_fingerprint" else 1)
        assert body["co_recipients"][0]["assets"][0]["raw_amount"] == (
            "13" if reason == "repeated_fingerprint" else "9"
        )
        assert expand_good in body["co_recipients"][0]["tx_hashes"]
        assert "88" * 32 not in body["co_recipients"][0]["tx_hashes"]
        assert "22" * 32 not in body["co_recipients"][0]["tx_hashes"]
        assert len(backend.calls) == 2
        _assert_trc20_only(backend)


def _raise_history(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    class _Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def history(self, *_args: object, **_kwargs: object) -> None:
            raise exc

    monkeypatch.setattr(
        "guardian.relations.fetch._tron_reader",
        lambda: (_Client, lambda *_args: {}),
    )


def _read_raised(monkeypatch: pytest.MonkeyPatch, exc: BaseException):
    _raise_history(monkeypatch, exc)
    return read_tron(
        object(),
        "tron-grid",
        address=r1.TRON_PAYER,
        direction="to",
        network="tron-mainnet",
        page_cap=2,
        stop_timestamp=r1.STAMP - 1,
        clock=lambda: r1.CLOCK,
    )


def test_tron_history_exceptions_keep_the_reason_and_drop_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _read_raised(monkeypatch, BudgetExhausted("daily_cap"))
    assert budget.status == "gap"
    assert budget.reason == "daily_cap"
    assert budget.parts == _gap_parts("daily_cap")
    assert budget.edges == []

    redirected = _read_raised(monkeypatch, SecurityError("redirect_host", reason="redirect_host"))
    assert redirected.status == "gap"
    assert redirected.reason == "redirect_host"
    assert redirected.parts == _gap_parts("redirect_host")
    assert redirected.edges == []

    _raise_history(monkeypatch, SecurityError("endpoint_mismatch", reason="endpoint_mismatch"))
    with pytest.raises(SecurityError) as caught:
        read_tron(
            object(),
            "tron-grid",
            address=r1.TRON_PAYER,
            direction="to",
            network="tron-mainnet",
            page_cap=2,
            stop_timestamp=r1.STAMP - 1,
            clock=lambda: r1.CLOCK,
        )
    assert caught.value.reason == "endpoint_mismatch"


def test_tron_expand_with_rows_withholds_seed_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _tron_backend(tmp_path, monkeypatch)
    backend.add(lambda call: "transactions/trc20" in call.url, [
        r1.tron_page([
            r1.tron_row(r1.TRON_PAYER, r1.TRON_SEED, "5", "aa" * 32),
            r1.tron_row(r1.TRON_PAYER, r1.TRON_DEST, "5", "bb" * 32),
        ], "pageTwo"),
        r1.tron_page([r1.tron_row(r1.TRON_PAYER, r1.TRON_DEST, "7", "cc" * 32)], None),
    ])
    code, body = r1.invoke(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.TRON_PAYER, "--network", "tron-mainnet",
        "--seed", r1.TRON_SEED,
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["seed_receipts"] is None
    assert body["evidence_note"] == "seed_receipts_unknown"
    assert body["hub"] is None
    assert [row["address"] for row in body["co_recipients"]] == [r1.TRON_DEST]
    kinds = [item["kind"] for item in body["co_recipients"][0]["evidence"]]
    assert kinds == ["same_asset", "amount_band"]
    same = body["co_recipients"][0]["evidence"][0]
    assert same == {
        "assets": [{
            "contract": r1.TRON_TOKEN,
            "decimals": 6,
            "kind": "token",
            "network": "tron-mainnet",
            "symbol": "SYN",
        }],
        "kind": "same_asset",
    }
    _assert_trc20_only(backend)


def _evidence_edge(recipient: str, amount: int | None, digest: str) -> Edge:
    return Edge(
        "ethereum-mainnet", digest, 1, r1.STAMP, "blockscout-eth",
        r1.PAYER, recipient, False, False, "token", r1.TOKEN, "SYN", 6, amount,
    )


def test_amount_band_skips_a_null_raw_amount() -> None:
    summary = seed_summary([
        _evidence_edge(r1.SEED, 100, r1.tx(1)),
        _evidence_edge(r1.SEED, None, r1.tx(2)),
    ])
    assert summary["amounts"][("ethereum-mainnet", "token", r1.TOKEN)] == [100]
    built = evidence_for([
        _evidence_edge(r1.LEFT, None, r1.tx(3)),
        _evidence_edge(r1.LEFT, 100, r1.tx(4)),
    ], summary)
    band = next(item for item in built["evidence"] if item["kind"] == "amount_band")
    assert band["matched_raw_amounts"] == ["100"]
    assert band["low"] == "80"
    assert band["high"] == "120"
    only_null = evidence_for([_evidence_edge(r1.LEFT, None, r1.tx(5))], summary)
    assert all(item["kind"] != "amount_band" for item in only_null["evidence"])
    null_seed = seed_summary([_evidence_edge(r1.SEED, None, r1.tx(6))])
    assert null_seed["amounts"] == {}
    from_null_seed = evidence_for([_evidence_edge(r1.LEFT, 100, r1.tx(7))], null_seed)
    assert all(item["kind"] != "amount_band" for item in from_null_seed["evidence"])


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
