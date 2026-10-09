"""Local matching. Publisher text does not choose the chain, the protocol, or the contract.

A DefiLlama pool id resolves only through the versioned mapping. The id itself
need not be an address, and an address-shaped id is not looked up as a contract.
A standing approval is permission exposure. It is not a deposit and it does not
prove promotion eligibility. A hold needs the mapped protocol as well as the
chain and a mapped contract. ``yield_change`` is not produced here; the comparison
step emits that only for a held position.
"""

from __future__ import annotations

from typing import Any

from guardian.yields.models import Holdings, MappedPool, PoolMapping
from guardian.yields.parsers import ParsedRecord

UNMAPPED_STATEMENT = "Missing mapping means no matched candidate."
APPROVAL_STATEMENT = (
    "A standing approval is permission exposure, not proof of a deposit or promotion eligibility. "
    "Eligibility is unknown."
)
CAMPAIGN_STATEMENT = (
    "A trusted feed mentioning a held protocol is not sufficient without a supported campaign or product mapping."
)
SPOOF_STATEMENT = "The campaign contract does not match the trusted mapping."
UNSUPPORTED_STATEMENT = "The campaign is not in the trusted mapping."
HELD_STATEMENT = "Matched a held pool position through the versioned mapping."


def match_record(parsed: ParsedRecord, mapping: PoolMapping, holdings: Holdings) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    if parsed.claim_status != "ok":
        return _blank("unsupported"), (_decision(parsed, "unsupported", UNSUPPORTED_STATEMENT, "unsupported", "none", "not_matched"),)
    if parsed.source == "defillama_pools":
        pool = mapping.pool(parsed.record_id)
        if pool is None:
            return _blank("unmapped"), (_decision(parsed, "unmapped", UNMAPPED_STATEMENT, "unknown", "none", "not_matched"),)
        return _from_pool(pool), _holders(parsed, pool, holdings)
    if not parsed.campaign_id:
        return _blank("campaign_required"), (
            _decision(parsed, "campaign_required", CAMPAIGN_STATEMENT, _coverage(parsed, "unknown"), "none", "not_matched"),
        )
    campaign = mapping.campaign(parsed.campaign_id)
    if campaign is None:
        return _blank("unsupported"), (_decision(parsed, "unsupported", UNSUPPORTED_STATEMENT, "unsupported", "none", "not_matched"),)
    pool = mapping.pool(campaign.pool_id)
    if pool is None:
        return _blank("unsupported"), (_decision(parsed, "unsupported", UNSUPPORTED_STATEMENT, "unsupported", "none", "not_matched"),)
    if parsed.claimed_contracts and not set(parsed.claimed_contracts) <= {item.address for item in pool.contracts}:
        return _blank("spoofed", claimed=parsed.claimed_contracts), (
            _decision(parsed, "spoofed", SPOOF_STATEMENT, "unsupported", "none", "not_matched"),
        )
    return _from_pool(pool, claimed=parsed.claimed_contracts), _holders(parsed, pool, holdings)


def _holders(parsed: ParsedRecord, pool: MappedPool, holdings: Holdings) -> tuple[dict[str, Any], ...]:
    matches: list[dict[str, Any]] = []
    held: set[str] = set()
    for position in holdings.positions:
        if not _position_holds(
            position.chain, position.protocol, position.contracts, position.raw_amount, pool,
        ):
            continue
        held.add(position.wallet_id)
        matches.append(_decision(
            parsed,
            "held_position",
            HELD_STATEMENT,
            "partial",
            "position",
            "held_position",
            wallet_id=position.wallet_id,
            pool_id=pool.pool_id,
        ))
    mapped = {item.address for item in pool.contracts}
    for approval in holdings.approvals:
        if not approval.active or approval.wallet_id in held or approval.chain != pool.chain:
            continue
        if approval.spender not in mapped and approval.token not in mapped:
            continue
        matches.append(_decision(
            parsed,
            "approval_only",
            APPROVAL_STATEMENT,
            "unknown",
            "approval",
            "unknown",
            wallet_id=approval.wallet_id,
            pool_id=pool.pool_id,
        ))
    return tuple(matches)


def _position_holds(
    chain: str,
    protocol: str,
    contracts: tuple[str, ...],
    raw_amount: int,
    pool: MappedPool,
) -> bool:
    if raw_amount <= 0 or chain != pool.chain or protocol != pool.protocol:
        return False
    mapped = {item.address for item in pool.contracts}
    return any(address in mapped for address in contracts)


def _from_pool(pool: MappedPool, claimed: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "pool_id": pool.pool_id,
        "chain": pool.chain,
        "protocol": pool.protocol,
        "contracts": [
            {"role": item.role, "address": item.address, "mapped": True}
            for item in pool.contracts
        ],
        "mapping_status": "mapped",
        "claimed_contracts": list(claimed),
    }


def _blank(status: str, claimed: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "pool_id": None,
        "chain": None,
        "protocol": None,
        "contracts": [],
        "mapping_status": status,
        "claimed_contracts": list(claimed),
    }


def _coverage(parsed: ParsedRecord, default: str) -> str:
    if parsed.read_coverage == "not_observable":
        return "not_observable"
    return default


def _decision(
    parsed: ParsedRecord,
    kind: str,
    statement: str,
    coverage: str,
    via: str,
    eligibility: str,
    wallet_id: str | None = None,
    pool_id: str | None = None,
) -> dict[str, Any]:
    if parsed.read_coverage == "not_observable" and kind == "campaign_required":
        coverage = "not_observable"
    return {
        "kind": kind,
        "source": parsed.source,
        "record_id": parsed.record_id,
        "pool_id": pool_id,
        "wallet_id": wallet_id,
        "eligibility": eligibility,
        "via": via,
        "yield_change": False,
        "statement": statement,
        "mapping_status": kind if kind in {"unmapped", "spoofed", "campaign_required", "unsupported"} else "mapped",
        "read_coverage": coverage,
    }
