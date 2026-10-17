"""Read one address in one direction from a source the profile already has.

BSC keyless reads use 48club ``eth_getLogs`` for ERC-20 transfers only.
Native and internal BNB are a gap on that path. NodeReal is used only when
the caller already selected it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from guardian.balances.internal import _receipt_state
from guardian.chains.evm.accounting import TRANSFER_TOPIC, pad_address
from guardian.errors import ConfigError, SecurityError
from guardian.identities import parse_address
from guardian.security.transport_policy import Operation
from guardian.sources.blockscout import BlockscoutClient, HistoryRead
from guardian.sources.budget import BudgetExhausted
from guardian.sources.nodereal import MAX_BLOCK_SPAN, NodeRealClient, _method_refusal
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

_TAILS = ("token-transfers", "transactions", "internal-transactions")
_NATIVE_KINDS = frozenset({"transactions", "internal-transactions"})
_TOKEN_KINDS = frozenset({"token-transfers"})
_NFT = frozenset({"ERC-721", "ERC-1155", "ERC721", "ERC1155"})
_CLUB_RANGE = 1000
_NATIVE_LOG_GAP = "not_in_keyless_logs"
_TRON_UNREAD_GAP = "not_in_trc20_endpoint"
# One part shares this many nr_getAssetTransfers pages. It is not per chunk.
_NODEREAL_ITEM_CAP = 5_000
# eth_getBlockByNumber calls per part. Cached hits are free. The cache lives for one command.
_TIME_LOOKUP_CAP = 32
_CAP_REASONS = frozenset({"page_cap", "item_cap", "time_lookup_cap"})
_CODE_SKIP = frozenset({"rate_limited", "daily_cap", "redirect_host"})
_NODEREAL_PARTS = (
    ("20", "token-transfers"),
    ("external", "transactions"),
    ("internal", "internal-transactions"),
)


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
    contract_withhold: bool = False
    contract_check: dict[str, str] | None = None


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
    page_lengths: list[int] = []

    def accept_page(batch: list[Any]) -> bool:
        page_lengths.append(len(batch))
        return True

    try:
        walk = client.history(
            path, window, None, page_cap=page_cap, accept_page=accept_page,
        )
    except BudgetExhausted as exc:
        return _tron_outcome("gap", str(getattr(exc, "reason", "") or "") or "budget_exhausted")
    except SecurityError as exc:
        if getattr(exc, "reason", "") != "redirect_host":
            raise
        return _tron_outcome("gap", "redirect_host")
    caught = tap.security_error
    if caught is not None and getattr(caught, "reason", "") != "redirect_host":
        raise caught
    if caught is not None:
        status, reason = "gap", "redirect_host"
    elif walk.complete and not walk.error:
        status, reason = "complete", ""
    elif walk.reason in {"page_cap", "request_cap"}:
        status, reason = "partial", walk.reason
    else:
        status, reason = "gap", walk.reason or "read_error"
    unreadable = False
    zeros = 0
    contract: bool | None = None
    # The page that repeated a fingerprint is already in walk.rows. That cursor
    # is unusable, so those rows are not counted. Earlier pages stay.
    rows = list(walk.rows)
    lengths = list(page_lengths)
    if walk.reason == "repeated_fingerprint" and lengths:
        drop = lengths[-1]
        if drop > 0:
            rows = rows[:-drop]
        lengths = lengths[:-1]
    fold = _PageFold(_no_row_key)
    for page in _pages_of(rows, lengths):
        pairs: list[tuple[Any, Edge]] = []
        for row in page:
            parsed = _tron_edge(row, address, direction, network, source_id, stop_timestamp)
            if parsed == "bad":
                unreadable = True
                continue
            if parsed == "zero":
                zeros += 1
                continue
            if parsed is None or not isinstance(parsed, Edge):
                continue
            pairs.append((row, parsed))
            if direction == "from" and parsed.sender_contract is True:
                contract = True
        fold.add_page(pairs)
    if unreadable and status == "complete":
        status, reason = "partial", "unreadable_row"
    edges, status, reason = fold.apply(status, reason)
    return _note_contract(_tron_outcome(status, reason, edges, contract, zeros), direction)


def _tron_outcome(
    status: str,
    reason: str,
    edges: list[Edge] | None = None,
    contract: bool | None = None,
    zeros: int = 0,
) -> Outcome:
    """TRC-20 keeps its own status. Native TRX and TRC-10 were not requested."""
    recorded = [
        {"part": "trc20", "read_status": status, "reason": reason},
        {"part": "trx", "read_status": "gap", "reason": _TRON_UNREAD_GAP},
        {"part": "trc10", "read_status": "gap", "reason": _TRON_UNREAD_GAP},
    ]
    return _rolled(recorded, edges or [], zeros, contract)


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
        if state != "gap" or why == "repeated_cursor":
            part_bad = False
            fold = _PageFold(_blockscout_key(kind))
            for page in _pages_of(read.items, getattr(read, "page_lengths", ())):
                pairs: list[tuple[Any, Edge]] = []
                for item in page:
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
                    pairs.append((item, parsed))
                    if direction == "from" and parsed.sender_contract is True:
                        contract = True
                fold.add_page(pairs)
            if state == "complete" and part_bad:
                state, why = "partial", "unreadable_row"
            kept, state, why = fold.apply(state, why)
            edges.extend(kept)
        recorded.append({"part": kind, "read_status": state, "reason": why})
    return _note_contract(_rolled(recorded, edges, zeros, contract, unreadable), direction)


def _rolled(recorded, edges, zeros, contract, unreadable: bool = False) -> Outcome:
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


def _note_contract(outcome: Outcome, direction: str) -> Outcome:
    """A False flag is an observation. Missing flags stay unknown."""
    if direction != "from":
        return outcome
    flags = [edge.sender_contract for edge in outcome.edges]
    if any(flag is True for flag in flags):
        outcome.payer_is_contract = True
        outcome.contract_withhold = False
    elif outcome.payer_is_contract is None and any(flag is False for flag in flags):
        outcome.payer_is_contract = False
    return outcome


def _retry_sleep(seconds: float) -> None:
    """Wait out one retry. ``read_with_retry`` already caps this at 30 seconds."""
    time.sleep(max(0.0, float(seconds)))


def _retry_controls(transport: Any, source_id: str) -> tuple[Callable[[float], None], Callable[[], float]]:
    """Uncapped retry sleep and a real random unit. A missing group does not invent a wait.

    The token bucket still uses the sleeper installed on the budget group. This
    wait does not pass through that sleeper, so the 10 second budget cap does
    not shorten ``Retry-After``.
    """
    try:
        source = transport.endpoints.get(source_id)
        transport.budgets.group(source.budget_group)
    except (AttributeError, ConfigError, SecurityError):
        return (lambda _seconds: None), random.random
    return _retry_sleep, random.random


class _Lookups:
    """Block timestamps for one part. Successful stamps are cached for the command."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        adapter: str,
        clock: Callable[[], float],
        cache: dict[tuple[str, int], int],
        cap: int,
    ) -> None:
        self.transport = transport
        self.source_id = source_id
        self.adapter = adapter
        self.clock = clock
        self.cache = cache
        self.cap = cap
        self.used = 0

    def one(self, block: int) -> tuple[int | None, str]:
        key = (self.source_id, block)
        found = self.cache.get(key)
        if found is not None:
            return found, ""
        if self.used >= self.cap:
            return None, "time_lookup_cap"
        self.used += 1
        payload, reason = _jsonrpc(
            self.transport,
            self.source_id,
            self.adapter,
            "eth_getBlockByNumber",
            [hex(block), False],
            self.clock,
        )
        if reason:
            return None, reason
        if not isinstance(payload, Mapping):
            return None, "read_error"
        stamp = _hex_int(payload.get("timestamp"))
        if stamp is None:
            return None, "window_unchecked"
        self.cache[key] = stamp
        return stamp, ""


def _host_block_reason(outcome: Outcome) -> str:
    if outcome.reason in _CODE_SKIP:
        return outcome.reason
    for part in outcome.parts or []:
        reason = part.get("reason")
        if reason in _CODE_SKIP:
            return str(reason)
        stamped = part.get("timestamp_reason")
        if stamped in _CODE_SKIP:
            return str(stamped)
    return ""


def _attach_payer_code(outcome: Outcome, transport: Any, source_id: str, adapter: str, address: str, clock) -> None:
    """One eth_getCode for the payer on the host this read already uses."""
    blocked = _host_block_reason(outcome)
    if blocked:
        outcome.contract_check = {"status": "gap", "reason": blocked}
        outcome.contract_withhold = True
        if outcome.payer_is_contract is not True:
            outcome.payer_is_contract = None
        return
    payload, reason = _jsonrpc(transport, source_id, adapter, "eth_getCode", [address, "latest"], clock)
    text = payload.strip().lower() if isinstance(payload, str) else ""
    digits = text[2:] if text.startswith("0x") else None
    invalid = bool(reason) or digits is None or any(char not in "0123456789abcdef" for char in digits)
    if invalid:
        outcome.contract_check = {"status": "gap", "reason": reason or "read_error"}
    else:
        outcome.contract_check = {"status": "complete", "reason": ""}
    if outcome.payer_is_contract is True:
        outcome.contract_withhold = False
        return
    if invalid:
        outcome.payer_is_contract = None
        outcome.contract_withhold = True
        return
    if digits == "":
        outcome.payer_is_contract = False
        outcome.contract_withhold = False
        return
    outcome.payer_is_contract = True
    outcome.contract_withhold = False


def _time_failure(edges: list[Edge], why: str, *, accepted: bool = False) -> tuple[str, str]:
    reason = why or "window_unchecked"
    if edges or accepted or reason in _CAP_REASONS:
        return "partial", reason
    return "gap", reason


def read_club(
    transport: Any,
    source_id: str,
    *,
    address: str,
    direction: str,
    network: str,
    family: str,
    page_cap: int,
    stop_timestamp: int,
    entry: Mapping[str, Any],
    clock: Callable[[], float],
    range_limit: int = _CLUB_RANGE,
    time_cache: dict[tuple[str, int], int] | None = None,
) -> Outcome:
    """ERC-20 logs from 48club. Native and internal parts stay gaps."""
    del entry
    cache = time_cache if time_cache is not None else {}

    def finish(outcome: Outcome) -> Outcome:
        if direction == "from":
            _attach_payer_code(outcome, transport, source_id, "evm_rpc", address, clock)
        return outcome

    width = range_limit if isinstance(range_limit, int) and 1 <= range_limit <= _CLUB_RANGE else _CLUB_RANGE
    try:
        pad_address(address)
    except ValueError:
        return finish(_club_outcome("gap", "bad_address", [], 0, None))
    head, failed = _jsonrpc(transport, source_id, "evm_rpc", "eth_blockNumber", [], clock)
    if failed or not isinstance(head, str):
        return finish(_club_outcome("gap", failed or "read_error", [], 0, None))
    cursor = _hex_int(head)
    if cursor is None:
        return finish(_club_outcome("gap", "read_error", [], 0, None))
    edges: list[Edge] = []
    zeros = 0
    unreadable = False
    status, reason = "complete", ""
    saw = False
    covered = False
    chunks = 0
    chunk_pages: list[list[tuple[Any, Edge]]] = []
    lookups = _Lookups(transport, source_id, "evm_rpc", clock, cache, _TIME_LOOKUP_CAP)
    while chunks < page_cap and cursor >= 0:
        start = max(0, cursor - width + 1)
        logs, failed = _jsonrpc(
            transport,
            source_id,
            "evm_rpc",
            "eth_getLogs",
            [_club_filter(address, direction, start, cursor)],
            clock,
        )
        chunks += 1
        if failed or not isinstance(logs, list):
            status, reason = ("gap", failed or "read_error") if not saw else ("partial", failed or "read_error")
            break
        prior = saw
        saw = True
        part_edges, part_zeros, part_bad, _blocks = _club_logs(
            logs, address, direction, network, family, source_id,
        )
        zeros += part_zeros
        unreadable = unreadable or part_bad
        if start != 0:
            opening, why = lookups.one(start)
            if opening is None:
                # A finished chunk, or this chunk's empty log read, is a successful request.
                # Rows waiting for their first stamp stay a gap.
                status, reason = _time_failure(edges, why, accepted=prior or not part_edges)
                break
            if opening <= stop_timestamp:
                covered = True
        else:
            covered = True
        stopped = False
        page_pairs: list[tuple[Any, Edge]] = []
        for edge, block, raw in part_edges:
            stamp, why = lookups.one(block)
            if stamp is None:
                status, reason = _time_failure(edges, why, accepted=prior)
                stopped = True
                break
            if stamp < stop_timestamp:
                continue
            edge.timestamp = stamp
            edges.append(edge)
            page_pairs.append((raw, edge))
        chunk_pages.append(page_pairs)
        if stopped or covered:
            break
        cursor = start - 1
    else:
        if status == "complete":
            status, reason = "partial", "page_cap"
    if status == "complete" and not covered:
        status, reason = "partial", "page_cap"
    if unreadable and status == "complete":
        status, reason = "partial", "unreadable_row"
    fold = _PageFold(_club_key)
    for page in chunk_pages:
        fold.add_page(page)
    edges, status, reason = fold.apply(status, reason)
    return finish(_club_outcome(status, reason, edges, zeros, None))


def _club_outcome(status: str, reason: str, edges: list[Edge], zeros: int, contract: bool | None) -> Outcome:
    recorded = [
        {"part": "token-transfers", "read_status": status, "reason": reason},
        {"part": "transactions", "read_status": "gap", "reason": _NATIVE_LOG_GAP},
        {"part": "internal-transactions", "read_status": "gap", "reason": _NATIVE_LOG_GAP},
    ]
    return _rolled(recorded, edges, zeros, contract)


