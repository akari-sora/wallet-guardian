"""WP-5 offline fixtures. No socket. Status 2 stays partial. v2 does not send limit."""

from __future__ import annotations

import json

from guardian.balances.internal import normalize_internal_batch
from guardian.sources.blockscout import BlockscoutClient

WALLET = "0x0000000000000000000000000000000000000001"
OTHER = "0x0000000000000000000000000000000000000002"
TX = "0x" + "ab" * 32
WEI_06 = 6 * 10**17


class _Result:
    def __init__(self, body, *, status: int = 200, ok: bool = True, reason: str = "", detail: str = "") -> None:
        self.body = body
        self.status = status
        self.ok = ok
        self.reason = reason
        self.detail = detail
        self.retryable = False
        self.blocked = status == 403


class _Transport:
    def __init__(self, pages: list) -> None:
        self.pages = list(pages)
        self.calls = []

    def retry_attempts(self, _source_id: str) -> int:
        return 0

    def request(self, _source_id: str, operation):
        self.calls.append(operation)
        page = self.pages.pop(0)
        if isinstance(page, _Result):
            return page
        return _Result(page)


def _client(pages, *, credential: str = "") -> tuple[BlockscoutClient, _Transport]:
    transport = _Transport(pages)
    client = BlockscoutClient(transport, "blockscout", credential=credential or None)
    return client, transport


def _internal(block: int, value: str = str(WEI_06)) -> dict:
    return {
        "block_number": block,
        "error": None,
        "from": {"hash": OTHER},
        "index": 3,
        "success": True,
        "to": {"hash": WALLET},
        "transaction_hash": TX,
        "type": "call",
        "value": value,
    }


def test_txlistinternal_status_2_is_partial_indexer_lag_and_cursor_stays() -> None:
    client, transport = _client([
        {"message": "Internal txs have not yet been processed", "result": None, "status": "2"},
    ])
    read = client.account_history(
        action="txlistinternal",
        address=WALLET,
        start_block=1,
        end_block=20,
        previous_cursor="9",
    )
    assert read.category == "partial"
    assert read.reason == "indexer_lag"
    assert read.reason_codes == ("indexer_lag",)
    assert read.pagination_complete is False
    assert read.cursor == "9"
    assert read.items == ()
    assert read.category != "empty"
    assert len(transport.calls) == 1
    again, _transport = _client([
        {"message": "OK", "result": "not yet been processed", "status": "0"},
    ])
    lagged = again.account_history(
        action="txlistinternal",
        address=WALLET,
        start_block=1,
        end_block=20,
        previous_cursor="9",
    )
    assert lagged.category == "partial"
    assert lagged.reason == "indexer_lag"
    assert lagged.cursor == "9"
    assert lagged.pagination_complete is False


def test_v2_internal_stops_at_cursor_and_does_not_send_limit() -> None:
    client, transport = _client([
        {
            "items": [_internal(12), _internal(4)],
            "next_page_params": {"block_number": 4, "index": 1, "items_count": 50, "limit": 50},
        },
    ])
    read = client.read_v2(WALLET, "internal-transactions", stop_block=9, previous_cursor="9")
    assert read.pagination_complete is True
    assert read.category == "ok"
    assert len(read.items) == 1
    assert read.items[0]["block_number"] == 12
    assert len(transport.calls) == 1
    operation = transport.calls[0]
    assert operation.path.endswith("/internal-transactions")
    assert operation.path.startswith("/api/v2/addresses/")
    assert operation.query in (None, {})
    assert operation.headers is None
    blob = json.dumps({"path": operation.path, "query": operation.query})
    assert "limit" not in blob
    assert "api.blockscout.com" not in blob


def test_v2_follows_next_page_without_limit_and_stops() -> None:
    client, transport = _client([
        {
            "items": [_internal(18)],
            "next_page_params": {"block_number": 18, "index": 0, "limit": 50},
        },
        {
            "items": [_internal(11), _internal(2)],
            "next_page_params": {"block_number": 2, "index": 0},
        },
    ])
    read = client.read_v2(
        WALLET,
        "transactions",
        stop_block=10,
        previous_cursor="10",
    )
    assert [item["block_number"] for item in read.items] == [18, 11]
    assert read.capability == "history.native_external"
    assert len(transport.calls) == 2
    assert transport.calls[1].query == {"block_number": "18", "index": "0"}
    assert "limit" not in transport.calls[1].query
    token = _client([
        {"items": [_internal(7)], "next_page_params": None},
    ])
    fungible = token[0].read_v2(WALLET, "token-transfers", stop_block=1, previous_cursor="1")
    assert fungible.capability == "history.fungible"
    assert fungible.pagination_complete is True


