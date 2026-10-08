"""Address-family parsing. The declared family is the only family.

Nothing here infers one chain's address from another, signs, or opens a socket.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

from eth_utils import is_checksum_address, to_checksum_address

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATORS = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_BECH32M = 0x2BC830A3
_TON_RAW = re.compile(r"^(-?(?:0|[1-9][0-9]*)):([0-9a-fA-F]{64})$")
_MOVE = re.compile(r"^0x([0-9a-fA-F]{1,64})$")
_TRON_HEX = re.compile(r"^(?:0x)?(41[0-9a-fA-F]{40})$")
COSMOS_HRP = {
    "cosmos-hub": "cosmos",
    "osmosis-mainnet": "osmo",
}
FAMILIES = frozenset({"evm", "solana", "bitcoin", "tron", "ton", "sui", "aptos", "cosmos"})


class IdentityError(ValueError):
    """A declared address could not be normalized. The message is a reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class NormalizedAddress:
    family: str
    canonical: str
    identity_key: str
    display: str


@dataclass(frozen=True)
class WalletIdentity:
    """wallet_id is the stable identity. label is display only."""

    wallet_id: str
    label: str
    normalized: NormalizedAddress

    @property
    def family(self) -> str:
        return self.normalized.family

    @property
    def identity_key(self) -> str:
        return self.normalized.identity_key


def parse_address(family: str, text: str, network: str | None = None) -> NormalizedAddress:
    """Validate text as the declared family. Never derive another family from it."""
    if family not in FAMILIES:
        raise IdentityError("unknown_family")
    if not isinstance(text, str) or not text or len(text) > 200:
        raise IdentityError("bad_address")
    if family == "evm":
        return _parse_evm(text)
    if family == "solana":
        return _parse_solana(text)
    if family == "bitcoin":
        return _parse_bitcoin(text)
    if family == "tron":
        return _parse_tron(text)
    if family == "ton":
        return _parse_ton(text)
    if family in {"sui", "aptos"}:
        return _parse_move(family, text)
    return _parse_cosmos(text, network)


def index_wallets(wallets: list[WalletIdentity]) -> dict[str, WalletIdentity]:
    """Index by wallet_id. The same label does not overwrite another wallet."""
    by_id: dict[str, WalletIdentity] = {}
    by_identity: dict[tuple[str, str], str] = {}
    for wallet in wallets:
        if not isinstance(wallet.wallet_id, str) or _UUID.fullmatch(wallet.wallet_id) is None:
            raise IdentityError("bad_wallet_id")
        if not isinstance(wallet.label, str) or not wallet.label.strip():
            raise IdentityError("empty_label")
        if wallet.wallet_id in by_id:
            raise IdentityError("duplicate_wallet_id")
        key = (wallet.family, wallet.identity_key)
        if key in by_identity:
            raise IdentityError("duplicate_identity")
        by_id[wallet.wallet_id] = wallet
        by_identity[key] = wallet.wallet_id
    return by_id


def _parse_evm(text: str) -> NormalizedAddress:
    if len(text) != 42 or not text.startswith("0x"):
        raise IdentityError("bad_evm_format")
    body = text[2:]
    if any(char not in "0123456789abcdefABCDEF" for char in body):
        raise IdentityError("bad_evm_hex")
    mixed = any(char.islower() for char in body) and any(char.isupper() for char in body)
    if mixed and not is_checksum_address(text):
        raise IdentityError("bad_evm_checksum")
    canonical = to_checksum_address(text)
    return NormalizedAddress("evm", canonical, "0x" + body.lower(), canonical)


def _parse_solana(text: str) -> NormalizedAddress:
    try:
        raw = _b58decode(text)
    except IdentityError:
        raise IdentityError("bad_solana_encoding") from None
    if len(raw) != 32:
        raise IdentityError("bad_solana_length")
    canonical = _b58encode(raw)
    return NormalizedAddress("solana", canonical, canonical, canonical)


