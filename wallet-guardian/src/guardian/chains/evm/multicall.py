"""Multicall3 aggregate3(getEthBalance) encode and decode.

The contract address is fixed. Selectors are computed from the signatures.
This module does not store or compare a code hash: empty code is empty code.
A failed aggregate3 item is not a zero balance.
"""

from __future__ import annotations

from typing import Sequence

from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_AGGREGATE3 = "aggregate3((address,bool,bytes)[])"
_GET_ETH_BALANCE = "getEthBalance(address)"
AGGREGATE3_SELECTOR = function_signature_to_4byte_selector(_AGGREGATE3)
GET_ETH_BALANCE_SELECTOR = function_signature_to_4byte_selector(_GET_ETH_BALANCE)


def bytecode_present(code: str) -> bool | None:
    """True when ``eth_getCode`` returned non-empty code.

    ``None`` means the hex is unusable, which is a failed read, not a fallback.
    ``0x``, ``0x0``, and all-zero bytes are empty. No hash is derived.
    """
    if not isinstance(code, str) or not code.startswith(("0x", "0X")):
        return None
    body = code[2:]
    if body == "":
        return False
    if len(body) % 2 == 1:
        body = "0" + body
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        return None
    return any(byte != 0 for byte in raw)


def encode_aggregate3(wallets: Sequence[str]) -> str:
    """One ``aggregate3`` call. Each item allows failure so one wallet cannot become zero."""
    calls = []
    for wallet in wallets:
        inner = GET_ETH_BALANCE_SELECTOR + encode(["address"], [wallet])
        calls.append((MULTICALL3, True, inner))
    body = encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (AGGREGATE3_SELECTOR + body).hex()


def decode_aggregate3_balances(data: str, wallets: Sequence[str]) -> list[int | None] | None:
    """Per-wallet wei, or ``None`` for that wallet when its item failed.

    ``None`` as the return value means the whole payload is unusable.
    A failed item is ``None``, never ``0``. A successful zero word is ``0``.
    """
    raw = _hex_bytes(data)
    if raw is None:
        return None
    try:
        rows = decode(["(bool,bytes)[]"], raw)[0]
    except Exception:
        return None
    if len(rows) != len(wallets):
        return None
    found: list[int | None] = []
    for success, blob in rows:
        if success is not True:
            found.append(None)
            continue
        if not isinstance(blob, (bytes, bytearray)) or len(blob) != 32:
            found.append(None)
            continue
        found.append(int.from_bytes(blob, "big"))
    return found


def decode_aggregate3_calls(data: str) -> list[tuple[str, bool, bytes]] | None:
    """Decode calldata after the selector. Used to check the request, not a balance."""
    raw = _hex_bytes(data)
    if raw is None or len(raw) < 4 or raw[:4] != AGGREGATE3_SELECTOR:
        return None
    try:
        rows = decode(["(address,bool,bytes)[]"], raw[4:])[0]
    except Exception:
        return None
    found = []
    for target, allow_failure, blob in rows:
        if not isinstance(target, str):
            return None
        found.append(("0x" + target[2:].lower(), bool(allow_failure), bytes(blob)))
    return found


def balance_address(calldata: bytes) -> str | None:
    """The address inside one ``getEthBalance`` calldata word, if that is what it is."""
    if len(calldata) != 36 or calldata[:4] != GET_ETH_BALANCE_SELECTOR:
        return None
    try:
        address = decode(["address"], calldata[4:])[0]
    except Exception:
        return None
    if not isinstance(address, str):
        return None
    return "0x" + address[2:].lower()


def _hex_bytes(data: str) -> bytes | None:
    if not isinstance(data, str) or not data.startswith(("0x", "0X")) or len(data) % 2 != 0:
        return None
    try:
        return bytes.fromhex(data[2:])
    except ValueError:
        return None
