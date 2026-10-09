"""Alchemy Transfers API stub. Not a default route.

Free-tier eth_getLogs on Alchemy is 10 blocks. The hostname and key come from
the operator environment. This module does not open a socket, does not add a
capability file, and does not replace approval-log reads. Asset transfers are
not a substitute for Approval or ApprovalForAll.
"""

from __future__ import annotations

LOG_BLOCK_CAP = 10
ENDPOINT_ENV = "ALCHEMY_TRANSFERS_URL"
CREDENTIAL_ENV = "ALCHEMY_API_KEY"
ROUTED_BY_DEFAULT = False


def describe() -> dict[str, object]:
    return {
        "credential_env": CREDENTIAL_ENV,
        "does_not_replace": ["approval_logs", "operator_logs"],
        "endpoint_env": ENDPOINT_ENV,
        "free_eth_getLogs_block_cap": LOG_BLOCK_CAP,
        "routed_by_default": ROUTED_BY_DEFAULT,
        "supports": ["asset_transfers"],
    }