def _parse_bitcoin(text: str) -> NormalizedAddress:
    lowered = text.lower()
    if lowered.startswith(("bc1", "tb1", "bcrt1")):
        return _parse_bitcoin_segwit(text)
    if text[:1] in {"1", "3"}:
        return _parse_bitcoin_base58(text)
    raise IdentityError("bad_bitcoin_format")


def _parse_bitcoin_segwit(text: str) -> NormalizedAddress:
    try:
        hrp, data = _bech32_parts(text)
    except IdentityError as exc:
        suffix = exc.reason.removeprefix("bad_bech32_")
        raise IdentityError(f"bad_bitcoin_{suffix}") from None
    spec = None
    if _bech32_checksum(hrp, data, "bech32"):
        spec = "bech32"
    elif _bech32_checksum(hrp, data, "bech32m"):
        spec = "bech32m"
    else:
        raise IdentityError("bad_bitcoin_checksum")
    if hrp != "bc":
        raise IdentityError("bad_bitcoin_hrp")
    if not data or len(data) <= 6:
        raise IdentityError("bad_bitcoin_program")
    version = data[0]
    program = _convertbits(data[1:-6], 5, 8, False)
    if version > 16 or program is None or not 2 <= len(program) <= 40:
        raise IdentityError("bad_bitcoin_program")
    if version == 0:
        if spec != "bech32":
            raise IdentityError("bad_bitcoin_witness")
        if len(program) not in {20, 32}:
            raise IdentityError("bad_bitcoin_program")
    elif spec != "bech32m":
        raise IdentityError("bad_bitcoin_witness")
    canonical = text.lower()
    return NormalizedAddress("bitcoin", canonical, canonical, canonical)


def _parse_bitcoin_base58(text: str) -> NormalizedAddress:
    try:
        payload = _b58check_decode(text)
    except IdentityError as exc:
        if exc.reason == "bad_base58_charset":
            raise IdentityError("bad_bitcoin_charset") from None
        raise IdentityError("bad_bitcoin_checksum") from None
    if len(payload) != 21 or payload[0] not in {0x00, 0x05}:
        raise IdentityError("bad_bitcoin_version")
    canonical = _b58check_encode(payload)
    return NormalizedAddress("bitcoin", canonical, canonical, canonical)


def _parse_tron(text: str) -> NormalizedAddress:
    match = _TRON_HEX.fullmatch(text)
    if match:
        payload = bytes.fromhex(match.group(1))
    else:
        if len(text) != 34 or not text.startswith("T"):
            raise IdentityError("bad_tron_format")
        try:
            payload = _b58check_decode(text)
        except IdentityError as exc:
            if exc.reason == "bad_base58_charset":
                raise IdentityError("bad_tron_charset") from None
            raise IdentityError("bad_tron_checksum") from None
    if len(payload) != 21 or payload[0] != 0x41:
        raise IdentityError("bad_tron_prefix")
    canonical = _b58check_encode(payload)
    return NormalizedAddress("tron", canonical, payload.hex(), canonical)


def _parse_ton(text: str) -> NormalizedAddress:
    match = _TON_RAW.fullmatch(text)
    if match:
        workchain_text, account_hex = match.group(1), match.group(2)
        workchain = int(workchain_text)
        if str(workchain) != workchain_text:
            raise IdentityError("bad_ton_format")
        canonical = f"{workchain}:{account_hex.lower()}"
        return NormalizedAddress("ton", canonical, canonical, canonical)
    if len(text) != 48:
        raise IdentityError("bad_ton_format")
    translated = text.replace("-", "+").replace("_", "/")
    try:
        raw = base64.b64decode(translated, validate=True)
    except binascii.Error:
        raise IdentityError("bad_ton_format") from None
    if len(raw) != 36:
        raise IdentityError("bad_ton_format")
    body, checksum = raw[:34], int.from_bytes(raw[34:], "big")
    if _crc16_xmodem(body) != checksum:
        raise IdentityError("bad_ton_crc")
    workchain_byte = body[1]
    workchain = workchain_byte - 256 if workchain_byte > 127 else workchain_byte
    canonical = f"{workchain}:{body[2:34].hex()}"
    return NormalizedAddress("ton", canonical, canonical, canonical)


