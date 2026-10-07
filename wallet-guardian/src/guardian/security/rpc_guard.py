"""Agent RPC allowlist. Signing and broadcast methods are refused before any network call."""

from __future__ import annotations

from guardian.errors import SecurityError

FORBIDDEN_RPC_METHODS = frozenset(
    {
        "eth_sendTransaction",
        "eth_sendRawTransaction",
        "wallet_sendCalls",
        "eth_sign",
        "personal_sign",
        "eth_signTypedData",
        "eth_signTypedData_v1",
        "eth_signTypedData_v3",
        "eth_signTypedData_v4",
        "eth_signTransaction",
    }
)

AGENT_READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getBalance",
        "eth_call",
        "eth_getCode",
        "eth_getStorageAt",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_getLogs",
        "eth_getTransactionCount",
        "eth_gasPrice",
        "eth_feeHistory",
        "net_version",
        "web3_clientVersion",
    }
)


def assert_agent_method(method: str, *, allow_estimate: bool = False) -> None:
    if not isinstance(method, str) or not method:
        raise SecurityError("rpc method is missing")
    if method in FORBIDDEN_RPC_METHODS:
        raise SecurityError(f"agent rpc method is forbidden: {method}")
    allowed = set(AGENT_READ_METHODS)
    if allow_estimate:
        allowed.add("eth_estimateGas")
    if method not in allowed:
        raise SecurityError(f"rpc method is not on the agent allowlist: {method}")