def read_nodereal(
    transport: Any,
    source_id: str,
    *,
    address: str,
    direction: str,
    network: str,
    family: str,
    page_cap: int,
    stop_timestamp: int,
    entry: Mapping[str, Any],
    clock: Callable[[], float],
    time_cache: dict[tuple[str, int], int] | None = None,
) -> Outcome:
    """Bounded NodeReal asset transfers. A chunk past the cap is not a finished window."""
    cache = time_cache if time_cache is not None else {}
    pause, rng = _retry_controls(transport, source_id)

    def finish(outcome: Outcome) -> Outcome:
        if direction == "from":
            _attach_payer_code(outcome, transport, source_id, "nodereal_transfers", address, clock)
        return _note_contract(outcome, direction)

    symbol, decimals = _native(entry)
    head, failed = _jsonrpc(transport, source_id, "nodereal_transfers", "eth_blockNumber", [], clock)
    cursor_head = _hex_int(head) if isinstance(head, str) else None
    if failed or cursor_head is None:
        reason = failed or "read_error"
        recorded = [
            {"part": part, "read_status": "gap", "reason": reason}
            for _category, part in _NODEREAL_PARTS
        ]
        return finish(_rolled(recorded, [], 0, None))
    client = NodeRealClient(transport, source_id, network=network, clock=clock, sleep=pause, random=rng)
    edges: list[Edge] = []
    zeros = 0
    recorded: list[dict[str, str]] = []
    for category, part in _NODEREAL_PARTS:
        lookups = _Lookups(transport, source_id, "nodereal_transfers", clock, cache, _TIME_LOOKUP_CAP)
        rows, lengths, state, why = _nodereal_part(
            client, address, direction, category, cursor_head, page_cap, stop_timestamp, lookups,
        )
        part_bad = False
        time_reason = ""
        indexes = list(range(len(rows)))
        repeated = state == "gap" and why == "repeated_cursor"
        if state != "gap" or repeated:
            # A repeated cursor stays gap. The rows already kept still need timestamps.
            rows, time_reason, indexes = _stamp_nodereal_rows(
                rows, lookups, "" if repeated else (why if why in _CODE_SKIP else ""),
            )
            if time_reason and not repeated:
                capped = time_reason in _CAP_REASONS
                if not rows and state == "complete" and not capped and time_reason not in _CODE_SKIP:
                    state, why = "gap", time_reason
                elif not rows or state == "complete":
                    state, why = "partial", time_reason
        if state != "gap" or why == "repeated_cursor":
            fold = _PageFold(_nodereal_key(category))
            for page in _indexed_pages(rows, indexes, lengths):
                pairs: list[tuple[Any, Edge]] = []
                for item in page:
                    parsed = _nodereal_edge(
                        item, category, address, direction, network, family, source_id, stop_timestamp, symbol, decimals,
                    )
                    if parsed == "bad":
                        part_bad = True
                        continue
                    if parsed == "zero":
                        zeros += 1
                        continue
                    if parsed is None or not isinstance(parsed, Edge):
                        continue
                    pairs.append((item, parsed))
                fold.add_page(pairs)
            if state == "complete" and part_bad:
                state, why = "partial", "unreadable_row"
            kept, state, why = fold.apply(state, why)
            edges.extend(kept)
        record = {"part": part, "read_status": state, "reason": why}
        if time_reason:
            record["timestamp_reason"] = time_reason
        recorded.append(record)
    return finish(_rolled(recorded, edges, zeros, None))


class _PageFold:
    """One part's pages. A row key drops a repeat. A later equal row without a key does not stay complete."""

    def __init__(self, key_of) -> None:
        self._key_of = key_of
        self.edges: list[Edge] = []
        self._keys: set[tuple] = set()
        self._marks: set[tuple] = set()
        self.ambiguous = False

    def add_page(self, pairs: list[tuple[Any, Edge]]) -> None:
        page_keys: set[tuple] = set()
        page_marks: list[tuple] = []
        for raw, edge in pairs:
            key = self._key_of(raw, edge)
            if key is not None:
                if key in self._keys or key in page_keys:
                    continue
                page_keys.add(key)
                self.edges.append(edge)
                continue
            mark = (edge.tx_hash, edge.sender, edge.recipient, edge.contract, edge.raw_amount)
            if mark in self._marks:
                self.ambiguous = True
                continue
            page_marks.append(mark)
            self.edges.append(edge)
        self._keys.update(page_keys)
        self._marks.update(page_marks)

    def apply(self, state: str, reason: str) -> tuple[list[Edge], str, str]:
        if self.ambiguous and state == "complete":
            return self.edges, "partial", "ambiguous_duplicate"
        return self.edges, state, reason


