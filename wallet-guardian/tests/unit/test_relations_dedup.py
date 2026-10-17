"""Relations dedup. Fixture transport only. No live network."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from guardian.relations.markdown import render_markdown
from guardian.sources.fixtures import FixtureMiss

_SPEC = importlib.util.spec_from_file_location(
    "relations_dedup_helpers",
    Path(__file__).with_name("test_relations_r1.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
r1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r1)

TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CURSOR = {"block_number": 5, "index": 1}
INSIDE = r1.STAMP


def _parts(read: dict) -> dict:
    return {part["part"]: part for part in read["parts"]}


def _tail(call, name: str) -> bool:
    return urlsplit(call.url).path.rstrip("/").endswith("/" + name)


def _blockscout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> r1.PathBackend:
    sources, routes, secrets = r1.blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    return r1.install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=r1.SEED, family="evm", account_networks=["ethereum-mainnet"],
    )


def _tron(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> r1.PathBackend:
    sources = {"tron-grid": r1._source("tron_grid", "TRONGRID_API_URL", "tron-public")}
    routes = [{"network": "tron-mainnet", "capability": "history.fungible", "source_ids": ["tron-grid"]}]
    return r1.install(
        tmp_path, monkeypatch, networks=["tron-mainnet"], sources=sources, routes=routes,
        secrets={"TRONGRID_API_URL": "https://tron.example.invalid"},
        address=r1.TRON_PAYER, family="tron", account_networks=["tron-mainnet"],
    )


def _nodereal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> r1.PathBackend:
    sources = {
        "nodereal-bsc": r1._source(
            "nodereal_transfers", "NODEREAL_BSC_URL", "nodereal-bsc", "NODEREAL_API_KEY",
        ),
    }
    routes = [{"network": "bsc-mainnet", "capability": "history.native_external", "source_ids": ["nodereal-bsc"]}]
    return r1.install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes,
        secrets={
            "NODEREAL_API_KEY": "synthetic-nodereal-key-dedup",
            "NODEREAL_BSC_URL": "https://nodereal.example.invalid",
        },
        address=r1.MONITORED, family="evm", account_networks=["bsc-mainnet"],
    )


def _club(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> r1.PathBackend:
    sources = {"nodereal-bsc": r1._source("nodereal_transfers", "NODEREAL_BSC_URL", "nodereal-bsc")}
    routes = [{"network": "bsc-mainnet", "capability": "history.native_external", "source_ids": ["nodereal-bsc"]}]
    backend = r1.install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes,
        secrets={"NODEREAL_BSC_URL": "https://bsc.example.invalid"},
        address=r1.MONITORED, family="evm", account_networks=["bsc-mainnet"],
    )
    r1.allow_fixture_hosts(monkeypatch, "rpc-bsc.48.club")
    return backend


def _nr(sender: str, recipient: str, number: int, value: str) -> dict:
    return {
        "blockNum": hex(5),
        "contractAddress": r1.TOKEN,
        "from": sender,
        "hash": r1.tx(number),
        "isError": "0",
        "metadata": {"blockTimestamp": INSIDE},
        "to": recipient,
        "tokenDecimal": "6",
        "value": value,
    }


def _club_log(sender: str, recipient: str, digest: str, log_index: str) -> dict:
    return {
        "address": r1.TOKEN,
        "blockNumber": hex(8),
        "data": "0x" + f"{4:064x}",
        "logIndex": log_index,
        "topics": [
            TOPIC,
            "0x" + sender[2:].rjust(64, "0"),
            "0x" + recipient[2:].rjust(64, "0"),
        ],
        "transactionHash": digest,
    }


def _internal(sender: str, recipient: str, value: int, digest: str, index: int) -> dict:
    row = r1.native_item(sender, recipient, value, INSIDE, digest)
    row["index"] = index
    return row


def _token_at(sender: str, recipient: str, value: int, digest: str, log_index: int) -> dict:
    row = r1.token_item(sender, recipient, value, INSIDE, digest)
    row["logIndex"] = log_index
    return row


def _nr_native(sender: str, recipient: str, number: int, value: str, **extra: object) -> dict:
    row = {
        "blockNum": hex(5),
        "from": sender,
        "hash": r1.tx(number),
        "isError": "0",
        "metadata": {"blockTimestamp": INSIDE},
        "to": recipient,
        "value": value,
    }
    row.update(extra)
    return row


def _nr_routes(transfer):
    """Head stays inside one block span. ``transfer`` sees one category's params."""

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if method == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        if method != "nr_getAssetTransfers":
            raise FixtureMiss()
        return transfer(body["params"][0])

    return answer


def _gaps(text: str) -> str:
    start = text.index("## Gaps\n") + len("## Gaps\n")
    end = text.index("\n\n", start)
    return text[start:end]


