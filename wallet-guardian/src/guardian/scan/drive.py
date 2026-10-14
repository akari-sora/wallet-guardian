"""Catch-up reads for logs, approvals, and indexers.

Each fully read chunk commits its events and the prefix cursor together.
A later chunk failure leaves that prefix in place. Native history, token
history, and permissions do not share one success flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from guardian.chains.evm.accounting import TRANSFER_TOPIC, account_logs, pad_address
from guardian.chains.evm.adapter import LOG_RANGE_MAX, evm_rpc
from guardian.chains.evm.permissions import APPROVAL_LOG_TOPICS, approval_log_operation, scan_approvals
from guardian.chains.evm.permits import PERMIT2_PERMIT_TOPIC
from guardian.coverage import CoverageObservation
from guardian.scan.window import (
    LOG_SOURCE_REFUSALS,
    log_source_refusal,
    plan_chunks,
    read_log_leaf,
    read_logs,
    reject_wallet_emitter,
    window_covers,
)
from guardian.security.transport_policy import Operation
from guardian.sources.alchemy import AlchemyClient
from guardian.sources.blockscout import BlockscoutClient
from guardian.sources.budget import BootstrapStop, BudgetExhausted
from guardian.sources.etherscan import EtherscanClient, HistoryRead
from guardian.sources.key_guide import credential_hint
from guardian.sources.nodereal import NodeRealClient

# Used when polling.round_deadline_seconds is omitted. See docs/mcm-live-01r.md.
DEFAULT_ROUND_DEADLINE_SECONDS = 180

_INDEXERS = frozenset({"alchemy_transfers", "blockscout", "etherscan_v2", "nodereal_transfers"})
_SOURCE_REFUSALS = frozenset({
    "alchemy_chain_not_supported",
    "bot_challenge",
    "credential_missing",
    "method_not_found",
    "nodereal_chain_not_supported",
    "plan_not_supported",
    "unauthorized",
})
_INDEXER_METHOD = {
    "alchemy_transfers": "alchemy_getAssetTransfers",
    "blockscout": "account/txlist",
    "etherscan_v2": "account/txlist",
    "nodereal_transfers": "nr_getAssetTransfers",
}
_LOG_CAPABILITIES = ("history.token", "permissions.standard")


@dataclass
class ScanContext:
    public_config: Any
    transport: Any
    database: Any
    tenant: str
    clock: Callable[[], float]
    started_at: float
    source_id: str
    network: str
    chain_id: Any
    native_asset: str
    head: int | None
    checkpoint: dict[str, Any]
    bootstrap_origin: int | None
    deadline: int | None
    events: list[Any]
    seen_transfers: set[tuple[str, ...]]
    candidates: list[Any]
    observations: dict[tuple[str, str, str], CoverageObservation]
    seen: set[tuple[str, str, str]]
    baseline: list[tuple[str, str, str]]
    explicit_cursors: list[dict[str, Any]]
    held_cursors: set[tuple[str, str, str]]
    started: str
    log_evidence: list[dict[str, Any]]
    peers: list[tuple[str, str]]
    emit: Callable[..., None]
    baseline_cb: Callable[..., None]
    advance: Callable[..., None]
    hold: Callable[..., None]
    movement_rows: Callable[..., list[dict[str, Any]]]
    indexer_rows: Callable[..., list[dict[str, Any]]]
    remember_approval: Callable[..., None]
    overlap: int = 2
    plan_cache: Any = None


@dataclass
class _Outcome:
    rows: list[dict[str, Any]] = field(default_factory=list)
    refusal: str | None = None
    partial: bool = False
    stopped: str | None = None


def read_token_logs(scan: ScanContext, stored: int | None, log_done: set[str]) -> None:
    if scan.network in log_done:
        return
    log_done.add(scan.network)
    wallets = list(scan.peers)
    capability = "history.fungible"
    if scan.head is None:
        _block_all(scan, wallets, capability, "head_unreadable")
        return

    def walk() -> None:
        _token_walk(scan, wallets, stored, capability)

    if not _split_backfill(scan, stored):
        walk()
        return
    try:
        _quick_token_window(scan, wallets, stored)
    except (BootstrapStop, BudgetExhausted):
        walk()
        return
    scan.deferred_token = walk


def _token_walk(scan: ScanContext, wallets: list, stored: int | None, capability: str) -> None:
    candidates = _log_plan(scan)
    if not candidates:
        _finish(
            scan, capability, wallets, 0, scan.head, [], [], _refused_log_reason(scan), None,
            "empty", scan.source_id,
        )
        return
    wallet_set = {address.lower() for _wallet, address in wallets}
    leaves: list[tuple[int, int]] = []
    gaps: list[str] = []
    hard: str | None = None
    stopped: str | None = None
    had_rows = False
    origin = 0
    reported = candidates[0]
    chosen = {"id": reported}

    def on_leaf(start: int, end: int, logs: list[Any]) -> None:
        nonlocal had_rows
        source_id = chosen["id"]
        rows: list[dict[str, Any]] = []
        for wallet_id, address in wallets:
            rows.extend(scan.movement_rows(account_logs(logs, address), wallet_id, scan.network, source_id))
        if rows:
            had_rows = True
        _note_logs(scan, logs)
        _commit_leaf(scan, rows, capability, end, [wallet_id for wallet_id, _address in wallets])
        leaves.append((start, end))

    for index, candidate in enumerate(candidates):
        planned = _next_log_chunks(scan, index=index, candidate=candidate, stored=stored, leaves=leaves, origin=origin, per_chunk=2)
        if planned is None:
            break
        chunks, origin = planned
        chosen["id"] = candidate
        reported = candidate
        contract = _log_contract(scan, wallet_set, candidate)
        batch = _source_flag(scan, "batch_reads", candidate)
        failed = False
        for start, end in chunks:
            if _past_deadline(scan):
                stopped = "deadline"
                failed = True
                break
            try:
                read = _transfer_span(scan, wallets, start, end, contract, batch, on_leaf, candidate)
            except BudgetExhausted:
                stopped = "budget_exhausted"
                failed = True
                break
            except BootstrapStop as exc:
                stopped = exc.reason
                failed = True
                break
            if not read.ok:
                if read.gap:
                    gaps.append(read.gap)
                else:
                    hard = read.reason or "read_error"
                failed = True
                break
        if stopped in {"deadline", "bootstrap_request_cap"}:
            break
        if not failed:
            hard = None
            gaps = []
            stopped = None
            break
        if hard in LOG_SOURCE_REFUSALS:
            _remember_log_refusal(scan, candidate, hard)
        elif any(str(item).startswith("single_block_unreadable:") for item in gaps):
            break
    _finish(
        scan, capability, wallets, origin, scan.head, leaves, gaps, hard, stopped,
        "ok" if had_rows else "empty", reported,
    )


def read_approvals(
    scan: ScanContext,
    address: str,
    wallet_id: str,
    stored: int | None,
    active: dict[tuple[str, str], list[dict[str, Any]]],
    notes: list[dict[str, Any]],
) -> None:
    """One wallet's approval window. gap_detection is true only when origin..target was read."""
    if scan.head is None:
        _block_caps(scan, wallet_id, "head_unreadable")
        return
    candidates = _log_plan(scan)
    if not candidates:
        targets = scan.peers if len(scan.peers) > 1 else [(wallet_id, address)]
        _finish(
            scan, "permissions.erc20", targets, 0, scan.head, [], [], _refused_log_reason(scan), None,
            "empty", scan.source_id, permission=True,
        )
        return
    wallets = {(item[1].lower(), item[0]) for item in scan.peers}
    wallet_for = {addr: wid for addr, wid in wallets}
    leaves: list[tuple[int, int]] = []
    reported = candidates[0]
    chosen = {"id": reported}
    status = {"found": False}
    owners = [address] if len(scan.peers) <= 1 else [item[1] for item in scan.peers]
    # Followers still need their own state reads. The log query is shared when several wallets are on the chain.
    leader = not scan.peers or scan.peers[0][0] == wallet_id
    advance = {"on": True}

    def on_leaf(start: int, end: int, logs: list[Any]) -> None:
        if not leader:
            return
        source_id = chosen["id"]
        grouped: dict[str, list[dict[str, Any]]] = {item.lower(): [] for item in owners}
        for log in logs:
            owner = _topic_address(log, 1)
            if owner in grouped:
                grouped[owner].append(log)
            elif len(owners) == 1:
                grouped[owners[0].lower()].append(log)
        new_events: list[Any] = []
        before = len(scan.events)
        for owner in owners:
            wid = wallet_for.get(owner.lower(), wallet_id)
            # Allowance and balance stay on the family RPC. Only the log span moves.
            scan_rows = scan_approvals(
                scan.transport,
                scan.source_id,
                owner,
                start,
                end,
                previous_cursor=None if stored is None else str(stored),
                gap_detection_complete=False,
                prefetched_logs=tuple(grouped.get(owner.lower(), [])),
            )
            if any(item.category == "error" for item in scan_rows.coverage.values()):
                raise _LeafError("read_error")
            if scan_rows.rows:
                status["found"] = True
            scan.remember_approval(scan, wid, scan_rows, active, new_events)
        _note_logs(scan, logs)
        cursors = []
        if advance["on"]:
            for owner in owners:
                wid = wallet_for.get(owner.lower(), wallet_id)
                for capability in (
                    "permissions.erc1155_operator",
                    "permissions.erc20",
                    "permissions.erc721_operator",
                    "permissions.erc721_token",
                ):
                    cursors.extend(_advancing_cursors(scan, [wid], capability, end))
        scan.database.commit_capability_cycle(
            scan.tenant, events=scan.events[before:], capability_rows=[], cursors=cursors, alerts=[],
        )
        if advance["on"]:
            leaves.append((start, end))
        _permit_notes(scan, owners, start, end, notes, wallet_for, source_id)

    def walk() -> None:
        _approval_walk(
            scan, wallet_id, address, stored, candidates, chosen, owners, on_leaf, leaves, status,
        )

    if not leader:
        return
    # The quick windows (token, then approvals) run before either historical walk.
    if _split_backfill(scan, stored):
        advance["on"] = False
        try:
            _quick_approval_window(scan, stored, owners, on_leaf, chosen)
        except (BootstrapStop, BudgetExhausted, _LeafError):
            pass
        finally:
            advance["on"] = True
    _flush_deferred_token(scan)
    walk()


