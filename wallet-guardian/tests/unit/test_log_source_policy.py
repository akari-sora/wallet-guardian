"""Log error classification. Policy refusals are not bisected. A real range cap still is."""

from __future__ import annotations

from guardian.chains.evm.adapter import evm_rpc
from guardian.scan.window import log_source_refusal, range_refusal, read_logs


class _Result:
    def __init__(self, body: dict, *, ok: bool = False, reason: str = "rpc_error") -> None:
        self.body = body
        self.ok = ok
        self.reason = reason
        self.retryable = False


class _Transport:
    def __init__(self, decide) -> None:
        self.decide = decide
        self.calls = 0

    def request(self, _source_id: str, operation):
        self.calls += 1
        params = operation.params[0]
        start = int(params["fromBlock"], 16)
        end = int(params["toBlock"], 16)
        return self.decide(start, end)


def _read(transport, start: int, end: int):
    return read_logs(
        transport,
        "rpc-fixture",
        lambda leaf_start, leaf_end: evm_rpc("eth_getLogs", [{"fromBlock": hex(leaf_start), "toBlock": hex(leaf_end), "topics": []}]),
        start,
        end,
        wallets=set(),
        retry_attempts=0,
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
    )


def test_addressless_filter_is_not_bisected() -> None:
    body = {"error": {"code": -32701, "message": "please specify an address"}}
    transport = _Transport(lambda _start, _end: _Result(body))
    read = _read(transport, 1, 80)
    assert transport.calls == 1
    assert read.reason == "filter_requires_address"
    assert read.gap is None
    assert read.refused_range is False
    assert log_source_refusal(body, "", 1, 80) == "filter_requires_address"
    assert range_refusal(body, "") is False


def test_one_block_limit_exceeded_is_method_disabled() -> None:
    body = {"error": {"code": -32005, "message": "limit exceeded"}}
    transport = _Transport(lambda _start, _end: _Result(body))
    read = _read(transport, 4, 4)
    assert transport.calls == 1
    assert read.reason == "method_disabled"
    assert read.gap is None
    assert "single_block_unreadable" not in (read.gap or "")


def test_wide_limit_exceeded_is_not_method_disabled() -> None:
    body = {"error": {"code": -32005, "message": "limit exceeded"}}
    assert log_source_refusal(body, "", 1, 40) is None


def test_maximum_block_cap_still_bisects() -> None:
    def decide(start: int, end: int):
        if end > start:
            return _Result({"error": {"code": -32005, "message": "maximum 1000 blocks"}})
        return _Result({"result": []}, ok=True, reason="")

    transport = _Transport(decide)
    read = _read(transport, 1, 4)
    assert transport.calls > 1
    assert read.ok is True
    assert read.covered_end == 4


def test_named_range_phrases_still_bisect() -> None:
    for message in ("blocks range is too wide", "query ranges over the cap"):
        body = {"error": {"code": -32602, "message": message}}
        assert range_refusal(body, "") is True
        transport = _Transport(lambda _start, _end, body=body: _Result(body))
        read = _read(transport, 10, 13)
        assert transport.calls > 1
        assert read.refused_range is True or read.ok is False


def test_single_block_timeout_stays_unreadable() -> None:
    body = {"error": {"code": -32000, "message": "timeout"}}
    transport = _Transport(lambda _start, _end: _Result(body, reason="timeout"))
    read = _read(transport, 7, 7)
    assert transport.calls == 1
    assert read.gap == "single_block_unreadable:7"
    assert read.reason == "timeout"
    assert log_source_refusal(body, "timeout", 7, 7) is None