def _inbound(tmp_path: Path, capsys, backend: r1.PathBackend, *extra: str):
    return r1.invoke(capsys, r1.inbound_argv(tmp_path / "monitor.json", "--yes", *extra), backend)


def test_blockscout_repeated_cursor_drops_the_replayed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Page 2's next points at page 2. That page is not fetched again and is not counted."""
    backend = _blockscout(tmp_path, monkeypatch)
    seen = {"transactions": 0}

    def answer(call):
        if _tail(call, "token-transfers") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "transactions"):
            seen["transactions"] += 1
            if seen["transactions"] == 1:
                return r1.scout([r1.native_item(r1.LEFT, r1.SEED, 10, INSIDE, r1.tx(1))], CURSOR)
            return r1.scout([r1.native_item(r1.LEFT, r1.SEED, 20, INSIDE, r1.tx(2))], CURSOR)
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    calls = [call for call in backend.calls if _tail(call, "transactions")]
    assert len(calls) == 2
    assert r1.call_query(calls[1]).get("block_number") == ["5"]
    assert r1.call_query(calls[1]).get("index") == ["1"]
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == "repeated_cursor"
    assert _parts(read)["transactions"] == {
        "part": "transactions", "read_status": "gap", "reason": "repeated_cursor",
    }
    assert _parts(read)["token-transfers"]["read_status"] == "complete"
    row = body["counterparties"][0]
    assert row["address"] == r1.LEFT
    assert row["count"] == 1
    assert row["assets"][0]["raw_amount"] == "10"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_blockscout_cross_page_overlap_is_partial_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Token rows have no log index here. The same row on a later page is not counted twice."""
    backend = _blockscout(tmp_path, monkeypatch)
    seen = {"token-transfers": 0}
    first = [
        r1.token_item(r1.LEFT, r1.SEED, 1, INSIDE, r1.tx(1)),
        r1.token_item(r1.LEFT, r1.SEED, 2, INSIDE, r1.tx(2)),
    ]
    second = [
        r1.token_item(r1.LEFT, r1.SEED, 2, INSIDE, r1.tx(2)),
        r1.token_item(r1.LEFT, r1.SEED, 3, INSIDE, r1.tx(3)),
    ]

    def answer(call):
        if _tail(call, "transactions") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "token-transfers"):
            seen["token-transfers"] += 1
            if seen["token-transfers"] == 1:
                return r1.scout(first, {"block_number": 4, "index": 0})
            return r1.scout(second, None)
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "ambiguous_duplicate"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "partial", "reason": "ambiguous_duplicate",
    }
    row = body["counterparties"][0]
    assert row["count"] == 3
    assert row["assets"][0]["raw_amount"] == "6"
    assert row["tx_hashes"] == [r1.tx(1), r1.tx(2), r1.tx(3)]


def test_blockscout_same_page_token_rows_both_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two identical token rows on one page stay two rows, and the part stays complete."""
    backend = _blockscout(tmp_path, monkeypatch)
    row = r1.token_item(r1.LEFT, r1.SEED, 4, INSIDE, r1.tx(1))
    r1.fill_tails(backend, [row, dict(row)], [])
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["token-transfers"] == {"part": "token-transfers", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 2
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "8"


def test_blockscout_internal_index_collapses_one_trace_and_keeps_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same transaction hash plus index is one trace. A second index is another."""
    backend = _blockscout(tmp_path, monkeypatch)
    rows = [
        _internal(r1.LEFT, r1.SEED, 10, r1.tx(1), 1),
        _internal(r1.LEFT, r1.SEED, 10, r1.tx(1), 1),
        _internal(r1.LEFT, r1.SEED, 20, r1.tx(1), 2),
    ]
    backend.add(r1.tail_is("token-transfers"), r1.empty_scout())
    backend.add(r1.tail_is("transactions"), r1.empty_scout())
    backend.add(r1.tail_is("internal-transactions"), r1.scout(rows))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["internal-transactions"] == {
        "part": "internal-transactions", "read_status": "complete", "reason": "",
    }
    row = body["counterparties"][0]
    assert row["count"] == 2
    assert row["assets"][0]["raw_amount"] == "30"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_nodereal_repeated_page_key_drops_the_replayed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A pageKey that points at the page just read stops the walk and does not count that page."""
    backend = _nodereal(tmp_path, monkeypatch)
    seen = {"20": 0}

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if method != "nr_getAssetTransfers":
            raise FixtureMiss()
        params = body["params"][0]
        if params["category"] != ["20"]:
            return r1.rpc({"transfers": []})
        seen["20"] += 1
        if seen["20"] == 1:
            return r1.rpc({"pageKey": "p2", "transfers": [_nr(r1.OTHER, r1.DISPERSE, 1, "4")]})
        return r1.rpc({"pageKey": "p2", "transfers": [_nr(r1.OTHER, r1.DISPERSE, 2, "9")]})

    backend.add(lambda _call: True, answer)
    code, body = _inbound(
        tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    )
    assert code == 0, body
    token_calls = []
    for call in backend.calls:
        payload = r1.rpc_call(call)
        if payload["method"] == "nr_getAssetTransfers" and payload["params"][0]["category"] == ["20"]:
            token_calls.append(payload["params"][0])
    assert len(token_calls) == 2
    assert "pageKey" not in token_calls[0]
    assert token_calls[1]["pageKey"] == "p2"
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == "repeated_cursor"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "gap", "reason": "repeated_cursor",
    }
    row = body["counterparties"][0]
    assert row["count"] == 1
    assert row["assets"][0]["raw_amount"] == "4"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_nodereal_cross_page_overlap_is_partial_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """NodeReal rows in these fixtures have no separate row key. A later copy is not a second event."""
    backend = _nodereal(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if method != "nr_getAssetTransfers":
            raise FixtureMiss()
        params = body["params"][0]
        if params["category"] != ["20"]:
            return r1.rpc({"transfers": []})
        if params.get("pageKey"):
            return r1.rpc({"transfers": [
                _nr(r1.OTHER, r1.DISPERSE, 2, "5"),
                _nr(r1.OTHER, r1.DISPERSE, 3, "6"),
            ]})
        return r1.rpc({"pageKey": "p2", "transfers": [
            _nr(r1.OTHER, r1.DISPERSE, 1, "4"),
            _nr(r1.OTHER, r1.DISPERSE, 2, "5"),
        ]})

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "ambiguous_duplicate"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "partial", "reason": "ambiguous_duplicate",
    }
    assert _parts(read)["transactions"]["read_status"] == "complete"
    row = body["counterparties"][0]
    assert row["count"] == 3
    assert row["assets"][0]["raw_amount"] == "15"
    assert row["tx_hashes"] == [r1.tx(1), r1.tx(2), r1.tx(3)]