def _pages_of(items, lengths) -> list[list[Any]]:
    rows = list(items)
    if not lengths:
        return [rows]
    pages: list[list[Any]] = []
    cursor = 0
    for length in lengths:
        size = length if isinstance(length, int) and length > 0 else 0
        pages.append(rows[cursor:cursor + size])
        cursor += size
    if cursor < len(rows):
        pages.append(rows[cursor:])
    return pages


def _indexed_pages(rows: list[Any], indexes: list[int], lengths: list[int]) -> list[list[Any]]:
    if not lengths:
        return [list(rows)]
    owners: list[int] = []
    for number, length in enumerate(lengths):
        size = length if isinstance(length, int) and length > 0 else 0
        owners.extend([number] * size)
    grouped: list[list[Any]] = [[] for _ in lengths]
    for row, index in zip(rows, indexes):
        if index < 0 or index >= len(owners):
            if not grouped:
                grouped.append([])
            grouped[-1].append(row)
            continue
        grouped[owners[index]].append(row)
    return grouped


def _limit_rows(items: list[Any], lengths: list[int]) -> tuple[list[Any], list[int]]:
    cap = _NODEREAL_ITEM_CAP if _NODEREAL_ITEM_CAP > 0 else 0
    if len(items) <= cap:
        return list(items), list(lengths)
    fitted: list[int] = []
    left = cap
    for length in lengths:
        if left <= 0:
            break
        size = length if isinstance(length, int) and length > 0 else 0
        if size == 0:
            fitted.append(0)
            continue
        take = size if size <= left else left
        fitted.append(take)
        left -= take
    return list(items[:cap]), fitted


def _no_row_key(_raw: Any, _edge: Edge):
    return None


def _blockscout_key(kind: str):
    def key(raw: Any, edge: Edge):
        if not isinstance(raw, Mapping) or not edge.tx_hash:
            return None
        if kind == "transactions":
            return ("tx", edge.tx_hash)
        if kind == "internal-transactions":
            position = _trace_position(raw)
            if position is None:
                return None
            return ("trace", edge.tx_hash, position)
        if kind == "token-transfers":
            position = _log_position(raw)
            if position is None:
                return None
            return ("log", edge.tx_hash, position)
        return None

    return key


def _nodereal_key(category: str):
    def key(raw: Any, edge: Edge):
        if not isinstance(raw, Mapping) or not edge.tx_hash:
            return None
        if category == "external":
            return ("tx", edge.tx_hash)
        if category == "internal":
            position = _trace_position(raw)
            if position is None:
                return None
            return ("trace", edge.tx_hash, position)
        return None

    return key


def _club_key(raw: Any, edge: Edge):
    if not isinstance(raw, Mapping) or not edge.tx_hash:
        return None
    position = _log_position(raw)
    if position is None:
        return None
    return ("log", edge.tx_hash, position)


def _trace_position(raw: Mapping[str, Any]) -> str | None:
    for name in ("uniqueId", "traceId", "traceAddress", "index"):
        found = raw.get(name)
        if isinstance(found, str) and found.strip():
            return f"{name}:{found.strip()}"
        if isinstance(found, int) and not isinstance(found, bool):
            return f"{name}:{found}"
        if (
            isinstance(found, list)
            and found
            and all(isinstance(part, int) and not isinstance(part, bool) for part in found)
        ):
            return name + ":" + ".".join(str(part) for part in found)
    return None


def _log_position(raw: Mapping[str, Any]) -> str | None:
    for name in ("logIndex", "log_index"):
        found = raw.get(name)
        if isinstance(found, bool):
            continue
        if isinstance(found, int):
            return str(found)
        if isinstance(found, str) and found.strip():
            text = found.strip()
            if text.lower().startswith("0x"):
                try:
                    return str(int(text, 16))
                except ValueError:
                    return text.lower()
            if text.isdigit():
                return str(int(text))
            return text.lower()
    return None


