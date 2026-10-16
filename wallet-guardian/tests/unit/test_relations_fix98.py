"""FIX-98 relations checks. Synthetic addresses and the public Disperse contract only.

No live network. Responses come from the fixture backend.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from guardian.relations.command import CLUB_BUDGET_SPEC
from guardian.relations.fetch import _NODEREAL_ITEM_CAP, _TIME_LOOKUP_CAP
from guardian.sources.budget import BudgetBook, BudgetSpec
from guardian.sources.fixtures import FixtureMiss, RawResponse

_SPEC = importlib.util.spec_from_file_location(
    "relations_fix98_helpers",
    Path(__file__).with_name("test_relations_r1.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
r1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r1)

TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STOP = int(r1.CLOCK) - 90 * 86400
INSIDE = r1.STAMP
HEAD = 250_000
ROOT = Path(__file__).resolve().parents[2]
CITATION = "https://docs.48.club/privacy-rpc.md"
HOSTS = ("rpc-bsc.48.club", "explorer.optimism.io")
_ENDPOINT_KEYS = {"endpoint", "url", "base_url", "rpc", "rpc_url", "endpoint_url"}
_TEXT_SUFFIXES = {".py", ".json", ".md", ".toml", ".txt", ".yml", ".yaml"}


class Clock:
    def __init__(self) -> None:
        self.now = r1.CLOCK

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def _parts(read: dict) -> dict:
    return {part["part"]: part for part in read["parts"]}


def _club_log(sender: str, recipient: str, block: int, digest: str) -> dict:
    return {
        "address": r1.TOKEN,
        "blockNumber": hex(block),
        "data": "0x" + f"{4:064x}",
        "logIndex": "0x0",
        "topics": [
            TOPIC,
            "0x" + sender[2:].rjust(64, "0"),
            "0x" + recipient[2:].rjust(64, "0"),
        ],
        "transactionHash": digest,
    }


def _nr_row(sender: str, recipient: str, number: int, *, failed: bool = False, block: int = 5) -> dict:
    return {
        "blockNum": hex(block),
        "contractAddress": r1.TOKEN,
        "from": sender,
        "hash": r1.tx(number),
        "isError": "1" if failed else "0",
        "to": recipient,
        "tokenDecimal": "6",
        "value": "4",
    }


def _club_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, retry_attempts: int = 0):
    sources = {"nodereal-bsc": r1._source("nodereal_transfers", "NODEREAL_BSC_URL", "nodereal-bsc")}
    routes = [{"network": "bsc-mainnet", "capability": "history.native_external", "source_ids": ["nodereal-bsc"]}]
    backend = r1.install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes,
        secrets={"NODEREAL_BSC_URL": "https://bsc.example.invalid"},
        address=r1.MONITORED, family="evm", account_networks=["bsc-mainnet"],
        retry_attempts=retry_attempts,
    )
    r1.allow_fixture_hosts(monkeypatch, "rpc-bsc.48.club")
    return backend


def _club_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, retry_attempts: int):
    sources = {"bsc-logs-48club": r1._source("evm_rpc", "RPC_BSC_LOGS_URL", "bsc-logs-48club")}
    routes = [{"network": "bsc-mainnet", "capability": "history.token", "source_ids": ["bsc-logs-48club"]}]
    backend = r1.install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes,
        secrets={"RPC_BSC_LOGS_URL": "https://rpc-bsc.48.club"},
        address=r1.MONITORED, family="evm", account_networks=["bsc-mainnet"],
        retry_attempts=retry_attempts,
    )
    r1.allow_fixture_hosts(monkeypatch, "rpc-bsc.48.club")
    return backend


def _nodereal_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = {
        "nodereal-bsc": r1._source(
            "nodereal_transfers", "NODEREAL_BSC_URL", "nodereal-bsc", "NODEREAL_API_KEY",
        ),
    }
    routes = [{"network": "bsc-mainnet", "capability": "history.native_external", "source_ids": ["nodereal-bsc"]}]
    return r1.install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes,
        secrets={
            "NODEREAL_API_KEY": "synthetic-nodereal-key-fix98",
            "NODEREAL_BSC_URL": "https://nodereal.example.invalid",
        },
        address=r1.MONITORED, family="evm", account_networks=["bsc-mainnet"],
    )


def _run(capsys, argv, backend, clock=None):
    code = r1.main(argv, backend=backend, clock=(lambda: r1.CLOCK) if clock is None else clock)
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    return code, body, captured


def _code_answer(code: str):
    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_blockNumber":
            return r1.rpc(hex(20))
        if method == "eth_getLogs":
            return r1.rpc([_club_log(r1.PAYER, r1.OTHER, 8, r1.tx(8))])
        if method == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        if method == "eth_getCode":
            return r1.rpc(code)
        raise FixtureMiss()

    return answer


def test_m1_contract_payer_hides_co_recipients(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)
    backend.add(lambda _call: True, _code_answer("0x6000"))
    code, body, _captured = _run(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", "bsc-mainnet", "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is True
    assert "contract" in body["hub_reasons"]
    assert "co_recipients" not in body
    plan_host = body["plan"]["queries"][0]["host"]
    coded = [r1.rpc_call(call) for call in backend.calls if r1.rpc_call(call)["method"] == "eth_getCode"]
    assert coded
    assert coded[0]["params"] == [r1.PAYER, "latest"]
    assert all(urlsplit(call.url).hostname == plan_host for call in backend.calls)


def test_m1_failed_code_does_not_set_hub_false(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_getCode":
            return RawResponse(500, {"content-type": "application/json"}, b"")
        return _code_answer("0x")(call)

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", "bsc-mainnet", "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is None
    assert body["hub"] is not False
    assert "co_recipients" not in body


def test_m1_empty_code_still_lists_co_recipients(tmp_path, monkeypatch, capsys) -> None:
    """Keyless native parts stay gaps, so an empty code does not claim hub false."""
    backend = _club_install(tmp_path, monkeypatch)
    backend.add(lambda _call: True, _code_answer("0x"))
    code, body, _captured = _run(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", "bsc-mainnet", "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is None
    assert body["hub"] is not False
    assert body["co_recipients"]
    assert body["co_recipients"][0]["address"] == r1.OTHER


def test_m1_nodereal_empty_code_sets_hub_false(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    row = _nr_row(r1.PAYER, r1.OTHER, 1)
    row["metadata"] = {"blockTimestamp": INSIDE}

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if body["method"] == "eth_getCode":
            assert body["params"] == [r1.PAYER, "latest"]
            return r1.rpc("0x")
        if body["method"] == "nr_getAssetTransfers":
            category = body["params"][0]["category"][0]
            if category == "20":
                return r1.rpc({"transfers": [row]})
            return r1.rpc({"transfers": []})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", r1.PAYER, "--network", "bsc-mainnet", "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "complete"
    assert body["hub"] is False
    assert body["co_recipients"][0]["address"] == r1.OTHER


def test_m1_inbound_does_not_query_code(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        method = body["method"]
        if method == "eth_getCode":
            raise AssertionError(method)
        if method == "eth_blockNumber":
            return r1.rpc(hex(20))
        if method == "eth_getLogs":
            return r1.rpc([_club_log(r1.OTHER, r1.DISPERSE, 8, r1.tx(8))])
        if method == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["counterparties"][0]["address"] == r1.OTHER
    assert body["counterparties"][0]["is_contract"] == "unknown"
    assert r1.TOKEN[2:].lower() not in "".join(call.body.decode() for call in backend.calls).lower()


def test_m2_nodereal_flat_row_and_failed_row(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    failed_sender = r1.evm(9)
    rows = [
        _nr_row(r1.OTHER, r1.DISPERSE, 1),
        _nr_row(failed_sender, r1.DISPERSE, 2, failed=True),
    ]

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if body["method"] == "eth_getBlockByNumber":
            assert int(body["params"][0], 16) == 5
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        if body["method"] == "nr_getAssetTransfers":
            category = body["params"][0]["category"][0]
            if category == "20":
                return r1.rpc({"transfers": rows})
            return r1.rpc({"transfers": []})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "complete"
    assert [row["address"] for row in body["counterparties"]] == [r1.OTHER]
    assert failed_sender not in [row["address"] for row in body["counterparties"]]
    assert any(r1.rpc_call(call)["method"] == "eth_getBlockByNumber" for call in backend.calls)


def test_m3_page_cap_keeps_the_first_page(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    row = _nr_row(r1.OTHER, r1.DISPERSE, 1)
    row["metadata"] = {"blockTimestamp": INSIDE}

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if body["method"] == "nr_getAssetTransfers":
            category = body["params"][0]["category"][0]
            if category == "20":
                return r1.rpc({"pageKey": "next", "transfers": [row]})
            return r1.rpc({"transfers": []})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "page_cap"
    assert _parts(body["reads"][0])["token-transfers"]["reason"] == "page_cap"
    assert body["counterparties"] != []
    assert body["counterparties"][0]["address"] == r1.OTHER


def test_m3_item_cap_keeps_rows(tmp_path, monkeypatch, capsys) -> None:
    assert _NODEREAL_ITEM_CAP == 5000
    monkeypatch.setattr("guardian.relations.fetch._NODEREAL_ITEM_CAP", 1)
    backend = _nodereal_install(tmp_path, monkeypatch)
    first = _nr_row(r1.OTHER, r1.DISPERSE, 1)
    second = _nr_row(r1.LEFT, r1.DISPERSE, 2, block=6)
    for row in (first, second):
        row["metadata"] = {"blockTimestamp": INSIDE}

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if body["method"] == "nr_getAssetTransfers":
            category = body["params"][0]["category"][0]
            if category == "20":
                return r1.rpc({"transfers": [first, second]})
            return r1.rpc({"transfers": []})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "partial"
    assert token["reason"] == "item_cap"
    assert [row["address"] for row in body["counterparties"]] == [r1.OTHER]


def test_m3_first_request_failure_is_a_gap(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(1000))
        if body["method"] == "nr_getAssetTransfers":
            return RawResponse(500, {"content-type": "application/json"}, b"")
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "http_5xx"
    assert body["counterparties"] is None
    assert body["counterparties"] != []


def test_m4_timestamp_cache_is_shared(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(150_000))
        if body["method"] == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(STOP)})
        if body["method"] == "nr_getAssetTransfers":
            return r1.rpc({"transfers": []})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "complete"
    looked = [
        r1.rpc_call(call) for call in backend.calls if r1.rpc_call(call)["method"] == "eth_getBlockByNumber"
    ]
    assert len(looked) == 1
    assert int(looked[0]["params"][0], 16) == 50_000


def test_m4_time_lookup_cap_keeps_earlier_rows(tmp_path, monkeypatch, capsys) -> None:
    assert _TIME_LOOKUP_CAP == 32
    backend = _club_install(tmp_path, monkeypatch)
    logs = [_club_log(r1.OTHER, r1.DISPERSE, block, r1.tx(block)) for block in range(1, 34)]

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(100))
        if body["method"] == "eth_getLogs":
            return r1.rpc(logs)
        if body["method"] == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "partial"
    assert token["reason"] == "time_lookup_cap"
    assert body["counterparties"] != []
    assert body["counterparties"] is not None
    looked = [call for call in backend.calls if r1.rpc_call(call)["method"] == "eth_getBlockByNumber"]
    assert len(looked) == 32


def test_m4_later_timestamp_failure_keeps_the_reason(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)
    log = _club_log(r1.OTHER, r1.DISPERSE, 2000, r1.tx(9))

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(2500))
        if body["method"] == "eth_getLogs":
            start = int(body["params"][0]["fromBlock"], 16)
            if start >= 1501:
                return r1.rpc([log])
            return r1.rpc([])
        if body["method"] == "eth_getBlockByNumber":
            number = int(body["params"][0], 16)
            if number in {1501, 2000}:
                return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
            return RawResponse(429, {"content-type": "application/json"}, b"")
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "2", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "partial"
    assert token["reason"] == "rate_limited"
    assert token["reason"] != "window_unchecked"
    assert body["counterparties"][0]["address"] == r1.OTHER


def test_m4_first_timestamp_failure_is_a_gap(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(2500))
        if body["method"] == "eth_getLogs":
            return r1.rpc([_club_log(r1.OTHER, r1.DISPERSE, 2000, r1.tx(9))])
        if body["method"] == "eth_getBlockByNumber":
            return RawResponse(429, {"content-type": "application/json"}, b"")
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "gap"
    assert token["reason"] == "rate_limited"
    assert body["reads"][0]["reason"] == "rate_limited"
    assert body["counterparties"] is None


def test_m5_partial_with_no_edges_counts_null(tmp_path, monkeypatch, capsys) -> None:
    sources, routes, secrets = r1.blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = r1.install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=r1.SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(r1.tail_is("token-transfers"), r1.scout(
        [r1.token_item(r1.PAYER, r1.LEFT, 0, INSIDE, r1.tx(1))],
        {"block_number": 4, "index": 0},
    ))
    backend.add(r1.tail_is("transactions"), r1.empty_scout())
    backend.add(r1.tail_is("internal-transactions"), r1.empty_scout())
    code, body, _captured = _run(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--markdown", "--max-pages", "1",
        "--payer", r1.PAYER, "--network", "ethereum-mainnet", "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "partial"
    assert body["recipient_count"] is None
    assert body["seed_receipts"] is None
    assert body["co_recipients"] is None
    text = Path(body["markdown_path"]).read_text(encoding="utf-8")
    assert "Count: 0" not in text


def test_m5_partial_with_edges_keeps_the_count(tmp_path, monkeypatch, capsys) -> None:
    sources, routes, secrets = r1.blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = r1.install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=r1.SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(r1.tail_is("token-transfers"), r1.scout(
        [r1.token_item(r1.PAYER, r1.LEFT, 4, INSIDE, r1.tx(1))],
        {"block_number": 4, "index": 0},
    ))
    backend.add(r1.tail_is("transactions"), r1.empty_scout())
    backend.add(r1.tail_is("internal-transactions"), r1.empty_scout())
    code, body, _captured = _run(capsys, r1.expand_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1",
        "--payer", r1.PAYER, "--network", "ethereum-mainnet", "--seed", r1.SEED,
    ), backend)
    assert code == 0, body
    assert body["recipient_count"] == 1
    assert body["co_recipients"][0]["address"] == r1.LEFT


def test_m6_retry_after_advances_the_clock(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_profile(tmp_path, monkeypatch, retry_attempts=1)
    clock = Clock()
    backend.budget_sleeper = clock.sleep
    seen = {"block": 0}

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            seen["block"] += 1
            if seen["block"] == 1:
                return RawResponse(429, {"retry-after": "30", "content-type": "application/json"}, b"")
            return r1.rpc(hex(10))
        if body["method"] == "eth_getLogs":
            return r1.rpc([])
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend, clock)
    assert code == 0, body
    assert seen["block"] == 2
    assert clock.now - r1.CLOCK >= 30


def test_m6_backoff_uses_the_random_source(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_profile(tmp_path, monkeypatch, retry_attempts=1)
    clock = Clock()
    backend.budget_sleeper = clock.sleep
    monkeypatch.setattr("guardian.relations.fetch.random.random", lambda: 1.0)
    seen = {"block": 0}

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            seen["block"] += 1
            if seen["block"] == 1:
                return RawResponse(500, {"content-type": "application/json"}, b"")
            return r1.rpc(hex(10))
        if body["method"] == "eth_getLogs":
            return r1.rpc([])
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend, clock)
    assert code == 0, body
    assert seen["block"] == 2
    assert clock.now - r1.CLOCK >= 0.5


def _nr_window(answer_row, transfers):
    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(HEAD))
        if body["method"] == "eth_getBlockByNumber":
            number = int(body["params"][0], 16)
            return answer_row(number, body["params"][0])
        if body["method"] == "nr_getAssetTransfers":
            return transfers(body["params"][0])
        raise FixtureMiss()

    return answer


def _kept_row():
    row = _nr_row(r1.OTHER, r1.DISPERSE, 3, block=140_000)
    row["metadata"] = {"blockTimestamp": INSIDE}
    return row


def test_m7_nodereal_page_cap_is_partial(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    row = _kept_row()

    def block(_number, raw):
        return r1.rpc({"number": raw, "timestamp": hex(INSIDE)})

    def transfers(params):
        if params["category"][0] == "20" and int(params["fromBlock"], 16) == 150_000:
            return r1.rpc({"transfers": [row]})
        return r1.rpc({"transfers": []})

    backend.add(lambda _call: True, _nr_window(block, transfers))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "page_cap"
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "partial"
    assert token["reason"] == "page_cap"
    assert body["counterparties"] != []
    assert body["counterparties"] is not None


def test_m7_nodereal_reaches_the_window(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    row = _kept_row()

    def block(number, raw):
        stamp = STOP if number == 150_000 else INSIDE
        return r1.rpc({"number": raw, "timestamp": hex(stamp)})

    def transfers(params):
        if params["category"][0] == "20" and int(params["fromBlock"], 16) == 150_000:
            return r1.rpc({"transfers": [row]})
        return r1.rpc({"transfers": []})

    backend.add(lambda _call: True, _nr_window(block, transfers))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "2", "--since-days", "90",
        "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "complete"
    assert body["reads"][0]["read_status"] == "complete"
    assert body["reads"][0]["reason"] == ""
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "complete"
    assert token["reason"] == ""
    assert body["counterparties"] != []
    assert body["counterparties"][0]["address"] == r1.OTHER


def test_m7_nodereal_missing_timestamp_is_window_unchecked(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    row = _kept_row()

    def block(number, raw):
        if number == 49_999:
            return r1.rpc({"number": raw})
        return r1.rpc({"number": raw, "timestamp": hex(INSIDE)})

    def transfers(params):
        if int(params["fromBlock"], 16) != 150_000:
            return r1.rpc({"transfers": []})
        category = params["category"][0]
        if category == "20":
            return r1.rpc({"transfers": [row]})
        native = {
            "blockNum": hex(140_000),
            "from": r1.OTHER,
            "hash": r1.tx(4 if category == "external" else 5),
            "isError": "0",
            "metadata": {"blockTimestamp": INSIDE},
            "to": r1.DISPERSE,
            "value": "4",
        }
        return r1.rpc({"transfers": [native]})

    backend.add(lambda _call: True, _nr_window(block, transfers))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "2", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "window_unchecked"
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "partial"
    assert token["reason"] == "window_unchecked"
    assert body["counterparties"] != []


def test_m7_nodereal_later_chunk_failure_keeps_rows(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    row = _kept_row()

    def block(_number, raw):
        return r1.rpc({"number": raw, "timestamp": hex(INSIDE)})

    def transfers(params):
        if int(params["fromBlock"], 16) != 150_000:
            return RawResponse(500, {"content-type": "application/json"}, b"")
        if params["category"][0] == "20":
            return r1.rpc({"transfers": [row]})
        return r1.rpc({"transfers": []})

    backend.add(lambda _call: True, _nr_window(block, transfers))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "2", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "http_5xx"
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "partial"
    assert token["reason"] == "http_5xx"
    assert body["counterparties"] != []
    assert body["counterparties"][0]["address"] == r1.OTHER


def test_m7_nodereal_first_chunk_failure_is_a_gap(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)

    def block(_number, raw):
        return r1.rpc({"number": raw, "timestamp": hex(INSIDE)})

    def transfers(_params):
        return RawResponse(500, {"content-type": "application/json"}, b"")

    backend.add(lambda _call: True, _nr_window(block, transfers))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "2", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "http_5xx"
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "gap"
    assert token["reason"] == "http_5xx"
    assert body["counterparties"] is None


def test_m7_club_page_cap_is_partial(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)
    log = _club_log(r1.OTHER, r1.DISPERSE, 2000, r1.tx(9))

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(2500))
        if body["method"] == "eth_getLogs":
            return r1.rpc([log])
        if body["method"] == "eth_getBlockByNumber":
            return r1.rpc({"number": body["params"][0], "timestamp": hex(INSIDE)})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "not_in_keyless_logs"
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "partial"
    assert token["reason"] == "page_cap"
    assert body["counterparties"] != []


def test_m7_club_reaches_the_window(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)
    log = _club_log(r1.OTHER, r1.DISPERSE, 2000, r1.tx(9))

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(2500))
        if body["method"] == "eth_getLogs":
            start = int(body["params"][0]["fromBlock"], 16)
            if start >= 1501:
                return r1.rpc([log])
            return r1.rpc([])
        if body["method"] == "eth_getBlockByNumber":
            number = int(body["params"][0], 16)
            stamp = STOP if number == 1501 else INSIDE
            return r1.rpc({"number": body["params"][0], "timestamp": hex(stamp)})
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "2", "--since-days", "90",
        "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "not_in_keyless_logs"
    token = _parts(body["reads"][0])["token-transfers"]
    assert token["read_status"] == "complete"
    assert token["reason"] == ""
    assert body["counterparties"][0]["address"] == r1.OTHER


def test_m1_optimism_key_is_not_sent(tmp_path, monkeypatch, capsys) -> None:
    key = "synthetic-optimism-key-fix98"
    sources = {
        "blockscout-optimism": r1._source(
            "blockscout", "BLOCKSCOUT_OPTIMISM_URL", "blockscout-optimism", "BLOCKSCOUT_API_KEY",
        ),
    }
    backend = r1.install(
        tmp_path, monkeypatch, networks=["optimism-mainnet"], sources=sources,
        routes=r1.history_routes("optimism-mainnet", ["blockscout-optimism"]),
        secrets={
            "BLOCKSCOUT_API_KEY": key,
            "BLOCKSCOUT_OPTIMISM_URL": "https://explorer.optimism.io",
        },
        address=r1.MONITORED, family="evm", account_networks=["optimism-mainnet"],
    )
    r1.allow_fixture_hosts(monkeypatch, "explorer.optimism.io")
    r1.fill_tails(backend, [], [])
    code, body, captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "optimism-mainnet",
    ), backend)
    assert code == 0, body
    assert body["plan"]["queries"][0]["host"] == "explorer.optimism.io"
    assert backend.calls
    for call in backend.calls:
        assert urlsplit(call.url).hostname == "explorer.optimism.io"
        assert not getattr(call, "credential_headers", ())
        assert key not in call.url
        assert "authorization" not in call.url.lower()
    assert key not in captured.out


def test_m1_old_optimism_host_without_a_key_still_rewrites(tmp_path, monkeypatch, capsys) -> None:
    sources = {
        "blockscout-optimism": r1._source(
            "blockscout", "BLOCKSCOUT_OPTIMISM_URL", "blockscout-optimism", "BLOCKSCOUT_API_KEY",
        ),
    }
    backend = r1.install(
        tmp_path, monkeypatch, networks=["optimism-mainnet"], sources=sources,
        routes=r1.history_routes("optimism-mainnet", ["blockscout-optimism"]),
        secrets={"BLOCKSCOUT_OPTIMISM_URL": "https://optimism.blockscout.com"},
        address=r1.MONITORED, family="evm", account_networks=["optimism-mainnet"],
    )
    r1.allow_fixture_hosts(monkeypatch, "explorer.optimism.io")
    r1.fill_tails(backend, [], [])
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "optimism-mainnet",
    ), backend)
    assert code == 0, body
    assert body["plan"]["queries"][0]["host"] == "explorer.optimism.io"
    assert body["reads"][0]["read_status"] == "complete"
    assert backend.calls
    assert all(urlsplit(call.url).hostname == "explorer.optimism.io" for call in backend.calls)
    assert all("optimism.blockscout.com" not in call.url for call in backend.calls)


def _redirect(location: str) -> RawResponse:
    return RawResponse(301, {"location": location}, b"")


def test_m2_club_redirect_is_not_followed(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)
    backend.add(lambda _call: True, _redirect("https://other.example.invalid/rpc"))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "redirect_host"
    assert body["counterparties"] is None
    assert all(urlsplit(call.url).hostname == "rpc-bsc.48.club" for call in backend.calls)


def test_m2_nodereal_redirect_is_not_followed(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)
    backend.add(lambda _call: True, _redirect("https://other.example.invalid/rpc"))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "redirect_host"
    assert all(urlsplit(call.url).hostname == "nodereal.example.invalid" for call in backend.calls)


def test_m2_explorer_redirect_is_not_followed(tmp_path, monkeypatch, capsys) -> None:
    sources = {
        "blockscout-optimism": r1._source("blockscout", "BLOCKSCOUT_OPTIMISM_URL", "blockscout-optimism"),
    }
    backend = r1.install(
        tmp_path, monkeypatch, networks=["optimism-mainnet"], sources=sources,
        routes=r1.history_routes("optimism-mainnet", ["blockscout-optimism"]),
        secrets={"BLOCKSCOUT_OPTIMISM_URL": "https://explorer.optimism.io"},
        address=r1.MONITORED, family="evm", account_networks=["optimism-mainnet"],
    )
    r1.allow_fixture_hosts(monkeypatch, "explorer.optimism.io")
    backend.add(lambda _call: True, _redirect("https://other.example.invalid/api"))
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "optimism-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "redirect_host"
    assert backend.calls
    assert all(urlsplit(call.url).hostname == "explorer.optimism.io" for call in backend.calls)


def test_m3_unregistered_jsonrpc_is_not_a_gap(tmp_path, monkeypatch, capsys) -> None:
    backend = _club_install(tmp_path, monkeypatch)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 1
    assert body["status"] == "error"
    assert "fixture_miss" in body["error"]
    assert "counterparties" not in body


def test_m3_unregistered_nodereal_page_is_not_a_gap(tmp_path, monkeypatch, capsys) -> None:
    backend = _nodereal_install(tmp_path, monkeypatch)

    def answer(call):
        body = r1.rpc_call(call)
        if body["method"] == "eth_blockNumber":
            return r1.rpc(hex(1000))
        raise FixtureMiss()

    backend.add(lambda _call: True, answer)
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 1
    assert body["status"] == "error"
    assert "fixture_miss" in body["error"]
    assert "counterparties" not in body


def test_m4_disabled_club_sends_nothing(tmp_path, monkeypatch, capsys) -> None:
    sources = {
        "bsc-logs-48club": {
            "adapter": "evm_rpc",
            "budget_group": "bsc-logs-48club",
            "enabled": False,
            "endpoint_env": "RPC_BSC_LOGS_URL",
        },
    }
    routes = [{"network": "bsc-mainnet", "capability": "history.token", "source_ids": ["bsc-logs-48club"]}]
    backend = r1.install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes, secrets={},
        address=r1.MONITORED, family="evm", account_networks=["bsc-mainnet"],
    )
    code, body, _captured = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", r1.DISPERSE, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0, body
    assert backend.calls == []
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "source_disabled"
    assert body["counterparties"] is None


def test_m5_missing_budget_group_uses_one_spec() -> None:
    slept: list[float] = []

    def book(retry: int | None):
        document = {}
        if retry is not None:
            document["bsc-logs-48club"] = {
                "max_concurrency": 1,
                "max_requests_per_day": 10,
                "max_rps": 5,
                "retry_attempts": retry,
            }
        document["other"] = {
            "max_concurrency": 1,
            "max_requests_per_day": 10,
            "max_rps": 5,
            "retry_attempts": 7,
        }
        return BudgetBook.from_document(document, clock=lambda: 1.0, sleep=slept.append)

    created = book(None)
    group = created.ensure_group("bsc-logs-48club", CLUB_BUDGET_SPEC)
    assert group.spec.retry_attempts == 3
    assert created.group("other").spec.retry_attempts == 7
    again = created.ensure_group(
        "bsc-logs-48club",
        BudgetSpec(max_rps=1, max_concurrency=1, max_requests_per_day=10, retry_attempts=9),
    )
    assert again.spec.retry_attempts == 3
    group.pause(30)
    assert slept == [30]
    kept = book(0).ensure_group("bsc-logs-48club", CLUB_BUDGET_SPEC)
    assert kept.spec.retry_attempts == 0


def _endpoint_values(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _ENDPOINT_KEYS and isinstance(value, str):
                yield value
            else:
                yield from _endpoint_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _endpoint_values(item)


def test_m9_hosts_stay_inside_relations() -> None:
    guardian = ROOT / "src" / "guardian"
    for path in guardian.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if "relations" in path.relative_to(guardian).parts:
            continue
        text = path.read_text(encoding="utf-8")
        scrubbed = text.replace(CITATION, "")
        for host in HOSTS:
            assert host not in scrubbed, path
        assert "48.club" not in scrubbed, path
    for folder in (
        ROOT / "config",
        ROOT / "examples",
        ROOT / "src" / "guardian" / "_resources" / "examples",
    ):
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8")
            for host in HOSTS:
                assert host not in text, path
            if path.suffix == ".json":
                values = "\n".join(_endpoint_values(json.loads(text)))
                assert "48.club" not in values, path
                assert "explorer.optimism.io" not in values, path
