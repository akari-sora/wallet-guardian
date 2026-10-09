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

# Documented public Lido deployments on ethereum-mainnet.
# symbol() was stETH, wstETH, and unstETH on 2026-10-09. This package does not re-check them at runtime.
# https://docs.lido.fi/deployed-contracts/
LIDO_STETH = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
LIDO_WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
LIDO_WITHDRAWAL_QUEUE = "0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1"
LIDO_SOURCE = "https://docs.lido.fi/deployed-contracts/"

# Documented read candidates. Agreement of symbol, name, asset, or MORPHO() is not a safety certificate.
# sDAI: https://docs.spark.fi/dev/savings/sdai-token
# Steakhouse USDC MetaMorpho: https://github.com/morpho-org/metamorpho
# Morpho Blue (MORPHO()): https://docs.morpho.org/get-started/resources/addresses/
DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
SDAI = "0x83F20F44975D03b1b09e64809B757c47f942BEeA"
SDAI_SOURCE = "https://docs.spark.fi/dev/savings/sdai-token"
STEAKHOUSE_USDC = "0xBEEF01735c132Ada46AA9aA4c54623cAA92A64CB"
STEAKHOUSE_SOURCE = "https://github.com/morpho-org/metamorpho"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MORPHO_SOURCE = "https://docs.morpho.org/get-started/resources/addresses/"
# Compound III Ethereum USDC Comet proxy. The launch implementation is provenance only.
# https://docs.compound.finance/
COMET_USDC = "0xc3d688B66703497DAA19211EEdff47f25384cdc3"
COMET_IMPLEMENTATION = "0x42F9505a376761b180e27a01bA0554244ED1DE7D"
COMET_SOURCE = "https://docs.compound.finance/"
USDC_SOURCE = "https://developers.circle.com/stablecoins/usdc-contract-addresses"
DAI_SOURCE = "https://github.com/makerdao/dss"

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
    if protocol_id == "lido":
        return frozenset({
            LIDO_STETH.lower(),
            LIDO_WSTETH.lower(),
            LIDO_WITHDRAWAL_QUEUE.lower(),
        })
    if protocol_id == "erc4626_pins":
        return frozenset({
            SDAI.lower(),
            DAI.lower(),
            STEAKHOUSE_USDC.lower(),
            USDC.lower(),
            MORPHO.lower(),
        })
    if protocol_id == "compound_v3":
        return frozenset({
            COMET_USDC.lower(),
            COMET_IMPLEMENTATION.lower(),
            USDC.lower(),
        })
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