def test_nodereal_same_page_rows_both_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two identical NodeReal rows in one response both count, and the part stays complete."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr(r1.OTHER, r1.DISPERSE, 1, "4")

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if method != "nr_getAssetTransfers":
            raise FixtureMiss()
        params = body["params"][0]
        if params["category"] != ["20"]:
            return r1.rpc({"transfers": []})
        return r1.rpc({"transfers": [row, dict(row)]})

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 2
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "8"


def test_nodereal_exact_item_cap_stays_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A finished page that lands on the item cap is partial. It is not a finished window."""
    monkeypatch.setattr("guardian.relations.fetch._NODEREAL_ITEM_CAP", 1)
    backend = _nodereal(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if method != "nr_getAssetTransfers":
            raise FixtureMiss()
        params = body["params"][0]
        if params["category"] != ["20"]:
            return r1.rpc({"transfers": []})
        return r1.rpc({"transfers": [_nr(r1.OTHER, r1.DISPERSE, 1, "4")]})

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "item_cap"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "partial", "reason": "item_cap",
    }
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "4"


def _club_answer(logs: list):
    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(10))
        if method == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        if method == "eth_getLogs":
            return r1.rpc(logs)
        raise FixtureMiss()

    return answer


def test_club_repeated_log_counts_once_and_stays_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same transaction hash and log index is one transfer. The token part stays complete."""
    backend = _club(tmp_path, monkeypatch)
    row = _club_log(r1.OTHER, r1.DISPERSE, r1.tx(1), "0x0")
    backend.add(lambda _call: True, _club_answer([row, dict(row)]))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "4"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def test_club_two_log_indexes_in_one_transaction_count_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two logs in one transaction stay two rows when their log indexes differ."""
    backend = _club(tmp_path, monkeypatch)
    backend.add(lambda _call: True, _club_answer([
        _club_log(r1.OTHER, r1.DISPERSE, r1.tx(1), "0x0"),
        _club_log(r1.OTHER, r1.DISPERSE, r1.tx(1), "0x1"),
    ]))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 2
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "8"
    assert body["counterparties"][0]["tx_hash_count"] == 1


def test_tron_same_page_duplicate_rows_both_count_and_stay_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """TRC-20 has no log index. Two identical rows on one page are both counted."""
    backend = _tron(tmp_path, monkeypatch)
    row = r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "4", "ab" * 32)
    backend.add(lambda call: "transactions/trc20" in call.url, r1.tron_page([row, dict(row)], None))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.TRON_PAYER, "--network", "tron-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["parts"][0] == {"part": "trc20", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 2
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "8"


def test_tron_cross_page_duplicate_row_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same TRC-20 row on a later page is kept once, and that part is no longer complete."""
    backend = _tron(tmp_path, monkeypatch)
    backend.add(lambda call: "transactions/trc20" in call.url, [
        r1.tron_page([
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "4", "aa" * 32),
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "5", "bb" * 32),
        ], "pageTwo"),
        r1.tron_page([
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "4", "aa" * 32),
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "6", "cc" * 32),
        ], None),
    ])
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.TRON_PAYER, "--network", "tron-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == "not_in_trc20_endpoint"
    assert read["parts"][0] == {
        "part": "trc20", "read_status": "partial", "reason": "ambiguous_duplicate",
    }
    assert read["parts"][1] == {"part": "trx", "read_status": "gap", "reason": "not_in_trc20_endpoint"}
    row = body["counterparties"][0]
    assert row["count"] == 3
    assert row["assets"][0]["raw_amount"] == "15"
    assert row["tx_hashes"] == ["aa" * 32, "bb" * 32, "cc" * 32]
    assert len(backend.calls) == 2