def _approval_walk(scan, wallet_id, address, stored, candidates, chosen, owners, on_leaf, leaves, status) -> None:
    gaps: list[str] = []
    hard: str | None = None
    stopped: str | None = None
    origin = 0
    reported = candidates[0]
    for index, candidate in enumerate(candidates):
        planned = _next_log_chunks(scan, index=index, candidate=candidate, stored=stored, leaves=leaves, origin=origin, per_chunk=2)
        if planned is None:
            break
        chunks, origin = planned
        chosen["id"] = candidate
        reported = candidate
        failed = False
        for start, end in chunks:
            if _past_deadline(scan):
                stopped = "deadline"
                failed = True
                break
            try:
                read = read_logs(
                    _log_transport(scan),
                    candidate,
                    lambda leaf_start, leaf_end, owners=owners: _approval_operation(owners, leaf_start, leaf_end),
                    start,
                    end,
                    wallets={item.lower() for item in owners},
                    retry_attempts=scan.transport.retry_attempts(candidate),
                    sleep=_sleep(scan, candidate),
                    clock=scan.clock,
                    on_leaf=on_leaf,
                )
            except BudgetExhausted:
                stopped = "budget_exhausted"
                failed = True
                break
            except BootstrapStop as exc:
                stopped = exc.reason
                failed = True
                break
            except _LeafError as exc:
                hard = exc.reason
                failed = True
                break
            if not read.ok:
                if read.gap:
                    gaps.append(read.gap)
                else:
                    hard = read.reason or "read_error"
                failed = True
                break
        if stopped in {"deadline", "bootstrap_request_cap"}:
            break
        if not failed:
            hard = None
            gaps = []
            stopped = None
            break
        if hard in LOG_SOURCE_REFUSALS:
            _remember_log_refusal(scan, candidate, hard)
        elif any(str(item).startswith("single_block_unreadable:") for item in gaps):
            break
    targets = scan.peers if len(scan.peers) > 1 else [(wallet_id, address)]
    _finish(
        scan, "permissions.erc20", targets, origin, scan.head, leaves, gaps, hard, stopped,
        "ok" if status["found"] else "empty", reported, permission=True,
    )


def history_external(scan: ScanContext, private_inputs: Any, wallet_id: str, address: str, stored: int | None, plan_cache: Any) -> list[dict[str, Any]]:
    ids = _route_ids(scan.public_config, scan.network, "history.native_external")
    if not ids:
        scan.observations[(wallet_id, scan.network, "history.native_external")] = _blocked("endpoint_missing")
        return []
    refusal: str | None = None
    refusal_adapter: str | None = None
    refusal_env: str | None = None
    for source_id in ids:
        source = scan.public_config.document["sources"].get(source_id, {})
        adapter = source.get("adapter")
        if adapter not in _INDEXERS:
            continue
        if source.get("enabled") is not True:
            continue
        if not _endpoint_ready(scan, source_id):
            continue
        cached = None if plan_cache is None else plan_cache.reason(source_id, scan.network)
        if cached == "plan_not_supported":
            refusal = cached
            refusal_adapter = str(adapter)
            continue
        status = private_inputs.source_status.get(source_id)
        credential_name = source.get("credential_env") if isinstance(source.get("credential_env"), str) else None
        missing_key = credential_name is not None and (status is None or not status.credential_present)
        # PRO without a key still calls once so a 402 payment response becomes credential_missing,
        # then the keyless instance is next. Other keyed sources do not call.
        pro_without_key = missing_key and adapter == "blockscout" and credential_name == "BLOCKSCOUT_API_KEY"
        if missing_key and not pro_without_key:
            if refusal not in {"bot_challenge", "plan_not_supported"}:
                refusal = "credential_missing"
                refusal_adapter = str(adapter)
                refusal_env = credential_name
            continue
        if scan.head is None:
            refusal = "head_unreadable"
            refusal_adapter = str(adapter)
            continue
        _record_indexer(scan, source_id, str(adapter))
        outcome = _run_indexer(scan, source_id, source, str(adapter), wallet_id, address, stored, plan_cache)
        if outcome.refusal == "plan_not_supported" and plan_cache is not None:
            plan_cache.remember(source_id, scan.network, "plan_not_supported")
        if outcome.refusal:
            refusal = outcome.refusal
            refusal_adapter = str(adapter)
            if outcome.refusal == "credential_missing":
                refusal_env = credential_name
            continue
        _note_bsc_internal(scan, wallet_id, private_inputs)
        return outcome.rows
    _refuse_history(scan, wallet_id, refusal, refusal_adapter, refusal_env)
    _note_bsc_internal(scan, wallet_id, private_inputs)
    return []


