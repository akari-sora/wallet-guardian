"""EVM address parsing. Mixed-case input is never rewritten into a different account."""

from __future__ import annotations

from eth_utils import is_checksum_address, to_checksum_address

from guardian.errors import ConfigError


def parse_evm_address(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("address must be a string")
    if len(value) > 64:
        raise ConfigError("address too long")
    raw = value.strip()
    if len(raw) != 42 or not raw.startswith("0x"):
        raise ConfigError("expected a 20-byte hex address")
    body = raw[2:]
    if any(ch not in "0123456789abcdefABCDEF" for ch in body):
        raise ConfigError("address contains non-hex characters")
    mixed = not (body.islower() or body.isupper())
    if mixed:
        if not is_checksum_address(raw):
            raise ConfigError("checksum mismatch; refusing to rewrite the address")
        return raw
    return to_checksum_address(raw)


def same_account(left: str, right: str) -> bool:
    return parse_evm_address(left) == parse_evm_address(right)