def _nodereal_part(client, address, direction, category, head, page_cap, stop_timestamp, lookups: _Lookups):
    items: list[Any] = []
    lengths: list[int] = []
    cursor = head
    pages_left = page_cap
    accepted = False
    while pages_left > 0 and cursor >= 0:
        start = max(0, cursor - MAX_BLOCK_SPAN)
        room = _NODEREAL_ITEM_CAP - len(items)
        page, reason, done, used, ok = _nodereal_chunk(
            client, address, direction, category, start, cursor, pages_left, room, lengths,
        )
        pages_left -= used
        accepted = accepted or ok
        if reason == "repeated_cursor":
            items.extend(page)
            trimmed, trimmed_lengths = _limit_rows(items, lengths)
            return trimmed, trimmed_lengths, "gap", "repeated_cursor"
        if not done:
            items.extend(page)
            if not accepted:
                why = reason or "read_error"
                if why in _CAP_REASONS:
                    return [], [], "partial", why
                return [], [], "gap", why
            why = reason or "read_error"
            trimmed, trimmed_lengths = _limit_rows(items, lengths)
            if len(items) >= _NODEREAL_ITEM_CAP and why == "item_cap":
                return trimmed, trimmed_lengths, "partial", "item_cap"
            return trimmed, trimmed_lengths, "partial", why
        items.extend(page)
        if len(items) >= _NODEREAL_ITEM_CAP:
            trimmed, trimmed_lengths = _limit_rows(items, lengths)
            return trimmed, trimmed_lengths, "partial", "item_cap"
        if start == 0 or _covers(page, stop_timestamp):
            return list(items), list(lengths), "complete", ""
        opening, why = lookups.one(start)
        if opening is None:
            why = why or "window_unchecked"
            return list(items), list(lengths), "partial", why
        if opening <= stop_timestamp:
            return list(items), list(lengths), "complete", ""
        cursor = start - 1
    trimmed, trimmed_lengths = _limit_rows(items, lengths)
    return trimmed, trimmed_lengths, "partial", "page_cap"


def _nodereal_chunk(
    client: NodeRealClient,
    address,
    direction,
    category,
    start,
    end,
    page_budget: int,
    item_budget: int,
    lengths: list[int],
):
    kept: list[Any] = []
    token: str | None = None
    used = 0
    succeeded = False
    sent: set[str] = set()
    pause, rng = client.sleep, client.random
    while used < page_budget and len(kept) < item_budget:
        if token is not None:
            sent.add(token)
        used += 1
        operation = client.operation(
            category=category, address=address, start=start, end=end, page_key=token, side=direction,
        )
        try:
            result = read_with_retry(
                lambda operation=operation: client.transport.request(client.source_id, operation),
                retry_attempts=client.transport.retry_attempts(client.source_id),
                sleep=pause,
                random=rng,
                clock=client.clock,
            )
        except BudgetExhausted as exc:
            return kept, str(getattr(exc, "reason", "") or "") or "budget_exhausted", False, used, succeeded
        except SecurityError as exc:
            if getattr(exc, "reason", "") != "redirect_host":
                raise
            return kept, "redirect_host", False, used, succeeded
        except TransportError as exc:
            if exc.reason == "fixture_miss":
                raise
            return kept, exc.reason or "read_error", False, used, succeeded
        refusal = _method_refusal(getattr(result, "body", None))
        if refusal:
            return kept, refusal, False, used, succeeded
        payload, reason = _rpc_payload(result)
        if reason:
            return kept, reason, False, used, succeeded
        if not isinstance(payload, Mapping) or not isinstance(payload.get("transfers"), list):
            return kept, "missing_transfers", False, used, succeeded
        succeeded = True
        batch = payload["transfers"]
        room = item_budget - len(kept)
        page_key = payload.get("pageKey")
        if isinstance(page_key, str) and page_key != "" and page_key in sent:
            return kept, "repeated_cursor", False, used, True
        if len(batch) > room:
            taken = batch[:room]
            kept.extend(taken)
            lengths.append(len(taken))
            return kept, "item_cap", False, used, True
        kept.extend(batch)
        lengths.append(len(batch))
        if page_key is None or page_key == "":
            return kept, "", True, used, True
        if not isinstance(page_key, str):
            return kept, "bad_page_key", False, used, succeeded
        token = page_key
    if len(kept) >= item_budget:
        return kept[:item_budget], "item_cap", False, used, True
    return kept, "page_cap", False, used, succeeded


def _stamp_nodereal_rows(
    rows: list[Any], lookups: _Lookups, stopped: str = "",
) -> tuple[list[Any], str, list[int]]:
    stamped: list[Any] = []
    indexes: list[int] = []
    reason = ""
    halted = False
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping) or _nodereal_stamp(item) is not None or _failed(item):
            stamped.append(item)
            indexes.append(index)
            continue
        if stopped:
            reason = stopped
            continue
        block = _flexible_block(item)
        if block is None:
            stamped.append(item)
            indexes.append(index)
            continue
        if halted:
            continue
        stamp, why = lookups.one(block)
        if stamp is None:
            reason = why or "window_unchecked"
            if reason in _CODE_SKIP:
                halted = True
            continue
        copied = dict(item)
        meta = copied.get("metadata")
        merged = dict(meta) if isinstance(meta, Mapping) else {}
        merged["blockTimestamp"] = stamp
        copied["metadata"] = merged
        stamped.append(copied)
        indexes.append(index)
    return stamped, reason, indexes


