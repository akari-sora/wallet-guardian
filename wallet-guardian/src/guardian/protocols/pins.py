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

# Kinza Pool addresses are documented read-only candidates. They are not a safe list,
# not a spender trust list, and not an execution whitelist. Providers are registry
# identities and are intentionally absent from evm_contracts("kinza").
KINZA_POOLS = {
    "bsc-mainnet": "0xcB0620b181140e57D1C0D8b724cde623cA963c8C",
    "ethereum-mainnet": "0xeA14474946C59Dee1F103aD517132B3F19Cef1bE",
}
KINZA_PROVIDERS = {
    "bsc-mainnet": "0xCa20a50ea454Bd9F37a895182ff3309F251Fd7cE",
    "ethereum-mainnet": "0x0218890dd636c3a1cf0f98557eE2d3612c6bD953",
}
KINZA_SOURCE = {
    "bsc-mainnet": "https://docs.kinza.finance/resources/deployed-contracts/bnb-chain",
    "ethereum-mainnet": "https://docs.kinza.finance/resources/deployed-contracts/ethereum",
}

# Identity is network + address. The legacy token uses one address string on two chains.
UXLINK_NEW_TOKEN = "0x3991B07b2951a4300Da8c76e7d2c7eddE861Fef3"
UXLINK_LEGACY_TOKEN = "0x1A6B3A62391ECcaaa992ade44cd4AFe6bEC8CfF1"
UXLINK_SYMBOL_CHECK = "auxiliary_only"
UXLINK_SOURCES = (
    "https://docs.uxlink.io/layer/announcement/uxlink-supply-distribution",
    "https://docs.uxlink.io/layer/announcement/notice",
    "https://dapp.uxlink.io/new-uxlink-tokens/claim",
)
UXLINK_TOKENS = (
    {
        "network": "ethereum-mainnet",
        "chain_id": 1,
        "token": UXLINK_NEW_TOKEN,
        "status": "documented_new_token",
        "claimability": "not_assessed",
    },
    {
        "network": "arbitrum-one",
        "chain_id": 42161,
        "token": UXLINK_LEGACY_TOKEN,
        "status": "deprecated_legacy",
        "migration_eligibility": "unknown",
        "claimability": "unknown",
        "price_mapping": "separate_from_new_token",
    },
    {
        "network": "bsc-mainnet",
        "chain_id": 56,
        "token": UXLINK_LEGACY_TOKEN,
        "status": "deprecated_legacy",
        "migration_eligibility": "unknown",
        "claimability": "unknown",
        "price_mapping": "separate_from_new_token",
    },
)

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
    if protocol_id == "kinza":
        return frozenset(address.lower() for address in KINZA_POOLS.values())
    return frozenset()


def uxlink_token(network: str) -> dict[str, object] | None:
    """The pinned UXLINK token for one network, or None when that network is not pinned."""
    if not isinstance(network, str):
        return None
    for row in UXLINK_TOKENS:
        if row["network"] == network:
            return row
    return None


def uxlink_identity(network: str, address: str) -> bool:
    """True when network + address is a pinned UXLINK token. Address alone is not enough."""
    row = uxlink_token(network)
    if row is None or not isinstance(address, str):
        return False
    token = row["token"]
    return isinstance(token, str) and token.lower() == address.lower()