class _LeafError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _transfer_span(scan, wallets, start, end, contract, batch, on_leaf, source_id):
    """Read incoming and outgoing logs on the same ranges.

    A leaf is committed only when both directions of that exact span succeed.
    If either direction refuses the width, both are split at the same midpoint.
    """
    addresses = [address for _wallet, address in wallets]
    wallet_set = {address.lower() for address in addresses}
    transport = _log_transport(scan)
    if batch:
        incoming = _transfer_operation(start, end, addresses, 2, contract, wallet_set)
        outgoing = _transfer_operation(start, end, addresses, 1, contract, wallet_set)
        operation = Operation(
            protocol="jsonrpc",
            adapter="evm_rpc",
            batch=(
                {"method": "eth_getLogs", "params": incoming.params},
                {"method": "eth_getLogs", "params": outgoing.params},
            ),
        )
        try:
            result = transport.request(source_id, operation, credits=2)
        except (BudgetExhausted, BootstrapStop):
            raise
        except Exception:
            result = None
        logs = _result_logs(result) if result is not None else None
        if logs is not None:
            on_leaf(start, end, logs)
            from guardian.scan.window import LogRead
            return LogRead(True, "", None, end)
        body = getattr(result, "body", None) if result is not None else None
        policy = log_source_refusal(body, str(getattr(result, "reason", "") or ""), start, end)
        if policy:
            from guardian.scan.window import LogRead
            return LogRead(False, policy, None, None)
    return _paired_transfers(
        scan, transport, source_id, addresses, start, end, contract, wallet_set, on_leaf,
    )


def _paired_transfers(scan, transport, source_id, addresses, start, end, contract, wallet_set, on_leaf):
    """Bisect both directions together. Commit only a span both sides have read."""
    from guardian.scan.window import LogRead

    if end < start:
        return LogRead(False, "bad_range", None, None)
    incoming = _one_direction(scan, transport, source_id, addresses, start, end, 2, contract, wallet_set)
    if incoming[0].reason in LOG_SOURCE_REFUSALS:
        return LogRead(False, incoming[0].reason, None, None)
    outgoing = _one_direction(scan, transport, source_id, addresses, start, end, 1, contract, wallet_set)
    if outgoing[0].reason in LOG_SOURCE_REFUSALS:
        return LogRead(False, outgoing[0].reason, None, None)
    if incoming[0].ok and outgoing[0].ok:
        on_leaf(start, end, _dedupe_logs([*incoming[1], *outgoing[1]]))
        return LogRead(True, "", None, end)
    gap = incoming[0].gap or outgoing[0].gap
    if start == end:
        if gap is None:
            gap = f"single_block_unreadable:{start}"
        reason = incoming[0].reason or outgoing[0].reason or "read_error"
        return LogRead(False, reason, gap, None)
    if gap:
        return LogRead(False, incoming[0].reason or outgoing[0].reason or "read_error", gap, None)
    hard_in = not incoming[0].ok and not incoming[0].refused_range
    hard_out = not outgoing[0].ok and not outgoing[0].refused_range
    if hard_in or hard_out:
        reason = incoming[0].reason if hard_in and incoming[0].reason else outgoing[0].reason
        return LogRead(False, reason or "read_error", None, None)
    if (incoming[0].refused_range or outgoing[0].refused_range) and end > start:
        mid = start + (end - start) // 2
        left = _paired_transfers(
            scan, transport, source_id, addresses, start, mid, contract, wallet_set, on_leaf,
        )
        if not left.ok:
            return left
        return _paired_transfers(
            scan, transport, source_id, addresses, mid + 1, end, contract, wallet_set, on_leaf,
        )
    return LogRead(False, incoming[0].reason or outgoing[0].reason or "read_error", None, None)


def _one_direction(scan, transport, source_id, addresses, start, end, index, contract, wallet_set):
    return read_log_leaf(
        transport,
        source_id,
        lambda leaf_start, leaf_end, index=index: _transfer_operation(
            leaf_start, leaf_end, addresses, index, contract, wallet_set,
        ),
        start,
        end,
        wallets=wallet_set,
        retry_attempts=scan.transport.retry_attempts(source_id),
        sleep=_sleep(scan, source_id),
        clock=scan.clock,
    )


