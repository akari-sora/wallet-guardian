"""MCM-W2-02. Shared quotas, bounded retry, and incomplete pagination."""

from __future__ import annotations

import email.utils
import os
import stat

import pytest

from guardian.errors import SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted, BudgetGroup, BudgetSpec, usage_path
from guardian.sources.fixtures import FixtureFailure, RawResponse, request_key
from guardian.sources.pagination import walk_page_key, walk_page_offset
from guardian.sources.retry import classify_response, read_with_retry
from guardian.sources.transport import TransportError
from mcm_w2_support import SYNTH, budget, clock_pair, harness, install, json_body, source

RPC = {"id": 1, "jsonrpc": "2.0", "result": "0x1"}


def _op(method: str = "eth_getBalance") -> Operation:
    return Operation("jsonrpc", "evm_rpc", method=method, params=[SYNTH, "latest"])


def _open(retry_attempts: int = 0, **extra):
    now, slept, clock, sleep = clock_pair()
    transport, backend, clock, sleep = harness(
        {"rpc": source("evm_rpc", "RPC_URL", "readers")},
        {"readers": budget(retry_attempts=retry_attempts, **extra)},
        {"RPC_URL": "https://rpc.example.invalid/rpc"},
        retry_clock=(now, slept, clock, sleep),
    )
    return transport, backend, slept, clock, sleep


def _script(transport, operation: Operation, responses) -> None:
    call = transport.encode("rpc", operation)
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = list(responses)


def _retry(transport, operation: Operation, sleep, clock, *, max_wait: float = 30):
    return read_with_retry(
        lambda: transport.request("rpc", operation),
        retry_attempts=transport.retry_attempts("rpc"),
        sleep=sleep,
        random=lambda: 1.0,
        clock=clock,
        max_wait=max_wait,
    )


def test_mcm_w2_02_shared_budget_group_is_one_quota():
    now, slept, clock, sleep = clock_pair()
    transport, backend, _clock, _sleep = harness(
        {
            "east": source("evm_rpc", "EAST_URL", "shared"),
            "west": source("evm_rpc", "WEST_URL", "shared"),
            "other": source("evm_rpc", "OTHER_URL", "separate"),
        },
        {
            "shared": budget(retry_attempts=0, max_requests_per_day=2),
            "separate": budget(retry_attempts=0, max_requests_per_day=5),
        },
        {
            "EAST_URL": "https://east.example.invalid/rpc",
            "OTHER_URL": "https://other.example.invalid/rpc",
            "WEST_URL": "https://west.example.invalid/rpc",
        },
        retry_clock=(now, slept, clock, sleep),
    )
    operation = _op()
    for source_id, value in (("east", "0x1"), ("west", "0x2")):
        install(transport, source_id, operation, json_body({"id": 1, "jsonrpc": "2.0", "result": value}))
        result = transport.request(source_id, operation)
        assert int(result.body["result"], 16) > 0
    assert transport.budgets.group("shared") is transport.budgets.group("shared")
    assert transport.budgets.group("shared").requests_today == 2
    with pytest.raises(BudgetExhausted) as caught:
        transport.request("east", operation)
    assert caught.value.reason == "daily_cap"
    assert len(backend.calls) == 2
    install(transport, "other", operation, json_body({"id": 1, "jsonrpc": "2.0", "result": "0x3"}))
    other = transport.request("other", operation)
    assert int(other.body["result"], 16) == 3
    assert transport.budgets.group("separate").requests_today == 1
    assert transport.budgets.group("shared").requests_today == 2