def test_markdown_gaps_list_each_part_and_the_node_cap() -> None:
    """Gaps lists every part, then unqueried rows, then truncated. The text is the whole section."""
    payload = {
        "command": "inbound",
        "generated_at": "2026-01-01T00:00:00Z",
        "since_days": 90,
        "status": "partial",
        "networks": ["tron-mainnet", "ethereum-mainnet"],
        "plan": {
            "queries": [],
            "unqueried": [{
                "network": "solana-mainnet",
                "address": r1.SOL,
                "read_status": "unsupported",
                "reason": "unsupported",
            }],
        },
        "reads": [
            {
                "network": "tron-mainnet",
                "source_id": "tron-grid",
                "read_status": "gap",
                "reason": "not_in_trc20_endpoint",
                "parts": [
                    {"part": "trc20", "read_status": "partial", "reason": "page_cap"},
                    {"part": "trx", "read_status": "gap", "reason": "not_in_trc20_endpoint"},
                    {"part": "trc10", "read_status": "gap", "reason": "not_in_trc20_endpoint"},
                ],
            },
            {
                "network": "bitcoin-mainnet",
                "source_id": None,
                "read_status": "unsupported",
                "reason": "unsupported",
                "parts": [],
            },
        ],
        "counterparties": [],
        "truncated": {"reason": "node_cap", "total": 3},
    }
    section = _gaps(render_markdown(payload))
    assert section == "\n".join([
        "- tron-mainnet tron-grid trc20 partial page_cap",
        "- tron-mainnet tron-grid trx gap not_in_trc20_endpoint",
        "- tron-mainnet tron-grid trc10 gap not_in_trc20_endpoint",
        "- bitcoin-mainnet None unsupported unsupported",
        f"- solana-mainnet {r1.SOL} unsupported unsupported",
        "- truncated node_cap total 3",
    ])


def test_markdown_gaps_show_node_cap_when_every_part_is_complete() -> None:
    """A complete read still shows its parts, and truncated is not rendered as None."""
    payload = {
        "command": "inbound",
        "generated_at": "2026-01-01T00:00:00Z",
        "since_days": 90,
        "status": "partial",
        "network": "ethereum-mainnet",
        "plan": {"queries": []},
        "reads": [{
            "network": "ethereum-mainnet",
            "source_id": "blockscout-eth",
            "read_status": "complete",
            "reason": "",
            "parts": [
                {"part": "token-transfers", "read_status": "complete", "reason": ""},
                {"part": "transactions", "read_status": "complete", "reason": ""},
                {"part": "internal-transactions", "read_status": "complete", "reason": ""},
            ],
        }],
        "counterparties": [],
        "truncated": {"reason": "node_cap", "total": 3},
    }
    section = _gaps(render_markdown(payload))
    assert section == "\n".join([
        "- ethereum-mainnet blockscout-eth token-transfers complete",
        "- ethereum-mainnet blockscout-eth transactions complete",
        "- ethereum-mainnet blockscout-eth internal-transactions complete",
        "- truncated node_cap total 3",
    ])