def _transfer_operation(start, end, addresses, index, contract, wallets):
    pads = [pad_address(address) for address in addresses]
    topic = pads[0] if len(pads) == 1 else pads
    topics = [TRANSFER_TOPIC, None, None]
    topics[index] = topic
    payload: dict[str, Any] = {"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}
    if contract:
        payload["address"] = contract
    operation = evm_rpc("eth_getLogs", [payload])
    reject_wallet_emitter(operation, wallets)
    return operation


def _approval_operation(owners: list[str], start: int, end: int):
    if len(owners) == 1:
        return approval_log_operation(owners[0], start, end)
    pads = [pad_address(owner) for owner in owners]
    payload = {
        "fromBlock": hex(start),
        "toBlock": hex(end),
        "topics": [list(APPROVAL_LOG_TOPICS), pads],
    }
    operation = evm_rpc("eth_getLogs", [payload])
    reject_wallet_emitter(operation, {owner.lower() for owner in owners})
    return operation


def _run_indexer(scan, source_id, source, adapter, wallet_id, address, stored, plan_cache) -> _Outcome:
    page_size = _source_int(scan, "page_size", 1000, source_id=source_id)
    chunks, origin = _indexer_chunks(scan, source, stored)
    limited = _LimitedTransport(scan.transport, scan, source_id)
    if adapter == "etherscan_v2":
        client = _etherscan(scan, source_id, source, limited)
        native = _walk_pages(scan, chunks, origin, wallet_id, "history.native_external", source_id, True, lambda start, end: _safe_history(client, scan.chain_id, "txlist", address, start, end, stored))
        _remember_plan(plan_cache, source_id, scan.network, native.refusal)
        if native.refusal or _indexer_stopped(native) or _plan_cached(plan_cache, source_id, scan.network):
            return native
        token = _walk_pages(scan, chunks, origin, wallet_id, "history.fungible", source_id, False, lambda start, end: _safe_history(client, scan.chain_id, "tokentx", address, start, end, stored), cursor=False)
        _remember_plan(plan_cache, source_id, scan.network, token.refusal)
        native.rows.extend(token.rows)
        if token.stopped:
            native.stopped = token.stopped
            native.partial = True
        return native
    if adapter == "nodereal_transfers":
        client = NodeRealClient(limited, source_id, network=scan.network, clock=scan.clock, sleep=_sleep(scan, source_id), random=lambda: 0.0)
        native = _walk_pages(scan, chunks, origin, wallet_id, "history.native_external", source_id, True, lambda start, end: _nodereal_sides(client, "external", address, start, end, stored))
        if native.refusal:
            _walk_bsc_internal(scan, client, source, source_id, wallet_id, address)
            return native
        if _indexer_stopped(native):
            _walk_bsc_internal(scan, client, source, source_id, wallet_id, address)
            return native
        token = _walk_pages(scan, chunks, origin, wallet_id, "history.fungible", source_id, False, lambda start, end: _nodereal_sides(client, "20", address, start, end, stored), cursor=False)
        _walk_bsc_internal(scan, client, source, source_id, wallet_id, address)
        native.rows.extend(token.rows)
        if token.refusal and token.refusal not in {None}:
            native.partial = True
        if token.stopped:
            native.stopped = token.stopped
            native.partial = True
        return native
    if adapter == "blockscout":
        client = _blockscout(scan, source_id, source, limited)
        pro = _blockscout_pro(source)
        chain_id = scan.chain_id if pro else None
        native = _walk_pages(scan, chunks, origin, wallet_id, "history.native_external", source_id, True, lambda start, end: _safe_blockscout(client, "txlist", address, start, end, page_size, stored, chain_id, pro))
        _remember_plan(plan_cache, source_id, scan.network, native.refusal)
        if native.refusal or _indexer_stopped(native) or _plan_cached(plan_cache, source_id, scan.network):
            return native
        token = _walk_pages(scan, chunks, origin, wallet_id, "history.fungible", source_id, False, lambda start, end: _safe_blockscout(client, "tokentx", address, start, end, page_size, stored, chain_id, pro), cursor=False)
        _remember_plan(plan_cache, source_id, scan.network, token.refusal)
        native.rows.extend(token.rows)
        if token.stopped:
            native.stopped = token.stopped
            native.partial = True
        return native
    if adapter == "alchemy_transfers":
        return _run_alchemy(scan, source_id, wallet_id, address, stored, chunks, origin, limited)
    return _Outcome(refusal="unsupported_source")


def _run_alchemy(scan, source_id, wallet_id, address, stored, chunks, origin, transport=None) -> _Outcome:
    """External, token, and NFT transfers. Only native history is marked finished.

    Token and NFT rows are events. Approval coverage stays with the log scan.
    """
    client = AlchemyClient(
        transport or scan.transport,
        source_id,
        network=scan.network,
        clock=scan.clock,
        sleep=_sleep(scan, source_id),
        random=lambda: 0.0,
    )
    leaves: list[tuple[int, int]] = []
    rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    hard: str | None = None
    stopped: str | None = None
    native_count = 0
    owner = _owner_for(scan, wallet_id)
    for start, end in chunks:
        if _past_deadline(scan):
            stopped = "deadline"
            break
        try:
            read = _alchemy_window(client, address, start, end, stored)
        except BootstrapStop as exc:
            stopped = exc.reason
            break
        except BudgetExhausted:
            stopped = "budget_exhausted"
            break
        except Exception:
            hard = "read_error"
            break
        if read is None:
            hard = "read_error"
            break
        if read.reason in {"bootstrap_request_cap", "deadline"} and not read.pagination_complete:
            stopped = read.reason
            safe_end, kept = _indexed_prefix(read.items, start)
            if safe_end is not None:
                external, tokens, nfts = _split_alchemy(kept)
                native_rows = scan.indexer_rows(external, owner, wallet_id, scan.network, scan.native_asset, True, source_id)
                token_rows = scan.indexer_rows(tokens, owner, wallet_id, scan.network, scan.native_asset, False, source_id)
                nft_rows = scan.indexer_rows(nfts, owner, wallet_id, scan.network, scan.native_asset, False, source_id)
                for row in nft_rows:
                    row["capability"] = "history.nft"
                    row["is_native"] = False
                native_count += len(native_rows)
                built = [*native_rows, *token_rows, *nft_rows]
                rows.extend(built)
                _commit_leaf(scan, built, "history.native_external", safe_end, [wallet_id])
                leaves.append((start, safe_end))
            break
        if getattr(read, "blocked", False) and read.reason in _SOURCE_REFUSALS:
            if leaves:
                hard = read.reason
                break
            return _Outcome(refusal=read.reason)
        if read.category == "error" or not read.pagination_complete:
            if start == end:
                gaps.append(f"single_block_unreadable:{start}")
            else:
                hard = read.reason or "read_error"
            break
        external, tokens, nfts = _split_alchemy(read.items)
        native_rows = scan.indexer_rows(external, owner, wallet_id, scan.network, scan.native_asset, True, source_id)
        token_rows = scan.indexer_rows(tokens, owner, wallet_id, scan.network, scan.native_asset, False, source_id)
        nft_rows = scan.indexer_rows(nfts, owner, wallet_id, scan.network, scan.native_asset, False, source_id)
        for row in nft_rows:
            row["capability"] = "history.nft"
            row["is_native"] = False
        native_count += len(native_rows)
        built = [*native_rows, *token_rows, *nft_rows]
        rows.extend(built)
        _commit_leaf(scan, built, "history.native_external", end, [wallet_id])
        leaves.append((start, end))
    _finish(
        scan,
        "history.native_external",
        [(wallet_id, owner)],
        origin,
        int(scan.head),
        leaves,
        gaps,
        hard,
        stopped,
        "ok" if native_count else "empty",
        source_id,
    )
    return _Outcome(rows=rows, partial=bool(hard or gaps or stopped), stopped=stopped)


def _alchemy_window(client: AlchemyClient, address: str, start: int, end: int, stored: int | None) -> HistoryRead:
    items: list[Any] = []
    last: HistoryRead | None = None
    for side in ("from", "to"):
        read = client.transfers(
            address=address,
            start_block=start,
            end_block=end,
            side=side,
            previous_cursor=None if stored is None else str(stored),
        )
        last = read
        if read.blocked or read.category == "error" or read.reason in {"bootstrap_request_cap", "deadline"} or not read.pagination_complete:
            return read
        items.extend(read.items)
    if last is None:
        return HistoryRead("history.native_external", (), False, False, (), None, "error", False, "read_error", ("read_error",), "")
    name = "empty" if not items else "ok"
    return HistoryRead("history.native_external", tuple(items), True, True, (), str(end), name, False, "", (), "")


def _split_alchemy(items) -> tuple[list[Any], list[Any], list[Any]]:
    external: list[Any] = []
    tokens: list[Any] = []
    nfts: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if category == "external":
            external.append(item)
        elif category == "erc20":
            tokens.append(item)
        elif category in {"erc721", "erc1155"}:
            nfts.append(item)
    return external, tokens, nfts


def _walk_pages(scan, chunks, origin, wallet_id, capability, source_id, native, fetch, cursor=True) -> _Outcome:
    leaves: list[tuple[int, int]] = []
    rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    hard: str | None = None
    stopped: str | None = None
    for start, end in chunks:
        if _past_deadline(scan):
            stopped = "deadline"
            break
        try:
            read = fetch(start, end)
        except BootstrapStop as exc:
            stopped = exc.reason
            break
        except BudgetExhausted:
            stopped = "budget_exhausted"
            break
        except Exception:
            hard = "read_error"
            break
        if read is None:
            hard = "read_error"
            break
        if getattr(read, "blocked", False) and read.reason in _SOURCE_REFUSALS:
            if leaves:
                hard = read.reason
                break
            return _Outcome(refusal=read.reason)
        if read.reason in {"bootstrap_request_cap", "deadline"} and not read.pagination_complete:
            stopped = read.reason
            safe_end, kept = _indexed_prefix(read.items, start)
            if safe_end is not None:
                built = scan.indexer_rows(kept, _owner_for(scan, wallet_id), wallet_id, scan.network, scan.native_asset, native, source_id)
                rows.extend(built)
                if cursor:
                    _commit_leaf(scan, built, capability, safe_end, [wallet_id])
                leaves.append((start, safe_end))
            break
        if read.category == "error" or not read.pagination_complete:
            if start == end:
                gaps.append(f"single_block_unreadable:{start}")
            else:
                hard = read.reason or "read_error"
            break
        built = scan.indexer_rows(read.items, _owner_for(scan, wallet_id), wallet_id, scan.network, scan.native_asset, native, source_id)
        rows.extend(built)
        if cursor:
            _commit_leaf(scan, built, capability, end, [wallet_id])
        else:
            before = len(scan.events)
            scan.emit(built, scan.candidates, scan.events, scan.seen_transfers, scan.tenant, scan.chain_id, scan.started, scan.head, source_id)
            if scan.events[before:]:
                scan.database.commit_capability_cycle(
                    scan.tenant, events=scan.events[before:], capability_rows=[], cursors=[], alerts=[],
                )
        leaves.append((start, end))
    if cursor:
        _finish(scan, capability, [(wallet_id, _owner_for(scan, wallet_id))], origin, int(scan.head), leaves, gaps, hard, stopped, "ok" if rows else "empty", source_id)
    return _Outcome(rows=rows, partial=bool(hard or gaps or stopped), stopped=stopped)


def _nodereal_sides(client, category, address, start, end, stored):
    items = []
    last = None
    for side in ("from", "to"):
        read = client.transfers(
            category=category, address=address, start_block=start, end_block=end, side=side,
            previous_cursor=None if stored is None else str(stored),
        )
        if read.blocked or read.category == "error" or read.reason in {"bootstrap_request_cap", "deadline"}:
            return read
        items.extend(read.items)
        last = read
    if last is None:
        return None
    from guardian.sources.etherscan import HistoryRead
    category_name = "empty" if not items else "ok"
    return HistoryRead(last.capability, tuple(items), True, True, (), str(end), category_name, False, "", (), "")


def _walk_bsc_internal(scan, client, source, source_id, wallet_id, address) -> None:
    """NodeReal internal history on BSC only, with its own cursor."""
    if scan.network != "bsc-mainnet" or scan.head is None:
        return
    from guardian.balances.internal import normalize_internal_batch, status_for_refusal
    from guardian.sources.etherscan import HistoryRead

    stored = _stored_block(scan, wallet_id, "history.native_internal")
    # The cursor is new. Do not emit a baseline_summary for the first internal page.
    scan.seen.add((wallet_id, scan.network, "history.native_internal"))
    chunks, origin = _indexer_chunks(scan, source, stored)
    chain_id = scan.chain_id if isinstance(scan.chain_id, int) else None

    def fetch(start: int, end: int):
        read = _nodereal_sides(client, "internal", address, start, end, None)
        if read is None or getattr(read, "blocked", False) or read.category == "error" or not read.pagination_complete:
            return read
        normalized = normalize_internal_batch(read.items, chain_id=chain_id, owner=address)
        if normalized.gap:
            return HistoryRead(
                "history.native_internal",
                normalized.rows,
                False,
                False,
                (normalized.gap,),
                None,
                "partial",
                False,
                normalized.gap,
                (normalized.gap,),
                "",
            )
        name = "empty" if not normalized.rows else "ok"
        return HistoryRead("history.native_internal", normalized.rows, True, True, (), str(end), name, False, "", (), "")

    outcome = _walk_pages(
        scan, chunks, origin, wallet_id, "history.native_internal", source_id, True, fetch, cursor=True,
    )
    key = (wallet_id, scan.network, "history.native_internal")
    if key in scan.observations:
        return
    _set_internal_status(scan, wallet_id, status_for_refusal(outcome.refusal or outcome.stopped or "read_error"))


def _note_bsc_internal(scan, wallet_id: str, private_inputs: Any) -> None:
    if scan.network != "bsc-mainnet":
        return
    key = (wallet_id, scan.network, "history.native_internal")
    if key in scan.observations:
        return
    chosen = None
    for source_id in _route_ids(scan.public_config, scan.network, "history.native_external"):
        source = scan.public_config.document["sources"].get(source_id, {})
        if source.get("adapter") == "nodereal_transfers" and source.get("enabled") is True:
            chosen = (source_id, source)
            break
    if chosen is None or not _endpoint_ready(scan, chosen[0]):
        _set_internal_status(scan, wallet_id, "not_queried")
        return
    source_id, source = chosen
    status = private_inputs.source_status.get(source_id)
    credential_name = source.get("credential_env") if isinstance(source.get("credential_env"), str) else None
    missing = credential_name is not None and (status is None or not getattr(status, "credential_present", False))
    if missing:
        _set_internal_status(scan, wallet_id, "credential_missing", credential_hint(credential_name))
        return
    _set_internal_status(scan, wallet_id, "read_error")


def _set_internal_status(scan, wallet_id: str, status: str, hint: str = "") -> None:
    if scan.network != "bsc-mainnet":
        return
    key = (wallet_id, scan.network, "history.native_internal")
    if key in scan.observations:
        return
    scan.observations[key] = _blocked(status, hint)
    scan.hold(scan.held_cursors, wallet_id, scan.network, "history.native_internal")


def _stored_block(scan, wallet_id: str, capability: str) -> int | None:
    try:
        rows = scan.database.capability_cursors_for(scan.tenant, wallet_id=wallet_id, network=scan.network)
    except Exception:
        return None
    for row in rows:
        if row.get("capability") != capability:
            continue
        text = row.get("block_number") or row.get("cursor")
        if isinstance(text, int) and not isinstance(text, bool):
            return text
        if isinstance(text, str) and text.isdigit():
            return int(text)
    return None


def _safe_history(client, chain_id, action, address, start, end, stored):
    if not isinstance(chain_id, int):
        return None
    return client.account_history(
        chain_id=chain_id, action=action, address=address, start_block=start, end_block=end,
        previous_cursor=None if stored is None else str(stored), request_cap=8,
    )


def _safe_blockscout(client, action, address, start, end, page_size, stored, chain_id, pro: bool):
    return client.account_history(
        action=action, address=address, start_block=start, end_block=end, page_size=page_size,
        previous_cursor=None if stored is None else str(stored),
        chain_id=chain_id if isinstance(chain_id, int) and not isinstance(chain_id, bool) else None,
        pro=pro,
    )


def _blockscout_pro(source) -> bool:
    return source.get("credential_env") == "BLOCKSCOUT_API_KEY"


def _etherscan(scan, source_id, source, transport=None) -> EtherscanClient:
    name = source.get("credential_env") if isinstance(source.get("credential_env"), str) else "ETHERSCAN_API_KEY"
    secret = _secret(scan, name)
    return EtherscanClient(
        transport or scan.transport, source_id, credential=secret, credential_env=name, clock=scan.clock, sleep=_sleep(scan, source_id),
    )


def _blockscout(scan, source_id, source, transport=None) -> BlockscoutClient:
    name = source.get("credential_env") if isinstance(source.get("credential_env"), str) else ""
    secret = _secret(scan, name) if name else ""
    return BlockscoutClient(
        transport or scan.transport, source_id, clock=scan.clock, sleep=_sleep(scan, source_id), random=lambda: 0.0, credential=secret,
    )


def _secret(scan, name: str) -> str:
    """Profile, then process environment. The same resolver doctor uses. A 401 does not switch."""
    import os

    from guardian.credentials import reveal_for_profile_env

    env_name = scan.public_config.document["private_inputs"]["secret_profile_file_env"]
    return reveal_for_profile_env(name, os.environ, env_name)


def _finish(scan, capability, wallets, origin, target, leaves, gaps, hard, stopped, category, source_id, permission=False) -> None:
    covered = leaves[-1][1] if leaves else None
    complete = (
        hard is None
        and stopped is None
        and not gaps
        and window_covers(leaves, origin, target, covered, gaps)
    )
    capabilities = (
        "permissions.erc1155_operator",
        "permissions.erc20",
        "permissions.erc721_operator",
        "permissions.erc721_token",
    ) if permission else (capability,)
    for wallet_id, _address in wallets:
        for name in capabilities:
            if hard and not leaves:
                scan.observations[(wallet_id, scan.network, name)] = _blocked(hard)
                scan.hold(scan.held_cursors, wallet_id, scan.network, name)
                continue
            if complete:
                scan.observations[(wallet_id, scan.network, name)] = _window(category, origin, target, source_id, True, scan.checkpoint)
                scan.baseline_cb(wallet_id, scan.network, name, scan.seen, scan.baseline, scan.candidates, scan.tenant)
                scan.advance(scan.explicit_cursors, scan.held_cursors, wallet_id, scan.network, name, target)
                continue
            extra = list(gaps)
            if stopped:
                extra.append(stopped)
            if stopped in {"bootstrap_request_cap", "deadline"}:
                extra.append("historical_backfill_pending")
            if not extra:
                extra.append("unread_gap")
            scan.observations[(wallet_id, scan.network, name)] = _window("partial", origin, target, source_id, False, scan.checkpoint, tuple(dict.fromkeys(extra)))
            if leaves:
                end = leaves[-1][1]
                known = _known_cursor_block(scan, wallet_id, name)
                if known is not None and end < known:
                    scan.hold(scan.held_cursors, wallet_id, scan.network, name)
                else:
                    scan.advance(scan.explicit_cursors, scan.held_cursors, wallet_id, scan.network, name, end)
                    _note_cursor_block(scan, wallet_id, name, end)
            else:
                scan.hold(scan.held_cursors, wallet_id, scan.network, name)


def _window(category, origin, target, source_id, complete, checkpoint, gaps: tuple[str, ...] = ()) -> CoverageObservation:
    reasons: list[str] = []
    if complete:
        reasons.append("incremental_window_complete")
        if origin > 0:
            reasons.append("pre_origin_history_unknown")
        observed = category if category in {"ok", "empty"} else "ok"
    else:
        observed = "partial"
        reasons.extend(gaps)
        if "unread_gap" not in reasons:
            reasons.append("unread_gap")
    digest = checkpoint.get("hash") if isinstance(checkpoint.get("hash"), str) else None
    number = checkpoint.get("height")
    return CoverageObservation(
        category=observed,
        pagination_complete=complete,
        gap_detection_complete=complete,
        gaps=() if complete else tuple(dict.fromkeys(gaps or ("unread_gap",))),
        cursor=str(target) if complete and target is not None else None,
        declared="block_range",
        scope_from=str(origin),
        scope_to=None if target is None else str(target),
        sources=(source_id,),
        trust="single_provider" if complete else "unverified",
        reason_codes=tuple(dict.fromkeys(reasons)),
        checkpoint_number=None if not isinstance(number, int) else str(number),
        checkpoint_hash=digest,
        checkpoint_status="observed" if isinstance(number, int) else "unknown",
        price_quality="not_applicable",
    )


def _commit_leaf(scan, rows, capability, end, wallet_ids) -> None:
    before = len(scan.events)
    scan.emit(rows, scan.candidates, scan.events, scan.seen_transfers, scan.tenant, scan.chain_id, scan.started, scan.head, scan.source_id)
    cursors = _advancing_cursors(scan, wallet_ids, capability, end)
    scan.database.commit_capability_cycle(
        scan.tenant, events=scan.events[before:], capability_rows=[], cursors=cursors, alerts=[],
    )


def _advancing_cursors(scan, wallet_ids, capability: str, end: int) -> list[dict[str, str]]:
    """Cursor rows that move forward. An overlap re-read must not walk the cursor backward."""
    rows = []
    for wallet_id in wallet_ids:
        known = _known_cursor_block(scan, wallet_id, capability)
        if known is not None and end < known:
            continue
        rows.append(_cursor(wallet_id, scan.network, capability, end))
        _note_cursor_block(scan, wallet_id, capability, end)
    return rows


def _known_cursor_block(scan, wallet_id: str, capability: str) -> int | None:
    book = _cursor_book(scan)
    key = (wallet_id, capability)
    if key not in book:
        book[key] = _load_cursor_block(scan, wallet_id, capability)
    value = book[key]
    return value if isinstance(value, int) else None


def _note_cursor_block(scan, wallet_id: str, capability: str, end: int) -> None:
    book = _cursor_book(scan)
    key = (wallet_id, capability)
    current = book.get(key)
    if not isinstance(current, int) or end > current:
        book[key] = end


def _cursor_book(scan) -> dict:
    book = getattr(scan, "_cursor_high", None)
    if book is None:
        book = {}
        scan._cursor_high = book
    return book


def _load_cursor_block(scan, wallet_id: str, capability: str) -> int | None:
    rows = scan.database.capability_cursors_for(scan.tenant, wallet_id=wallet_id, network=scan.network)
    for row in rows:
        if row.get("capability") != capability:
            continue
        text = row.get("block_number")
        if text is None:
            text = row.get("cursor")
        try:
            return int(text)
        except (TypeError, ValueError):
            return None
    return None


def _cursor(wallet_id: str, network: str, capability: str, block: int) -> dict[str, str]:
    return {
        "block_number": str(block),
        "capability": capability,
        "cursor": str(block),
        "network": network,
        "wallet_id": wallet_id,
    }


def _record_indexer(scan, source_id: str, adapter: str) -> None:
    """Record provider, method, and consent before an address is sent to an indexer."""
    book = getattr(scan.transport, "indexer_disclosures", None)
    if not isinstance(book, list):
        return
    privacy = scan.public_config.document.get("privacy")
    consent = privacy.get("third_party_wallet_queries") if isinstance(privacy, dict) else None
    book.append({
        "adapter": adapter,
        "consent": consent if isinstance(consent, str) else "deny_until_local_consent",
        "method": _INDEXER_METHOD.get(adapter, adapter),
        "purpose": "history.native_external",
        "source_id": source_id,
    })


def _refuse_history(scan, wallet_id, refusal, adapter: str | None = None, credential_env: str | None = None) -> None:
    reason = refusal or "unsupported_source"
    hint = credential_hint(credential_env) if reason == "credential_missing" and credential_env else ""
    if reason == "credential_missing" and adapter == "blockscout":
        scan.observations[(wallet_id, scan.network, "history.native_external")] = CoverageObservation(
            category="partial",
            pagination_complete=False,
            gap_detection_complete=False,
            declared="not_read",
            reason_codes=("credential_missing", "blocked"),
            trust="unverified",
            price_quality="not_applicable",
            credential_hint=hint,
        )
        scan.hold(scan.held_cursors, wallet_id, scan.network, "history.native_external")
        return
    if scan.network == "avalanche-c" and reason == "plan_not_supported":
        scan.observations[(wallet_id, scan.network, "history.native_external")] = CoverageObservation(
            category="error",
            pagination_complete=False,
            gap_detection_complete=False,
            declared="expansion_pending",
            reason_codes=("expansion_pending", "blocked"),
            trust="unverified",
            price_quality="not_applicable",
        )
    else:
        scan.observations[(wallet_id, scan.network, "history.native_external")] = _blocked(reason, hint)
    scan.hold(scan.held_cursors, wallet_id, scan.network, "history.native_external")


def _block_all(scan, wallets, capability, reason) -> None:
    for wallet_id, _address in wallets:
        scan.observations[(wallet_id, scan.network, capability)] = _blocked(reason)
        scan.hold(scan.held_cursors, wallet_id, scan.network, capability)


def _block_caps(scan, wallet_id, reason) -> None:
    for capability in (
        "permissions.erc1155_operator",
        "permissions.erc20",
        "permissions.erc721_operator",
        "permissions.erc721_token",
    ):
        scan.observations[(wallet_id, scan.network, capability)] = _blocked(reason)
        scan.hold(scan.held_cursors, wallet_id, scan.network, capability)


def _blocked(reason: str, hint: str = "") -> CoverageObservation:
    return CoverageObservation(
        category="error",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="not_read",
        reason_codes=(reason, "blocked"),
        trust="unverified",
        price_quality="unknown",
        credential_hint=hint,
    )


def _endpoint_ready(scan, source_id: str) -> bool:
    registry = getattr(scan.transport, "endpoints", None)
    if registry is None:
        return False
    try:
        registered = registry.get(source_id)
    except Exception:
        return False
    return bool(getattr(registered, "endpoint", ""))


def _note_logs(scan, logs) -> None:
    height = scan.checkpoint.get("height")
    stamp = scan.checkpoint.get("timestamp")
    for log in logs:
        if not isinstance(log, dict):
            continue
        block = _int(log.get("blockNumber"))
        digest = log.get("blockHash") if isinstance(log.get("blockHash"), str) else None
        tx = log.get("transactionHash") if isinstance(log.get("transactionHash"), str) else None
        moment = log.get("timestamp") if isinstance(log.get("timestamp"), (int, str)) and not isinstance(log.get("timestamp"), bool) else None
        if moment is None and block is not None and block == height:
            moment = stamp
        if block is None and digest is None and moment is None:
            continue
        scan.log_evidence.append({
            "block": block,
            "hash": digest,
            "time": moment,
            "tx": tx.lower() if isinstance(tx, str) else None,
        })


def _permit_notes(scan, owners, start, end, notes, wallet_for, source_id: str | None = None) -> None:
    label = source_id or scan.source_id
    for owner in owners:
        try:
            operation = evm_rpc("eth_getLogs", [{
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "topics": [PERMIT2_PERMIT_TOPIC, pad_address(owner)],
            }])
            reject_wallet_emitter(operation, {item.lower() for item in owners})
            result = _log_transport(scan).request(label, operation)
        except (BudgetExhausted, BootstrapStop):
            raise
        except Exception:
            continue
        body = getattr(result, "body", None)
        rows = body.get("result") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            continue
        for log in rows:
            if not isinstance(log, dict):
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or not topics or str(topics[0]).lower() != PERMIT2_PERMIT_TOPIC.lower():
                continue
            tx = str(log.get("transactionHash") or "")
            notes.append({
                "kind": "relay_permit",
                "network": scan.network,
                "not_current_allowance": True,
                "tx": tx.lower(),
                "wallet_id": wallet_for.get(owner.lower(), ""),
            })


def _topic_address(log: dict, index: int) -> str:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) <= index or not isinstance(topics[index], str):
        return ""
    topic = topics[index].lower()
    if topic.startswith("0x") and len(topic) >= 42:
        return "0x" + topic[-40:]
    return ""


