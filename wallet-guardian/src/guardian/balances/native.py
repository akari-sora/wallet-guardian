"""Definite-block native balances and the balance_delta decision.

The first valid observation is a quiet baseline. A failed, malformed, or
wrong-chain read does not replace that baseline with zero. The comparison is
a net change between checkpoints, not a proven transfer and not a cause.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from guardian.chains.evm.adapter import EvmAdapter, evm_rpc, hex_quantity
from guardian.coverage import CoverageObservation
from guardian.pricing.catalog import load_risk_sources
from guardian.pricing.coingecko import CoinGeckoClient
from guardian.pricing.quote import value_usd
from guardian.rules import ETH_NETWORKS, dedup_key
from guardian.sources.budget import BudgetExhausted

QUOTE_MAX_AGE_SECONDS = 900
_WEI = Decimal(10) ** 18
_SHORT = {
    "arbitrum-one": "Arbitrum",
    "avalanche-c": "Avalanche",
    "base-mainnet": "Base",
    "bsc-mainnet": "BSC",
    "ethereum-mainnet": "Ethereum",
    "optimism-mainnet": "Optimism",
    "polygon-pos": "Polygon",
}


@dataclass(frozen=True)
class ConfirmationPolicy:
    """How this chain asks for a checkpoint. Retention is not finality."""

    name: str
    prefer_tag: str | None
    retention_blocks: int
    tag_is_finalized: bool


@dataclass
class ReadResult:
    eligible: bool
    failure: str | None = None
    had_block: bool = False
    raw: int | None = None
    block_number: int | None = None
    block_hash: str | None = None
    block_timestamp: int | None = None
    binding: str | None = None
    tag_used: bool = False
    nonce: str | None = None


@dataclass
class PrimeResult:
    skip: set[tuple[str, str]] = field(default_factory=set)
    covered: set[tuple[str, str]] = field(default_factory=set)
    outbox_written: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    quotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    decimals: dict[str, int] = field(default_factory=dict)


def confirmation_policy(network: str, entry: Mapping[str, Any]) -> ConfirmationPolicy:
    """Use the chain's own checkpoint. Do not copy Ethereum's depth onto BSC."""
    if network == "bsc-mainnet":
        return ConfirmationPolicy(
            name="bsc_bep126_fast_finality",
            prefer_tag="finalized",
            retention_blocks=15,
            tag_is_finalized=False,
        )
    if entry.get("family") == "evm":
        return ConfirmationPolicy(
            name="evm_finalized_tag",
            prefer_tag="finalized",
            retention_blocks=64,
            tag_is_finalized=network == "ethereum-mainnet",
        )
    return ConfirmationPolicy("unspecified", None, 0, False)


def prime_native_balances(
    *,
    public_config: Any,
    private_inputs: Any,
    transport: Any,
    database: Any,
    enabled: Sequence[tuple[Any, str]],
    registry: Mapping[str, Mapping[str, Any]],
    clock: Callable[[], float],
    started: str,
    tenant: str,
    observations: dict[tuple[str, str, str], CoverageObservation],
    native_reads: list[dict[str, str]],
    seen: set[tuple[str, str, str]],
    baseline: list[tuple[str, str, str]],
    candidates: list[Any],
    started_at: float,
    deadline: int | None,
) -> PrimeResult:
    """Read every EVM native balance before log or indexer backfill."""
    from guardian.chains.evm.permissions import _PERMISSION_CAPABILITIES
    from guardian.pipeline import _baseline, _block_caps, _block_identity, _family_source

    result = PrimeResult()
    fetched: list[dict[str, Any]] = []
    networks: list[str] = []
    for account, network in enabled:
        entry = registry.get(network) or {}
        if entry.get("family") != "evm":
            continue
        networks.append(network)
        wallet_id = account.wallet_id
        asset, decimals, symbol = _asset(entry, network)
        policy = confirmation_policy(network, entry)
        label = account.label if isinstance(getattr(account, "label", None), str) else wallet_id
        source_id = _family_source(public_config, private_inputs, network, "evm_rpc")
        item: dict[str, Any] = {
            "asset": asset,
            "decimals": decimals,
            "eligible": False,
            "label": label,
            "network": network,
            "observation": None,
            "policy": policy,
            "raw": None,
            "skip": False,
            "source_id": source_id or "",
            "symbol": symbol,
            "wallet_id": wallet_id,
        }
        if source_id is None:
            _block_caps(
                wallet_id, network, observations,
                ("balances.native", "history.fungible", *_PERMISSION_CAPABILITIES),
                "endpoint_missing",
            )
            item["skip"] = True
            fetched.append(item)
            continue
        adapter = EvmAdapter(entry, transport, rpc_source_id=source_id)
        session = adapter.open(clock=clock, started_at=started_at, deadline=deadline)
        if not session.identity.ok:
            _block_identity(wallet_id, network, observations, session.identity)
            item["skip"] = True
            fetched.append(item)
            continue
        read = read_definite_balance(transport, source_id, account.normalized.canonical, policy)
        item["read"] = read
        if read.eligible and read.raw is not None and read.block_number is not None and read.block_hash:
            item["eligible"] = True
            item["raw"] = read.raw
            item["observation"] = _coverage(read, policy)
        elif read.had_block:
            item["failure"] = read.failure or "read_error"
            item["observation"] = _error_coverage(item["failure"])
        else:
            balance = session.native_balance(account.normalized.canonical)
            item["observation"] = balance.observation
            item["failure"] = read.failure or balance.reason or "block_unreadable"
            if balance.raw_wei:
                item["raw"] = _raw_text(balance.raw_wei)
        fetched.append(item)
    quotes = native_quotes(public_config, transport, clock, networks)
    result.quotes = quotes
    alerts = public_config.document.get("alerts") if isinstance(public_config.document.get("alerts"), Mapping) else {}
    native_eth = _config_decimal(alerts.get("native_eth_threshold"), Decimal("0.5"))
    usd_threshold = _config_decimal(alerts.get("asset_usd_threshold"), Decimal("1000"))
    units = unit_thresholds(alerts.get("native_unit_thresholds"))
    for item in fetched:
        wallet_id = item["wallet_id"]
        network = item["network"]
        if item["skip"]:
            result.skip.add((wallet_id, network))
            continue
        observation = item["observation"]
        if observation is not None:
            observations[(wallet_id, network, "balances.native")] = observation
        raw = item["raw"]
        if isinstance(raw, int):
            native_reads.append({"network": network, "raw_amount": str(raw), "wallet_id": wallet_id})
            _baseline(wallet_id, network, "balances.native", seen, baseline, candidates, tenant)
        result.covered.add((wallet_id, network))
        result.decimals[network] = int(item["decimals"])
        if not item["eligible"]:
            _remember_failure(database, tenant, item, started)
            continue
        written, decision = commit_eligible(
            database, tenant, item, quotes, native_eth, usd_threshold, units, started,
        )
        result.outbox_written += written
        if decision is not None and (decision.get("alert") or decision.get("threshold_decision") == "threshold_unavailable"):
            result.decisions.append(decision)
    return result


def read_definite_balance(transport: Any, source_id: str, address: str, policy: ConfirmationPolicy) -> ReadResult:
    """Balance at one block. ``latest`` is not a checkpoint."""
    block, tag_used, failure = _confirmation_block(transport, source_id, policy)
    if block is None:
        return ReadResult(False, failure or "block_unreadable", False)
    raw, binding, balance_failure = _balance_at(transport, source_id, address, block)
    if raw is None:
        return ReadResult(False, balance_failure or "read_error", True)
    after, after_failure = _block_by_number(transport, source_id, block["number"])
    if after is None or after["hash"] != block["hash"] or after["number"] != block["number"]:
        return ReadResult(False, after_failure or "reorg_during_read", True)
    return ReadResult(
        True,
        None,
        True,
        raw,
        block["number"],
        block["hash"],
        block.get("timestamp"),
        binding,
        tag_used,
        _nonce(transport, source_id, address, block, binding),
    )


def native_quotes(public_config: Any, transport: Any, clock: Callable[[], float], networks: Sequence[str]) -> dict[str, dict[str, Any]]:
    """One simple-price request for the pinned native coin ids. Missing is not zero."""
    source_id = _price_source(public_config, transport)
    if source_id is None or not networks:
        return {}
    try:
        catalog = load_risk_sources()
    except Exception:
        return {}
    ids: list[str] = []
    by_network: dict[str, str] = {}
    for network in networks:
        priced = catalog.priced_network(network)
        if priced is None or not priced.native_coin_id:
            continue
        by_network[network] = priced.native_coin_id
        if priced.native_coin_id not in ids:
            ids.append(priced.native_coin_id)
    if not ids:
        return {}
    client = CoinGeckoClient(transport, source_id, catalog, {})
    parsed = client.simple_prices(ids, max_age_seconds=QUOTE_MAX_AGE_SECONDS, now=_now(clock))
    quotes: dict[str, dict[str, Any]] = {}
    for network, coin_id in by_network.items():
        row = parsed.get(coin_id)
        if isinstance(row, dict):
            quotes[network] = row
    return quotes


def unit_thresholds(value: Any) -> dict[str, Decimal]:
    """Explicit native-unit amounts keyed by canonical network. Absent is not 0.5."""
    if not isinstance(value, Mapping):
        return {}
    found: dict[str, Decimal] = {}
    for network, raw in value.items():
        if not isinstance(network, str) or not network:
            continue
        number = _config_decimal(raw, None)
        if number is None:
            continue
        found[network] = number
    return found


def judge_threshold(
    *,
    network: str,
    magnitude: int,
    decimals: int,
    native_eth: Decimal,
    usd_threshold: Decimal,
    units: Mapping[str, Decimal],
    quote: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """OR of a native-unit floor and a fresh USD floor. Decreases use magnitude."""
    native_hit = False
    native_available = False
    native_basis = None
    explicit = units.get(network)
    explicit_raw = None
    eth_hit = False
    unit_hit = False
    if network in ETH_NETWORKS:
        native_available = True
        eth_hit = magnitude >= int((native_eth * _WEI).to_integral_value())
    if explicit is not None:
        native_available = True
        explicit_raw = int((explicit * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))
        unit_hit = magnitude >= explicit_raw
    native_hit = eth_hit or unit_hit
    if eth_hit:
        native_basis = "native_eth"
    elif unit_hit:
        native_basis = "native_unit"
    usd_hit = False
    usd_available = False
    usd_value = None
    usd_quality = "unavailable"
    if isinstance(quote, Mapping) and quote.get("quality") == "available" and isinstance(quote.get("price"), Decimal):
        try:
            usd_value = value_usd(magnitude, decimals, quote["price"])
        except ValueError:
            usd_value = None
        if isinstance(usd_value, Decimal):
            usd_available = True
            usd_quality = "available"
            usd_hit = usd_value >= usd_threshold
        else:
            usd_quality = str(quote.get("reason") or "malformed")
    elif isinstance(quote, Mapping):
        usd_quality = str(quote.get("quality") or quote.get("reason") or "unavailable")
    if native_hit or usd_hit:
        decision = "above"
    elif not native_available and not usd_available:
        decision = "threshold_unavailable"
    else:
        decision = "below"
    if native_hit:
        basis = native_basis or "native_unit"
    elif usd_hit:
        basis = "usd"
    else:
        basis = "none"
    return {
        "basis": basis,
        "decision": decision,
        "native_available": native_available,
        "native_hit": native_hit,
        "native_unit_raw": None if explicit_raw is None else str(explicit_raw),
        "native_unit_threshold": None if explicit is None else format(explicit, "f"),
        "quote_age_policy_seconds": QUOTE_MAX_AGE_SECONDS,
        "quote_quality": usd_quality,
        "usd_available": usd_available,
        "usd_hit": usd_hit,
        "usd_threshold": format(usd_threshold, "f"),
        "usd_value": None if usd_value is None else format(usd_value, "f"),
    }


def commit_eligible(
    database: Any,
    tenant: str,
    item: Mapping[str, Any],
    quotes: Mapping[str, Mapping[str, Any]],
    native_eth: Decimal,
    usd_threshold: Decimal,
    units: Mapping[str, Decimal],
    started: str,
) -> tuple[int, dict[str, Any] | None]:
    read: ReadResult = item["read"]
    policy: ConfirmationPolicy = item["policy"]
    if isinstance(item, dict):
        item["tenant"] = tenant
    for attempt in range(2):
        stored = database.native_balance_baseline(tenant, item["wallet_id"], item["network"], item["asset"])
        planned = plan_transition(
            item, stored, quotes.get(item["network"]), native_eth, usd_threshold, units, started,
            retry=attempt > 0,
        )
        if planned["action"] == "discard":
            return 0, planned.get("decision")
        status = database.commit_native_observation(
            tenant,
            wallet_id=item["wallet_id"],
            network=item["network"],
            asset=item["asset"],
            expected_version=0 if stored is None else int(stored["version"]),
            checkpoint=_checkpoint_row(item, read, policy, started),
            continuity=planned["continuity"],
            retention_blocks=policy.retention_blocks,
            reset_checkpoints=planned["reset"],
            alert=planned.get("alert"),
            record_failure=None,
        )
        if status == "committed":
            decision = planned.get("decision")
            return (1 if planned.get("alert") else 0), decision
        if status == "rejected":
            return 0, planned.get("decision")
    return 0, None


def plan_transition(
    item: Mapping[str, Any],
    stored: Mapping[str, Any] | None,
    quote: Mapping[str, Any] | None,
    native_eth: Decimal,
    usd_threshold: Decimal,
    units: Mapping[str, Decimal],
    started: str,
    retry: bool = False,
) -> dict[str, Any]:
    """Decide whether this observation may move the baseline, and whether it alerts."""
    read: ReadResult = item["read"]
    policy: ConfirmationPolicy = item["policy"]
    if stored is None:
        return {"action": "commit", "alert": None, "continuity": "baseline", "decision": None, "reset": False}
    old_block = int(stored["block_number"])
    old_hash = str(stored["block_hash"])
    old_raw = int(stored["raw_balance"])
    new_block = int(read.block_number or 0)
    new_hash = str(read.block_hash or "")
    new_raw = int(read.raw or 0)
    if new_block == old_block and new_hash != old_hash:
        return {"action": "discard", "alert": None, "continuity": "reorg_same_height", "decision": None, "reset": False}
    if new_block == old_block and new_hash == old_hash and new_raw != old_raw:
        return {"action": "discard", "alert": None, "continuity": "inconsistent_balance", "decision": None, "reset": False}
    if new_block < old_block:
        # A conflicting writer already stored a newer checkpoint. Do not replace it.
        if not retry and old_block - new_block > policy.retention_blocks:
            return {"action": "commit", "alert": None, "continuity": "gap", "decision": None, "reset": True}
        return {"action": "discard", "alert": None, "continuity": "backwards", "decision": None, "reset": False}
    if new_block == old_block and new_raw == old_raw:
        return {"action": "commit", "alert": None, "continuity": "continuous", "decision": None, "reset": False}
    signed = new_raw - old_raw
    if signed == 0:
        continuity = "gap_disclosed" if stored.get("last_failed_at") else "continuous"
        return {"action": "commit", "alert": None, "continuity": continuity, "decision": None, "reset": False}
    judgment = judge_threshold(
        network=item["network"],
        magnitude=abs(signed),
        decimals=int(item["decimals"]),
        native_eth=native_eth,
        usd_threshold=usd_threshold,
        units=units,
        quote=quote,
    )
    gap = bool(stored.get("last_failed_at")) or (new_block - old_block > policy.retention_blocks)
    continuity = "gap_disclosed" if gap else "continuous"
    payload = _payload(
        item, stored, read, policy, signed, judgment, continuity, started,
    )
    alert = payload if judgment["decision"] == "above" else None
    decision = payload
    return {"action": "commit", "alert": alert, "continuity": continuity, "decision": decision, "reset": False}


def _remember_failure(database: Any, tenant: str, item: Mapping[str, Any], started: str) -> None:
    stored = database.native_balance_baseline(tenant, item["wallet_id"], item["network"], item["asset"])
    if stored is None:
        return
    database.commit_native_observation(
        tenant,
        wallet_id=item["wallet_id"],
        network=item["network"],
        asset=item["asset"],
        expected_version=int(stored["version"]),
        checkpoint=None,
        continuity=str(stored.get("continuity") or "continuous"),
        retention_blocks=0,
        reset_checkpoints=False,
        alert=None,
        record_failure=started,
    )


def _payload(
    item: Mapping[str, Any],
    stored: Mapping[str, Any],
    read: ReadResult,
    policy: ConfirmationPolicy,
    signed: int,
    judgment: Mapping[str, Any],
    continuity: str,
    started: str,
) -> dict[str, Any]:
    old_block = int(stored["block_number"])
    old_hash = str(stored["block_hash"])
    new_block = int(read.block_number or 0)
    new_hash = str(read.block_hash or "")
    identity = observation_identity(
        item["wallet_id"], item["network"], item["asset"], old_block, old_hash, new_block, new_hash,
    )
    direction = "increase" if signed > 0 else "decrease"
    amount = format_raw(abs(signed), int(item["decimals"]))
    chain = _SHORT.get(item["network"], item["network"])
    detail = (
        f"{item['label']} · {chain}: native balance {direction}d by {amount} {item['symbol']} "
        "between the recorded checkpoints. Transaction attribution is unavailable."
    )
    if continuity == "gap_disclosed":
        detail += " The comparison uses the last usable observation, so the interval is longer than one quiet round."
    previous_nonce = stored.get("nonce")
    nonce_changed = None
    if isinstance(previous_nonce, str) and isinstance(read.nonce, str):
        nonce_changed = _nonce_int(previous_nonce) != _nonce_int(read.nonce)
    alert = judgment["decision"] == "above"
    return {
        "alert": alert,
        "asset": item["asset"],
        "attribution": "unavailable",
        "binding": read.binding,
        "cause": "unknown",
        "confirmation_policy": policy.name,
        "continuity": continuity,
        "counterparty_label": None,
        "dedup_key": dedup_key(str(item.get("tenant") or ""), item["wallet_id"], item["network"], "balance_delta", identity),
        "detail": detail,
        "direction": direction,
        "explained_by": [],
        "gap_disclosure": "longer_interval" if continuity == "gap_disclosed" else None,
        "identity": identity,
        "kind": "balance_delta",
        "network": item["network"],
        "new_checkpoint": _point(new_block, new_hash, read.block_timestamp, started),
        "nonce": {
            "changed": nonce_changed,
            "current": read.nonce,
            "previous": previous_nonce if isinstance(previous_nonce, str) else None,
        },
        "old_checkpoint": _point(old_block, old_hash, _optional_int(stored.get("block_timestamp")), str(stored.get("observed_at") or "")),
        "signed_delta_raw": str(signed),
        "tenant_id": item.get("tenant") or "",
        "threshold": judgment,
        "threshold_amount_raw": str(abs(signed)),
        "threshold_decision": judgment["decision"],
        "wallet_id": item["wallet_id"],
    }


def observation_identity(
    wallet_id: str, network: str, asset: str, old_block: int, old_hash: str, new_block: int, new_hash: str,
) -> str:
    material = "\x1f".join((wallet_id, network, asset, str(old_block), old_hash, str(new_block), new_hash))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def format_raw(raw: int, decimals: int) -> str:
    """Decimal text of an integer balance. No binary float."""
    if decimals < 0:
        raise ValueError("decimals")
    sign = "-" if raw < 0 else ""
    quantum = Decimal(abs(raw)).scaleb(-decimals)
    text = format(quantum, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return sign + (text or "0")


def _checkpoint_row(item: Mapping[str, Any], read: ReadResult, policy: ConfirmationPolicy, started: str) -> dict[str, Any]:
    return {
        "binding": read.binding or "fixed_height",
        "block_hash": read.block_hash,
        "block_number": read.block_number,
        "block_timestamp": None if read.block_timestamp is None else str(read.block_timestamp),
        "confirmation_policy": policy.name,
        "nonce": read.nonce,
        "observed_at": started,
        "raw_balance": str(read.raw),
        "source_id": item["source_id"],
    }


def _point(number: int, digest: str, timestamp: int | None, observed_at: str) -> dict[str, Any]:
    return {
        "block_hash": digest,
        "block_number": str(number),
        "block_timestamp": None if timestamp is None else str(timestamp),
        "observed_at": observed_at,
    }


def _coverage(read: ReadResult, policy: ConfirmationPolicy) -> CoverageObservation:
    if read.tag_used and policy.tag_is_finalized:
        status = "finalized"
    elif read.tag_used:
        status = "confirmed"
    else:
        status = "observed"
    return CoverageObservation(
        category="ok",
        pagination_complete=True,
        gap_detection_complete=True,
        declared="definite_block_balance",
        sources=("evm-rpc",),
        trust="single_provider",
        checkpoint_kind="block",
        checkpoint_number=str(read.block_number),
        checkpoint_hash=read.block_hash,
        checkpoint_status=status,
        price_quality="unknown",
    )


def _error_coverage(reason: str) -> CoverageObservation:
    return CoverageObservation(
        category="error",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="not_read",
        reason_codes=(reason,),
        trust="unverified",
        price_quality="unknown",
    )


def _confirmation_block(transport: Any, source_id: str, policy: ConfirmationPolicy) -> tuple[dict[str, Any] | None, bool, str | None]:
    if policy.prefer_tag:
        block, _failure = _block_by_number(transport, source_id, policy.prefer_tag)
        if block is not None:
            return block, True, None
    head, failure = _quantity_call(transport, source_id, "eth_blockNumber", [])
    if head is None:
        return None, False, failure or "block_unreadable"
    block, failure = _block_by_number(transport, source_id, head)
    if block is None:
        return None, False, failure or "block_unreadable"
    return block, False, None


def _balance_at(transport: Any, source_id: str, address: str, block: Mapping[str, Any]) -> tuple[int | None, str | None, str | None]:
    bound = {"blockHash": block["hash"], "requireCanonical": True}
    raw, failure = _quantity_call(transport, source_id, "eth_getBalance", [address, bound])
    if raw is not None:
        return raw, "eip1898_hash", None
    if failure and not _params_rejected(failure):
        return None, None, failure
    raw, failure = _quantity_call(transport, source_id, "eth_getBalance", [address, hex(int(block["number"]))])
    if raw is None:
        return None, None, failure or "read_error"
    return raw, "fixed_height", None


def _nonce(transport: Any, source_id: str, address: str, block: Mapping[str, Any], binding: str | None) -> str | None:
    params: list[Any]
    if binding == "eip1898_hash":
        params = [address, {"blockHash": block["hash"], "requireCanonical": True}]
    else:
        params = [address, hex(int(block["number"]))]
    raw, failure = _quantity_call(transport, source_id, "eth_getTransactionCount", params)
    if raw is None and binding == "eip1898_hash" and (failure is None or _params_rejected(failure)):
        raw, _failure = _quantity_call(transport, source_id, "eth_getTransactionCount", [address, hex(int(block["number"]))])
    if raw is None:
        return None
    return hex(raw)


def _block_by_number(transport: Any, source_id: str, number: int | str) -> tuple[dict[str, Any] | None, str | None]:
    tag: Any = number if isinstance(number, str) else hex(number)
    body, failure = _rpc(transport, source_id, "eth_getBlockByNumber", [tag, False])
    if body is None:
        return None, failure or "block_unreadable"
    if _rpc_error(body):
        if _params_rejected_body(body):
            return None, "unsupported_block"
        return None, "read_error"
    result = body.get("result")
    if result is None:
        return None, "unsupported_block"
    parsed = _parse_block(result)
    if parsed is None:
        return None, "block_unreadable"
    return parsed, None


def _quantity_call(transport: Any, source_id: str, method: str, params: list[Any]) -> tuple[int | None, str | None]:
    body, failure = _rpc(transport, source_id, method, params)
    if body is None:
        return None, failure or "read_error"
    if _rpc_error(body):
        err = body.get("error")
        message = ""
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
        return None, message or "read_error"
    result = body.get("result")
    if result is None:
        return None, "malformed_balance" if method == "eth_getBalance" else "read_error"
    parsed = hex_quantity(result)
    if parsed is None:
        return None, "malformed_balance" if method == "eth_getBalance" else "malformed_quantity"
    return parsed, None


def _rpc(transport: Any, source_id: str, method: str, params: list[Any]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = transport.request(source_id, evm_rpc(method, params), priority="high")
    except BudgetExhausted as exc:
        return None, exc.reason or "budget_exhausted"
    except Exception as exc:
        reason = getattr(exc, "reason", None)
        return None, reason if isinstance(reason, str) and reason else "read_error"
    if getattr(result, "status", 0) == 429 or getattr(result, "category", "") == "rate_limited":
        return None, "rate_limited"
    body = getattr(result, "body", None)
    if not isinstance(body, dict):
        return None, "read_error"
    return body, None


def _parse_block(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    number = hex_quantity(result.get("number")) if isinstance(result.get("number"), str) else None
    digest = result.get("hash")
    if not isinstance(number, int) or not isinstance(digest, str):
        return None
    text = digest.strip().lower()
    if not text.startswith("0x") or len(text) != 66:
        return None
    try:
        int(text[2:], 16)
    except ValueError:
        return None
    timestamp = hex_quantity(result.get("timestamp")) if isinstance(result.get("timestamp"), str) else None
    return {"hash": text, "number": number, "timestamp": timestamp}


def _rpc_error(body: Mapping[str, Any]) -> bool:
    return "error" in body and body.get("result") is None


def _params_rejected(failure: str) -> bool:
    text = failure.lower()
    needles = ("invalid params", "invalid argument", "cannot unmarshal", "unsupported", "unknown block", "blockhash")
    return any(needle in text for needle in needles)


def _params_rejected_body(body: Mapping[str, Any]) -> bool:
    err = body.get("error")
    message = ""
    code = None
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        code = err.get("code")
    elif isinstance(err, str):
        message = err
    if code in {-32602, -32601}:
        return True
    return _params_rejected(message)


def _asset(entry: Mapping[str, Any], network: str) -> tuple[str, int, str]:
    native = entry.get("native_asset") if isinstance(entry.get("native_asset"), Mapping) else {}
    identity = native.get("identity") if isinstance(native.get("identity"), str) else f"native:{network}"
    decimals = native.get("decimals") if isinstance(native.get("decimals"), int) and not isinstance(native.get("decimals"), bool) else 18
    symbol = native.get("symbol") if isinstance(native.get("symbol"), str) and native.get("symbol") else "NATIVE"
    return identity, decimals, symbol


def _price_source(public_config: Any, transport: Any) -> str | None:
    sources = public_config.document.get("sources")
    if not isinstance(sources, Mapping):
        return None
    for name, source in sources.items():
        if not isinstance(source, Mapping) or source.get("adapter") != "coingecko" or source.get("enabled") is not True:
            continue
        endpoints = getattr(transport, "endpoints", None)
        if endpoints is None:
            return None
        try:
            registered = endpoints.get(name)
        except Exception:
            return None
        if not getattr(registered, "endpoint", ""):
            return None
        return name
    return None


def _config_decimal(value: Any, default: Decimal | None) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation:
            if default is None:
                raise ValueError("threshold") from None
            return default
    else:
        if default is None:
            raise ValueError("threshold")
        return default
    if not number.is_finite() or number < 0:
        if default is None:
            raise ValueError("threshold")
        return default
    return number


def _raw_text(value: str) -> int | None:
    text = value.strip()
    if text.startswith(("0x", "0X")):
        return hex_quantity(text)
    if text.isdigit():
        return int(text)
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _nonce_int(value: str) -> int | None:
    return hex_quantity(value) if value.startswith(("0x", "0X")) else _optional_int(value)


def _now(clock: Callable[[], float]) -> int:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