def test_mcm_w2_02_rps_concurrency_daily_and_credit_caps():
    _now, slept, clock, sleep = clock_pair()
    spec = BudgetSpec.from_document({
        "max_concurrency": 1,
        "max_credits_per_month": 20,
        "max_requests_per_day": 3,
        "max_rps": 1,
        "retry_attempts": 0,
    })
    group = BudgetGroup("caps", spec, clock=clock, sleep=sleep)
    first = group.acquire(priority="normal")
    assert slept == []
    assert group.requests_today == 1
    with pytest.raises(BudgetExhausted) as caught:
        group.acquire(priority="high")
    assert caught.value.reason == "concurrency"
    assert group.requests_today == 1
    first.release()
    group.acquire(priority="normal").release()
    assert slept == [1.0]
    group.acquire(priority="health", credits=1).release()
    assert group.requests_today == 3
    with pytest.raises(BudgetExhausted) as caught:
        group.acquire(priority="health")
    assert caught.value.reason == "daily_cap"

    credit_spec = BudgetSpec.from_document({
        "max_concurrency": 2,
        "max_credits_per_month": 20,
        "max_requests_per_day": 50,
        "max_rps": 1000,
        "retry_attempts": 0,
    })
    credits = BudgetGroup("credits", credit_spec, clock=clock, sleep=sleep)
    credits.acquire(priority="normal", credits=10).release()
    credits.acquire(priority="health", credits=10).release()
    assert credits.credits_month == 20
    with pytest.raises(BudgetExhausted) as caught:
        credits.acquire(priority="health", credits=1)
    assert caught.value.reason == "credit_cap"
    assert credits.credits_month == 20


def test_mcm_w2_02_low_priority_stops_before_the_hard_cap():
    _now, _slept, clock, sleep = clock_pair()
    spec = BudgetSpec.from_document({
        "max_concurrency": 2,
        "max_requests_per_day": 2,
        "max_rps": 1000,
        "retry_attempts": 0,
    })
    group = BudgetGroup("reserve", spec, clock=clock, sleep=sleep)
    group.acquire(priority="low").release()
    with pytest.raises(BudgetExhausted) as caught:
        group.acquire(priority="low")
    assert caught.value.reason == "low_priority"
    assert group.requests_today == 1
    group.acquire(priority="health").release()
    assert group.requests_today == 2
    with pytest.raises(BudgetExhausted) as caught:
        group.acquire(priority="high")
    assert caught.value.reason == "daily_cap"

    credit_spec = BudgetSpec.from_document({
        "max_concurrency": 2,
        "max_credits_per_month": 20,
        "max_requests_per_day": 50,
        "max_rps": 1000,
        "retry_attempts": 0,
    })
    credits = BudgetGroup("credit-reserve", credit_spec, clock=clock, sleep=sleep)
    credits.acquire(priority="low", credits=10).release()
    with pytest.raises(BudgetExhausted) as caught:
        credits.acquire(priority="low", credits=10)
    assert caught.value.reason == "low_priority"
    assert credits.credits_month == 10
    credits.acquire(priority="health", credits=10).release()
    assert credits.credits_month == 20


def test_mcm_w2_02_retry_after_seconds_http_date_and_cap():
    operation = _op()
    transport, backend, slept, clock, sleep = _open(retry_attempts=1)
    _script(transport, operation, [
        RawResponse(429, {"Retry-After": "2"}, b""),
        json_body({"id": 1, "jsonrpc": "2.0", "result": "0x2"}),
    ])
    result = _retry(transport, operation, sleep, clock)
    assert int(result.body["result"], 16) == 2
    assert slept == [2.0]
    assert len(backend.calls) == 2

    transport, backend, slept, clock, sleep = _open(retry_attempts=1)
    header = email.utils.formatdate(clock() + 5, usegmt=True)
    _script(transport, operation, [
        RawResponse(429, {"Retry-After": header}, b""),
        json_body({"id": 1, "jsonrpc": "2.0", "result": "0x5"}),
    ])
    result = _retry(transport, operation, sleep, clock)
    assert int(result.body["result"], 16) == 5
    assert slept == [5.0]

    transport, _backend, slept, clock, sleep = _open(retry_attempts=1)
    _script(transport, operation, [
        RawResponse(429, {"Retry-After": "99999"}, b""),
        json_body({"id": 1, "jsonrpc": "2.0", "result": "0x6"}),
    ])
    result = _retry(transport, operation, sleep, clock, max_wait=30)
    assert int(result.body["result"], 16) == 6
    assert slept == [30.0]