def _result_logs(result) -> list[Any] | None:
    if result is None or not getattr(result, "ok", False):
        return None
    body = getattr(result, "body", None)
    if isinstance(body, list):
        merged = []
        for item in body:
            if not isinstance(item, dict) or "error" in item or not isinstance(item.get("result"), list):
                return None
            merged.extend(item["result"])
        return merged
    if isinstance(body, dict) and isinstance(body.get("result"), list):
        return list(body["result"])
    return None


def _dedupe_logs(logs: list[Any]) -> list[Any]:
    found = []
    seen = set()
    for log in logs:
        if not isinstance(log, dict):
            continue
        key = (str(log.get("transactionHash")), str(log.get("logIndex")))
        if key in seen:
            continue
        seen.add(key)
        found.append(log)
    return found


def _round_deadline(scan) -> int:
    value = scan.deadline
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_ROUND_DEADLINE_SECONDS


def _remaining_seconds(scan) -> float:
    return _round_deadline(scan) - (scan.clock() - scan.started_at)


def _past_deadline(scan) -> bool:
    if getattr(scan, "deadline_hit", False):
        return True
    return _remaining_seconds(scan) <= 0


def _sleeper_for(scan):
    backend = getattr(scan.transport, "backend", None)
    sleeper = getattr(backend, "sleeper", None)
    if sleeper is None:
        sleeper = getattr(scan.transport, "sleeper", None)
    if sleeper is None:
        import time
        return time.sleep
    return sleeper


