"""TRON ABI words for constant reads.

A TRON address is 21 bytes (0x41 plus 20 bytes). An ABI address argument is
the 20-byte payload, left-padded to 32 bytes. The leading 0x41 is not part
of the word. Only the allowlisted read selectors may be simulated.
"""

from __future__ import annotations

from guardian.errors import SecurityError
from guardian.identities import IdentityError, parse_address

READ_SELECTORS = {
    "balanceOf(address)": "70a08231",
    "allowance(address,address)": "dd62ed3e",
}


def assert_read_selector(selector: str) -> None:
    """Refuse every selector that is not an allowlisted read."""
    if selector not in READ_SELECTORS:
        raise SecurityError("selector_rejected", reason="selector_rejected")


def abi_address_word(address: str) -> str:
    """32-byte hex word. Uses the 20-byte payload, not the 21-byte address."""
    parsed = parse_address("tron", address)
    raw = bytes.fromhex(parsed.identity_key)
    if len(raw) != 21 or raw[0] != 0x41:
        raise IdentityError("bad_tron_prefix")
    return raw[1:].rjust(32, b"\x00").hex()
