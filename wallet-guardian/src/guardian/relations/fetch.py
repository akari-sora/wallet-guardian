"""Read one address in one direction from a source the profile already has."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from guardian.identities import parse_address
from guardian.sources.blockscout import BlockscoutClient
from guardian.sources.etherscan import EtherscanClient, plan_not_supported, resolve_credential
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

_TAILS = ("token-transfers", "transactions", "internal-transactions")
_ETHERSCAN_ACTIONS = ("txlist", "tokentx", "txlistinternal")
_ETHERSCAN_END_BLOCK = 99_999_999
_ETHERSCAN_OFFSET = 1000
_NFT = frozenset({"ERC-721", "ERC-1155", "ERC721", "ERC1155"})


@dataclass
class Edge:
    network: str
    tx_hash: str
    block_number: int | None
    timestamp: int
    source_id: str
    sender: str
    recipient: str
    sender_contract: bool | None
    recipient_contract: bool | None
    kind: str
    contract: str | None
    symbol: str | None
    decimals: int | None
    raw_amount: int | None


@dataclass
class Outcome:
    status: str
    reason: str
    edges: list[Edge]
    payer_is_contract: bool | None = None
    include_edges: bool = True


def read_blockscout(
    transport: Any,
    source_id: str,
    *,
    credential: str | None,
    address: str,
    direction: str,
    network: str,
    family: str,
    page_cap: int,
    stop_timestamp: int,
    entry: Mapping[str, Any],
    clock: Callable[[], float],
) -> Outcome:
    client = BlockscoutClient(
        transport,
        source_id,
        credential=credential or None,
        clock=clock,
        sleep=lambda _seconds: None,
        random=lambda: 0.0,
    )
    reads = []
    for tail in _TAILS:
        reads.append((tail, client.read_v2(
            address,
            tail,
            query={"filter": direction},
            keep_partial=True,
            stop_timestamp=stop_timestamp,
            page_cap=page_cap,
        )))
    return _from_history(reads, address, direction, network, family, source_id, entry, stop_timestamp)


def read_etherscan(
    transport: Any,
    source_id: str,
    source: Mapping[str, Any],
    *,
    secrets: Mapping[str, str],
    address: str,
    direction: str,
    network: str,
    family: str,
    page_cap: int,
    stop_timestamp: int,
    entry: Mapping[str, Any],
    clock: Callable[[], float],
) -> Outcome:
    chain_id = entry.get("evm_chain_id")
    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        return Outcome("gap", "chain_id_missing", [])
    env_name = source.get("credential_env")
    if not isinstance(env_name, str) or not env_name:
        return Outcome("gap", "credential_missing", [])
    try:
        credential = resolve_credential({}, env_name, secrets)
    except ValueError as exc:
        return Outcome("gap", str(exc) or "credential_missing", [])
    client = EtherscanClient(
        transport,
        source_id,
        credential=credential,
        credential_env=env_name,
        clock=clock,
        sleep=lambda _seconds: None,
        random=lambda: 0.0,
    )
    parts: list[tuple[str, Any]] = []
    for action in _ETHERSCAN_ACTIONS:
        parts.append((action, _etherscan_action(
            client, transport, source_id, chain_id, action, address, page_cap, clock,
        )))
    return _from_history(parts, address, direction, network, family, source_id, entry, stop_timestamp, etherscan=True)


def _tron_reader():
    """Load the TRON client after its package init. A direct import cycles."""
    import guardian.chains.tron.abi as tron_abi
    from guardian.sources.tron_grid import TronGridClient, frozen_filters

    if not tron_abi.READ_SELECTORS:
        raise RuntimeError("selectors")
    return TronGridClient, frozen_filters


def read_tron(
    transport: Any,
    source_id: str,
    *,
    address: str,
    direction: str,
    network: str,
    page_cap: int,
    stop_timestamp: int,
    clock: Callable[[], float],
) -> Outcome:
    client_cls, frozen_filters = _tron_reader()
    client = client_cls(
        transport,
        source_id,
        clock=clock,
        sleep=lambda _seconds: None,
        started_at=clock(),
        deadline=180,
    )
    window = frozen_filters(stop_timestamp * 1000, int(clock() * 1000))
    if direction == "from":
        window["only_from"] = "true"
    else:
        window["only_to"] = "true"
    path = f"/v1/accounts/{address}/transactions/trc20"
    walk = client.history(path, window, None, page_cap=page_cap)
    if walk.complete and not walk.error:
        status, reason = "complete", ""
    elif walk.reason == "page_cap":
        status, reason = "partial", "page_cap"
    else:
        status, reason = "gap", walk.reason or "read_error"
    edges: list[Edge] = []
    unreadable = False
    contract: bool | None = None
    for row in walk.rows:
        parsed = _tron_edge(row, address, direction, network, source_id, stop_timestamp)
        if parsed == "bad":
            unreadable = True
            continue
        if parsed is None:
            continue
        edges.append(parsed)
        if direction == "from" and parsed.sender_contract is True:
            contract = True
    if unreadable and status == "complete":
        status, reason = "partial", "unreadable_row"
    return Outcome(status, reason, edges, contract)


def _etherscan_action(client, transport, source_id, chain_id, action, address, page_cap, clock):
    from guardian.sources.etherscan import HistoryRead

    capability = {"txlist": "history.native_external", "txlistinternal": "history.native_internal", "tokentx": "history.fungible"}.get(action, "unspecified")
    items: list[Any] = []
    for page in range(1, page_cap + 1):
        operation = client.operation(
            chain_id=chain_id,
            module="account",
            action=action,
            address=address,
            start_block=0,
            end_block=_ETHERSCAN_END_BLOCK,
            page=page,
            offset=_ETHERSCAN_OFFSET,
        )
        try:
            result = read_with_retry(
                lambda operation=operation: transport.request(source_id, operation),
                retry_attempts=transport.retry_attempts(source_id),
                sleep=lambda _seconds: None,
                random=lambda: 0.0,
                clock=clock,
            )
        except TransportError as exc:
            if exc.reason == "fixture_miss":
                raise
            return HistoryRead(capability, tuple(items), False, False, (), None, "error", False, exc.reason, (exc.reason,), "")
        except Exception:
            return HistoryRead(capability, tuple(items), False, False, (), None, "error", False, "error", ("error",), "")
        if plan_not_supported(getattr(result, "body", None)):
            return HistoryRead(capability, (), False, False, (), None, "error", True, "plan_not_supported", ("plan_not_supported",), "")
        if getattr(result, "category", "") == "empty":
            break
        if not getattr(result, "ok", False):
            reason = str(getattr(result, "reason", "") or "error")
            return HistoryRead(capability, tuple(items), False, False, (), None, "error", False, reason, (reason,), "")
        body = getattr(result, "body", None)
        rows = body.get("result") if isinstance(body, dict) else None
        if isinstance(rows, str):
            lowered = rows.lower()
            if "no transactions found" in lowered or "no records found" in lowered:
                break
            return HistoryRead(capability, tuple(items), False, False, (), None, "error", False, "malformed", ("malformed",), "")
        if not isinstance(rows, list):
            return HistoryRead(capability, tuple(items), False, False, (), None, "error", False, "malformed", ("malformed",), "")
        items.extend(rows)
        if len(rows) < _ETHERSCAN_OFFSET:
            break
    else:
        return HistoryRead(capability, tuple(items), False, False, (), None, "partial", False, "page_cap", ("page_cap",), "")
    category = "empty" if not items else "ok"
    return HistoryRead(capability, tuple(items), True, True, (), None, category, False, "", (), "")


def _from_history(parts, address, direction, network, family, source_id, entry, stop_timestamp, *, etherscan: bool = False) -> Outcome:
    statuses: list[str] = []
    reason = ""
    edges: list[Edge] = []
    unreadable = False
    contract: bool | None = None
    symbol, decimals = _native(entry)
    for kind, read in parts:
        state, why = _history_status(read)
        statuses.append(state)
        if state != "complete" and not reason:
            reason = why
        if state == "gap" and not getattr(read, "items", ()):
            continue
        for item in read.items:
            parsed = _evm_edge(
                item, kind, address, direction, network, family, source_id, stop_timestamp,
                symbol, decimals, etherscan=etherscan,
            )
            if parsed == "bad":
                unreadable = True
                continue
            if parsed is None:
                continue
            edges.append(parsed)
            if direction == "from" and parsed.sender_contract is True:
                contract = True
    if "gap" in statuses:
        status = "gap"
        reason = reason or "error"
    elif "partial" in statuses or unreadable:
        status = "partial"
        reason = reason or ("unreadable_row" if unreadable else "partial")
    else:
        status, reason = "complete", ""
    return Outcome(status, reason, edges, contract)


def _history_status(read: Any) -> tuple[str, str]:
    category = getattr(read, "category", "error")
    reason = str(getattr(read, "reason", "") or "")
    if category in {"ok", "empty"} and getattr(read, "pagination_complete", False) and not getattr(read, "blocked", False):
        return "complete", ""
    if category == "partial":
        return "partial", reason or "partial"
    return "gap", reason or "error"


def _evm_edge(item, kind, address, direction, network, family, source_id, stop_timestamp, native_symbol, native_decimals, *, etherscan: bool):
    if not isinstance(item, Mapping):
        return "bad"
    if _failed(item):
        return None
    sender, sender_contract = _party(item.get("from"))
    recipient, recipient_contract = _party(item.get("to"))
    sender = _canonical(family, sender)
    recipient = _canonical(family, recipient)
    if not sender or not recipient:
        return "bad"
    if direction == "to" and not _same(family, recipient, address):
        return None
    if direction == "from" and not _same(family, sender, address):
        return None
    stamp = _stamp(item)
    if stamp is None:
        return "bad"
    if stamp < stop_timestamp:
        return None
    tx_hash = _tx_hash(item)
    if not tx_hash:
        return "bad"
    if etherscan:
        asset = _etherscan_asset(kind, item, family, native_symbol, native_decimals)
    else:
        asset = _blockscout_asset(kind, item, family, native_symbol, native_decimals)
    if asset is None:
        return None
    kind_name, contract, symbol, decimals, raw = asset
    return Edge(
        network, tx_hash, _block(item), stamp, source_id, sender, recipient,
        sender_contract, recipient_contract, kind_name, contract, symbol, decimals, raw,
    )


def _tron_edge(row, address, direction, network, source_id, stop_timestamp):
    if not isinstance(row, Mapping):
        return "bad"
    label = row.get("type")
    if isinstance(label, str) and label.casefold() not in {"transfer", ""}:
        return None
    if isinstance(label, str) and label.casefold() == "approval":
        return None
    sender = _canonical("tron", row.get("from") if isinstance(row.get("from"), str) else "")
    recipient = _canonical("tron", row.get("to") if isinstance(row.get("to"), str) else "")
    if not sender or not recipient:
        return "bad"
    if direction == "to" and recipient != address:
        return None
    if direction == "from" and sender != address:
        return None
    stamp = _stamp(row)
    if stamp is None or stamp < stop_timestamp:
        return None if stamp is not None else "bad"
    tx_hash = _tx_hash(row)
    if not tx_hash:
        return "bad"
    token = row.get("token_info") if isinstance(row.get("token_info"), Mapping) else {}
    contract = _canonical("tron", token.get("address") if isinstance(token.get("address"), str) else "")
    if not contract:
        return "bad"
    symbol = token.get("symbol") if isinstance(token.get("symbol"), str) and token.get("symbol") else None
    return Edge(
        network, tx_hash, _block(row), stamp, source_id, sender, recipient, None, None,
        "token", contract, symbol, _decimals(token.get("decimals")), _amount(row.get("value")),
    )


def _blockscout_asset(kind, item, family, native_symbol, native_decimals):
    if kind == "token-transfers":
        token = item.get("token") if isinstance(item.get("token"), Mapping) else {}
        token_type = str(token.get("type") or "").upper().replace("_", "-")
        if token_type in _NFT:
            return None
        contract = _canonical(family, str(token.get("address") or token.get("address_hash") or ""))
        if not contract:
            return None
        total = item.get("total") if isinstance(item.get("total"), Mapping) else {}
        raw = _amount(total.get("value") if "value" in total else item.get("value"))
        decimals = _decimals(token.get("decimals"))
        if decimals is None:
            decimals = _decimals(total.get("decimals"))
        symbol = token.get("symbol") if isinstance(token.get("symbol"), str) and token.get("symbol") else None
        return "token", contract, symbol, decimals, raw
    raw = _amount(item.get("value"))
    return "native", None, native_symbol, native_decimals, raw


def _etherscan_asset(kind, item, family, native_symbol, native_decimals):
    if kind == "tokentx":
        contract = _canonical(family, str(item.get("contractAddress") or ""))
        if not contract:
            return None
        symbol = item.get("tokenSymbol") if isinstance(item.get("tokenSymbol"), str) and item.get("tokenSymbol") else None
        return "token", contract, symbol, _decimals(item.get("tokenDecimal")), _amount(item.get("value"))
    return "native", None, native_symbol, native_decimals, _amount(item.get("value"))


def _failed(item: Mapping[str, Any]) -> bool:
    if item.get("success") is False:
        return True
    status = item.get("status")
    if isinstance(status, str) and status.lower() in {"error", "reverted"}:
        return True
    if str(item.get("isError") or "") == "1":
        return True
    if str(item.get("txreceipt_status") or "") == "0":
        return True
    return False


def _party(value: Any) -> tuple[str, bool | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, Mapping):
        raw = value.get("hash") or value.get("address") or ""
        flag = value.get("is_contract")
        return (raw if isinstance(raw, str) else ""), flag if isinstance(flag, bool) else None
    return "", None


def _canonical(family: str, text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    try:
        return parse_address(family, text.strip()).canonical
    except ValueError:
        return ""


def _same(family: str, left: str, right: str) -> bool:
    if family == "evm":
        return left.lower() == right.lower()
    return left == right


def _tx_hash(item: Mapping[str, Any]) -> str:
    for key in ("tx_hash", "transaction_hash", "hash", "transaction_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text[:2].lower() == "0x":
                return "0x" + text[2:].lower()
            return text.lower()
    return ""


def _block(item: Mapping[str, Any]) -> int | None:
    for key in ("block_number", "blockNumber", "block"):
        value = item.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _stamp(item: Mapping[str, Any]) -> int | None:
    from guardian.sources.blockscout import _item_timestamp

    return _item_timestamp(item)


def _amount(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _decimals(value: Any) -> int | None:
    return _amount(value)


def _native(entry: Mapping[str, Any]) -> tuple[str | None, int | None]:
    native = entry.get("native_asset")
    if not isinstance(native, Mapping):
        return None, None
    symbol = native.get("symbol") if isinstance(native.get("symbol"), str) else None
    decimals = native.get("decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        decimals = None
    return symbol, decimals