def _parse_move(family: str, text: str) -> NormalizedAddress:
    match = _MOVE.fullmatch(text)
    if match is None:
        raise IdentityError("bad_move_hex")
    canonical = "0x" + match.group(1).lower().zfill(64)
    return NormalizedAddress(family, canonical, canonical, canonical)


def _parse_cosmos(text: str, network: str | None) -> NormalizedAddress:
    if network is None:
        raise IdentityError("cosmos_network_required")
    if network not in COSMOS_HRP:
        raise IdentityError("unknown_cosmos_network")
    expected = COSMOS_HRP[network]
    try:
        hrp, data = _bech32_parts(text)
    except IdentityError as exc:
        suffix = exc.reason.removeprefix("bad_bech32_")
        raise IdentityError(f"bad_cosmos_{suffix}") from None
    if not _bech32_checksum(hrp, data, "bech32"):
        raise IdentityError("bad_cosmos_checksum")
    if hrp != expected:
        raise IdentityError("bad_cosmos_hrp")
    payload = _convertbits(data[:-6], 5, 8, False)
    if payload is None or len(payload) not in {20, 32}:
        raise IdentityError("bad_cosmos_length")
    canonical = text.lower()
    return NormalizedAddress("cosmos", canonical, canonical, canonical)


def _bech32_parts(text: str) -> tuple[str, list[int]]:
    if any(ord(char) < 33 or ord(char) > 126 for char in text):
        raise IdentityError("bad_bech32_charset")
    if text.lower() != text and text.upper() != text:
        raise IdentityError("bad_bech32_case")
    if len(text) < 8 or len(text) > 90:
        raise IdentityError("bad_bech32_format")
    lowered = text.lower()
    separator = lowered.rfind("1")
    if separator < 1 or separator + 7 > len(lowered):
        raise IdentityError("bad_bech32_format")
    hrp = lowered[:separator]
    data: list[int] = []
    for char in lowered[separator + 1:]:
        index = _BECH32_CHARSET.find(char)
        if index < 0:
            raise IdentityError("bad_bech32_charset")
        data.append(index)
    return hrp, data


def _bech32_checksum(hrp: str, data: list[int], spec: str) -> bool:
    constant = 1 if spec == "bech32" else _BECH32M
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == constant


def _bech32_polymod(values: list[int]) -> int:
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(_BECH32_GENERATORS):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_create_checksum(hrp: str, data: list[int], spec: str) -> list[int]:
    constant = 1 if spec == "bech32" else _BECH32M
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]) ^ constant
    return [(polymod >> 5 * (5 - index)) & 31 for index in range(6)]


def _bech32_encode(hrp: str, data: list[int], spec: str) -> str:
    combined = data + _bech32_create_checksum(hrp, data, spec)
    return hrp + "1" + "".join(_BECH32_CHARSET[value] for value in combined)


def _convertbits(data: list[int] | bytes, frombits: int, tobits: int, pad: bool) -> list[int] | None:
    acc = 0
    bits = 0
    result: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            result.append((acc >> bits) & maxv)
    if pad:
        if bits:
            result.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return result


def _b58decode(text: str) -> bytes:
    if not text or any(char not in _B58_ALPHABET for char in text):
        raise IdentityError("bad_base58_charset")
    zeros = 0
    for char in text:
        if char != "1":
            break
        zeros += 1
    number = 0
    for char in text:
        number = number * 58 + _B58_ALPHABET.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * zeros + body


def _b58encode(data: bytes) -> str:
    zeros = 0
    for byte in data:
        if byte != 0:
            break
        zeros += 1
    number = int.from_bytes(data, "big")
    chars: list[str] = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(_B58_ALPHABET[rem])
    return "1" * zeros + "".join(reversed(chars))


def _b58check_decode(text: str) -> bytes:
    raw = _b58decode(text)
    if len(raw) < 5:
        raise IdentityError("bad_base58_checksum")
    payload, checksum = raw[:-4], raw[-4:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if digest != checksum:
        raise IdentityError("bad_base58_checksum")
    return payload


def _b58check_encode(payload: bytes) -> str:
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _b58encode(payload + digest)


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc
