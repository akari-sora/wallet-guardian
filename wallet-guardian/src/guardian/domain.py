"""Pure monitoring rules: discovery, accounting, claims, alerts, news, and risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable

from guardian.amounts import decimal_or_none, format_units, parse_raw
from guardian.errors import PolicyError, SecurityError, StaleSnapshot

QUALITY = {"COMPLETE", "PARTIAL", "STALE", "UNKNOWN", "ERROR", "UNSUPPORTED"}
SEVERITY = {"CRITICAL", "HIGH", "ACTION", "INFO"}


@dataclass(frozen=True)
class Leg:
    leg_id: str
    kind: str
    chain_id: int
    token_key: str
    symbol: str
    raw_amount: int
    decimals: int
    price: Decimal | None
    receipt_id: str | None = None
    consumes: tuple[str, ...] = ()


@dataclass
class Valuation:
    status: str
    net: Decimal | None
    priced_subtotal: Decimal
    unpriced_leg_ids: list[str]
    debt_raw_present: bool


def value_legs(legs: Iterable[Leg]) -> Valuation:
    rows = list(legs)
    consumed = {item for leg in rows for item in leg.consumes}
    priced = Decimal(0)
    unpriced: list[str] = []
    debt = False
    for leg in rows:
        if leg.kind == "receipt" and leg.leg_id in consumed:
            continue
        if leg.kind == "debt":
            debt = True
        if leg.price is None:
            unpriced.append(leg.leg_id)
            continue
        units = Decimal(leg.raw_amount) / (Decimal(10) ** leg.decimals)
        signed = -units if leg.kind == "debt" else units
        priced += signed * leg.price
    return Valuation(
        status="PARTIAL" if unpriced else "COMPLETE",
        net=None if unpriced else priced,
        priced_subtotal=priced,
        unpriced_leg_ids=unpriced,
        debt_raw_present=debt,
    )


@dataclass(frozen=True)
class Transfer:
    sender: str
    recipient: str
    raw_amount: int
    gas_wei: int = 0


def tenant_flows(transfers: Iterable[Transfer], confirmed_own: set[str]) -> dict[str, int]:
    external_in = 0
    external_out = 0
    internal = 0
    gas = 0
    for item in transfers:
        gas += item.gas_wei
        own_sender = item.sender in confirmed_own
        own_recipient = item.recipient in confirmed_own
        if own_sender and own_recipient:
            internal += item.raw_amount
        elif own_recipient:
            external_in += item.raw_amount
        elif own_sender:
            external_out += item.raw_amount
    return {
        "external_in": external_in,
        "external_out": external_out,
        "internal": internal,
        "net_external": external_in - external_out,
        "gas_wei": gas,
    }


def observation_quality(
    *,
    observed_at: datetime,
    source_updated_at: datetime | None,
    max_source_age_seconds: int,
    provider_status: str,
) -> str:
    if provider_status in {"ERROR", "PARTIAL", "UNSUPPORTED", "UNKNOWN"}:
        return provider_status
    if source_updated_at is None:
        return "UNKNOWN"
    if source_updated_at.tzinfo is None or observed_at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    age = (observed_at - source_updated_at).total_seconds()
    if age > max_source_age_seconds:
        return "STALE"
    return "COMPLETE"


def merge_observation(previous: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Error or partial payloads must not replace a known balance with a fresh zero."""
    quality = incoming["quality"]
    if quality not in QUALITY:
        raise ValueError("unknown quality")
    if previous and quality in {"ERROR", "PARTIAL"}:
        retained = dict(previous)
        retained["quality"] = "PARTIAL"
        retained["fresh"] = False
        retained["retained_last_known_good"] = True
        retained["incoming_quality"] = quality
        return retained
    merged = dict(incoming)
    merged["fresh"] = quality == "COMPLETE"
    merged["retained_last_known_good"] = False
    return merged


class SnapshotBook:
    def __init__(self) -> None:
        self.latest = 0
        self.rows: dict[int, list[dict[str, Any]]] = {}

    def commit(self, generation: int, items: list[dict[str, Any]]) -> None:
        if generation < self.latest or (self.latest and generation == self.latest):
            raise StaleSnapshot("an older or duplicate run cannot overwrite the snapshot")
        self.latest = generation
        self.rows[generation] = items