def _club_filter(address: str, direction: str, start: int, end: int) -> dict[str, Any]:
    padded = pad_address(address)
    topics: list[Any] = [TRANSFER_TOPIC, None, padded] if direction == "to" else [TRANSFER_TOPIC, padded, None]
    return {"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}


def _club_logs(logs, address, direction, network, family, source_id):
    edges: list[tuple[Edge, int]] = []
    zeros = 0
    bad = False
    blocks: set[int] = set()
    for item in logs:
        parsed = _club_log(item, address, direction, network, family, source_id)
        if parsed == "bad":
            bad = True
            continue
        if parsed == "zero":
            zeros += 1
            continue
        if parsed is None or not isinstance(parsed, tuple):
            continue
        edge, block = parsed
        blocks.add(block)
        edges.append((edge, block, item))
    return edges, zeros, bad, blocks


def _club_log(item, address, direction, network, family, source_id):
    if not isinstance(item, Mapping) or item.get("removed") is True:
        return None
    topics = item.get("topics")
    if not isinstance(topics, list) or not topics or not isinstance(topics[0], str):
        return "bad"
    if topics[0].lower() != TRANSFER_TOPIC.lower():
        return None
    if len(topics) != 3:
        return None
    sender = _canonical(family, _topic_address(topics[1]))
    recipient = _canonical(family, _topic_address(topics[2]))
    token = _canonical(family, str(item.get("address") or ""))
    if direction == "to":
        if not recipient or not _same(family, recipient, address) or not sender:
            return None if recipient else "bad"
    elif direction == "from":
        if not sender or not _same(family, sender, address) or not recipient:
            return None if sender else "bad"
    else:
        return None
    if not token:
        return "bad"
    raw = _word(item.get("data"))
    if raw is None:
        return "bad"
    if raw == 0:
        return "zero"
    block = _hex_int(item.get("blockNumber"))
    tx_hash = _log_hash(item.get("transactionHash"))
    if block is None or not tx_hash:
        return "bad"
    edge = Edge(
        network, tx_hash, block, 0, source_id, sender, recipient, None, None,
        "token", token, None, None, raw,
    )
    return edge, block


def _nodereal_edge(item, category, address, direction, network, family, source_id, stop_timestamp, native_symbol, native_decimals):
    if not isinstance(item, Mapping):
        return "bad"
    if _failed(item):
        return None
    sender = _canonical(family, item.get("from") if isinstance(item.get("from"), str) else "")
    recipient = _canonical(family, item.get("to") if isinstance(item.get("to"), str) else "")
    if direction == "to":
        if not recipient or not _same(family, recipient, address) or not sender:
            return None if recipient else "bad"
    elif direction == "from":
        if not sender or not _same(family, sender, address) or not recipient:
            return None if sender else "bad"
    else:
        return None
    stamp = _nodereal_stamp(item)
    if stamp is None:
        return "bad"
    if stamp < stop_timestamp:
        return None
    tx_hash = _log_hash(item.get("hash") or item.get("tx_hash"))
    if not tx_hash:
        return "bad"
    block = _flexible_block(item)
    raw = _raw_contract_value(item)
    if raw is None:
        return "bad"
    if raw == 0:
        return "zero"
    if category == "20":
        token = _raw_contract_address(item, family)
        if not token:
            return "bad"
        return Edge(
            network, tx_hash, block, stamp, source_id, sender, recipient, None, None,
            "token", token, _symbol(item), _raw_decimals(item), raw,
        )
    return Edge(
        network, tx_hash, block, stamp, source_id, sender, recipient, None, None,
        "native", None, native_symbol, native_decimals, raw,
    )


def _jsonrpc(transport, source_id, adapter, method, params, clock):
    operation = Operation(protocol="jsonrpc", adapter=adapter, method=method, params=params)
    pause, rng = _retry_controls(transport, source_id)
    try:
        result = read_with_retry(
            lambda: transport.request(source_id, operation),
            retry_attempts=transport.retry_attempts(source_id),
            sleep=pause,
            random=rng,
            clock=clock,
        )
    except BudgetExhausted as exc:
        return None, str(getattr(exc, "reason", "") or "") or "budget_exhausted"
    except SecurityError as exc:
        if getattr(exc, "reason", "") != "redirect_host":
            raise
        return None, "redirect_host"
    except TransportError as exc:
        if exc.reason == "fixture_miss":
            raise
        return None, exc.reason or "read_error"
    return _rpc_payload(result)


def _rpc_payload(result: Any) -> tuple[Any, str]:
    if not getattr(result, "ok", False):
        reason = str(getattr(result, "reason", "") or "")
        if reason == "rate_limited":
            return None, "rate_limited"
        body = getattr(result, "body", None)
        if isinstance(body, Mapping):
            return None, _rpc_reason(body)
        return None, reason or "read_error"
    body = getattr(result, "body", None)
    if not isinstance(body, Mapping) or "result" not in body or "error" in body:
        if isinstance(body, Mapping):
            return None, _rpc_reason(body)
        return None, "rpc_error"
    return body.get("result"), ""


def _rpc_reason(body: Mapping[str, Any]) -> str:
    err = body.get("error")
    message = ""
    if isinstance(err, Mapping):
        message = str(err.get("message") or "")
    elif isinstance(err, str):
        message = err
    text = message.lower()
    if "rate limit" in text or "too many" in text or "limit exceeded" in text:
        return "rate_limited"
    if err is not None:
        return "rpc_error"
    return "rpc_error"


def _covers(items: list[Any], stop_timestamp: int) -> bool:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        stamp = _nodereal_stamp(item)
        if stamp is not None and stamp <= stop_timestamp:
            return True
    return False


def _nodereal_stamp(item: Mapping[str, Any]) -> int | None:
    meta = item.get("metadata")
    if isinstance(meta, Mapping):
        raw = meta.get("blockTimestamp")
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return int(parsed.timestamp())
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw
    return _stamp(item)


def _topic_address(topic: Any) -> str:
    if not isinstance(topic, str) or not topic.lower().startswith("0x"):
        return ""
    hexed = topic[2:]
    if len(hexed) < 40 or any(char not in "0123456789abcdefABCDEF" for char in hexed):
        return ""
    return "0x" + hexed[-40:]


def _word(value: Any) -> int | None:
    if not isinstance(value, str) or not value.lower().startswith("0x"):
        return None
    hexed = value[2:]
    if hexed == "" or len(hexed) > 64 or any(char not in "0123456789abcdefABCDEF" for char in hexed):
        return None
    return int(hexed, 16)


def _hex_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith("0x"):
        try:
            return int(text, 16)
        except ValueError:
            return None
    if text.isdigit():
        return int(text)
    return None


def _log_hash(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) != 66 or not text.lower().startswith("0x"):
        return ""
    if any(char not in "0123456789abcdefABCDEF" for char in text[2:]):
        return ""
    return "0x" + text[2:].lower()


def _flexible_block(item: Mapping[str, Any]) -> int | None:
    for key in ("blockNum", "blockNumber", "block_number", "block"):
        parsed = _hex_int(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _raw_contract(item: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = item.get("rawContract")
    return raw if isinstance(raw, Mapping) else {}


def _raw_contract_value(item: Mapping[str, Any]) -> int | None:
    if "value" in item:
        parsed = _hex_int(item.get("value"))
        if parsed is not None:
            return parsed
    return _hex_int(_raw_contract(item).get("value"))


def _raw_contract_address(item: Mapping[str, Any], family: str) -> str:
    for key in ("contractAddress", "tokenAddress"):
        found = item.get(key)
        if isinstance(found, str) and found.strip():
            parsed = _canonical(family, found)
            if parsed:
                return parsed
    return _canonical(family, str(_raw_contract(item).get("address") or ""))


def _raw_decimals(item: Mapping[str, Any]) -> int | None:
    parsed = _hex_int(item.get("tokenDecimal"))
    if parsed is not None:
        return parsed
    return _hex_int(_raw_contract(item).get("decimal"))


def _symbol(item: Mapping[str, Any]) -> str | None:
    asset = item.get("asset")
    if isinstance(asset, str) and asset and asset.upper() not in {"BNB", "ETH"}:
        return asset
    return None


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
    if raw is None:
        return "bad"
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
        if raw is None:
            return "bad"
        decimals = _decimals(token.get("decimals"))
        if decimals is None:
            decimals = _decimals(total.get("decimals"))
        symbol = token.get("symbol") if isinstance(token.get("symbol"), str) and token.get("symbol") else None
        return "token", contract, symbol, decimals, raw
    raw = _amount(item.get("value"))
    if raw is None:
        return "bad"
    return "native", None, native_symbol, native_decimals, raw


def _failed(item: Mapping[str, Any]) -> bool:
    if _receipt_state(item) == "failure":
        return True
    if item.get("success") is False:
        return True
    status = item.get("status")
    if isinstance(status, str) and status.lower() in {"error", "reverted"}:
        return True
    if "isError" not in item:
        return False
    flag = item.get("isError")
    if isinstance(flag, bool):
        return flag
    return str(flag).strip() not in {"0", "false", "False", ""}


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