def test_dedup_r2_blockscout_same_page_native_hash_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """One transaction hash on one transactions page is one row, and the part stays complete."""
    backend = _blockscout(tmp_path, monkeypatch)
    row = r1.native_item(r1.LEFT, r1.SEED, 10, INSIDE, r1.tx(1))
    backend.add(r1.tail_is("token-transfers"), r1.empty_scout())
    backend.add(r1.tail_is("transactions"), r1.scout([row, dict(row)]))
    backend.add(r1.tail_is("internal-transactions"), r1.empty_scout())
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["transactions"] == {"part": "transactions", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_blockscout_same_native_hash_across_pages_stays_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same transaction hash on a later transactions page is still one complete row."""
    backend = _blockscout(tmp_path, monkeypatch)
    seen = {"transactions": 0}

    def answer(call):
        if _tail(call, "token-transfers") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "transactions"):
            seen["transactions"] += 1
            row = r1.native_item(r1.LEFT, r1.SEED, 10, INSIDE, r1.tx(1))
            if seen["transactions"] == 1:
                return r1.scout([row], {"block_number": 4, "index": 0})
            return r1.scout([row], None)
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["transactions"] == {"part": "transactions", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_nodereal_same_page_external_hash_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """One external hash on one NodeReal page is one row, and that part stays complete."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr_native(r1.OTHER, r1.DISPERSE, 1, "10")

    def transfer(params):
        if params["category"] != ["external"]:
            return r1.rpc({"transfers": []})
        return r1.rpc({"transfers": [row, dict(row)]})

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["transactions"] == {"part": "transactions", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_nodereal_same_external_hash_across_pages_stays_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same external hash on a later page is one complete row, not an ambiguous duplicate."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr_native(r1.OTHER, r1.DISPERSE, 1, "10")

    def transfer(params):
        if params["category"] != ["external"]:
            return r1.rpc({"transfers": []})
        if params.get("pageKey"):
            return r1.rpc({"transfers": [dict(row)]})
        return r1.rpc({"pageKey": "p2", "transfers": [row]})

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["transactions"] == {"part": "transactions", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"


def test_dedup_r2_blockscout_same_page_log_index_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A token row that repeats the same log index on one page counts once and stays complete."""
    backend = _blockscout(tmp_path, monkeypatch)
    row = _token_at(r1.LEFT, r1.SEED, 4, r1.tx(1), 1)
    r1.fill_tails(backend, [row, dict(row)], [])
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["token-transfers"] == {"part": "token-transfers", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "4"


def test_dedup_r2_blockscout_same_log_index_across_pages_stays_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same transaction hash and log index on a later page is one complete token row."""
    backend = _blockscout(tmp_path, monkeypatch)
    seen = {"token-transfers": 0}

    def answer(call):
        if _tail(call, "transactions") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "token-transfers"):
            seen["token-transfers"] += 1
            row = _token_at(r1.LEFT, r1.SEED, 4, r1.tx(1), 7)
            if seen["token-transfers"] == 1:
                return r1.scout([row], {"block_number": 4, "index": 0})
            return r1.scout([dict(row)], None)
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["token-transfers"] == {"part": "token-transfers", "read_status": "complete", "reason": ""}
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "4"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_blockscout_distinct_log_indexes_across_pages_count_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two log indexes of one transaction stay two complete rows when the pages differ."""
    backend = _blockscout(tmp_path, monkeypatch)
    seen = {"token-transfers": 0}

    def answer(call):
        if _tail(call, "transactions") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "token-transfers"):
            seen["token-transfers"] += 1
            if seen["token-transfers"] == 1:
                return r1.scout(
                    [_token_at(r1.LEFT, r1.SEED, 4, r1.tx(1), 1)],
                    {"block_number": 4, "index": 0},
                )
            second = r1.token_item(r1.LEFT, r1.SEED, 4, INSIDE, r1.tx(1))
            second["log_index"] = 2
            return r1.scout([second], None)
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["token-transfers"] == {"part": "token-transfers", "read_status": "complete", "reason": ""}
    row = body["counterparties"][0]
    assert row["count"] == 2
    assert row["assets"][0]["raw_amount"] == "8"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_nodereal_same_page_trace_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """One internal hash plus traceId on one page is one complete row."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr_native(r1.OTHER, r1.DISPERSE, 1, "10", traceId="0")

    def transfer(params):
        if params["category"] != ["internal"]:
            return r1.rpc({"transfers": []})
        return r1.rpc({"transfers": [row, dict(row)]})

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["internal-transactions"] == {
        "part": "internal-transactions", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"


def test_dedup_r2_nodereal_same_trace_across_pages_stays_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same internal hash and traceId on a later page is one complete row."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr_native(r1.OTHER, r1.DISPERSE, 1, "10", traceId="0")

    def transfer(params):
        if params["category"] != ["internal"]:
            return r1.rpc({"transfers": []})
        if params.get("pageKey"):
            return r1.rpc({"transfers": [dict(row)]})
        return r1.rpc({"pageKey": "p2", "transfers": [row]})

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["internal-transactions"] == {
        "part": "internal-transactions", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"


def test_dedup_r2_nodereal_distinct_traces_across_pages_count_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two trace positions of one internal hash stay two complete rows across pages."""
    backend = _nodereal(tmp_path, monkeypatch)

    def transfer(params):
        if params["category"] != ["internal"]:
            return r1.rpc({"transfers": []})
        if params.get("pageKey"):
            return r1.rpc({"transfers": [_nr_native(r1.OTHER, r1.DISPERSE, 1, "10", traceId="1")]})
        return r1.rpc({
            "pageKey": "p2",
            "transfers": [_nr_native(r1.OTHER, r1.DISPERSE, 1, "10", traceId="0")],
        })

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["internal-transactions"] == {
        "part": "internal-transactions", "read_status": "complete", "reason": "",
    }
    row = body["counterparties"][0]
    assert row["count"] == 2
    assert row["assets"][0]["raw_amount"] == "20"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_nodereal_repeated_cursor_stamps_unstamped_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Kept rows with no timestamp are still looked up. The part stays gap/repeated_cursor."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr(r1.OTHER, r1.DISPERSE, 1, "4")
    row.pop("metadata")

    def transfer(params):
        if params["category"] != ["20"]:
            return r1.rpc({"transfers": []})
        if params.get("pageKey"):
            return r1.rpc({"pageKey": "p2", "transfers": [_nr(r1.OTHER, r1.DISPERSE, 2, "9")]})
        return r1.rpc({"pageKey": "p2", "transfers": [row]})

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == "repeated_cursor"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "gap", "reason": "repeated_cursor",
    }
    rows = body["counterparties"]
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["count"] == 1
    assert rows[0]["assets"][0]["raw_amount"] == "4"
    assert rows[0]["tx_hashes"] == [r1.tx(1)]
    lookups = [
        r1.rpc_call(call) for call in backend.calls if r1.rpc_call(call)["method"] == "eth_getBlockByNumber"
    ]
    assert lookups
    assert lookups[0]["params"][0] == hex(5)


def test_dedup_r2_blockscout_page_cap_keeps_rows_when_next_cursor_is_illegal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """At the page cap, an unusable next cursor does not erase the page already read."""
    backend = _blockscout(tmp_path, monkeypatch)

    def answer(call):
        if _tail(call, "token-transfers") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "transactions"):
            return r1.scout(
                [r1.native_item(r1.LEFT, r1.SEED, 10, INSIDE, r1.tx(1))],
                {"block_number": None},
            )
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(
        tmp_path, capsys, backend, "--max-pages", "1", "--seed", r1.SEED, "--network", "ethereum-mainnet",
    )
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "page_cap"
    assert _parts(read)["transactions"] == {
        "part": "transactions", "read_status": "partial", "reason": "page_cap",
    }
    assert len([call for call in backend.calls if _tail(call, "transactions")]) == 1
    row = body["counterparties"][0]
    assert row["count"] == 1
    assert row["assets"][0]["raw_amount"] == "10"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_tron_page_cap_keeps_its_reason_when_a_row_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A page cap stays page_cap when a later page repeats a row. The repeat is not counted."""
    backend = _tron(tmp_path, monkeypatch)
    backend.add(lambda call: "transactions/trc20" in call.url, [
        r1.tron_page([
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "4", "aa" * 32),
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "5", "bb" * 32),
        ], "pageTwo"),
        r1.tron_page([
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "4", "aa" * 32),
            r1.tron_row(r1.TRON_SEED, r1.TRON_PAYER, "6", "cc" * 32),
        ], "pageThree"),
    ])
    code, body = _inbound(
        tmp_path, capsys, backend, "--max-pages", "2", "--seed", r1.TRON_PAYER, "--network", "tron-mainnet",
    )
    assert code == 0, body
    read = body["reads"][0]
    assert read["parts"][0] == {"part": "trc20", "read_status": "partial", "reason": "page_cap"}
    row = body["counterparties"][0]
    assert row["count"] == 3
    assert row["assets"][0]["raw_amount"] == "15"
    assert row["tx_hashes"] == ["aa" * 32, "bb" * 32, "cc" * 32]
    assert len(backend.calls) == 2


def test_dedup_r2_blockscout_gap_keeps_repeated_cursor_when_kept_pages_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Overlap on the pages kept before a repeated cursor does not replace gap/repeated_cursor."""
    backend = _blockscout(tmp_path, monkeypatch)
    seen = {"token-transfers": 0}

    def answer(call):
        if _tail(call, "transactions") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "token-transfers"):
            seen["token-transfers"] += 1
            if seen["token-transfers"] == 1:
                return r1.scout([
                    r1.token_item(r1.LEFT, r1.SEED, 1, INSIDE, r1.tx(1)),
                    r1.token_item(r1.LEFT, r1.SEED, 2, INSIDE, r1.tx(2)),
                ], {"block_number": 8, "index": 1})
            if seen["token-transfers"] == 2:
                return r1.scout([
                    r1.token_item(r1.LEFT, r1.SEED, 2, INSIDE, r1.tx(2)),
                    r1.token_item(r1.LEFT, r1.SEED, 3, INSIDE, r1.tx(3)),
                ], {"block_number": 6, "index": 1})
            return r1.scout(
                [r1.token_item(r1.LEFT, r1.SEED, 9, INSIDE, r1.tx(9))],
                {"block_number": 6, "index": 1},
            )
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    calls = [call for call in backend.calls if _tail(call, "token-transfers")]
    assert len(calls) == 3
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == "repeated_cursor"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "gap", "reason": "repeated_cursor",
    }
    row = body["counterparties"][0]
    assert row["count"] == 3
    assert row["assets"][0]["raw_amount"] == "6"
    assert row["tx_hashes"] == [r1.tx(1), r1.tx(2), r1.tx(3)]


def test_dedup_r2_club_same_log_across_chunks_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same 48club hash and log index in two chunks is one complete token row."""
    backend = _club(tmp_path, monkeypatch)
    row = _club_log(r1.OTHER, r1.DISPERSE, r1.tx(1), "0x0")

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(1001))
        if method == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        if method == "eth_getLogs":
            return r1.rpc([row])
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    logs = [call for call in backend.calls if r1.rpc_call(call)["method"] == "eth_getLogs"]
    assert len(logs) == 2
    read = body["reads"][0]
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "4"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def test_dedup_r2_blockscout_internal_same_index_across_pages_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same internal hash and index on a later page is one complete trace."""
    backend = _blockscout(tmp_path, monkeypatch)
    seen = {"internal-transactions": 0}

    def answer(call):
        if _tail(call, "token-transfers") or _tail(call, "transactions"):
            return r1.scout([])
        if _tail(call, "internal-transactions"):
            seen["internal-transactions"] += 1
            row = _internal(r1.LEFT, r1.SEED, 10, r1.tx(1), 1)
            if seen["internal-transactions"] == 1:
                return r1.scout([row], {"block_number": 4, "index": 0})
            return r1.scout([dict(row)], None)
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert _parts(read)["internal-transactions"] == {
        "part": "internal-transactions", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def _token_row(value: int, digest: str, **fields: object) -> dict:
    row = r1.token_item(r1.LEFT, r1.SEED, value, INSIDE, digest)
    row.update(fields)
    return row


def _blockscout_token_pages(backend: r1.PathBackend, first: list, second: list) -> None:
    seen = {"token-transfers": 0}

    def answer(call):
        if _tail(call, "transactions") or _tail(call, "internal-transactions"):
            return r1.scout([])
        if _tail(call, "token-transfers"):
            seen["token-transfers"] += 1
            if seen["token-transfers"] == 1:
                return r1.scout(first, {"block_number": 4, "index": 0})
            return r1.scout(second, None)
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)


def test_dedup_r3_blockscout_log_index_then_missing_across_pages_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A later page of the same transfer with no log index is not a second complete event."""
    backend = _blockscout(tmp_path, monkeypatch)
    _blockscout_token_pages(
        backend,
        [_token_row(4, r1.tx(1), logIndex=7)],
        [_token_row(4, r1.tx(1))],
    )
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "ambiguous_duplicate"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "partial", "reason": "ambiguous_duplicate",
    }
    row = body["counterparties"][0]
    assert row["count"] == 1
    assert row["assets"][0]["raw_amount"] == "4"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_blockscout_missing_then_log_index_across_pages_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A later log index does not count again when the first page of that transfer had none."""
    backend = _blockscout(tmp_path, monkeypatch)
    _blockscout_token_pages(
        backend,
        [_token_row(4, r1.tx(1))],
        [_token_row(4, r1.tx(1), logIndex=7)],
    )
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "ambiguous_duplicate"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "partial", "reason": "ambiguous_duplicate",
    }
    row = body["counterparties"][0]
    assert row["count"] == 1
    assert row["assets"][0]["raw_amount"] == "4"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_unknown_log_index_uses_the_other_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """logIndex "unknown" is not an identity. Different decimal log_index values stay two complete rows."""
    backend = _blockscout(tmp_path, monkeypatch)
    _blockscout_token_pages(
        backend,
        [_token_row(4, r1.tx(1), logIndex="unknown", log_index="1")],
        [_token_row(4, r1.tx(1), logIndex="unknown", log_index="2")],
    )
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    row = body["counterparties"][0]
    assert row["count"] == 2
    assert row["assets"][0]["raw_amount"] == "8"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_negative_log_index_uses_the_other_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A negative logIndex is not an identity. A valid hex log_index on each page still counts."""
    backend = _blockscout(tmp_path, monkeypatch)
    _blockscout_token_pages(
        backend,
        [_token_row(4, r1.tx(1), logIndex=-1, log_index="0x3")],
        [_token_row(4, r1.tx(1), logIndex=-1, log_index="0x4")],
    )
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    row = body["counterparties"][0]
    assert row["count"] == 2
    assert row["assets"][0]["raw_amount"] == "8"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_invalid_hex_log_index_uses_the_other_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """An unparsable 0x logIndex does not hide a valid log_index, and it is not a shared key."""
    backend = _blockscout(tmp_path, monkeypatch)
    _blockscout_token_pages(
        backend,
        [_token_row(4, r1.tx(1), logIndex="0xzz", log_index=8)],
        [_token_row(4, r1.tx(1), logIndex="0xzz", log_index=9)],
    )
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    row = body["counterparties"][0]
    assert row["count"] == 2
    assert row["assets"][0]["raw_amount"] == "8"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_invalid_log_indexes_on_one_page_both_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two rows whose log indexes are both invalid follow the same-page fuzzy rule."""
    backend = _blockscout(tmp_path, monkeypatch)
    row = _token_row(4, r1.tx(1), logIndex="unknown")
    r1.fill_tails(backend, [row, dict(row)], [])
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "complete", "reason": "",
    }
    row_out = body["counterparties"][0]
    assert row_out["count"] == 2
    assert row_out["assets"][0]["raw_amount"] == "8"
    assert row_out["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_invalid_log_indexes_across_pages_follow_the_fuzzy_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A negative log index is not a key. The same row on a later page is partial, not a second event."""
    backend = _blockscout(tmp_path, monkeypatch)
    _blockscout_token_pages(
        backend,
        [_token_row(4, r1.tx(1), logIndex=-1)],
        [_token_row(4, r1.tx(1), logIndex=-1)],
    )
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.SEED, "--network", "ethereum-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "ambiguous_duplicate"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers", "read_status": "partial", "reason": "ambiguous_duplicate",
    }
    row = body["counterparties"][0]
    assert row["count"] == 1
    assert row["assets"][0]["raw_amount"] == "4"
    assert row["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_nodereal_repeated_cursor_keeps_gap_when_timestamp_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed stamp on rows kept before a repeated cursor does not rewrite gap/repeated_cursor."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr(r1.OTHER, r1.DISPERSE, 1, "4")
    row.pop("metadata")

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if method == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0]})
        if method != "nr_getAssetTransfers":
            raise FixtureMiss()
        params = body["params"][0]
        if params["category"] != ["20"]:
            return r1.rpc({"transfers": []})
        if params.get("pageKey"):
            return r1.rpc({"pageKey": "p2", "transfers": [_nr(r1.OTHER, r1.DISPERSE, 2, "9")]})
        return r1.rpc({"pageKey": "p2", "transfers": [row]})

    backend.add(lambda _call: True, answer)
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "gap"
    assert read["reason"] == "repeated_cursor"
    assert _parts(read)["token-transfers"] == {
        "part": "token-transfers",
        "read_status": "gap",
        "reason": "repeated_cursor",
        "timestamp_reason": "window_unchecked",
    }
    assert body["counterparties"] is None


