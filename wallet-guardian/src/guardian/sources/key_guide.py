"""Public hints for a missing credential. The hint names an env var, never a value."""

from __future__ import annotations

from typing import Any, Mapping

_ANCHOR = {
    "ALCHEMY_API_KEY": "alchemy",
    "BLOCKSCOUT_API_KEY": "blockscout",
    "BLOCKSCOUT_BASE_API_KEY": "blockscout",
    "BLOCKSCOUT_OPTIMISM_API_KEY": "blockscout",
    "COINGECKO_API_KEY": "coingecko",
    "ETHERSCAN_API_KEY": "etherscan",
    "GOPLUS_API_KEY": "goplus",
    "HELIUS_API_KEY": "helius",
    "NODEREAL_API_KEY": "nodereal",
    "OFAC_SLS_FEED": "ofac",
}


def credential_hint(env_name: str) -> str:
    """``ENV see docs/api-keys.md#provider``. No secret value."""
    anchor = _ANCHOR.get(env_name, "keys")
    return f"{env_name} see docs/api-keys.md#{anchor}"


def hint_for_source(source: Mapping[str, Any], blocked_reason: str | None) -> str | None:
    if blocked_reason != "credential_missing":
        return None
    env_name = source.get("credential_env")
    if not isinstance(env_name, str) or not env_name:
        return None
    return credential_hint(env_name)
