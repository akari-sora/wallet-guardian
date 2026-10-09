"""Refuse chain writes before any socket or transport is opened.

This is an operation allowlist, not an HTTP-method filter. A read-only POST
stays allowed. A broadcast or signature stays denied even when the verb is GET.
Method and path names are compared exactly: no trimming, no case folding, and
no prefix match. One forbidden batch element rejects the whole batch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from guardian.errors import SecurityError
from guardian.security.rpc_guard import AGENT_READ_METHODS

SOLANA_READ_METHODS = frozenset({
    "getAccountInfo",
    "getBalance",
    "getBlock",
    "getBlockHeight",
    "getBlockTime",
    "getBlocks",
    "getEpochInfo",
    "getEpochSchedule",
    "getFeeForMessage",
    "getFirstAvailableBlock",
    "getGenesisHash",
    "getHealth",
    "getInflationReward",
    "getLatestBlockhash",
    "getMinimumBalanceForRentExemption",
    "getMultipleAccounts",
    "getProgramAccounts",
    "getRecentPrioritizationFees",
    "getSignatureStatuses",
    "getSignaturesForAddress",
    "getSlot",
    "getSupply",
    "getTokenAccountBalance",
    "getTokenAccountsByDelegate",
    "getTokenAccountsByOwner",
    "getTokenSupply",
    "getTransaction",
    "getTransactionCount",
    "getVersion",
    "isBlockhashValid",
    "minimumLedgerSlot",
})

COMET_READ_METHODS = frozenset({
    "abci_info",
    "abci_query",
    "block",
    "block_by_hash",
    "block_results",
    "block_search",
    "blockchain",
    "commit",
    "consensus_params",
    "dump_consensus_state",
    "genesis",
    "genesis_chunked",
    "header",
    "header_by_hash",
    "health",
    "net_info",
    "num_unconfirmed_txs",
    "status",
    "tx",
    "tx_search",
    "validators",
})

NODEREAL_READ_METHODS = AGENT_READ_METHODS | frozenset({"nr_getAssetTransfers"})

SUI_GRPC_READ_METHODS = frozenset({
    "sui.rpc.v2.LedgerService/BatchGetObjects",
    "sui.rpc.v2.LedgerService/GetCheckpoint",
    "sui.rpc.v2.LedgerService/GetEpoch",
    "sui.rpc.v2.LedgerService/GetObject",
    "sui.rpc.v2.LedgerService/GetServiceInfo",
    "sui.rpc.v2.LedgerService/GetTransaction",
    "sui.rpc.v2.StateService/GetBalance",
    "sui.rpc.v2.StateService/GetCoinInfo",
    "sui.rpc.v2.StateService/ListBalances",
    "sui.rpc.v2.StateService/ListDynamicFields",
    "sui.rpc.v2.StateService/ListOwnedObjects",
})

ADAPTER_PROTOCOL = {
    "aptos_indexer": "graphql",
    "aptos_rest": "rest",
    "blockscout": "rest",
    "chainabuse": "rest",
    "coingecko": "rest",
    "comet_rpc": "jsonrpc",
    "cosmos_rest": "rest",
    "debank": "rest",
    "etherscan_v2": "rest",
    "evm_rpc": "jsonrpc",
    "esplora": "rest",
    "goplus": "rest",
    "helius_parsed_events": "rest",
    "nodereal_transfers": "jsonrpc",
    "ofac_sls": "rest",
    "solana_rpc": "jsonrpc",
    "sui_graphql": "graphql",
    "sui_grpc": "grpc",
    "toncenter_v2": "rest",
    "toncenter_v3": "rest",
    "tron_grid": "rest",
}

_JSONRPC_METHODS = {
    "comet_rpc": COMET_READ_METHODS,
    "evm_rpc": AGENT_READ_METHODS,
    "nodereal_transfers": NODEREAL_READ_METHODS,
    "solana_rpc": SOLANA_READ_METHODS,
}
_RPC_KEYS = frozenset({"id", "jsonrpc", "method", "params"})
# Read path allowlist for GET-only adapters (entries ending in '/' are prefixes,
# others are exact paths). Unknown paths fail closed. Provisional until each
# adapter is implemented (W4+); widen only with a reviewed read path. Paths are
# relative to the endpoint origin; an endpoint served under a base path (for
# example Esplora under /api) must include that base in the request path.
_GET_PREFIXES: dict[str, tuple[str, ...]] = {
    "aptos_rest": ("/v1/accounts/", "/v1/transactions/by_hash/", "/v1/transactions/by_version/", "/v1/blocks/", "/v1"),
    "blockscout": ("/api/v2/addresses/", "/api/v2/transactions/", "/api/v2/tokens/", "/api/v2/blocks/", "/api/v2/stats", "/api", "/v2/api"),
    "chainabuse": ("/v0/reports", "/v0/addresses/"),
    "coingecko": ("/api/v3/simple/", "/api/v3/coins/", "/api/v3/ping"),
    "cosmos_rest": ("/cosmos/bank/v1beta1/", "/cosmos/staking/v1beta1/", "/cosmos/authz/v1beta1/", "/cosmos/feegrant/v1beta1/", "/cosmos/base/tendermint/v1beta1/", "/ibc/core/channel/v1/", "/cosmos/tx/v1beta1/txs/"),
    "debank": ("/v1/user/", "/v1/token/", "/v1/protocol/"),
    "esplora": ("/address/", "/scripthash/", "/tx/", "/block/", "/blocks/tip/", "/api/address/", "/api/scripthash/", "/api/tx/", "/api/block/", "/api/blocks/tip/"),
    "goplus": ("/api/v1/token_security/", "/api/v1/address_security/", "/api/v1/approval_security/", "/api/v1/solana/token_security"),
    "ofac_sls": ("/api/download/", "/api/PublicationPreview/", "/exports/"),
}

CREDENTIAL_HEADERS = {
    "blockscout": frozenset({"authorization"}),
    "chainabuse": frozenset({"authorization"}),
    "goplus": frozenset({"authorization"}),
}
_GET_ONLY = frozenset({
    "aptos_rest",
    "blockscout",
    "chainabuse",
    "coingecko",
    "cosmos_rest",
    "debank",
    "esplora",
    "goplus",
    "ofac_sls",
})
_ETHERSCAN_PAIRS = frozenset({
    ("account", "balance"),
    ("account", "balancemulti"),
    ("account", "token1155tx"),
    ("account", "tokennfttx"),
    ("account", "tokentx"),
    ("account", "txlist"),
    ("account", "txlistinternal"),
    ("block", "getblocknobytime"),
    ("contract", "getabi"),
    ("contract", "getsourcecode"),
    ("logs", "getLogs"),
})
_TRON_POST = frozenset({
    "/wallet/getaccount",
    "/wallet/gettransactionbyid",
    "/wallet/gettransactioninfobyid",
    "/wallet/triggerconstantcontract",
    "/walletsolidity/getaccount",
    "/walletsolidity/getblockbyid",
    "/walletsolidity/getblockbynum",
    "/walletsolidity/getnowblock",
    "/walletsolidity/gettransactionbyid",
    "/walletsolidity/gettransactioncountbyblocknum",
    "/walletsolidity/gettransactioninfobyid",
    "/walletsolidity/triggerconstantcontract",
})
_TRON_DENY_PARTS = (
    "broadcasthex",
    "broadcasttransaction",
    "createtransaction",
    "easytransfer",
    "sign",
    "triggersmartcontract",
)
_TON_READS = frozenset({"getAddressInformation", "getTransactions", "runGetMethod"})
_HELIUS_POST = frozenset({
    "/v1/parsed-events/transaction-history",
    "/v1/parsed-events/transactions",
})
_GRAPHQL_KEYS = frozenset({"tx_bytes", "signatures"})
_TRON_KEYS = frozenset({"raw_data_hex", "signature"})


@dataclass(frozen=True)
class Operation:
    """One read the transport may attempt. ``url`` is never a place to add an endpoint."""

    protocol: str
    adapter: str
    method: str | None = None
    params: Any = None
    batch: Sequence[Mapping[str, Any]] | None = None
    http_method: str | None = None
    path: str | None = None
    query: Mapping[str, str] | None = None
    body: Any = None
    document: str | None = None
    documents: Sequence[Any] | None = None
    operation_name: str | None = None
    url: str | None = None
    headers: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        headers = self.headers
        if headers is None:
            return
        if isinstance(headers, (Mapping, str, bytes)) or not isinstance(headers, tuple):
            _deny("header_value_not_allowed")
        if any(not isinstance(item, str) for item in headers):
            _deny("header_value_not_allowed")
        for name in headers:
            if not name or name != name.strip() or any(mark in name for mark in " :;=\t\r\n"):
                _deny("header_value_not_allowed")


def assert_operation_allowed(operation: Operation) -> None:
    """Raise SecurityError before a backend, socket, or redirect is used."""
    if not isinstance(operation, Operation):
        _deny("protocol_denied")
    _assert_headers(operation)
    expected = ADAPTER_PROTOCOL.get(operation.adapter)
    if expected is None or operation.protocol != expected:
        _deny("protocol_denied")
    if operation.protocol == "jsonrpc":
        _assert_jsonrpc(operation)
        return
    if operation.protocol == "rest":
        _assert_rest(operation)
        return
    if operation.protocol == "graphql":
        _assert_graphql(operation)
        return
    if operation.protocol == "grpc":
        _assert_grpc(operation)
        return
    _deny("protocol_denied")


def operation_records(adapter: str) -> list[dict[str, Any]]:
    """Allowed operations for one adapter, in a stable order, for the capability files."""
    protocol = ADAPTER_PROTOCOL.get(adapter)
    if protocol is None:
        _deny("protocol_denied")
    records: list[dict[str, Any]] = []
    if protocol == "jsonrpc":
        for method in sorted(_JSONRPC_METHODS[adapter]):
            records.append({"method": method, "protocol": "jsonrpc"})
    elif adapter == "etherscan_v2":
        for module, action in sorted(_ETHERSCAN_PAIRS):
            records.append({
                "http_method": "GET",
                "path": "/v2/api",
                "protocol": "rest",
                "query": {"action": action, "module": module},
            })
        for action in sorted(AGENT_READ_METHODS):
            records.append({
                "http_method": "GET",
                "path": "/v2/api",
                "protocol": "rest",
                "query": {"action": action, "module": "proxy"},
            })
    elif adapter in _GET_ONLY:
        for prefix in _GET_PREFIXES[adapter]:
            records.append({"http_method": "GET", "path": prefix + ("*" if prefix.endswith("/") else ""), "protocol": "rest"})
    elif adapter == "tron_grid":
        records.append({"http_method": "GET", "path": "/v1/accounts/*", "protocol": "rest"})
        for path in sorted(_TRON_POST):
            records.append({"http_method": "POST", "path": path, "protocol": "rest"})
    elif adapter == "toncenter_v2":
        for name in sorted(_TON_READS):
            records.append({"http_method": "GET", "path": f"/api/v2/{name}", "protocol": "rest"})
        records.append({"http_method": "POST", "path": "/api/v2/jsonRPC", "protocol": "rest"})
        records.append({"http_method": "POST", "path": "/api/v2/runGetMethod", "protocol": "rest"})
    elif adapter == "toncenter_v3":
        records.append({"http_method": "GET", "path": "/api/v3/*", "protocol": "rest"})
    elif adapter == "helius_parsed_events":
        for path in sorted(_HELIUS_POST):
            records.append({"http_method": "POST", "path": path, "protocol": "rest"})
    elif protocol == "graphql":
        records.append({"operation": "query", "protocol": "graphql"})
    elif protocol == "grpc":
        for method in sorted(SUI_GRPC_READ_METHODS):
            records.append({"method": method, "protocol": "grpc"})
    records.sort(key=lambda item: (item["protocol"], item.get("http_method", ""), item.get("path", ""), item.get("method", ""), item.get("operation", "")))
    return records


def _assert_headers(operation: Operation) -> None:
    headers = operation.headers
    if not headers:
        return
    allowed = CREDENTIAL_HEADERS.get(operation.adapter)
    if allowed is None:
        _deny("header_not_allowed")
    for name in headers:
        if name not in allowed:
            _deny("header_not_allowed")


def _assert_jsonrpc(operation: Operation) -> None:
    allowed = _JSONRPC_METHODS[operation.adapter]
    if operation.batch is not None:
        if operation.method is not None:
            _deny("batch_malformed")
        if isinstance(operation.batch, (str, bytes)) or not isinstance(operation.batch, Sequence):
            _deny("batch_malformed")
        if len(operation.batch) == 0:
            _deny("batch_malformed")
        for item in operation.batch:
            _assert_rpc_item(item, allowed, malformed="batch_malformed", denied="batch_rejected")
        return
    item: dict[str, Any] = {}
    if operation.method is not None:
        item["method"] = operation.method
    if operation.params is not None:
        item["params"] = operation.params
    _assert_rpc_item(item, allowed, malformed="method_malformed", denied="method_denied")


def _assert_rpc_item(item: Any, allowed: frozenset[str], *, malformed: str, denied: str) -> None:
    if not isinstance(item, Mapping) or not set(item).issubset(_RPC_KEYS) or "method" not in item:
        _deny(malformed)
    method = item.get("method")
    if not isinstance(method, str):
        _deny(malformed)
    if method not in allowed:
        _deny(denied)


def _assert_rest(operation: Operation) -> None:
    method = operation.http_method
    path = _clean_path(operation.path)
    if not isinstance(method, str):
        _deny("rest_denied")
    adapter = operation.adapter
    if adapter == "tron_grid":
        _reject_fields(operation.body, _TRON_KEYS, "signed_payload")
        if _tron_denied(path) or not _tron_allowed(method, path):
            _deny("rest_denied")
        return
    if adapter == "etherscan_v2":
        if not _etherscan_allowed(method, path, operation.query):
            _deny("rest_denied")
        return
    if adapter == "toncenter_v2":
        if not _ton_v2_allowed(method, path, operation.body):
            _deny("rest_denied")
        return
    if adapter == "toncenter_v3":
        if not _ton_v3_allowed(method, path):
            _deny("rest_denied")
        return
    if adapter == "helius_parsed_events":
        if method != "POST" or path not in _HELIUS_POST:
            _deny("rest_denied")
        return
    if adapter in _GET_ONLY:
        if method != "GET" or not _get_path_allowed(adapter, path):
            _deny("rest_denied")
        return
    _deny("rest_denied")


def _get_path_allowed(adapter: str, path: str) -> bool:
    for prefix in _GET_PREFIXES.get(adapter, ()):
        if prefix.endswith("/"):
            if path.startswith(prefix) and len(path) > len(prefix):
                return True
        elif path == prefix:
            return True
    return False


def _assert_graphql(operation: Operation) -> None:
    if operation.documents is not None:
        if operation.document is not None:
            _deny("batch_malformed")
        if isinstance(operation.documents, (str, bytes)) or not isinstance(operation.documents, Sequence):
            _deny("batch_malformed")
        if len(operation.documents) == 0:
            _deny("batch_malformed")
        for item in operation.documents:
            _assert_graphql_item(item)
        return
    _assert_graphql_item({"query": operation.document, "variables": operation.body})


def _assert_graphql_item(item: Any) -> None:
    if isinstance(item, str):
        _parse_graphql(item)
        return
    if not isinstance(item, Mapping) or not isinstance(item.get("query"), str):
        _deny("graphql_malformed")
    _reject_fields(item, _GRAPHQL_KEYS, "signed_payload")
    _parse_graphql(item["query"])


def _assert_grpc(operation: Operation) -> None:
    method = operation.method
    if not isinstance(method, str) or method not in SUI_GRPC_READ_METHODS:
        _deny("grpc_denied")


def _etherscan_allowed(method: str, path: str, query: Mapping[str, str] | None) -> bool:
    if method != "GET" or path != "/v2/api" or not isinstance(query, Mapping):
        return False
    module = query.get("module")
    action = query.get("action")
    if not isinstance(module, str) or not isinstance(action, str):
        return False
    if any(not isinstance(value, str) for value in query.values()):
        return False
    if module == "proxy":
        return action in AGENT_READ_METHODS
    return (module, action) in _ETHERSCAN_PAIRS


def _tron_denied(path: str) -> bool:
    lowered = path.lower()
    return any(part in lowered for part in _TRON_DENY_PARTS)


def _tron_allowed(method: str, path: str) -> bool:
    if method == "GET":
        parts = [part for part in path.split("/") if part]
        return len(parts) >= 3 and parts[0] == "v1" and parts[1] == "accounts"
    if method == "POST":
        return path in _TRON_POST
    return False


def _ton_submit(path: str) -> bool:
    parts = [part for part in path.split("/") if part]
    for part in parts:
        if part in {"sendBoc", "sendBocReturnHash", "sendQuery", "estimateFee"}:
            return True
        if part.startswith("send") or part.startswith("estimate"):
            return True
    return False


def _ton_v2_allowed(method: str, path: str, body: Any) -> bool:
    if _ton_submit(path):
        return False
    if method == "GET" and path in {f"/api/v2/{name}" for name in _TON_READS}:
        return True
    if method == "POST" and path == "/api/v2/runGetMethod":
        return True
    if method == "POST" and path == "/api/v2/jsonRPC" and isinstance(body, Mapping):
        remote = body.get("method")
        return isinstance(remote, str) and remote in _TON_READS and set(body).issubset({"id", "jsonrpc", "method", "params"})
    return False


def _ton_v3_allowed(method: str, path: str) -> bool:
    if method != "GET" or _ton_submit(path):
        return False
    return path == "/api/v3" or path.startswith("/api/v3/")


def _clean_path(path: Any) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        _deny("rest_denied")
    if path != path.strip() or any(token in path for token in ("\\", "..", "%", "//", "?", "#", "\x00")):
        _deny("rest_denied")
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def _reject_fields(value: Any, keys: frozenset[str], reason: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in keys:
                _deny(reason)
            _reject_fields(item, keys, reason)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_fields(item, keys, reason)


def _parse_graphql(document: str) -> None:
    if not isinstance(document, str) or not document.strip():
        _deny("graphql_malformed")
    try:
        tokens = _tokenize_graphql(document)
        _scan_graphql(tokens)
    except SecurityError:
        raise
    except ValueError:
        _deny("graphql_malformed")


def _tokenize_graphql(document: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(document)
    while index < length:
        char = document[index]
        if char in " \t\r\n,":
            index += 1
            continue
        if char == "#":
            index = document.find("\n", index)
            if index < 0:
                break
            continue
        if char == '"':
            index = _skip_graphql_string(document, index)
            tokens.append(("STRING", ""))
            continue
        if char in "{}()[]:!@$.|&=":
            if document.startswith("...", index):
                tokens.append(("PUNCT", "..."))
                index += 3
                continue
            tokens.append(("PUNCT", char))
            index += 1
            continue
        if char.isascii() and (char.isalpha() or char == "_"):
            end = index + 1
            while end < length and document[end].isascii() and (document[end].isalnum() or document[end] == "_"):
                end += 1
            tokens.append(("NAME", document[index:end]))
            index = end
            continue
        if char.isascii() and (char.isdigit() or char == "-"):
            end = index + 1
            while end < length and document[end] in "0123456789.eE+-":
                end += 1
            tokens.append(("NUMBER", document[index:end]))
            index = end
            continue
        raise ValueError("bad graphql token")
    return tokens


def _skip_graphql_string(document: str, index: int) -> int:
    if document.startswith('"""', index):
        end = index + 3
        while end < len(document):
            if document.startswith('"""', end) and document[end - 1:end] != "\\":
                return end + 3
            end += 1
        raise ValueError("unterminated block string")
    end = index + 1
    while end < len(document):
        char = document[end]
        if char == "\\":
            end += 2
            continue
        if char == '"':
            return end + 1
        if char == "\n":
            raise ValueError("raw newline in string")
        end += 1
    raise ValueError("unterminated string")