def test_dedup_r3_nodereal_internal_without_trace_on_one_page_counts_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two internal rows with no trace position on one page both count, and that part stays complete."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr_native(r1.OTHER, r1.DISPERSE, 1, "10")

    def transfer(params):
        if params["category"] != ["internal"]:
            return r1.rpc({"transfers": []})
        return r1.rpc({"transfers": [row, dict(row)]})

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "complete"
    assert read["reason"] == ""
    assert _parts(read)["internal-transactions"] == {
        "part": "internal-transactions", "read_status": "complete", "reason": "",
    }
    assert body["counterparties"][0]["count"] == 2
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "20"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]


def test_dedup_r3_nodereal_internal_without_trace_across_pages_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same internal row with no trace position on a later page is one partial event."""
    backend = _nodereal(tmp_path, monkeypatch)
    row = _nr_native(r1.OTHER, r1.DISPERSE, 1, "10")

    def transfer(params):
        if params["category"] != ["internal"]:
            return r1.rpc({"transfers": []})
        if params.get("pageKey"):
            return r1.rpc({"transfers": [dict(row)]})
        return r1.rpc({"pageKey": "p2", "transfers": [row]})

    backend.add(lambda _call: True, _nr_routes(transfer))
    code, body = _inbound(tmp_path, capsys, backend, "--seed", r1.DISPERSE, "--network", "bsc-mainnet")
    assert code == 0, body
    read = body["reads"][0]
    assert read["read_status"] == "partial"
    assert read["reason"] == "ambiguous_duplicate"
    assert _parts(read)["internal-transactions"] == {
        "part": "internal-transactions", "read_status": "partial", "reason": "ambiguous_duplicate",
    }
    assert body["counterparties"][0]["count"] == 1
    assert body["counterparties"][0]["assets"][0]["raw_amount"] == "10"
    assert body["counterparties"][0]["tx_hashes"] == [r1.tx(1)]
