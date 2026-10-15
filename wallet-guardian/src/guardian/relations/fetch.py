"""Read one address in one direction from a source the profile already has."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from guardian.errors import SecurityError
from guardian.identities import parse_address
from guardian.sources.blockscout import BlockscoutClient
from guardian.sources.budget import BudgetExhausted
from guardian.sources.etherscan import EtherscanClient, HistoryRead, plan_not_supported, resolve_credential
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

_TAILS = ("token-transfers", "transactions", "internal-transactions")
_ETHERSCAN_ACTIONS = ("txlist", "tokentx", "txlistinternal")
_ETHERSCAN_OFFSET = 1000
_ETHERSCAN_PAGE_LIMIT = 10
_ETHERSCAN_END_BLOCK = "9999999999"
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
    parts: list[dict[str, str]] | None = None
    ignored_zero_value: int = 0


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
        try:
            read = client.read_v2(
                address,
                tail,
                query={"filter": direction},
                keep_partial=True,
                stop_timestamp=stop_timestamp,
                page_cap=page_cap,
                skip_pending=True,
            )
        except BudgetExhausted as exc:
            read = _gap_history(str(getattr(exc, "reason", "") or "") or "budget_exhausted")
        except SecurityError as exc:
            if getattr(exc, "reason", "") != "redirect_host":
                raise
            read = _gap_history("redirect_host")
        reads.append((tail, read))
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
        credential = resolve_credential(os.environ, env_name, secrets)
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
            client, transport, source_id, chain_id, action, address, page_cap, stop_timestamp, clock,
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
    try:
        walk = client.history(path, window, None, page_cap=page_cap)
    except BudgetExhausted as exc:
        return _part_outcome("gap", str(getattr(exc, "reason", "") or "") or "budget_exhausted", "trc20")
    except SecurityError as exc:
        if getattr(exc, "reason", "") != "redirect_host":
            raise
        return _part_outcome("gap", "redirect_host", "trc20")
    if walk.complete and not walk.error:
        status, reason = "complete", ""
    elif walk.reason in {"page_cap", "request_cap"}:
        status, reason = "partial", walk.reason
    else:
        status, reason = "gap", walk.reason or "read_error"
    if status == "gap":
        return _part_outcome(status, reason, "trc20")
    edges: list[Edge] = []
    unreadable = False
    zeros = 0
    contract: bool | None = None
    for row in walk.rows:
        parsed = _tron_edge(row, address, direction, network, source_id, stop_timestamp)
        if parsed == "bad":
            unreadable = True
            continue
        if parsed == "zero":
            zeros += 1
            continue
        if parsed is None or not isinstance(parsed, Edge):
            continue
        edges.append(parsed)
        if direction == "from" and parsed.sender_contract is True:
            contract = True
    if unreadable and status == "complete":
        status, reason = "partial", "unreadable_row"
    return _part_outcome(status, reason, "trc20", edges, contract, zeros)


def _part_outcome(status: str, reason: str, part: str, edges: list[Edge] | None = None, contract: bool | None = None, zeros: int = 0) -> Outcome:
    return Outcome(
        status,
        reason,
        edges or [],
        contract,
        [{"part": part, "read_status": status, "reason": reason}],
        zeros,
    )


def _gap_history(reason: str, capability: str = "unspecified") -> HistoryRead:
    text = reason or "upstream_error"
    return HistoryRead(capability, (), False, False, (), None, "error", False, text, (text,), "")


def _newest_first(operation):
    query = dict(operation.query or {})
    query["sort"] = "desc"
    query["endblock"] = _ETHERSCAN_END_BLOCK
    return replace(operation, query=query)


def _etherscan_page(result: Any) -> tuple[str, str]:
    """Classify one account page as empty, gap, or rows."""
    body = getattr(result, "body", None)
    if isinstance(body, dict) and str(body.get("status")) == "0":
        if _etherscan_absent(body):
            return "empty", ""
        return "gap", "upstream_error"
    category = str(getattr(result, "category", "") or "")
    if category == "empty":
        return "empty", ""
    if category != "ok":
        return "gap", str(getattr(result, "reason", "") or "") or "upstream_error"
    if not getattr(result, "ok", False):
        return "gap", str(getattr(result, "reason", "") or "") or "upstream_error"
    return "rows", ""


def _etherscan_absent(body: Mapping[str, Any]) -> bool:
    texts: list[str] = []
    for key in ("message", "result"):
        value = body.get(key)
        if isinstance(value, str):
            texts.append(value.lower())
    blob = " ".join(texts)
    return "no transactions found" in blob or "no records found" in blob


def _within_window(rows: list[Any], stop_timestamp: int) -> tuple[list[Any], bool]:
    kept: list[Any] = []
    for row in rows:
        stamp = _stamp(row) if isinstance(row, Mapping) else None
        if stamp is not None and stamp < stop_timestamp:
            return kept, True
        kept.append(row)
    return kept, False


def _etherscan_action(client, transport, source_id, chain_id, action, address, page_cap, stop_timestamp, clock):
    capability = {"txlist": "history.native_external", "txlistinternal": "history.native_internal", "tokentx": "history.fungible"}.get(action, "unspecified")
    # page * offset must stay within 10000. Offset is 1000, so at most 10 pages.
    limit = page_cap if page_cap < _ETHERSCAN_PAGE_LIMIT else _ETHERSCAN_PAGE_LIMIT
    items: list[Any] = []
    for page in range(1, limit + 1):
        operation = _newest_first(client.operation(
            chain_id=chain_id,
            module="account",
            action=action,
            address=address,
            start_block=0,
            end_block=0,
            page=page,
            offset=_ETHERSCAN_OFFSET,
        ))
        try:
            result = read_with_retry(
                lambda operation=operation: transport.request(source_id, operation),
                retry_attempts=transport.retry_attempts(source_id),
                sleep=lambda _seconds: None,
                random=lambda: 0.0,
                clock=clock,
            )
        except BudgetExhausted as exc:
            return _gap_history(str(getattr(exc, "reason", "") or "") or "budget_exhausted", capability)
        except SecurityError as exc:
            if getattr(exc, "reason", "") != "redirect_host":
                raise
            return _gap_history("redirect_host", capability)
        except TransportError as exc:
            if exc.reason == "fixture_miss":
                raise
            return _gap_history(exc.reason or "error", capability)
        except Exception:
            return _gap_history("error", capability)
        if plan_not_supported(getattr(result, "body", None)):
            return HistoryRead(capability, (), False, False, (), None, "error", True, "plan_not_supported", ("plan_not_supported",), "")
        decision, why = _etherscan_page(result)
        if decision == "gap":
            return _gap_history(why, capability)
        if decision == "empty":
            break
        body = getattr(result, "body", None)
        rows = body.get("result") if isinstance(body, dict) else None
        if isinstance(rows, str):
            lowered = rows.lower()
            if "no transactions found" in lowered or "no records found" in lowered:
                break
            return _gap_history("malformed", capability)
        if not isinstance(rows, list):
            return _gap_history("malformed", capability)
        kept, window_hit = _within_window(rows, stop_timestamp)
        items.extend(kept)
        if window_hit or len(rows) < _ETHERSCAN_OFFSET:
            break
    else:
        return HistoryRead(
            capability, tuple(items), False, False, (), None, "partial", False, "result_window", ("result_window",), "",
        )
    category = "empty" if not items else "ok"
    return HistoryRead(capability, tuple(items), True, True, (), None, category, False, "", (), "")


def _from_history(parts, address, direction, network, family, source_id, entry, stop_timestamp, *, etherscan: bool = False) -> Outcome:
    edges: list[Edge] = []
    recorded: list[dict[str, str]] = []
    zeros = 0
    unreadable = False
    contract: bool | None = None
    symbol, decimals = _native(entry)
    for kind, read in parts:
        state, why = _history_status(read)
        if state != "gap":
            part_bad = False
            for item in read.items:
                parsed = _evm_edge(
                    item, kind, address, direction, network, family, source_id, stop_timestamp,
                    symbol, decimals, etherscan=etherscan,
                )
                if parsed == "bad":
                    part_bad = True
                    unreadable = True
                    continue
                if parsed == "zero":
                    zeros += 1
                    continue
                if parsed is None or not isinstance(parsed, Edge):
                    continue
                edges.append(parsed)
                if direction == "from" and parsed.sender_contract is True:
                    contract = True
            if state == "complete" and part_bad:
                state, why = "partial", "unreadable_row"
        recorded.append({"part": kind, "read_status": state, "reason": why})
    gap = next((row for row in recorded if row["read_status"] == "gap"), None)
    if gap is not None:
        status, reason = "gap", gap["reason"] or "upstream_error"
    else:
        partial = next((row for row in recorded if row["read_status"] == "partial"), None)
        if partial is not None:
            status, reason = "partial", partial["reason"] or ("unreadable_row" if unreadable else "partial")
        elif unreadable:
            status, reason = "partial", "unreadable_row"
        else:
            status, reason = "complete", ""
    return Outcome(status, reason, edges, contract, recorded, zeros)


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
    raw_to = item.get("to")
    if raw_to is None or raw_to == "":
        return None
    sender, sender_contract = _party(item.get("from"))
    recipient, recipient_contract = _party(raw_to)
    sender = _canonical(family, sender)
    recipient = _canonical(family, recipient)
    if direction == "to":
        if not recipient or not _same(family, recipient, address):
            return None
        if not sender:
            return "bad"
    elif direction == "from":
        if not sender or not _same(family, sender, address):
            return None
        if not recipient:
            return "bad"
    else:
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
    if asset == "bad":
        return "bad"
    if asset is None:
        return None
    kind_name, contract, symbol, decimals, raw = asset
    if raw == 0:
        return "zero"
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
    raw = _amount(row.get("value"))
    if raw == 0:
        return "zero"
    return Edge(
        network, tx_hash, _block(row), stamp, source_id, sender, recipient, None, None,
        "token", contract, symbol, _decimals(token.get("decimals")), raw,
    )


def _blockscout_asset(kind, item, family, native_symbol, native_decimals):
    if kind == "token-transfers":
        token = item.get("token") if isinstance(item.get("token"), Mapping) else {}
        token_type = str(token.get("type") or "").upper().replace("_", "-")
        if token_type in _NFT:
            return None
        contract = _canonical(family, str(token.get("address") or token.get("address_hash") or ""))
        if not contract:
            return "bad"
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
            return "bad"
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