def _note_wait(scan, source_id: str, planned: float, actual: float) -> None:
    ledger = getattr(scan.transport, "provenance", None)
    if ledger is None or not hasattr(ledger, "note_backoff"):
        return
    ledger.note_backoff(source_id, actual)
    book = getattr(ledger, "backoff", None)
    if isinstance(book, list) and book and isinstance(book[-1], dict):
        book[-1]["planned_wait_seconds"] = planned
        book[-1]["actual_wait_seconds"] = actual


def _sleep(scan, source_id: str | None = None):
    """Wait for real. A wait that would pass the round deadline is not taken."""
    label = source_id or scan.source_id

    def pause(seconds: float) -> None:
        try:
            planned = float(seconds)
        except (TypeError, ValueError):
            planned = 0.0
        if planned < 0:
            planned = 0.0
        remaining = _remaining_seconds(scan)
        if planned > remaining:
            _note_wait(scan, label, planned, 0.0)
            scan.deadline_hit = True
            raise BootstrapStop("deadline")
        _note_wait(scan, label, planned, planned)
        if planned > 0:
            _sleeper_for(scan)(planned)
    return pause


def _source_int(scan, key: str, default: int, source_id: str | None = None) -> int:
    source = scan.public_config.document["sources"].get(source_id or scan.source_id, {})
    value = source.get("range_limit") if key == "log_range_max" else None
    if not isinstance(value, int) or isinstance(value, bool):
        value = source.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return default