def test_mcm_w2_02_etherscan_rate_limit_is_retried_and_empty_is_not_zero():
    limited = {
        "message": "NOTOK",
        "result": "Max rate limit reached, please use API Key for higher rate limit",
        "status": "0",
    }
    classified = classify_response(200, limited)
    assert classified.category == "rate_limited"
    assert classified.retryable is True
    now, slept, clock, sleep = clock_pair()
    transport, backend, clock, sleep = harness(
        {"scan": source("etherscan_v2", "SCAN_URL", "scan")},
        {"scan": budget(retry_attempts=1)},
        {"SCAN_URL": "https://api.example.invalid"},
        retry_clock=(now, slept, clock, sleep),
    )
    operation = Operation(
        "rest",
        "etherscan_v2",
        http_method="GET",
        path="/v2/api",
        query={"action": "balance", "address": SYNTH, "module": "account"},
    )
    call = transport.encode("scan", operation)
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = [
        json_body(limited),
        json_body({"message": "OK", "result": "7", "status": "1"}),
    ]
    result = read_with_retry(
        lambda: transport.request("scan", operation),
        retry_attempts=transport.retry_attempts("scan"),
        sleep=sleep,
        random=lambda: 1.0,
        clock=clock,
    )
    assert int(result.body["result"]) == 7
    assert slept == [0.5]
    assert len(backend.calls) == 2
    assert transport.budgets.group("scan").requests_today == 2

    empty_body = {"message": "No transactions found", "result": "No transactions found", "status": "0"}
    history = Operation(
        "rest",
        "etherscan_v2",
        http_method="GET",
        path="/v2/api",
        query={"action": "txlist", "address": SYNTH, "module": "account"},
    )
    install(transport, "scan", history, json_body(empty_body))
    empty = transport.request("scan", history)
    assert empty.category == "empty"
    assert empty.quality == "complete_in_declared_scope"
    assert empty.body["result"] == "No transactions found"
    assert empty.body["result"] != "0"
    assert empty.body["result"] != 0


def test_mcm_w2_02_jsonrpc_error_is_not_an_empty_result():
    transport, backend, _slept, _clock, _sleep = _open()
    operation = _op()
    install(transport, "rpc", operation, json_body({
        "error": {"code": -32000, "message": "down"},
        "id": 1,
        "jsonrpc": "2.0",
    }))
    result = transport.request("rpc", operation)
    assert result.ok is False
    assert result.quality == "error"
    assert result.reason == "rpc_error"
    assert result.body != []
    assert result.body != 0
    assert "result" not in result.body
    assert result.body["error"]["code"] == -32000
    assert len(backend.calls) == 1


def test_mcm_w2_02_auth_and_client_errors_are_not_retried():
    operation = _op()
    for status, reason in ((401, "credential_rejected"), (403, "forbidden"), (400, "bad_request")):
        transport, backend, slept, clock, sleep = _open(retry_attempts=3)
        _script(transport, operation, [RawResponse(status, {}, b"no")])
        result = _retry(transport, operation, sleep, clock)
        assert result.ok is False
        assert result.reason == reason
        assert result.body is None
        assert result.retryable is False
        assert slept == []
        assert len(backend.calls) == 1
        assert transport.budgets.group("readers").requests_today == 1
        if status in {401, 403}:
            assert result.blocked is True


def test_mcm_w2_02_timeout_stops_at_the_retry_cap():
    transport, backend, _slept, clock, sleep = _open(retry_attempts=2)
    operation = _op()
    _script(transport, operation, [FixtureFailure("timeout") for _ in range(5)])
    with pytest.raises(TransportError) as caught:
        _retry(transport, operation, sleep, clock)
    assert caught.value.reason == "timeout"
    assert caught.value.retryable is True
    assert len(backend.calls) == 3
    assert transport.budgets.group("readers").requests_today == 3


