"""Pinned stablecoin contracts used only as a size check.

A contract on this list is not priced at 1 USD. When CoinGecko does not return
a fresh quote, a raw balance at least as large as ``asset_usd_threshold`` units
can still raise an unvalued large-move alert. Decimals are the published token
decimals, not a price.
"""

from __future__ import annotations

# Circle and Tether publish these contract addresses. They are not watch addresses.
# USDC: https://developers.circle.com/stablecoins/usdc-contract-addresses
# USDT: https://tether.to/en/supported-protocols
_PINNED: dict[tuple[str, str], int] = {
    ("ethereum-mainnet", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"): 6,
    ("ethereum-mainnet", "0xdac17f958d2ee523a2206206994597c13d831ec7"): 6,
    ("ethereum-mainnet", "0x6b175474e89094c44da98b954eedeac495271d0f"): 18,
    ("arbitrum-one", "0xaf88d065e77c8cc2239327c5edb3a432268e5831"): 6,
    ("arbitrum-one", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"): 6,
    ("optimism-mainnet", "0x0b2c639c533813f4aa9d7837caf62653d097ff85"): 6,
    ("optimism-mainnet", "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58"): 6,
    ("base-mainnet", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"): 6,
    ("polygon-pos", "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"): 6,
    ("polygon-pos", "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"): 6,
    ("bsc-mainnet", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"): 18,
    ("bsc-mainnet", "0x55d398326f99059ff775485246999027b3197955"): 18,
    ("avalanche-c", "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e"): 6,
}


def stable_decimals(network: str, contract: str) -> int | None:
    """Published decimals for a pinned stable, or None when it is not pinned."""
    if not isinstance(network, str) or not isinstance(contract, str):
        return None
    return _PINNED.get((network, contract.lower()))