def test_v2_bearer_is_a_header_on_the_same_path() -> None:
    client, transport = _client(
        [{"items": [_internal(12)], "next_page_params": None}],
        credential="synthetic-blockscout-key",
    )
    read = client.read_v2(WALLET, "internal-transactions", stop_block=0, previous_cursor="0")
    assert read.category == "ok"
    operation = transport.calls[0]
    assert operation.headers == ("authorization",)
    assert operation.path.startswith("/api/v2/addresses/")
    assert "synthetic-blockscout-key" not in operation.path
    assert "apikey" not in json.dumps(operation.query)


def test_missing_items_bad_page_and_cloudflare_are_not_empty() -> None:
    missing, _transport = _client([{"message": "OK", "result": []}])
    read = missing.read_v2(WALLET, "internal-transactions", stop_block=0, previous_cursor="4")
    assert read.category == "error"
    assert read.reason == "missing_items"
    assert read.cursor == "4"
    assert read.pagination_complete is False
    assert read.category != "empty"

    bad, _transport = _client([
        {"items": [_internal(12)], "next_page_params": []},
    ])
    refused = bad.read_v2(WALLET, "internal-transactions", stop_block=0, previous_cursor="4")
    assert refused.category == "error"
    assert refused.reason == "bad_page"
    assert refused.cursor == "4"
    assert refused.items == ()
    assert refused.category != "empty"

    challenged, _transport = _client([
        _Result(None, status=403, ok=False, reason="forbidden", detail="<title>Just a moment...</title>"),
    ])
    challenge = challenged.read_v2(WALLET, "internal-transactions", stop_block=0, previous_cursor="4")
    assert challenge.reason == "bot_challenge"
    assert challenge.blocked is True
    assert challenge.category == "error"
    assert challenge.cursor == "4"
    assert challenge.category != "empty"


def test_blockscout_row_normalizes_to_evm_internal() -> None:
    parsed = normalize_internal_batch([_internal(12)], chain_id=10, owner=WALLET)
    assert parsed.gap is None
    assert len(parsed.rows) == 1
    row = parsed.rows[0]
    assert row["transfer_origin"] == "evm_internal"
    assert row["value"] == str(WEI_06)
    assert row["hash"] == TX
    assert row["to"] == WALLET
    assert row["execution_evidence"] == "evidenced"
    assert int(row["value"]) >= 5 * 10**17
    failed = dict(_internal(12))
    failed["success"] = False
    dropped = normalize_internal_batch([failed], chain_id=10, owner=WALLET)
    assert dropped.rows == ()
    assert dropped.gap is None


def test_dedup_r2_page_cap_ignores_illegal_next_params() -> None:
    """A full page at the cap keeps the previous cap result when the unused next cursor is illegal."""
    page = {"items": [_internal(12)], "next_page_params": {"block_number": None}}
    client, transport = _client([page])
    read = client.read_v2(WALLET, "internal-transactions", page_cap=1, stop_block=0, previous_cursor="4")
    assert len(transport.calls) == 1
    assert read.category == "error"
    assert read.reason == "page_cap"
    assert read.items == ()
    assert read.pagination_complete is False
    assert read.category != "empty"
    kept_client, kept_transport = _client([dict(page)])
    kept = kept_client.read_v2(
        WALLET,
        "internal-transactions",
        page_cap=1,
        keep_partial=True,
        stop_block=0,
        previous_cursor="4",
    )
    assert len(kept_transport.calls) == 1
    assert kept.category == "partial"
    assert kept.reason == "page_cap"
    assert kept.pagination_complete is False
    assert [item["block_number"] for item in kept.items] == [12]


def test_dedup_r2_default_repeated_cursor_is_an_error_without_rows() -> None:
    """The default read_v2 path reports repeated_cursor and does not return rows to submit."""
    client, transport = _client([
        {"items": [_internal(18)], "next_page_params": {"block_number": 12, "index": 1}},
        {"items": [_internal(11)], "next_page_params": {"block_number": 12, "index": 1}},
    ])
    read = client.read_v2(WALLET, "internal-transactions", stop_block=0, previous_cursor="0")
    assert len(transport.calls) == 2
    assert transport.calls[1].query == {"block_number": "12", "index": "1"}
    assert read.category == "error"
    assert read.reason == "repeated_cursor"
    assert read.items == ()
    assert read.pagination_complete is False
    assert read.category != "empty"


def test_txlist_status_1_is_unchanged() -> None:
    client, _transport = _client([
        {"message": "OK", "result": [{"hash": TX, "value": "1"}], "status": "1"},
    ])
    read = client.account_history(
        action="txlist",
        address=WALLET,
        start_block=1,
        end_block=9,
        previous_cursor="1",
    )
    assert read.category == "ok"
    assert read.pagination_complete is True
    assert read.cursor == "9"
    assert read.reason != "indexer_lag"
