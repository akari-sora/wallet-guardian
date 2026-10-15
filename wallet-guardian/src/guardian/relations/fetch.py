"""Read one address in one direction from a source the profile already has."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from guardian.errors import SecurityError
from guardian.identities import parse_address
from guardian.sources.blockscout import BlockscoutClient, HistoryRead
from guardian.sources.budget import BudgetExhausted

_TAILS = ("token-transfers", "transactions", "internal-transactions")
_NATIVE_KINDS = frozenset({"transactions", "internal-transactions"})
_TOKEN_KINDS = frozenset({"token-transfers"})
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


class _SecurityTap:
    """Forward a transport. Remember SecurityError from request, then raise it again."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport
        self.security_error: SecurityError | None = None

    def request(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._transport.request(*args, **kwargs)
        except SecurityError as exc:
            if self.security_error is None or getattr(self.security_error, "reason", "") == "redirect_host":
                self.security_error = exc
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)


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
    tap = _SecurityTap(transport)
    client = client_cls(
        tap,
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
    caught = tap.security_error
    if caught is not None and getattr(caught, "reason", "") != "redirect_host":
        raise caught
    if caught is not None:
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


def _from_history(parts, address, direction, network, family, source_id, entry, stop_timestamp) -> Outcome:
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
                    symbol, decimals,
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


def _evm_edge(item, kind, address, direction, network, family, source_id, stop_timestamp, native_symbol, native_decimals):
    if not isinstance(item, Mapping):
        return "bad"
    if _failed(item):
        return None
    raw_to = item.get("to")
    # Contract creation is a native row whose `to` is null or blank. A token row in that shape is unreadable.
    if kind in _NATIVE_KINDS and (raw_to is None or raw_to == ""):
        return None
    if kind in _TOKEN_KINDS and _party_text(raw_to) is None:
        return "bad"
    sender_text, sender_contract = _party(item.get("from"))
    recipient_text, recipient_contract = _party(raw_to)
    sender = _canonical(family, sender_text)
    recipient = _canonical(family, recipient_text)
    if direction == "to":
        verdict = _target_side(family, raw_to, recipient, address)
        if verdict != "keep":
            return verdict
        if _party_text(item.get("from")) is None or not sender:
            return "bad"
    elif direction == "from":
        verdict = _target_side(family, item.get("from"), sender, address)
        if verdict != "keep":
            return verdict
        if _party_text(raw_to) is None or not recipient:
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


def _failed(item: Mapping[str, Any]) -> bool:
    if item.get("success") is False:
        return True
    status = item.get("status")
    if isinstance(status, str) and status.lower() in {"error", "reverted"}:
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


def _party_text(value: Any) -> str | None:
    """Address text when the field has one. Missing and blank fields are None."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Mapping):
        raw = value.get("hash")
        if not isinstance(raw, str) or not raw.strip():
            raw = value.get("address")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None
    return None


def _target_side(family: str, raw: Any, canonical: str, address: str):
    """bad when the target field cannot be read. None when it is a different address."""
    if _party_text(raw) is None or not canonical:
        return "bad"
    if not _same(family, canonical, address):
        return None
    return "keep"


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