def test_mcm_w2_02_response_too_large_and_bad_json_are_errors():
    now, slept, clock, sleep = clock_pair()
    transport, backend, _clock, _sleep = harness(
        {"rpc": source("evm_rpc", "RPC_URL", "readers")},
        {"readers": budget(retry_attempts=2)},
        {"RPC_URL": "https://rpc.example.invalid/rpc"},
        max_response_bytes=64,
        retry_clock=(now, slept, clock, sleep),
    )
    huge = _op("eth_chainId")
    install(transport, "rpc", huge, RawResponse(200, {}, b"x" * 80))
    with pytest.raises(TransportError) as caught:
        transport.request("rpc", huge)
    assert caught.value.reason == "response_too_large"
    assert len(backend.calls) == 1
    broken = _op("eth_getCode")
    install(transport, "rpc", broken, RawResponse(200, {}, b"{"))
    with pytest.raises(TransportError) as caught:
        transport.request("rpc", broken)
    assert caught.value.reason == "bad_json"
    blank = _op("eth_blockNumber")
    install(transport, "rpc", blank, RawResponse(200, {}, b""))
    with pytest.raises(TransportError) as caught:
        transport.request("rpc", blank)
    assert caught.value.reason == "bad_json"
    operation = _op()
    install(transport, "rpc", operation, json_body(RPC))
    result = transport.request("rpc", operation)
    assert int(result.body["result"], 16) == 1
    assert len(backend.calls) == 4


def test_mcm_w2_02_page_cap_is_not_complete():
    done = walk_page_offset(lambda page: [9], page_cap=3, item_cap=10, offset=2)
    assert done.pagination_complete is True
    assert done.stop_reason == "exhausted"
    assert done.items == (9,)
    assert done.items[0] != 0
    capped = walk_page_offset(lambda page: [1, 2], page_cap=1, item_cap=10, offset=2)
    assert capped.pagination_complete is False
    assert capped.stop_reason == "page_cap"
    assert capped.pages_fetched == 1
    trimmed = walk_page_offset(lambda page: [1, 2, 3], page_cap=5, item_cap=2, offset=2)
    assert trimmed.pagination_complete is False
    assert trimmed.stop_reason == "item_cap"
    assert trimmed.items == (1, 2)
    pages = {
        None: {"pageKey": "next", "transfers": [{"value": "5"}]},
        "next": {"transfers": [{"value": "7"}]},
    }
    walked = walk_page_key(lambda token: pages[token], page_cap=5, item_cap=10)
    assert walked.pagination_complete is True
    assert walked.stop_reason == "exhausted"
    assert walked.items[0]["value"] == "5"
    limited = walk_page_key(
        lambda token: {"pageKey": "next", "transfers": [{"value": "5"}]},
        page_cap=1,
        item_cap=10,
    )
    assert limited.pagination_complete is False
    assert limited.stop_reason == "page_cap"
    missing = walk_page_key(lambda token: {"oops": []}, page_cap=3, item_cap=10)
    assert missing.pagination_complete is False
    assert missing.stop_reason == "error"
    assert missing.items == ()

    def blocked(_page: int):
        raise BudgetExhausted("daily_cap")

    paused = walk_page_offset(blocked, page_cap=3, item_cap=10, offset=2)
    assert paused.pagination_complete is False
    assert paused.stop_reason == "budget"

    def denied(_page: int):
        raise SecurityError("method_denied", reason="method_denied")

    with pytest.raises(SecurityError) as caught:
        walk_page_offset(denied, page_cap=2, item_cap=4, offset=1)
    assert caught.value.reason == "method_denied"


def test_mcm_w2_02_usage_reloads(tmp_path):
    _now, _slept, clock, sleep = clock_pair()
    spec = BudgetSpec.from_document({
        "max_concurrency": 1,
        "max_requests_per_day": 10,
        "max_rps": 1000,
        "retry_attempts": 0,
    })
    group = BudgetGroup("shared", spec, clock=clock, sleep=sleep)
    group.acquire().release()
    group.acquire().release()
    path = usage_path(tmp_path, group.name)
    group.save(path)
    assert path.is_file()
    assert not path.with_name(path.name + ".tmp").exists()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    loaded = BudgetGroup.load(path, spec, clock=clock, sleep=sleep)
    assert loaded.requests_today == 2
    assert loaded.credits_month == 2
    loaded.acquire().release()
    assert loaded.requests_today == 3