def _source_flag(scan, key: str, source_id: str | None = None) -> bool:
    source = scan.public_config.document["sources"].get(source_id or scan.source_id, {})
    return source.get(key) is True


def _log_contract(scan, wallets: set[str], source_id: str | None = None) -> str | None:
    source = scan.public_config.document["sources"].get(source_id or scan.source_id, {})
    value = source.get("log_contract")
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return None
    if value.lower() in wallets:
        return None
    return value


def _log_plan(scan) -> list[str]:
    """Enabled log routes with a ready endpoint. Otherwise the family RPC."""
    found: list[str] = []
    saw_cached = False
    for capability in _LOG_CAPABILITIES:
        for source_id in _route_ids(scan.public_config, scan.network, capability):
            if source_id in found:
                continue
            source = scan.public_config.document["sources"].get(source_id, {})
            if source.get("enabled") is not True or source.get("adapter") != "evm_rpc":
                continue
            if _cached_log_refusal(scan, source_id):
                saw_cached = True
                continue
            if not _endpoint_ready(scan, source_id):
                continue
            found.append(source_id)
    if found:
        return found
    if saw_cached or _cached_log_refusal(scan, scan.source_id):
        return []
    return [scan.source_id]


def _next_log_chunks(scan, *, index: int, candidate: str, stored: int | None, leaves, origin: int, per_chunk: int):
    """Chunks for this log source. None means the fallback budget cannot catch up."""
    resume = leaves[-1][1] if leaves else None
    if index > 0:
        start = (resume + 1) if resume is not None else origin
        if scan.head is None or not _can_catch_up(scan, candidate, start, int(scan.head), per_chunk=per_chunk):
            return None
    limit = min(_source_int(scan, "log_range_max", LOG_RANGE_MAX, source_id=candidate), LOG_RANGE_MAX)
    if index == 0:
        return plan_chunks(
            stored, scan.head, limit=limit, bootstrap_origin=scan.bootstrap_origin, overlap=scan.overlap,
        )
    if resume is None:
        chunks, _ignored = plan_chunks(None, scan.head, limit=limit, bootstrap_origin=origin, overlap=0)
    else:
        chunks, _ignored = plan_chunks(resume, scan.head, limit=limit, bootstrap_origin=None, overlap=0)
    return chunks, origin


def _can_catch_up(scan, source_id: str, start: int, end: int, *, per_chunk: int) -> bool:
    if end < start:
        return True
    limit = min(_source_int(scan, "log_range_max", LOG_RANGE_MAX, source_id=source_id), LOG_RANGE_MAX)
    chunks, _origin = plan_chunks(None, end, limit=limit, bootstrap_origin=start, overlap=0)
    needed = len(chunks) * per_chunk
    return _requests_remaining(scan, source_id) >= needed


def _requests_remaining(scan, source_id: str) -> int:
    try:
        group_name = scan.public_config.document["sources"][source_id]["budget_group"]
        group = scan.transport.budgets.group(group_name)
    except Exception:
        return 0
    cap = group.spec.max_requests_per_day
    used = group.requests_today
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        return 0
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        used = 0
    return max(0, cap - used)


def _route_ids(public_config, network: str, capability: str) -> list[str]:
    sources = public_config.document.get("sources") if isinstance(public_config.document.get("sources"), dict) else {}
    for route in public_config.document["routing"]:
        if route.get("network") == network and route.get("capability") == capability:
            ids = route.get("source_ids") or []
            found = []
            for item in ids:
                if not isinstance(item, str):
                    continue
                source = sources.get(item)
                if isinstance(source, dict) and source.get("enabled") is not True:
                    continue
                found.append(item)
            return found
    return []


def _split_backfill(scan, stored: int | None) -> bool:
    """True when the historical span is more than the one chunk that ends at head."""
    if scan.head is None:
        return False
    chunks, _origin, _candidate = _first_plan(scan, stored)
    return len(chunks) > 1


def _first_plan(scan, stored: int | None):
    plan = _log_plan(scan)
    if not plan:
        return [], 0, scan.source_id
    candidate = plan[0]
    limit = min(_source_int(scan, "log_range_max", LOG_RANGE_MAX, source_id=candidate), LOG_RANGE_MAX)
    chunks, origin = plan_chunks(
        stored,
        int(scan.head),
        limit=limit,
        bootstrap_origin=scan.bootstrap_origin,
        overlap=scan.overlap,
    )
    return chunks, origin, candidate