def token_identity(tokens: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[tuple[int, str], dict[str, Any]] = {}
    for token in tokens:
        key = (int(token["chain_id"]), str(token["contract"]).lower())
        found.setdefault(key, token)
    return list(found.values())


def collect_pages(fetch: Callable[[str | None], tuple[list[Any], str | None]]) -> list[Any]:
    items: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page, nxt = fetch(cursor)
        items.extend(page)
        if not nxt or nxt in seen:
            return items
        seen.add(nxt)
        cursor = nxt


def interpret_provider_body(status: int, body: dict[str, Any] | None) -> dict[str, Any]:
    if status != 200 or body is None:
        return {"quality": "ERROR", "balances": None, "complete": False}
    partial = (body.get("error") or {}).get("partialErrors") if isinstance(body.get("error"), dict) else None
    token_errors = [
        item for item in body.get("tokens") or [] if isinstance(item, dict) and item.get("error")
    ]
    if partial or token_errors:
        return {"quality": "PARTIAL", "balances": body.get("tokens"), "complete": False}
    return {"quality": "COMPLETE", "balances": body.get("tokens"), "complete": True}


def principal_exposure(*, spam: bool, outflow_raw: int) -> bool:
    if outflow_raw > 0:
        return True
    return not spam


def chain_capabilities(*, token_supported: bool, defi_supported: bool) -> dict[str, bool]:
    return {
        "token": token_supported,
        "defi": defi_supported,
        "defi_implied_by_token": False,
    }


def approval_coverage(*, history_start_block: int | None, indexed_from_genesis: bool) -> str:
    if indexed_from_genesis and history_start_block == 0:
        return "WINDOW_COMPLETE"
    return "PARTIAL"


def assess_rpc(
    *,
    expected_chain_id: int,
    reported_chain_id: int,
    block_time: datetime,
    now: datetime,
    max_lag_seconds: int,
    hosts_share_backend: bool,
) -> dict[str, Any]:
    problems: list[str] = []
    if reported_chain_id != expected_chain_id:
        problems.append("chain_id_mismatch")
    if (now - block_time).total_seconds() > max_lag_seconds:
        problems.append("tip_stale")
    if hosts_share_backend:
        problems.append("hosts_share_backend")
    return {"ok": not problems, "problems": problems}


def normalize_hyperliquid(
    *,
    clearinghouse: dict[str, Any],
    spot: dict[str, Any],
    subaccounts: list[Any] | None,
    mode: str,
) -> dict[str, Any]:
    positions = clearinghouse.get("assetPositions") or []
    balances = spot.get("balances") or []
    master_empty = not positions and not balances
    spot_value = decimal_or_none(spot.get("portfolio_value"))
    perp_value = decimal_or_none((clearinghouse.get("marginSummary") or {}).get("accountValue"))
    combined: Decimal | None
    if mode == "standard" and spot_value is not None and perp_value is not None:
        combined = spot_value + perp_value
    else:
        combined = None
    return {
        "master_empty": master_empty,
        "master_empty_means_all_zero": False,
        "subaccounts_known": subaccounts is not None,
        "subaccount_count": None if subaccounts is None else len(subaccounts),
        "mode": mode,
        "spot_value": None if spot_value is None else format(spot_value, "f"),
        "perp_value": None if perp_value is None else format(perp_value, "f"),
        "combined_value": None if combined is None else format(combined, "f"),
        "unified_double_count_avoided": mode != "standard",
    }


def erc4626_status(*, preview_assets: int, max_withdraw: int) -> dict[str, Any]:
    return {
        "preview_assets": str(preview_assets),
        "max_withdraw": str(max_withdraw),
        "immediately_withdrawable": max_withdraw > 0,
        "preview_is_proof_of_liquidity": False,
    }


def health_factor(
    *,
    protocol: str,
    debt_value: Decimal | None,
    collateral_threshold_value: Decimal | None,
    oracle_stale: bool,
) -> dict[str, Any]:
    if protocol != "aave-v3":
        return {"semantic": "UNSUPPORTED_FORMULA", "hf": None}
    if oracle_stale:
        return {"semantic": "ORACLE_STALE", "hf": None}
    if debt_value is None or collateral_threshold_value is None:
        return {"semantic": "UNKNOWN", "hf": None}
    if debt_value == 0:
        return {"semantic": "NO_DEBT", "hf": None}
    ratio = collateral_threshold_value / debt_value
    return {
        "semantic": "LIQUIDATABLE" if ratio < 1 else "ABOVE_ONE",
        "hf": format(ratio, "f"),
    }


def label_outgoing(*, confirmed_by_user: bool, strong_evidence: bool) -> str:
    if confirmed_by_user or strong_evidence:
        return "needs_review"
    return "unidentified_outgoing"


def label_delegation(*, delegated: bool, expected_by_user: bool) -> str:
    if not delegated:
        return "none"
    if expected_by_user:
        return "expected_delegation"
    return "delegation_review"


def classify_approval(*, allowance: int, expired: bool, permit_submitted: bool) -> dict[str, Any]:
    if allowance == 0:
        state = "zero"
    elif expired:
        state = "expired"
    else:
        state = "active"
    return {
        "state": state,
        "permit_submitted": permit_submitted,
        "unsigned_permit_visible": False,
    }


def bridge_progress(*, source_confirmed: bool, destination_settled: bool) -> str:
    if source_confirmed and not destination_settled:
        return "source_confirmed_destination_pending"
    if destination_settled:
        return "destination_settled"
    return "source_pending"


def aster_exit_stages() -> list[dict[str, str]]:
    return [
        {"stage": "asbnb_to_slisbnb", "protocol": "aster", "window": "aster"},
        {"stage": "slisbnb_to_bnb", "protocol": "lista", "window": "lista"},
    ]


def lst_ratio(*, market_raw: int, redeem_raw: int) -> dict[str, Any]:
    return {
        "market_raw": str(market_raw),
        "redeem_raw": str(redeem_raw),
        "treated_as_one_to_one": False,
        "same": market_raw == redeem_raw,
    }


def price_anomaly(*, deviation_bps: int, liquidity_known: bool, persistent: bool) -> str:
    if not liquidity_known or not persistent or deviation_bps < 500:
        return "unconfirmed"
    return "review_depeg"


@dataclass(frozen=True)
class WithdrawalRequest:
    uuid: int
    amount_in_slis_bnb: int
    start_time: int
    is_claimable: bool
    bnb_amount: int


def classify_lista_request(
    request: WithdrawalRequest | None,
    *,
    age_days: float | None,
    claim_evidence: bool,
) -> str:
    if request is None:
        if claim_evidence:
            return "CLAIMED"
        return "MISSING_NO_CLAIM_EVIDENCE"
    if age_days is not None and age_days >= 7 and not request.is_claimable:
        return "WAITING"
    if request.is_claimable:
        return "CLAIMABLE"
    return "WAITING"


def apply_swap_pop(requests: list[WithdrawalRequest], idx: int) -> list[WithdrawalRequest]:
    rows = list(requests)
    if idx < 0 or idx >= len(rows):
        raise PolicyError("idx is outside the current request list")
    if idx != len(rows) - 1:
        rows[idx] = rows[-1]
    rows.pop()
    return rows


def resolve_idx(requests: list[WithdrawalRequest], uuid: int, *, cached_idx: int | None = None) -> dict[str, Any]:
    matches = [index for index, row in enumerate(requests) if row.uuid == uuid]
    if len(matches) != 1:
        raise PolicyError("request uuid was not found exactly once; refusing amount or stale idx matching")
    current = matches[0]
    return {
        "idx": current,
        "stale_cache_rejected": cached_idx is not None and cached_idx != current,
        "matched_by": "uuid",
    }


def match_by_amount_forbidden(requests: list[WithdrawalRequest], amount: int) -> dict[str, Any]:
    hits = [row.uuid for row in requests if row.amount_in_slis_bnb == amount]
    return {"matched": False, "reason": "amount_alone_forbidden", "candidate_uuids": [str(item) for item in hits]}


def external_watcher_risk(watcher: dict[str, Any] | None) -> dict[str, Any]:
    if not watcher:
        return {"duplicate_risk": False, "gap_risk": False, "health": "ABSENT"}
    health = watcher.get("current_health", "UNVERIFIED")
    verified = health == "VERIFIED_HEALTHY"
    return {
        "duplicate_risk": not verified,
        "gap_risk": not verified and bool(watcher.get("handoff_requires_confirmation", True)),
        "health": health,
        "cancel_external_task": False,
        "suppress_expected_only": bool(watcher.get("suppress_expected_claim_duplicate")),
        "still_alert_abnormal": bool(watcher.get("alert_for_abnormal_delay_failure_loss_or_protocol_risk", True)),
    }


def semantic_key(tenant: str, chain: str, subject: str, incident: str, transition: str) -> str:
    return "|".join([tenant, chain, subject, incident, transition])


class AlertBook:
    def __init__(self) -> None:
        self.sent: dict[str, dict[str, Any]] = {}

    def emit(self, alert: dict[str, Any]) -> dict[str, Any] | None:
        if alert.get("severity") not in SEVERITY:
            raise ValueError("severity is not recognized")
        key = alert["dedup_key"]
        previous = self.sent.get(key)
        if previous and not alert.get("update"):
            return None
        self.sent[key] = alert
        return alert


def format_alert(alert: dict[str, Any]) -> str:
    from guardian.security.display_text import escape_plain

    def show(key: str, default: str = "") -> str:
        value = alert.get(key, default)
        if value is None:
            value = default
        return escape_plain(str(value))

    price = "未知" if alert.get("price") is None else escape_plain(str(alert["price"]))
    price_time = escape_plain(str(alert.get("price_time") or "未知"))
    return "\n".join(
        [
            f"{show('severity_label')}／{show('confidence')}｜{show('wallet_label')}｜{show('chain_label')}｜{show('subject')}",
            show("what"),
            f"數量 {show('amount_display')} {show('unit')}；估計價值 {price}（價格時間 {price_time}）",
            f"相關原因：{show('why')}",
            f"建議：{show('step')}",
            f"事件時間 {show('event_time')}；觀測時間 {show('observed_at')}",
            f"來源：{show('source')}；限制：{show('limits')}",
        ]
    )


def baseline_alerts(items: Iterable[dict[str, Any]], *, is_baseline: bool) -> list[dict[str, Any]]:
    if not is_baseline:
        return []
    return [item for item in items if item.get("severity") in {"CRITICAL", "HIGH", "ACTION"}]


def notify_decision(
    *,
    changes: list[dict[str, Any]],
    sources_healthy: bool,
    blind_alert_open: bool,
    recovered: bool,
) -> list[dict[str, Any]]:
    if not sources_healthy:
        if blind_alert_open:
            return []
        return [{"kind": "monitoring_blind", "severity": "HIGH"}]
    if recovered:
        return [{"kind": "monitoring_recovered", "severity": "INFO"}]
    return list(changes)


def monitoring_mode(*, scheduler_verified: bool, next_run_read_back: bool) -> str:
    if scheduler_verified and next_run_read_back:
        return "MONITORING_ACTIVE"
    return "RUN_ONCE"


@dataclass
class IngestResult:
    duplicate: bool
    alerts: list[dict[str, Any]] = field(default_factory=list)
    acked: bool = False


class EventIngest:
    def __init__(self) -> None:
        self.events: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.alerts = AlertBook()

    def handle(self, event: dict[str, Any], *, persisted: bool) -> IngestResult:
        if not persisted:
            raise RuntimeError("refuse to ack before the event is stored")
        identity = (event["chain_id"], event["tx_hash"], event["log_index"], event["kind"])
        if identity in self.events:
            return IngestResult(duplicate=True, acked=True)
        self.events[identity] = event
        alert = self.alerts.emit(
            {
                "severity": event.get("severity", "INFO"),
                "dedup_key": event["dedup_key"],
                "update": bool(event.get("update")),
            }
        )
        return IngestResult(duplicate=False, alerts=[alert] if alert else [], acked=True)

    def reorg(self, block_number: int, new_hash: str) -> list[dict[str, Any]]:
        corrections = []
        for event in self.events.values():
            if event.get("block_number") == block_number and event.get("block_hash") != new_hash:
                event["reorged"] = True
                corrections.append(dict(event))
        return corrections


def backfill_window(cursor: int, tip: int, overlap: int) -> range:
    start = max(0, cursor - overlap)
    return range(start, tip + 1)


class Outbox:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def enqueue(self, message: dict[str, Any]) -> int:
        self.rows.append({"message": message, "status": "pending"})
        return len(self.rows) - 1

    def ambiguous(self, index: int) -> None:
        self.rows[index]["status"] = "delivery_unknown"

    def reconcile(self, index: int, *, found: bool) -> str:
        status = "sent" if found else "retry"
        self.rows[index]["status"] = status
        return status


OFFICIAL_DOMAINS = {
    "lista": {"docs.bsc.lista.org", "lista.org", "www.lista.org"},
    "aster": {"docs.asterdex.com"},
    "aave": {"aave.com", "docs.aave.com"},
}


def classify_news(item: dict[str, Any]) -> str:
    if item.get("syndicated_from"):
        return "reprint"
    domain = str(item.get("domain") or "")
    official = {host for hosts in OFFICIAL_DOMAINS.values() for host in hosts}
    if domain in official:
        return "official"
    return "rumor"


def news_should_alert(
    item: dict[str, Any],
    *,
    has_exposure: bool,
    has_active_allowance: bool,
    seen_incidents: set[str],
) -> bool:
    if not has_exposure and not has_active_allowance:
        return False
    kind = classify_news(item)
    if kind == "rumor" and not item.get("direct_threat"):
        return False
    incident = str(item["incident_id"])
    if incident in seen_incidents and not item.get("upgrade"):
        return False
    return True


def ingest_untrusted(text: str) -> dict[str, Any]:
    return {"stored_as": "untrusted_evidence", "text_length": len(text or ""), "commands": []}


def sanitize_share(document: dict[str, Any]) -> dict[str, Any]:
    banned = {
        "wallets",
        "addresses",
        "address",
        "api_keys",
        "api_key",
        "secret",
        "secrets",
        "recipients",
        "recipient",
        "notifications",
        "database_path",
        "fixtures",
        "memory",
        "seed",
        "private",
        "token",
    }
    cleaned: dict[str, Any] = {}
    for key, value in document.items():
        if key in banned:
            continue
        if isinstance(value, dict):
            cleaned[key] = sanitize_share(value)
        elif isinstance(value, str):
            cleaned[key] = _strip_addresses(value)
        else:
            cleaned[key] = value
    return cleaned


def _strip_addresses(value: str) -> str:
    import re

    return re.sub(r"0x[a-fA-F0-9]{40}", "0xREDACTED", value)


def promote_decoder(*, tests_passed: bool, reviewed: bool, production: bool) -> dict[str, bool]:
    if production and not (tests_passed and reviewed):
        raise SecurityError("a new decoder cannot touch production state before tests and review")
    enabled = tests_passed and reviewed
    return {"enabled": enabled, "production": production and enabled}


def fee_label(wei: int | None) -> dict[str, Any]:
    if wei is None:
        return {"label": "費用未知", "is_free": False, "known": False}
    return {"label": f"估計 {format_units(wei, 18)} BNB", "is_free": False, "known": True}


def gas_budget(*, native_balance: int, fee: int, incoming_from_tx: int) -> dict[str, Any]:
    del incoming_from_tx
    return {
        "sufficient": native_balance >= fee,
        "available_wei": str(native_balance),
        "counted_incoming_claim_as_gas": False,
    }


def exact_approval(required: int) -> int:
    if required < 0 or required >= 2**256 - 1:
        raise PolicyError("refusing an unlimited approval amount")
    return required


def revoke_effect() -> dict[str, bool]:
    return {"requires_new_transaction": True, "recovers_already_transferred": False}


def initial_history_notice(*, already_claimable_before_start: bool) -> str:
    if already_claimable_before_start:
        return "already_actionable_at_baseline"
    return "baseline"


def diff_items(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[str]]:
    def key(item: dict[str, Any]) -> str:
        return "|".join([str(item["chain_id"]), str(item["position_id"])])

    old = {key(item): item for item in previous}
    new = {key(item): item for item in current}
    changed, added, removed = [], [], []
    for ident, item in new.items():
        if ident not in old:
            added.append(ident)
        elif old[ident].get("raw_amount") != item.get("raw_amount") or old[ident].get("status") != item.get("status"):
            changed.append(ident)
    for ident in old:
        if ident not in new:
            removed.append(ident)
    return {"changed": changed, "added": added, "removed": removed}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
