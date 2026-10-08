"""Pinned public protocol contracts. Manifests must match these; they cannot retarget a plugin.

Addresses are documented, not verified on-chain by this package. Retrieved 2026-10-09.
Aave pools: https://github.com/bgd-labs/aave-address-book
Lista proxy and slisBNB: config/protocols/lista-bsc.json (unchanged; this module does not edit it).
Solana stake program: the public Stake program id.
"""

from __future__ import annotations

LISTA_PROXY = "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6"
SLISBNB = "0xB0b84D294e0C75A6abe60171b70edEb2EFd14A1B"

AAVE_POOLS = {
    "arbitrum-one": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    "avalanche-c": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    "base-mainnet": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
    "bsc-mainnet": "0x6807dc923806fE8Fd134338EABCA509979a7e0cB",
    "ethereum-mainnet": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
    "optimism-mainnet": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    "polygon-pos": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
}

STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"

# Documented StakeStateV2 layout, not live-verified here.
# 4-byte Borsh tag, then Meta.rent_exempt_reserve (u64), then Authorized.staker, then withdrawer.
STAKE_STAKER_OFFSET = 12
STAKE_WITHDRAWER_OFFSET = 44

AAVE_SOURCE = "https://github.com/bgd-labs/aave-address-book"
LISTA_SOURCE = "https://docs.bsc.lista.org/for-developer/liquid-staking-slisbnb/smart-contract"
STAKE_SOURCE = "https://docs.solana.com/developing/runtime-facilities/programs"
RETRIEVED = "2026-10-09"


def evm_contracts(protocol_id: str) -> frozenset[str]:
    """Lowercase EVM addresses registered for one protocol. Empty when none are pinned."""
    if protocol_id == "lista_slisbnb":
        return frozenset({LISTA_PROXY.lower(), SLISBNB.lower()})
    if protocol_id == "aave_v3":
        return frozenset(address.lower() for address in AAVE_POOLS.values())
    return frozenset()