def _quick_token_window(scan, wallets, stored: int | None) -> None:
    """Head chunk only. Events are stored. The historical cursor stays put."""
    chunks, _origin, candidate = _first_plan(scan, stored)
    if len(chunks) < 2:
        return
    start, end = chunks[-1]
    wallet_set = {address.lower() for _wallet, address in wallets}
    contract = _log_contract(scan, wallet_set, candidate)
    batch = _source_flag(scan, "batch_reads", candidate)

    def on_leaf(_start: int, _end: int, logs: list[Any]) -> None:
        rows: list[dict[str, Any]] = []
        for wallet_id, address in wallets:
            rows.extend(scan.movement_rows(account_logs(logs, address), wallet_id, scan.network, candidate))
        _note_logs(scan, logs)
        _commit_events_only(scan, rows)

    _transfer_span(scan, wallets, start, end, contract, batch, on_leaf, candidate)


def _quick_approval_window(scan, stored, owners, on_leaf, chosen) -> None:
    chunks, _origin, candidate = _first_plan(scan, stored)
    if len(chunks) < 2:
        return
    start, end = chunks[-1]
    chosen["id"] = candidate
    read_logs(
        _log_transport(scan),
        candidate,
        lambda leaf_start, leaf_end, owners=owners: _approval_operation(owners, leaf_start, leaf_end),
        start,
        end,
        wallets={item.lower() for item in owners},
        retry_attempts=scan.transport.retry_attempts(candidate),
        sleep=_sleep(scan, candidate),
        clock=scan.clock,
        on_leaf=on_leaf,
    )


def _flush_deferred_token(scan) -> None:
    deferred = getattr(scan, "deferred_token", None)
    if deferred is None:
        return
    scan.deferred_token = None
    deferred()


def _commit_events_only(scan, rows) -> None:
    before = len(scan.events)
    scan.emit(
        rows, scan.candidates, scan.events, scan.seen_transfers, scan.tenant, scan.chain_id,
        scan.started, scan.head, scan.source_id,
    )
    if not scan.events[before:]:
        return
    scan.database.commit_capability_cycle(
        scan.tenant, events=scan.events[before:], capability_rows=[], cursors=[], alerts=[],
    )


def _log_transport(scan):
    cached = getattr(scan, "_log_cap", None)
    if cached is not None:
        return cached
    cached = _LogCapTransport(scan.transport, scan)
    scan._log_cap = cached
    return cached


class _LogCapTransport:
    """Counts eth_getLogs against the same per-source bootstrap cap as indexers.

    A JSON-RPC batch counts as one credit per log method inside it.
    """

    def __init__(self, inner, scan) -> None:
        self._inner = inner
        self._scan = scan

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def request(self, source_id: str, operation, **kwargs):
        if _counts_as_log(operation):
            reason = _reserve_bootstrap(self._scan, source_id, _log_credits(operation))
            if reason:
                raise BootstrapStop(reason)
        return self._inner.request(source_id, operation, **kwargs)


def _counts_as_log(operation) -> bool:
    if getattr(operation, "method", None) == "eth_getLogs":
        return True
    batch = getattr(operation, "batch", None)
    if not batch:
        return False
    return any(isinstance(item, dict) and item.get("method") == "eth_getLogs" for item in batch)


def _log_credits(operation) -> int:
    batch = getattr(operation, "batch", None)
    if batch:
        count = sum(1 for item in batch if isinstance(item, dict) and item.get("method") == "eth_getLogs")
        return max(1, count)
    return 1


def _reserve_bootstrap(scan, source_id: str, credits: int) -> str | None:
    if _past_deadline(scan):
        return "deadline"
    cap = getattr(scan.transport, "bootstrap_request_cap", None)
    used = getattr(scan.transport, "bootstrap_requests", None)
    if not isinstance(used, dict):
        return None
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        return None
    count = used.get(source_id, 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = 0
    if count >= cap or count + credits > cap:
        return "bootstrap_request_cap"
    used[source_id] = count + credits
    return None


class _LimitedTransport:
    """Counts indexer HTTP calls against the per-source bootstrap cap.

    The cap and the deadline are checked before the request is sent. Log reads
    keep the inner transport and are not counted here.
    """

    def __init__(self, inner, scan, source_id: str) -> None:
        self._inner = inner
        self._scan = scan
        self._source_id = source_id

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def request(self, source_id: str, operation, **kwargs):
        if _past_deadline(self._scan):
            raise BootstrapStop("deadline")
        cap = getattr(self._inner, "bootstrap_request_cap", None)
        used = getattr(self._inner, "bootstrap_requests", None)
        if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 and isinstance(used, dict):
            count = used.get(self._source_id, 0)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                count = 0
            if count >= cap:
                raise BootstrapStop("bootstrap_request_cap")
            used[self._source_id] = count + 1
        return self._inner.request(source_id, operation, **kwargs)


def _indexer_chunks(scan, source, stored):
    head = int(scan.head)
    width = _declared_span(source)
    if width is None:
        return plan_chunks(
            stored, head, limit=max(1, head), bootstrap_origin=scan.bootstrap_origin,
            overlap=scan.overlap, cap_at=None,
        )
    return plan_chunks(
        stored, head, limit=width, bootstrap_origin=scan.bootstrap_origin,
        overlap=scan.overlap, cap_at=None,
    )


def _declared_span(source) -> int | None:
    """A source that names its own range keeps that width. Others are one span."""
    if not isinstance(source, dict):
        return None
    for key in ("range_limit", "log_range_max"):
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
    return None


def _indexer_stopped(outcome: _Outcome) -> bool:
    return outcome.stopped in {"bootstrap_request_cap", "deadline"}


def _cached_log_refusal(scan, source_id: str) -> str | None:
    cache = getattr(scan, "plan_cache", None)
    if cache is None:
        return None
    found = cache.reason(source_id, scan.network)
    if found in LOG_SOURCE_REFUSALS or found == "plan_not_supported":
        return found
    return None


def _remember_log_refusal(scan, source_id: str, reason: str) -> None:
    cache = getattr(scan, "plan_cache", None)
    if cache is None or reason not in LOG_SOURCE_REFUSALS:
        return
    cache.remember(source_id, scan.network, reason)


def _refused_log_reason(scan) -> str:
    for capability in _LOG_CAPABILITIES:
        for source_id in _route_ids(scan.public_config, scan.network, capability):
            found = _cached_log_refusal(scan, source_id)
            if found:
                return found
    found = _cached_log_refusal(scan, scan.source_id)
    return found or "filter_requires_address"


def _plan_cached(plan_cache, source_id: str, network: str) -> bool:
    if plan_cache is None:
        return False
    return plan_cache.reason(source_id, network) == "plan_not_supported"


def _remember_plan(plan_cache, source_id: str, network: str, refusal: str | None) -> bool:
    if refusal != "plan_not_supported":
        return False
    if plan_cache is not None:
        plan_cache.remember(source_id, network, "plan_not_supported")
    return True


def _indexed_prefix(items, start: int) -> tuple[int | None, list[Any]]:
    """Last fully read block: the highest block strictly below the top block seen."""
    parsed: list[tuple[int, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        block = None
        for key in ("blockNumber", "blockNum", "block_number", "block"):
            block = _int(item.get(key))
            if block is not None:
                break
        if block is None or block < start:
            continue
        parsed.append((block, item))
    if not parsed:
        return None, []
    highest = max(block for block, _item in parsed)
    earlier = [block for block, _item in parsed if block < highest]
    if not earlier:
        return None, []
    safe_end = max(earlier)
    return safe_end, [item for block, item in parsed if block <= safe_end]


def _owner_for(scan, wallet_id: str) -> str:
    for wid, address in scan.peers:
        if wid == wallet_id:
            return address
    return scan.peers[0][1] if scan.peers else ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if text.startswith(("0x", "0X")):
                return int(text, 16)
            if text.isdigit():
                return int(text)
        except ValueError:
            return None
    return None
