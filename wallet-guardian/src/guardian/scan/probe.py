"""Read-only balance and log probe. A change can start one ``run-once``.

The probe does not write alerts, does not advance ``history.*`` cursors, and
does not start a sender. ``--no-send`` is the only delivery mode. There is no
websocket. The lock file is beside the probe state, not ``.shadow-round.lock``
and not the run-once lock.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from guardian.balances.native import (
    ReadResult,
    _asset,
    _checkpoint_row,
    _config_decimal,
    confirmation_policy,
    plan_transition,
    unit_thresholds,
)
from guardian.chains.evm.accounting import TRANSFER_TOPIC, pad_address
from guardian.chains.evm.adapter import evm_rpc, hex_quantity
from guardian.chains.evm.multicall import (
    MULTICALL3,
    bytecode_present,
    decode_aggregate3_balances,
    encode_aggregate3,
)
from guardian.chains.evm.permissions import APPROVAL_FOR_ALL_TOPIC, ERC20_APPROVAL_TOPIC
from guardian.chains.evm.permits import PERMIT2_LOG_TOPICS
from guardian.errors import ConfigError
from guardian.network_registry import load_registry
from guardian.resources import resource_path

_APPROVAL_TOPICS = {
    topic.lower()
    for topic in (ERC20_APPROVAL_TOPIC, APPROVAL_FOR_ALL_TOPIC, *PERMIT2_LOG_TOPICS)
}
_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


@dataclass
class _Meter:
    http: int = 0
    methods: int = 0


def probe_lock_path(probe_state: Path) -> Path:
    """Lock beside the probe state file. This is not the shadow round lock."""
    path = Path(probe_state)
    return path.with_name(path.name + ".balance-probe.lock")


@contextmanager
def probe_guard(probe_state: Path) -> Iterator[None]:
    """One probe at a time. A second probe raises ``balance_probe_locked``."""
    path = Path(probe_state)
    thread_lock = _thread_lock(str(path.resolve()))
    if not thread_lock.acquire(blocking=False):
        raise ConfigError("balance_probe_locked")
    lock_path = probe_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _lock_fd(descriptor)
    except OSError as exc:
        os.close(descriptor)
        thread_lock.release()
        raise ConfigError("balance_probe_locked") from exc
    try:
        yield
    finally:
        _unlock_fd(descriptor)
        os.close(descriptor)
        thread_lock.release()


def run_balance_probe(
    public_config: Any,
    private_inputs: Any,
    database: Any,
    *,
    probe_state: Path,
    backend: Any,
    clock: Callable[[], float],
    trigger: str,
    run_once: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """One round. Writes probe rows and, on a quiet balance, the shared baseline.

    A changed balance is left for ``run-once`` so this function does not alert.
    ``trigger`` ``none`` records the change and does not call ``run-once``.
    """
    if trigger not in {"run-once", "none"}:
        raise ConfigError("balance-probe trigger must be run-once or none")
    tenant = private_inputs.accounts.tenant_id
    from guardian.storage import Database

    probe_db = Database(Path(probe_state))
    try:
        try:
            with probe_guard(Path(probe_state)):
                return _round(
                    public_config,
                    private_inputs,
                    database,
                    probe_db,
                    backend=backend,
                    clock=clock,
                    trigger=trigger,
                    run_once=run_once,
                    tenant=tenant,
                )
        except ConfigError as exc:
            if str(exc) != "balance_probe_locked":
                raise
            _skip(probe_db, tenant, "balance_probe_locked", _iso(clock))
            return _envelope(trigger, status="skipped", reason="balance_probe_locked", run_once="not_called")
    finally:
        probe_db.close()


def _round(
    public_config: Any,
    private_inputs: Any,
    database: Any,
    probe_db: Any,
    *,
    backend: Any,
    clock: Callable[[], float],
    trigger: str,
    run_once: Callable[[], Any] | None,
    tenant: str,
) -> dict[str, Any]:
    from guardian.pipeline import _transport

    started = _iso(clock)
    registry = load_registry(resource_path("config", "chains"))
    alerts = public_config.document.get("alerts")
    alerts = alerts if isinstance(alerts, dict) else {}
    native_eth = _config_decimal(alerts.get("native_eth_threshold"), Decimal("0.5"))
    usd_threshold = _config_decimal(alerts.get("asset_usd_threshold"), Decimal("1000"))
    units = unit_thresholds(alerts.get("native_unit_thresholds"))
    transport = _transport(public_config, backend, clock)
    chains = []
    for network, accounts in _targets(public_config, private_inputs, registry).items():
        chains.append(_probe_chain(
            public_config,
            private_inputs,
            database,
            probe_db,
            transport,
            registry[network],
            network,
            accounts,
            tenant=tenant,
            started=started,
            trigger=trigger,
            native_eth=native_eth,
            usd_threshold=usd_threshold,
            units=units,
        ))
    run_state = "not_called"
    if trigger == "run-once" and probe_db.probe_handoff_outstanding(tenant):
        run_state = _handoff(
            public_config, private_inputs, database, probe_db, backend, clock, tenant, run_once,
        )
    return _envelope(trigger, chains=chains, run_once=run_state)


def _probe_chain(
    public_config: Any,
    private_inputs: Any,
    database: Any,
    probe_db: Any,
    transport: Any,
    entry: dict[str, Any],
    network: str,
    accounts: Sequence[Any],
    *,
    tenant: str,
    started: str,
    trigger: str,
    native_eth: Any,
    usd_threshold: Any,
    units: dict[str, Any],
) -> dict[str, Any]:
    from guardian.pipeline import _family_source

    meter = _Meter()
    wallets = len(accounts)
    source_id = _family_source(public_config, private_inputs, network, "evm_rpc")
    if source_id is None:
        return _chain_row(network, meter, wallets, failure="endpoint_missing")
    head, failure = _call(transport, source_id, "eth_getBlockByNumber", ["latest", False], meter)
    parsed = None if failure else _parse_head(head)
    if parsed is None:
        return _chain_row(network, meter, wallets, failure="head")
    number, block_hash, timestamp = parsed
    bound = {"blockHash": block_hash, "requireCanonical": True}
    code, code_failure = _call(transport, source_id, "eth_getCode", [MULTICALL3, bound], meter)
    if code_failure or not isinstance(code, str):
        return _chain_row(network, meter, wallets, failure="code")
    present = bytecode_present(code)
    if present is None:
        return _chain_row(network, meter, wallets, failure="code")
    addresses = [account.normalized.canonical for account in accounts]
    if present:
        balances, balance_failure, balance_source = _multicall(
            transport, source_id, addresses, bound, meter,
        )
    else:
        balances, balance_failure, balance_source = _balances(
            transport, source_id, addresses, bound, meter,
        )
    if balance_failure or balances is None:
        return _chain_row(network, meter, wallets, failure=balance_failure or "balance", balance_source=balance_source)
    cursor = probe_db.probe_cursor(tenant, network)
    start, end = _window(cursor, number, block_hash)
    incoming, in_failure = _logs(transport, source_id, _incoming_topics(addresses), start, end, meter)
    if in_failure or incoming is None:
        return _chain_row(network, meter, wallets, failure="logs", balance_source=balance_source)
    outgoing, out_failure = _logs(transport, source_id, _outgoing_topics(addresses), start, end, meter)
    if out_failure or outgoing is None:
        return _chain_row(network, meter, wallets, failure="logs", balance_source=balance_source)
    watched = {"0x" + address[2:].lower() for address in addresses}
    log_changes = _log_changes(incoming + outgoing, watched)
    if log_changes is None:
        return _chain_row(network, meter, wallets, failure="logs", balance_source=balance_source)
    policy = confirmation_policy(network, entry)
    asset, decimals, symbol = _asset(entry, network)
    plans: list[dict[str, Any]] = []
    for account, raw in zip(accounts, balances):
        if raw is None:
            return _chain_row(network, meter, wallets, failure="balance", balance_source=balance_source)
        plan = _plan_balance(
            database,
            account,
            raw,
            network=network,
            asset=asset,
            decimals=decimals,
            symbol=symbol,
            source_id=source_id,
            policy=policy,
            number=number,
            block_hash=block_hash,
            timestamp=timestamp,
            tenant=tenant,
            started=started,
            native_eth=native_eth,
            usd_threshold=usd_threshold,
            units=units,
        )
        if plan["action"] == "failed":
            return _chain_row(network, meter, wallets, failure="balance", balance_source=balance_source)
        plans.append(plan)
    changes = [item for item in plans if item["action"] == "change"]
    changes.extend({"action": "change", "reason": reason, "fingerprint": fingerprint} for reason, fingerprint in log_changes)
    for plan in plans:
        if plan["action"] != "commit":
            continue
        status = database.commit_native_observation(
            tenant,
            wallet_id=plan["wallet_id"],
            network=network,
            asset=asset,
            expected_version=plan["expected_version"],
            checkpoint=plan["checkpoint"],
            continuity=plan["continuity"],
            retention_blocks=policy.retention_blocks,
            reset_checkpoints=False,
            alert=None,
            record_failure=None,
            decision=None,
            notify=False,
        )
        if status not in {"committed", "conflict"}:
            return _chain_row(network, meter, wallets, failure="balance", balance_source=balance_source)
    inserted = _insert_triggers(probe_db, tenant, network, number, block_hash, changes, started, trigger)
    probe_db.upsert_probe_cursor(tenant, network, number, block_hash, started)
    names = sorted({item["reason"] for item in changes})
    return _chain_row(
        network,
        meter,
        wallets,
        triggered=inserted,
        reasons=names if inserted else [],
        cursor_advanced=True,
        balance_source=balance_source,
    )


def _plan_balance(
    database: Any,
    account: Any,
    raw: int,
    *,
    network: str,
    asset: str,
    decimals: int,
    symbol: str,
    source_id: str,
    policy: Any,
    number: int,
    block_hash: str,
    timestamp: int | None,
    tenant: str,
    started: str,
    native_eth: Any,
    usd_threshold: Any,
    units: dict[str, Any],
) -> dict[str, Any]:
    """Decide one wallet. A different raw balance is not written and is not turned into zero."""
    read = ReadResult(True, None, True, raw, number, block_hash, timestamp, "eip1898_hash", False, None)
    item = {
        "asset": asset,
        "decimals": decimals,
        "label": account.label if isinstance(account.label, str) else account.wallet_id,
        "network": network,
        "policy": policy,
        "read": read,
        "source_id": source_id,
        "symbol": symbol,
        "tenant": tenant,
        "wallet_id": account.wallet_id,
    }
    stored = database.native_balance_baseline(tenant, account.wallet_id, network, asset)
    if stored is not None:
        try:
            previous = int(stored["raw_balance"])
        except (TypeError, ValueError):
            return {"action": "failed"}
        if previous != raw:
            return {
                "action": "change",
                "fingerprint": _fingerprint("balance", account.wallet_id, str(raw)),
                "reason": "balance_change",
            }
    planned = plan_transition(item, stored, None, native_eth, usd_threshold, units, started)
    if planned.get("action") != "commit" or planned.get("alert"):
        return {"action": "skip"}
    return {
        "action": "commit",
        "checkpoint": _checkpoint_row(item, read, policy, started),
        "continuity": str(planned.get("continuity") or "continuous"),
        "expected_version": 0 if stored is None else int(stored["version"]),
        "wallet_id": account.wallet_id,
    }


def _insert_triggers(
    probe_db: Any,
    tenant: str,
    network: str,
    number: int,
    block_hash: str,
    changes: Sequence[dict[str, Any]],
    started: str,
    trigger: str,
) -> bool:
    handoff = "not_requested" if trigger == "none" else "pending"
    inserted = False
    seen: set[str] = set()
    for change in changes:
        fingerprint = str(change["fingerprint"])
        if fingerprint in seen or probe_db.has_probe_trigger(tenant, network, fingerprint):
            continue
        seen.add(fingerprint)
        probe_db.insert_probe_trigger(
            tenant,
            network=network,
            block_number=number,
            block_hash=block_hash,
            reason=str(change["reason"]),
            fingerprint=fingerprint,
            status="triggered",
            handoff=handoff,
            created_at=started,
        )
        inserted = True
    return inserted


def _log_changes(logs: Sequence[Any], watched: set[str]) -> list[tuple[str, str]] | None:
    found: list[tuple[str, str]] = []
    for log in logs:
        if not isinstance(log, dict):
            return None
        topics = log.get("topics")
        if not isinstance(topics, list) or not topics or not isinstance(topics[0], str):
            return None
        topic0 = topics[0].lower()
        if not _mentions(topics, watched):
            continue
        tx = log.get("transactionHash")
        index = _log_index(log.get("logIndex"))
        if not isinstance(tx, str) or not tx.startswith("0x") or index is None:
            return None
        if topic0 == TRANSFER_TOPIC.lower():
            found.append(("transfer_log", _fingerprint("transfer_log", tx.lower(), str(index))))
        elif topic0 in _APPROVAL_TOPICS:
            found.append(("approval_log", _fingerprint("approval_log", tx.lower(), str(index))))
    return found


def _mentions(topics: Sequence[Any], watched: set[str]) -> bool:
    for topic in topics[1:]:
        if not isinstance(topic, str) or len(topic) < 40:
            continue
        if ("0x" + topic[-40:]).lower() in watched:
            return True
    return False


def _log_index(value: Any) -> int | None:
    if isinstance(value, str):
        return hex_quantity(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _multicall(
    transport: Any,
    source_id: str,
    addresses: Sequence[str],
    bound: dict[str, Any],
    meter: _Meter,
) -> tuple[list[int | None] | None, str | None, str]:
    data = encode_aggregate3(addresses)
    result, failure = _call(
        transport,
        source_id,
        "eth_call",
        [{"data": data, "to": MULTICALL3}, bound],
        meter,
    )
    if failure or not isinstance(result, str):
        return None, "multicall", "aggregate3"
    decoded = decode_aggregate3_balances(result, addresses)
    if decoded is None:
        return None, "multicall", "aggregate3"
    if any(item is None for item in decoded):
        return decoded, "balance", "aggregate3"
    return decoded, None, "aggregate3"


def _balances(
    transport: Any,
    source_id: str,
    addresses: Sequence[str],
    bound: dict[str, Any],
    meter: _Meter,
) -> tuple[list[int | None] | None, str | None, str]:
    found: list[int | None] = []
    for address in addresses:
        result, failure = _call(transport, source_id, "eth_getBalance", [address, bound], meter)
        if failure or not isinstance(result, str):
            found.append(None)
            continue
        parsed = hex_quantity(result)
        found.append(parsed)
    if any(item is None for item in found):
        return found, "balance", "eth_getBalance"
    return found, None, "eth_getBalance"


def _logs(
    transport: Any,
    source_id: str,
    topics: list[Any],
    start: int,
    end: int,
    meter: _Meter,
) -> tuple[list[Any] | None, str | None]:
    result, failure = _call(
        transport,
        source_id,
        "eth_getLogs",
        [{"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}],
        meter,
    )
    if failure or not isinstance(result, list):
        return None, "logs"
    return result, None


def _call(
    transport: Any,
    source_id: str,
    method: str,
    params: list[Any],
    meter: _Meter,
) -> tuple[Any, str | None]:
    meter.http += 1
    meter.methods += 1
    try:
        result = transport.request(source_id, evm_rpc(method, params), priority="high")
    except Exception as exc:
        reason = getattr(exc, "reason", None)
        return None, reason if isinstance(reason, str) and reason else "read_error"
    if getattr(result, "ok", False) is not True:
        return None, "read_error"
    body = getattr(result, "body", None)
    if not isinstance(body, dict) or body.get("error") is not None or "result" not in body:
        return None, "read_error"
    return body.get("result"), None


def _handoff(
    public_config: Any,
    private_inputs: Any,
    database: Any,
    probe_db: Any,
    backend: Any,
    clock: Callable[[], float],
    tenant: str,
    run_once: Callable[[], Any] | None,
) -> str:
    try:
        if run_once is None:
            _default_run_once(public_config, private_inputs, database, backend, clock)
        else:
            run_once()
    except ConfigError as exc:
        if str(exc) != "run_once_locked":
            raise
        probe_db.mark_probe_handoff_locked(tenant)
        _skip(probe_db, tenant, "run_once_locked", _iso(clock))
        return "locked"
    probe_db.mark_probe_handoff_called(tenant)
    return "called"


def _default_run_once(
    public_config: Any,
    private_inputs: Any,
    database: Any,
    backend: Any,
    clock: Callable[[], float],
) -> None:
    from guardian.pipeline import run_capability_pipeline

    run_capability_pipeline(
        public_config,
        private_inputs.accounts,
        private_inputs,
        database,
        backend=backend,
        clock=clock,
        no_send=True,
        sender=None,
    )


def _skip(probe_db: Any, tenant: str, reason: str, created_at: str) -> None:
    probe_db.insert_probe_trigger(
        tenant,
        network=None,
        block_number=None,
        block_hash=None,
        reason=reason,
        fingerprint=_fingerprint("skip", reason, str(time.time_ns())),
        status="skipped",
        handoff=None,
        created_at=created_at,
    )


def _targets(public_config: Any, private_inputs: Any, registry: dict[str, Any]) -> dict[str, list[Any]]:
    enabled = set(public_config.document.get("enabled_networks") or [])
    grouped: dict[str, list[Any]] = {}
    for account in private_inputs.accounts.accounts:
        if account.family != "evm":
            continue
        for network in account.networks:
            if network not in enabled:
                continue
            entry = registry.get(network) or {}
            if entry.get("family") != "evm":
                continue
            grouped.setdefault(network, []).append(account)
    for accounts in grouped.values():
        accounts.sort(key=lambda item: item.wallet_id)
    return {network: grouped[network] for network in sorted(grouped)}


def _window(cursor: dict[str, Any] | None, number: int, block_hash: str) -> tuple[int, int]:
    if cursor is None:
        return number, number
    previous = int(cursor["block_number"])
    if previous == number and str(cursor["block_hash"]).lower() != block_hash.lower():
        return number, number
    if previous >= number:
        return number, number
    return previous + 1, number


def _incoming_topics(addresses: Sequence[str]) -> list[Any]:
    return [TRANSFER_TOPIC, None, [pad_address(address) for address in addresses]]


def _outgoing_topics(addresses: Sequence[str]) -> list[Any]:
    topic0 = [TRANSFER_TOPIC, ERC20_APPROVAL_TOPIC, APPROVAL_FOR_ALL_TOPIC, *PERMIT2_LOG_TOPICS]
    return [topic0, [pad_address(address) for address in addresses]]


def _parse_head(result: Any) -> tuple[int, str, int | None] | None:
    if not isinstance(result, dict):
        return None
    number = hex_quantity(result.get("number")) if isinstance(result.get("number"), str) else None
    digest = result.get("hash")
    if number is None or not isinstance(digest, str):
        return None
    text = digest.strip().lower()
    if len(text) != 66 or not text.startswith("0x"):
        return None
    try:
        int(text[2:], 16)
    except ValueError:
        return None
    timestamp = hex_quantity(result.get("timestamp")) if isinstance(result.get("timestamp"), str) else None
    return number, text, timestamp


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _chain_row(
    network: str,
    meter: _Meter,
    wallets: int,
    *,
    failure: str | None = None,
    triggered: bool = False,
    reasons: list[str] | None = None,
    cursor_advanced: bool = False,
    balance_source: str | None = None,
) -> dict[str, Any]:
    return {
        "balance_source": balance_source,
        "cursor_advanced": cursor_advanced,
        "failure": failure,
        "http": meter.http,
        "methods": meter.methods,
        "network": network,
        "reasons": list(reasons or []),
        "triggered": triggered,
        "wallets": wallets,
    }


def _envelope(
    trigger: str,
    *,
    status: str = "ok",
    reason: str | None = None,
    chains: list[dict[str, Any]] | None = None,
    run_once: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "alerts_written": 0,
        "chains": list(chains or []),
        "delivery": "no_send",
        "probe_alerts_written": 0,
        "reason": reason,
        "run_once": run_once,
        "sent": False,
        "status": status,
        "trigger": trigger,
    }
    return payload


def _iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _thread_lock(key: str) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _lock_fd(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.lseek(descriptor, 0, os.SEEK_END) < 1:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
