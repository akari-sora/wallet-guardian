"""Multicall3 aggregate3(getEthBalance). Selectors are computed. Empty code is not a hash."""

from __future__ import annotations

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

from guardian.chains.evm.multicall import (
    AGGREGATE3_SELECTOR,
    GET_ETH_BALANCE_SELECTOR,
    MULTICALL3,
    balance_address,
    bytecode_present,
    decode_aggregate3_balances,
    decode_aggregate3_calls,
    encode_aggregate3,
)

WALLET = "0x0000000000000000000000000000000000000001"


def test_selectors_are_computed_from_signatures() -> None:
    assert AGGREGATE3_SELECTOR == function_signature_to_4byte_selector("aggregate3((address,bool,bytes)[])")
    assert GET_ETH_BALANCE_SELECTOR == function_signature_to_4byte_selector("getEthBalance(address)")
    assert MULTICALL3 == "0xcA11bde05977b3631167028862bE2a173976CA11"


def test_empty_code_is_not_a_hash_and_not_present() -> None:
    assert bytecode_present("0x") is False
    assert bytecode_present("0x0") is False
    assert bytecode_present("0x00") is False
    assert bytecode_present("0x01") is True
    assert bytecode_present("nope") is None


def test_failed_aggregate_item_is_not_zero() -> None:
    payload = "0x" + encode(["(bool,bytes)[]"], [[(False, (0).to_bytes(32, "big"))]]).hex()
    assert decode_aggregate3_balances(payload, [WALLET]) == [None]
    good = "0x" + encode(["(bool,bytes)[]"], [[(True, (5).to_bytes(32, "big"))]]).hex()
    assert decode_aggregate3_balances(good, [WALLET]) == [5]
    calls = decode_aggregate3_calls(encode_aggregate3([WALLET]))
    assert calls is not None
    target, allow_failure, blob = calls[0]
    assert target == MULTICALL3.lower()
    assert allow_failure is True
    assert balance_address(blob) == WALLET