def _scan_graphql(tokens: list[tuple[str, str]]) -> None:
    index = 0
    saw = False
    while index < len(tokens):
        kind, value = tokens[index]
        if kind == "NAME" and value == "fragment":
            index = _skip_fragment(tokens, index)
            continue
        if kind == "NAME" and value in {"query", "mutation", "subscription"}:
            if value != "query":
                _deny(f"graphql_{value}")
            index = _skip_operation(tokens, index + 1)
            saw = True
            continue
        if kind == "PUNCT" and value == "{":
            index = _skip_selection(tokens, index)
            saw = True
            continue
        _deny("graphql_malformed")
    if not saw:
        _deny("graphql_malformed")


def _skip_operation(tokens: list[tuple[str, str]], index: int) -> int:
    if index < len(tokens) and tokens[index][0] == "NAME":
        index += 1
    if index < len(tokens) and tokens[index] == ("PUNCT", "("):
        index = _skip_parens(tokens, index)
    while index < len(tokens) and tokens[index] == ("PUNCT", "@"):
        index = _skip_directive(tokens, index)
    if index >= len(tokens) or tokens[index] != ("PUNCT", "{"):
        raise ValueError("missing selection")
    return _skip_selection(tokens, index)


def _skip_fragment(tokens: list[tuple[str, str]], index: int) -> int:
    index += 1
    if index >= len(tokens) or tokens[index][0] != "NAME":
        raise ValueError("fragment name")
    index += 1
    if index >= len(tokens) or tokens[index] != ("NAME", "on"):
        raise ValueError("fragment on")
    index += 1
    if index >= len(tokens) or tokens[index][0] != "NAME":
        raise ValueError("fragment type")
    index += 1
    while index < len(tokens) and tokens[index] == ("PUNCT", "@"):
        index = _skip_directive(tokens, index)
    if index >= len(tokens) or tokens[index] != ("PUNCT", "{"):
        raise ValueError("fragment selection")
    return _skip_selection(tokens, index)


def _skip_directive(tokens: list[tuple[str, str]], index: int) -> int:
    index += 1
    if index >= len(tokens) or tokens[index][0] != "NAME":
        raise ValueError("directive")
    index += 1
    if index < len(tokens) and tokens[index] == ("PUNCT", "("):
        return _skip_parens(tokens, index)
    return index


def _skip_parens(tokens: list[tuple[str, str]], index: int) -> int:
    depth = 0
    while index < len(tokens):
        if tokens[index] == ("PUNCT", "("):
            depth += 1
        elif tokens[index] == ("PUNCT", ")"):
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ValueError("unbalanced parens")


def _skip_selection(tokens: list[tuple[str, str]], index: int) -> int:
    depth = 0
    paren = 0
    while index < len(tokens):
        punct = tokens[index][1] if tokens[index][0] == "PUNCT" else ""
        if punct == "(":
            paren += 1
        elif punct == ")":
            paren -= 1
            if paren < 0:
                raise ValueError("unbalanced parens")
        elif paren == 0 and punct == "{":
            depth += 1
        elif paren == 0 and punct == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ValueError("unbalanced selection")


def _deny(reason: str) -> None:
    raise SecurityError(reason, reason=reason)
