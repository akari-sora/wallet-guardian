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
from guardian.scan.window import plan_chunks, read_logs, reject_wallet_emitter, window_covers
from guardian.security.transport_policy import Operation
from guardian.sources.blockscout import BlockscoutClient
from guardian.sources.budget import BudgetExhausted
from guardian.sources.etherscan import EtherscanClient
from guardian.sources.key_guide import credential_hint
from guardian.sources.nodereal import NodeRealClient

_INDEXERS = frozenset({"blockscout", "etherscan_v2", "nodereal_transfers"})
_SOURCE_REFUSALS = frozenset({
    "bot_challenge",
    "credential_missing",
    "method_not_found",
    "nodereal_chain_not_supported",
    "plan_not_supported",
    "unauthorized",
})
_INDEXER_METHOD = {
    "blockscout": "account/txlist",
    "etherscan_v2": "account/txlist",
    "nodereal_transfers": "nr_getAssetTransfers",
}


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


@dataclass
class _Outcome:
    rows: list[dict[str, Any]] = field(default_factory=list)
    refusal: str | None = None
    partial: bool = False


def read_token_logs(scan: ScanContext, stored: int | None, log_done: set[str]) -> None:
    if scan.network in log_done:
        return
    log_done.add(scan.network)
    wallets = list(scan.peers)
    capability = "history.fungible"
    if scan.head is None:
        _block_all(scan, wallets, capability, "head_unreadable")
        return
    limit = _source_int(scan, "log_range_max", LOG_RANGE_MAX)
    chunks, origin = plan_chunks(
        stored, scan.head, limit=min(limit, LOG_RANGE_MAX), bootstrap_origin=scan.bootstrap_origin, overlap=scan.overlap,
    )
    contract = _log_contract(scan, {address.lower() for _wallet, address in wallets})
    batch = _source_flag(scan, "batch_reads")
    leaves: list[tuple[int, int]] = []
    gaps: list[str] = []
    hard: str | None = None
    stopped: str | None = None
    had_rows = False

    def on_leaf(start: int, end: int, logs: list[Any]) -> None:
        nonlocal had_rows
        rows: list[dict[str, Any]] = []
        for wallet_id, address in wallets:
            rows.extend(scan.movement_rows(account_logs(logs, address), wallet_id, scan.network, scan.source_id))
        if rows:
            had_rows = True
        _note_logs(scan, logs)
        _commit_leaf(scan, rows, capability, end, [wallet_id for wallet_id, _address in wallets])
        leaves.append((start, end))

    for start, end in chunks:
        if _past_deadline(scan):
            stopped = "deadline"
            break
        try:
            read = _transfer_span(scan, wallets, start, end, contract, batch, on_leaf)
        except BudgetExhausted:
            stopped = "budget_exhausted"
            break
        if not read.ok:
            if read.gap:
                gaps.append(read.gap)
            else:
                hard = read.reason or "read_error"
            break
    _finish(
        scan, capability, wallets, origin, scan.head, leaves, gaps, hard, stopped,
        "ok" if had_rows else "empty", scan.source_id,
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
    limit = _source_int(scan, "log_range_max", LOG_RANGE_MAX)
    chunks, origin = plan_chunks(
        stored, scan.head, limit=min(limit, LOG_RANGE_MAX), bootstrap_origin=scan.bootstrap_origin, overlap=scan.overlap,
    )
    wallets = {(item[1].lower(), item[0]) for item in scan.peers}
    wallet_for = {addr: wid for addr, wid in wallets}
    leaves: list[tuple[int, int]] = []
    gaps: list[str] = []
    hard: str | None = None
    stopped: str | None = None
    found = False
    owners = [address] if len(scan.peers) <= 1 else [item[1] for item in scan.peers]
    # Followers still need their own state reads. The log query is shared when several wallets are on the chain.
    leader = not scan.peers or scan.peers[0][0] == wallet_id

    def on_leaf(start: int, end: int, logs: list[Any]) -> None:
        nonlocal found
        if not leader:
            return
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
                found = True
            scan.remember_approval(scan, wid, scan_rows, active, new_events)
        _note_logs(scan, logs)
        _permit_notes(scan, owners, start, end, notes, wallet_for)
        cursors = []
        for owner in owners:
            wid = wallet_for.get(owner.lower(), wallet_id)
            for capability in (
                "permissions.erc1155_operator",
                "permissions.erc20",
                "permissions.erc721_operator",
                "permissions.erc721_token",
            ):
                cursors.append(_cursor(wid, scan.network, capability, end))
        scan.database.commit_capability_cycle(
            scan.tenant, events=scan.events[before:], capability_rows=[], cursors=cursors, alerts=[],
        )
        leaves.append((start, end))

    if not leader:
        return
    for start, end in chunks:
        if _past_deadline(scan):
            stopped = "deadline"
            break
        try:
            read = read_logs(
                scan.transport,
                scan.source_id,
                lambda leaf_start, leaf_end, owners=owners: _approval_operation(owners, leaf_start, leaf_end),
                start,
                end,
                wallets={item.lower() for item in owners},
                retry_attempts=scan.transport.retry_attempts(scan.source_id),
                sleep=_sleep(scan),
                clock=scan.clock,
                on_leaf=on_leaf,
            )
        except BudgetExhausted:
            stopped = "budget_exhausted"
            break
        except _LeafError as exc:
            hard = exc.reason
            break
        if not read.ok:
            if read.gap:
                gaps.append(read.gap)
            else:
                hard = read.reason or "read_error"
            break
    targets = scan.peers if len(scan.peers) > 1 else [(wallet_id, address)]
    _finish(
        scan, "permissions.erc20", targets, origin, scan.head, leaves, gaps, hard, stopped,
        "ok" if found else "empty", scan.source_id, permission=True,
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
        return outcome.rows
    _refuse_history(scan, wallet_id, refusal, refusal_adapter, refusal_env)
    return []


class _LeafError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _transfer_span(scan, wallets, start, end, contract, batch, on_leaf):
    addresses = [address for _wallet, address in wallets]
    wallet_set = {address.lower() for address in addresses}
    incoming = _transfer_operation(start, end, addresses, 2, contract, wallet_set)
    outgoing = _transfer_operation(start, end, addresses, 1, contract, wallet_set)
    if batch:
        operation = Operation(
            protocol="jsonrpc",
            adapter="evm_rpc",
            batch=(
                {"method": "eth_getLogs", "params": incoming.params},
                {"method": "eth_getLogs", "params": outgoing.params},
            ),
        )
        try:
            result = scan.transport.request(scan.source_id, operation, credits=2)
        except BudgetExhausted:
            raise
        except Exception:
            result = None
        logs = _result_logs(result) if result is not None else None
        if logs is not None:
            on_leaf(start, end, logs)
            from guardian.scan.window import LogRead
            return LogRead(True, "", None, end)
    bag: list[tuple[int, int, list[Any]]] = []

    def stash(leaf_start: int, leaf_end: int, logs: list[Any]) -> None:
        bag.append((leaf_start, leaf_end, logs))

    first = read_logs(
        scan.transport, scan.source_id, lambda leaf_start, leaf_end: _transfer_operation(leaf_start, leaf_end, addresses, 2, contract, wallet_set),
        start, end, wallets=wallet_set, retry_attempts=scan.transport.retry_attempts(scan.source_id),
        sleep=_sleep(scan), clock=scan.clock, on_leaf=stash,
    )
    second = read_logs(
        scan.transport, scan.source_id, lambda leaf_start, leaf_end: _transfer_operation(leaf_start, leaf_end, addresses, 1, contract, wallet_set),
        start, end, wallets=wallet_set, retry_attempts=scan.transport.retry_attempts(scan.source_id),
        sleep=_sleep(scan), clock=scan.clock, on_leaf=stash,
    )
    _flush_pairs(bag, on_leaf, require_pair=True)
    if not first.ok:
        return first
    if not second.ok:
        return second
    from guardian.scan.window import LogRead
    return LogRead(True, "", None, end)


def _flush_pairs(bag, on_leaf, *, require_pair: bool) -> None:
    grouped: dict[tuple[int, int], list[Any]] = {}
    counts: dict[tuple[int, int], int] = {}
    for leaf_start, leaf_end, logs in bag:
        key = (leaf_start, leaf_end)
        grouped.setdefault(key, []).extend(logs)
        counts[key] = counts.get(key, 0) + 1
    for key in sorted(grouped):
        if require_pair and counts[key] < 2:
            continue
        on_leaf(key[0], key[1], _dedupe_logs(grouped[key]))


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
    limit = _source_int(scan, "log_range_max", LOG_RANGE_MAX, source_id=source_id)
    page_size = _source_int(scan, "page_size", 1000, source_id=source_id)
    chunks, origin = plan_chunks(
        stored, int(scan.head), limit=min(limit, LOG_RANGE_MAX), bootstrap_origin=scan.bootstrap_origin, overlap=scan.overlap,
    )
    if adapter == "etherscan_v2":
        client = _etherscan(scan, source_id, source)
        native = _walk_pages(scan, chunks, origin, wallet_id, "history.native_external", source_id, True, lambda start, end: _safe_history(client, scan.chain_id, "txlist", address, start, end, stored))
        if native.refusal:
            return native
        token = _walk_pages(scan, chunks, origin, wallet_id, "history.fungible", source_id, False, lambda start, end: _safe_history(client, scan.chain_id, "tokentx", address, start, end, stored), cursor=False)
        native.rows.extend(token.rows)
        return native
    if adapter == "nodereal_transfers":
        client = NodeRealClient(scan.transport, source_id, network=scan.network, clock=scan.clock, sleep=lambda _seconds: None, random=lambda: 0.0)
        native = _walk_pages(scan, chunks, origin, wallet_id, "history.native_external", source_id, True, lambda start, end: _nodereal_sides(client, "external", address, start, end, stored))
        if native.refusal:
            _probe_internal(scan, client, wallet_id, address, stored)
            return native
        token = _walk_pages(scan, chunks, origin, wallet_id, "history.fungible", source_id, False, lambda start, end: _nodereal_sides(client, "20", address, start, end, stored), cursor=False)
        _probe_internal(scan, client, wallet_id, address, stored)
        native.rows.extend(token.rows)
        if token.refusal and token.refusal not in {None}:
            native.partial = True
        return native
    if adapter == "blockscout":
        client = _blockscout(scan, source_id, source)
        pro = _blockscout_pro(source)
        chain_id = scan.chain_id if pro else None
        native = _walk_pages(scan, chunks, origin, wallet_id, "history.native_external", source_id, True, lambda start, end: _safe_blockscout(client, "txlist", address, start, end, page_size, stored, chain_id, pro))
        if native.refusal:
            return native
        token = _walk_pages(scan, chunks, origin, wallet_id, "history.fungible", source_id, False, lambda start, end: _safe_blockscout(client, "tokentx", address, start, end, page_size, stored, chain_id, pro), cursor=False)
        native.rows.extend(token.rows)
        return native
    return _Outcome(refusal="unsupported_source")


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
    return _Outcome(rows=rows, partial=bool(hard or gaps or stopped))


def _nodereal_sides(client, category, address, start, end, stored):
    items = []
    last = None
    for side in ("from", "to"):
        read = client.transfers(
            category=category, address=address, start_block=start, end_block=end, side=side,
            previous_cursor=None if stored is None else str(stored),
        )
        if read.blocked or read.category == "error":
            return read
        items.extend(read.items)
        last = read
    if last is None:
        return None
    from guardian.sources.etherscan import HistoryRead
    category_name = "empty" if not items else "ok"
    return HistoryRead(last.capability, tuple(items), True, True, (), str(end), category_name, False, "", (), "")


def _probe_internal(scan, client, wallet_id, address, stored) -> None:
    if scan.head is None:
        return
    try:
        read = client.transfers(
            category="internal", address=address, start_block=max(0, scan.head), end_block=scan.head, side="to",
            previous_cursor=None if stored is None else str(stored),
        )
    except Exception:
        scan.observations[(wallet_id, scan.network, "history.native_internal")] = _blocked("read_error")
        return
    if read.blocked or read.category == "error":
        scan.observations[(wallet_id, scan.network, "history.native_internal")] = _blocked(read.reason or "blocked")
        return
    if read.category == "empty":
        return


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


def _etherscan(scan, source_id, source) -> EtherscanClient:
    name = source.get("credential_env") if isinstance(source.get("credential_env"), str) else "ETHERSCAN_API_KEY"
    secret = _secret(scan, name)
    return EtherscanClient(
        scan.transport, source_id, credential=secret, credential_env=name, clock=scan.clock, sleep=lambda _seconds: None,
    )


def _blockscout(scan, source_id, source) -> BlockscoutClient:
    name = source.get("credential_env") if isinstance(source.get("credential_env"), str) else ""
    secret = _secret(scan, name) if name else ""
    return BlockscoutClient(
        scan.transport, source_id, clock=scan.clock, sleep=lambda _seconds: None, random=lambda: 0.0, credential=secret,
    )


def _secret(scan, name: str) -> str:
    from guardian.configuration import _env_path, _load_secret_profile
    import os
    path = _env_path(os.environ, scan.public_config.document["private_inputs"]["secret_profile_file_env"])
    secrets = _load_secret_profile(path)
    value = secrets.get(name, "")
    return value if isinstance(value, str) else ""


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
            if not extra:
                extra.append("unread_gap")
            scan.observations[(wallet_id, scan.network, name)] = _window("partial", origin, target, source_id, False, scan.checkpoint, tuple(dict.fromkeys(extra)))
            if leaves:
                scan.advance(scan.explicit_cursors, scan.held_cursors, wallet_id, scan.network, name, leaves[-1][1])
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
    cursors = [_cursor(wallet_id, scan.network, capability, end) for wallet_id in wallet_ids]
    scan.database.commit_capability_cycle(
        scan.tenant, events=scan.events[before:], capability_rows=[], cursors=cursors, alerts=[],
    )


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


def _permit_notes(scan, owners, start, end, notes, wallet_for) -> None:
    for owner in owners:
        try:
            operation = evm_rpc("eth_getLogs", [{
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "topics": [PERMIT2_PERMIT_TOPIC, pad_address(owner)],
            }])
            reject_wallet_emitter(operation, {item.lower() for item in owners})
            result = scan.transport.request(scan.source_id, operation)
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


def _past_deadline(scan) -> bool:
    if not isinstance(scan.deadline, int) or isinstance(scan.deadline, bool) or scan.deadline < 1:
        return False
    return scan.clock() - scan.started_at >= scan.deadline


def _sleep(scan):
    def pause(seconds: float) -> None:
        ledger = getattr(scan.transport, "provenance", None)
        if ledger is not None:
            ledger.note_backoff(scan.source_id, seconds)
    return pause


def _source_int(scan, key: str, default: int, source_id: str | None = None) -> int:
    source = scan.public_config.document["sources"].get(source_id or scan.source_id, {})
    value = source.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return default


def _source_flag(scan, key: str) -> bool:
    source = scan.public_config.document["sources"].get(scan.source_id, {})
    return source.get(key) is True


def _log_contract(scan, wallets: set[str]) -> str | None:
    source = scan.public_config.document["sources"].get(scan.source_id, {})
    value = source.get("log_contract")
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return None
    if value.lower() in wallets:
        return None
    return value


def _route_ids(public_config, network: str, capability: str) -> list[str]:
    for route in public_config.document["routing"]:
        if route.get("network") == network and route.get("capability") == capability:
            ids = route.get("source_ids") or []
            return [item for item in ids if isinstance(item, str)]
    return []


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
