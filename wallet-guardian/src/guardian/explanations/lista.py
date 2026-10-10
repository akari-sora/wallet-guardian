"""Lista StakeManager inflow explanation.

The packaged bytecode hash is a specification pin. It is not a live code reading.
``claimWithdrawFor`` is decoded and never submitted. Views are the allowlisted set.
"""

from __future__ import annotations

from typing import Any, Mapping

from eth_abi import decode, encode
from eth_hash.auto import keccak
from eth_utils import function_signature_to_4byte_selector

from guardian.chains.evm.accounting import normalize_address, pad_address
from guardian.explanations.discovery import (
    FoundLog,
    Slot,
    _data_words,
    _parse_log,
    _topic_address,
    discover_logs,
    lookback_bounds,
    reconstruct_genesis_queue,
    replay,
    request_identity,
    union_logs,
    upgraded_topic,
)
from guardian.explanations.models import (
    ERC1967_IMPLEMENTATION_SLOT,
    default_facts,
    document,
    empty_amounts,
    empty_parties,
    observed_raw_amount,
    settlement_ratio,
)
from guardian.explanations.registry import ExplainerManifest
from guardian.explanations.rpc import ExplanationRpc

_VIEWS = {
    "convertSnBnbToBnb(uint256)": (("uint256",), ("uint256",)),
    "getBotUndelegateRequest(uint256)": (("uint256",), ("(uint256,uint256,uint256,uint256)",)),
    "getUserRequestStatus(address,uint256)": (("address", "uint256"), ("bool", "uint256")),
    "getUserWithdrawalRequests(address)": (("address",), ("(uint256,uint256,uint256)[]",)),
    "hasRole(bytes32,address)": (("bytes32", "address"), ("bool",)),
}
_BOT = keccak(b"BOT")


class ListaExplainer:
    """Investigate a BSC native increase. A match is not proof of the explanation."""

    explainer_id = "lista-stakemanager"
    version = "1"

    def matches(self, alert: Mapping[str, Any]) -> bool:
        if alert.get("network") != "bsc-mainnet" or alert.get("asset") != "evm-native:56":
            return False
        if alert.get("kind") == "balance_delta" and alert.get("direction") == "increase":
            return True
        return (
            alert.get("transfer_origin") == "evm_internal"
            and alert.get("direction") == "in"
            and alert.get("kind") in {"native_eth", "native_unit", "asset_usd"}
        )

    def explain(self, ctx: Any) -> dict[str, Any]:
        alert = ctx.alert
        manifest: ExplainerManifest = ctx.manifest
        rpc: ExplanationRpc = ctx.rpc
        account = normalize_address(ctx.account)
        facts = default_facts()
        gaps: list[str] = []
        contradictions: list[str] = []
        established: list[str] = []
        parties = empty_parties()
        amounts = empty_amounts()
        refs: list[str] = []
        event_id = str(alert.get("dedup_key") or alert.get("identity") or "")
        if _internal_credit(alert):
            return _explain_transfer(
                ctx, alert, manifest, rpc, account, facts, gaps, contradictions, established,
                parties, amounts, refs, event_id,
            )
        old, new = _checkpoints(alert)
        checkpoints = {
            "new": "" if new is None else new[1],
            "old": "" if old is None else old[1],
        }
        if account is None or old is None or new is None:
            return _sealed(
                rpc,
                event_id, manifest, "UNVERIFIED", facts, ["malformed_event"], [], [],
                parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, None,
            )
        chain, chain_error = _chain_id(rpc)
        if chain_error:
            return _sealed(
                rpc,
                event_id, manifest, "UNVERIFIED", facts, [chain_error], [], [],
                parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, chain_error,
            )
        if chain != manifest.chain_id():
            return _sealed(
                rpc,
                event_id, manifest, "UNVERIFIED", facts, ["wrong_chain"], [], [],
                parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, None,
            )
        old_header, old_error = _header(rpc, old[0])
        new_header, new_error = _header(rpc, new[0])
        if old_error or new_error or old_header is None or new_header is None:
            code = old_error or new_error or "checkpoint_unverified"
            return _sealed(
                rpc,
                event_id, manifest, "UNVERIFIED", facts, [code], [], [],
                parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, code,
            )
        if old_header["hash"] != old[1] or new_header["hash"] != new[1]:
            return _sealed(
                rpc,
                event_id, manifest, "UNVERIFIED", facts, ["checkpoint_hash_mismatch"], [], [],
                parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, "reorg",
            )
        return _finish_span(
            ctx, alert, manifest, rpc, account, facts, gaps, contradictions, established,
            parties, amounts, refs, event_id, old, new, checkpoints, None, None,
        )


def _internal_credit(alert: Mapping[str, Any]) -> bool:
    return (
        alert.get("transfer_origin") == "evm_internal"
        and alert.get("direction") == "in"
        and alert.get("kind") in {"native_eth", "native_unit", "asset_usd"}
    )


def _explain_transfer(
    ctx: Any,
    alert: Mapping[str, Any],
    manifest: ExplainerManifest,
    rpc: ExplanationRpc,
    account: str | None,
    facts: dict[str, str],
    gaps: list[str],
    contradictions: list[str],
    established: list[str],
    parties: dict[str, str | None],
    amounts: dict[str, str | None],
    refs: list[str],
    event_id: str,
) -> dict[str, Any]:
    """Explain one internal native credit from its block, transaction, and amount.

    The alert has no balance checkpoint. None is invented. The residual uses
    ``raw_amount`` even when a later read fails.
    """
    checkpoints = {"new": "", "old": ""}
    block = _transfer_block(alert)
    tx_id = _transfer_tx(alert)
    amount = _transfer_amount(alert)

    def stop(code: str, retry: str | None) -> dict[str, Any]:
        return _sealed(
            rpc, event_id, manifest, "UNVERIFIED", facts, [code], [], [],
            parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, retry,
        )

    if account is None or block is None or tx_id is None or amount is None:
        return stop("malformed_event", None)
    chain, chain_error = _chain_id(rpc)
    if chain_error:
        return stop(chain_error, chain_error)
    if chain != manifest.chain_id():
        return stop("wrong_chain", None)
    header, header_error = _header(rpc, block)
    if header_error or header is None or not header.get("parent"):
        return stop(header_error or "checkpoint_unverified", header_error or "checkpoint_unverified")
    parent_header, parent_error = _header(rpc, block - 1)
    if parent_error or parent_header is None:
        return stop(parent_error or "checkpoint_unverified", parent_error or "checkpoint_unverified")
    if parent_header["hash"] != header["parent"]:
        return stop("parent_hash_mismatch", None)
    old = (block - 1, str(parent_header["hash"]))
    new = (block, str(header["hash"]))
    # The parent is only the queue read. It is not a balance observation.
    anchors = [{"block_number": new[0], "block_hash": new[1]}]
    return _finish_span(
        ctx, alert, manifest, rpc, account, facts, gaps, contradictions, established,
        parties, amounts, refs, event_id, old, new, checkpoints, amount, tx_id, anchors,
    )


def _finish_span(
    ctx: Any,
    alert: Mapping[str, Any],
    manifest: ExplainerManifest,
    rpc: ExplanationRpc,
    account: str,
    facts: dict[str, str],
    gaps: list[str],
    contradictions: list[str],
    established: list[str],
    parties: dict[str, str | None],
    amounts: dict[str, str | None],
    refs: list[str],
    event_id: str,
    old: tuple[int, str],
    new: tuple[int, str],
    checkpoints: dict[str, str],
    bound_amount: int | None,
    only_tx: str | None,
    evidence_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def seal(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _sealed(*args, evidence_blocks=evidence_blocks, **kwargs)

    logs, log_error = _collect_logs(ctx, rpc, manifest, account, old[0], new[0])
    if log_error and log_error != "truncated_logs":
        extra = _empty_queue_gap(rpc, manifest, account, new[1], gaps)
        gaps.extend(extra)
        gaps.append(log_error)
        return seal(
            rpc, event_id, manifest, "UNVERIFIED", facts, gaps, [], [],
            parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, log_error,
        )
    if log_error == "truncated_logs":
        gaps.append("truncated_logs")
    _remember_requests(ctx, logs)
    claims, conflict = _unique_claims([item for item in logs if item.kind == "claim"])
    if only_tx is not None:
        claims = [item for item in claims if item.tx_id == only_tx]
    if conflict:
        gaps.append("claim_evidence_mismatch")
    if not claims:
        if "truncated_logs" in gaps:
            return seal(
                rpc, event_id, manifest, "UNVERIFIED", facts, gaps, [], [],
                parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, "truncated_logs",
            )
        gaps.append("no_claim_in_interval")
        gaps.extend(_empty_queue_gap(rpc, manifest, account, new[1], gaps))
        return seal(
            rpc, event_id, manifest, "UNVERIFIED", facts, gaps, [], [],
            parties, amounts, [], _residual(alert, 0), refs, checkpoints, None, None, None,
        )
    effects: list[dict[str, Any]] = []
    identities: list[str] = []
    calculation: str | None = None
    request_uuid: str | None = None
    explained = 0
    verdicts: list[str] = []
    for claim in claims:
        outcome = _one_claim(
            rpc, manifest, account, claim, logs, old[0], old[1], facts, gaps, contradictions,
            established, parties, amounts, refs, ctx,
        )
        calculation = outcome["calculation"] or calculation
        if outcome["effect"] is not None:
            effects.append(outcome["effect"])
            explained += int(outcome["effect"]["raw_amount"])
        if outcome["identity"]:
            identities.append(outcome["identity"])
        if outcome.get("uuid"):
            request_uuid = str(outcome["uuid"])
        verdicts.append(str(outcome["verdict"]))
        if outcome["verdict"] == "NO_MATCH":
            return seal(
                rpc, event_id, manifest, "NO_MATCH", facts, gaps, contradictions, established,
                parties, amounts, effects, _residual(alert, explained), refs, checkpoints,
                identities[0] if len(identities) == 1 else None, request_uuid, None, calculation,
            )
    if bound_amount is not None:
        payment = amounts.get("actual_claim_payment")
        if isinstance(payment, str) and payment.lstrip("-").isdigit() and int(payment) != bound_amount:
            gaps.append("amount_inconsistent")
    if verdicts and all(item == "MATCHES_OWN_REQUEST" for item in verdicts) and not gaps:
        verdict = "MATCHES_OWN_REQUEST"
    elif any(item in {"MATCHES_OWN_REQUEST", "PARTIAL"} for item in verdicts):
        verdict = "PARTIAL"
    else:
        verdict = "UNVERIFIED"
    retry = None
    if "rate_limited" in gaps:
        retry = "rate_limited"
    elif "deadline" in gaps:
        retry = "deadline"
    elif "provider_call_budget" in gaps:
        retry = "provider_call_budget"
    identity = identities[0] if len(identities) == 1 else None
    return seal(
        rpc, event_id, manifest, verdict, facts, gaps, contradictions, established,
        parties, amounts, effects, _residual(alert, explained), refs, checkpoints,
        identity, request_uuid, retry, calculation,
    )


def _one_claim(
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    account: str,
    claim: FoundLog,
    logs: list[FoundLog],
    old_block: int,
    old_hash: str,
    facts: dict[str, str],
    gaps: list[str],
    contradictions: list[str],
    established: list[str],
    parties: dict[str, str | None],
    amounts: dict[str, str | None],
    refs: list[str],
    ctx: Any,
) -> dict[str, Any]:
    tx, tx_error = rpc.call("eth_getTransactionByHash", [claim.tx_id])
    receipt, receipt_error = rpc.call("eth_getTransactionReceipt", [claim.tx_id])
    refs.append(f"tx:{claim.tx_id}")
    refs.append(f"receipt:{claim.tx_id}")
    if tx_error or receipt_error or not isinstance(tx, dict) or not isinstance(receipt, dict):
        gaps.append(tx_error or receipt_error or "read_error")
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    identity_gap = _tx_receipt_identity(tx, receipt, claim)
    if identity_gap:
        gaps.append(identity_gap)
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    header, header_error = _header(rpc, claim.block_number)
    if header_error or header is None or not header.get("parent"):
        facts["implementation_at_execution"] = "unverified"
        gaps.append(header_error or "implementation_at_execution_unverified")
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    if claim.block_hash and claim.block_hash != header["hash"]:
        gaps.append("claim_evidence_mismatch")
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    checkpoint_gap = _checkpoint_identity(tx, receipt, header["hash"])
    if checkpoint_gap:
        gaps.append(checkpoint_gap)
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    submitter = normalize_address(tx.get("from"))
    parties["tx_submitter"] = submitter
    parties["request_account"] = account
    to = normalize_address(tx.get("to"))
    caller, calldata_user, calldata_index = _decode_call(manifest, tx.get("input"), submitter, to)
    parties["effective_caller"] = caller
    if calldata_user is not None and calldata_user != account:
        contradictions.append("beneficiary_contradiction")
        facts["beneficiary_payment"] = "contradicted"
        return {"verdict": "NO_MATCH", "effect": None, "identity": None, "uuid": None, "calculation": None}
    status_kind = _status_kind(receipt.get("status"))
    if status_kind == "failed":
        contradictions.append("failed_payment")
        facts["beneficiary_payment"] = "contradicted"
        return {"verdict": "NO_MATCH", "effect": None, "identity": None, "uuid": None, "calculation": None}
    if status_kind != "success":
        gaps.append("receipt_status_unavailable")
        facts["beneficiary_payment"] = "unavailable"
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    bound_gap, words = _bound_claim_log(receipt, claim, account, manifest.proxy(), header["hash"])
    if bound_gap or words is None:
        gaps.append(bound_gap or "claim_log_not_in_receipt")
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    logged_index = words[0]
    if calldata_index is None or calldata_index != logged_index:
        gaps.append("request_index_mismatch")
        return {"verdict": "PARTIAL", "effect": None, "identity": None, "uuid": None, "calculation": None}
    interval, impl_gap = _implementation(rpc, manifest, header, claim.tx_index, refs)
    if impl_gap or interval is None:
        facts["implementation_at_execution"] = "unverified"
        gaps.append(impl_gap or "implementation_at_execution_unverified")
        calculation = None
    else:
        facts["implementation_at_execution"] = "verified"
        established.append("implementation_at_execution")
        calculation = str(interval.get("calculation") or "")
    payment = words[1]
    if interval and interval.get("receipt_implies_payment") is True and words == claim.data_words:
        facts["beneficiary_payment"] = "verified"
        parties["payout_recipient"] = account
        established.append("beneficiary_payment")
        amounts["actual_claim_payment"] = str(payment)
    else:
        facts["beneficiary_payment"] = "unavailable"
        gaps.append("payment_not_implied")
        payment = None
    _role(rpc, manifest, header["hash"], submitter, facts)
    link = _link_request(
        rpc, manifest, account, claim, logs, old_block, old_hash, header, calculation, facts, gaps, amounts, refs, ctx,
    )
    identity = link.get("identity")
    uuid = link.get("uuid")
    matched = (
        facts["request_linkage"] == "verified"
        and facts["beneficiary_payment"] == "verified"
        and facts["implementation_at_execution"] == "verified"
        and link.get("reconciled") is True
        and payment is not None
    )
    if matched:
        return {
            "verdict": "MATCHES_OWN_REQUEST",
            "effect": {
                "asset": "evm-native:56",
                "kind": "native_credit",
                "log_index": claim.log_index,
                "raw_amount": str(payment),
                "recipient": account,
                "tx_id": claim.tx_id,
            },
            "identity": identity,
            "uuid": uuid,
            "calculation": calculation,
        }
    return {"verdict": "PARTIAL", "effect": None, "identity": identity, "uuid": uuid, "calculation": calculation}


def _link_request(
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    account: str,
    claim: FoundLog,
    logs: list[FoundLog],
    old_block: int,
    old_hash: str,
    header: dict[str, Any],
    calculation: str | None,
    facts: dict[str, str],
    gaps: list[str],
    amounts: dict[str, str | None],
    refs: list[str],
    ctx: Any,
) -> dict[str, Any]:
    start, start_error = _queue(rpc, manifest, account, old_hash)
    if start_error or start is None:
        facts["request_linkage"] = "unavailable"
        gaps.append(start_error or "historical_state_unavailable")
        return {}
    start_slots = [
        Slot(f"uuid:{uuid}", uuid, slis, None, None, None)
        for uuid, slis, _start in start
    ]
    before = [item for item in logs if item.block_number < claim.block_number]
    _links, queue = replay(start_slots, before)
    parent = str(header["parent"])
    chain_queue, chain_error = _queue(rpc, manifest, account, parent)
    if chain_error or chain_queue is None:
        facts["request_linkage"] = "unavailable"
        gaps.append(chain_error or "historical_state_unavailable")
        return {}
    if len(chain_queue) != len(queue) or any(slot.slis != row[1] for slot, row in zip(queue, chain_queue)):
        facts["request_linkage"] = "ambiguous"
        gaps.append("queue_mismatch")
        return {}
    else:
        for slot, row in zip(queue, chain_queue):
            slot.uuid = row[0]
        _attach_amounts(rpc, manifest, account, parent, queue, calculation, facts, gaps)
    earlier = [
        item for item in logs
        if item.block_number == claim.block_number and (item.tx_index, item.log_index) < (claim.tx_index, claim.log_index)
    ]
    links, _ = replay(queue, [*earlier, claim])
    link = next((item for item in links if item.log.tx_id == claim.tx_id and item.log.log_index == claim.log_index), None)
    if link is None or link.slot is None or link.reason != "linked":
        facts["request_linkage"] = "unavailable" if facts["request_linkage"] != "ambiguous" else "ambiguous"
        gaps.append("request_linkage_missing")
        return {}
    slot = link.slot
    if not slot.tx_id or slot.log_index is None:
        _fill_request_identity(ctx, rpc, manifest, account, old_block, [slot], logs, gaps)
    if not slot.tx_id or slot.log_index is None:
        facts["request_linkage"] = "ambiguous"
        if getattr(ctx, "lookback_cursor", None) == 0:
            gaps.append("request_identity_incomplete")
        else:
            gaps.append("discovery_incomplete")
        return {"uuid": None if slot.uuid is None else str(slot.uuid)}
    if slot.removed or slot.conflict or not slot.block_hash:
        facts["request_linkage"] = "inconsistent"
        gaps.append("request_evidence_conflict")
        return {"uuid": None if slot.uuid is None else str(slot.uuid)}
    facts["request_linkage"] = "verified"
    established_identity = request_identity(manifest.chain_id(), manifest.proxy(), account, slot.tx_id, slot.log_index)
    refs.append(f"request:{established_identity}")
    amounts["requested_slis_amount"] = str(slot.slis)
    request_log = next((item for item in logs if item.tx_id == slot.tx_id and item.log_index == slot.log_index), None)
    if request_log is not None and (request_log.removed or request_log.conflict or not request_log.block_hash):
        facts["request_linkage"] = "inconsistent"
        gaps.append("request_evidence_conflict")
        return {"identity": established_identity, "uuid": None if slot.uuid is None else str(slot.uuid)}
    if request_log is not None and request_log.block_hash != slot.block_hash:
        facts["request_linkage"] = "inconsistent"
        gaps.append("request_evidence_conflict")
        return {"identity": established_identity, "uuid": None if slot.uuid is None else str(slot.uuid)}
    if request_log is not None and request_log.data_words and request_log.data_words[0] != slot.slis:
        facts["request_linkage"] = "inconsistent"
        gaps.append("request_amount_inconsistent")
        return {"identity": established_identity, "uuid": None if slot.uuid is None else str(slot.uuid)}
    payment = claim.data_words[1] if len(claim.data_words) > 1 else None
    expected = slot.bnb
    if calculation == "stored_bnb" and expected is not None:
        amounts["request_time_bnb_quote"] = str(expected)
        amounts["contract_expected_claim_amount"] = str(expected)
        facts["request_time_quote"] = "verified"
    elif calculation == "legacy_share" and expected is not None:
        amounts["contract_expected_claim_amount"] = str(expected)
        facts["request_time_quote"] = "verified"
    else:
        facts["request_time_quote"] = "unavailable"
        gaps.append("request_time_quote_unavailable")
    amounts["effective_settlement_ratio"] = settlement_ratio(payment, slot.slis)
    reconciled = expected is not None and payment is not None and expected == payment
    if expected is not None and payment is not None and expected != payment:
        facts["request_linkage"] = "inconsistent"
        gaps.append("amount_inconsistent")
        reconciled = False
    return {
        "identity": established_identity,
        "reconciled": reconciled and facts["request_linkage"] == "verified",
        "uuid": None if slot.uuid is None else str(slot.uuid),
    }


def _attach_amounts(
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    account: str,
    block_hash: str,
    queue: list[Slot],
    calculation: str | None,
    facts: dict[str, str],
    gaps: list[str],
) -> None:
    for index, slot in enumerate(queue):
        if calculation == "legacy_share":
            amount, error = _legacy_amount(rpc, manifest, slot, block_hash)
        else:
            amount, error = _status_amount(rpc, manifest, account, index, block_hash)
        if error:
            gaps.append(error)
            continue
        slot.bnb = amount
    if calculation == "stored_bnb":
        facts["request_time_quote"] = "verified" if all(slot.bnb is not None for slot in queue) or not queue else facts["request_time_quote"]


def _implementation(
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    header: dict[str, Any],
    tx_index: int,
    refs: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    parent = str(header["parent"])
    claim_hash = str(header["hash"])
    start, start_error = _storage(rpc, manifest.proxy(), parent)
    end, end_error = _storage(rpc, manifest.proxy(), claim_hash)
    if start_error or end_error or start is None or end is None:
        return None, "implementation_at_execution_unverified"
    upgrades, upgrade_error = _upgrades(rpc, manifest.proxy(), header["number"], claim_hash)
    if upgrade_error or upgrades is None:
        return None, "implementation_at_execution_unverified"
    if any(item[0] == tx_index for item in upgrades):
        return None, "implementation_at_execution_unverified"
    impl = start
    for upgrade_index, _log_index, upgraded in upgrades:
        if upgrade_index < tx_index:
            impl = upgraded
    terminal = start
    for _upgrade_index, _log_index, upgraded in upgrades:
        terminal = upgraded
    if terminal != end:
        return None, "implementation_at_execution_unverified"
    code, code_error = rpc.call("eth_getCode", [impl, rpc.historical(claim_hash)])
    proxy_code, proxy_error = rpc.call("eth_getCode", [manifest.proxy(), rpc.historical(claim_hash)])
    if code_error or proxy_error or not isinstance(code, str) or not isinstance(proxy_code, str):
        return None, "implementation_at_execution_unverified"
    code_hash = _keccak_hex(code)
    proxy_hash = _keccak_hex(proxy_code)
    if code_hash is None or proxy_hash is None:
        return None, "implementation_at_execution_unverified"
    refs.append(f"code:{impl}:{claim_hash}:{code_hash}")
    if proxy_hash != manifest.proxy_code_hash():
        return None, "proxy_code_mismatch"
    for row in manifest.intervals():
        if str(row.get("implementation") or "").lower() != impl:
            continue
        if str(row.get("code_hash") or "").lower() != code_hash:
            continue
        start_block = int(row.get("from_block") or 0)
        end_block = row.get("to_block")
        if header["number"] < start_block:
            continue
        if end_block is not None and header["number"] > int(end_block):
            continue
        return row, None
    return None, "code_hash_mismatch"


def _upgrades(
    rpc: ExplanationRpc,
    proxy: str,
    block: int,
    header_hash: str,
) -> tuple[list[tuple[int, int, str]] | None, str | None]:
    rows, error = rpc.call("eth_getLogs", [{
        "address": proxy,
        "fromBlock": hex(block),
        "toBlock": hex(block),
        "topics": [upgraded_topic()],
    }])
    if error:
        return None, error
    if not isinstance(rows, list):
        return None, "read_error"
    found: list[tuple[int, int, str]] = []
    for row in rows:
        parsed = _one_upgrade(row, proxy, block, header_hash)
        if parsed is None:
            return None, "implementation_at_execution_unverified"
        found.append(parsed)
    found.sort()
    return found, None


def _one_upgrade(row: Any, proxy: str, block: int, header_hash: str) -> tuple[int, int, str] | None:
    if not isinstance(row, dict) or _removed(row):
        return None
    if normalize_address(row.get("address")) != proxy:
        return None
    if _hex_int(row.get("blockNumber")) != block:
        return None
    digest = row.get("blockHash")
    if not isinstance(digest, str) or digest.lower() != header_hash:
        return None
    topics = row.get("topics")
    if not isinstance(topics, list) or not topics or str(topics[0]).lower() != upgraded_topic():
        return None
    if len(topics) < 2:
        return None
    impl = normalize_address("0x" + str(topics[1])[-40:])
    tx_index = _hex_int(row.get("transactionIndex"))
    log_index = _hex_int(row.get("logIndex"))
    if impl is None or tx_index is None or log_index is None:
        return None
    return tx_index, log_index, impl


def _storage(rpc: ExplanationRpc, proxy: str, block_hash: str) -> tuple[str | None, str | None]:
    result, error = rpc.call("eth_getStorageAt", [proxy, ERC1967_IMPLEMENTATION_SLOT, rpc.historical(block_hash)])
    if error:
        return None, "historical_state_unavailable" if error == "historical_state_unavailable" else error
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 42:
        return None, "historical_state_unavailable"
    return normalize_address("0x" + result[-40:]), None


def _queue(rpc: ExplanationRpc, manifest: ExplainerManifest, account: str, block_hash: str) -> tuple[list[tuple[int, int, int]] | None, str | None]:
    result, error = _view(rpc, manifest, "getUserWithdrawalRequests(address)", [account], block_hash)
    if error:
        return None, error
    if not isinstance(result, str) or not result.startswith("0x"):
        return None, "historical_state_unavailable"
    try:
        decoded = decode(["(uint256,uint256,uint256)[]"], bytes.fromhex(result[2:]))
    except Exception:
        return None, "malformed_response"
    return [(int(uuid), int(slis), int(start)) for uuid, slis, start in decoded[0]], None


def _status_amount(rpc: ExplanationRpc, manifest: ExplainerManifest, account: str, index: int, block_hash: str) -> tuple[int | None, str | None]:
    result, error = _view(rpc, manifest, "getUserRequestStatus(address,uint256)", [account, index], block_hash)
    if error:
        return None, error
    if not isinstance(result, str) or not result.startswith("0x"):
        return None, "historical_state_unavailable"
    try:
        _claimable, amount = decode(["bool", "uint256"], bytes.fromhex(result[2:]))
    except Exception:
        return None, "malformed_response"
    return int(amount), None


def _legacy_amount(rpc: ExplanationRpc, manifest: ExplainerManifest, slot: Slot, block_hash: str) -> tuple[int | None, str | None]:
    if slot.uuid is None:
        return None, "request_time_quote_unavailable"
    result, error = _view(rpc, manifest, "getBotUndelegateRequest(uint256)", [slot.uuid], block_hash)
    if error:
        return None, error
    if not isinstance(result, str) or not result.startswith("0x"):
        return None, "historical_state_unavailable"
    try:
        _start, _end, total_bnb, total_slis = decode(["uint256", "uint256", "uint256", "uint256"], bytes.fromhex(result[2:]))
    except Exception:
        return None, "malformed_response"
    total_slis = int(total_slis)
    if total_slis <= 0:
        return None, "request_time_quote_unavailable"
    return (int(total_bnb) * slot.slis) // total_slis, None


def _view(rpc: ExplanationRpc, manifest: ExplainerManifest, signature: str, args: list[Any], block_hash: str) -> tuple[Any, str | None]:
    if manifest.view(signature) is None or signature not in _VIEWS:
        return None, "stale_abi"
    data = _encode(signature, args)
    return rpc.call("eth_call", [{"data": data, "to": manifest.proxy()}, rpc.historical(block_hash)])


def _role(rpc: ExplanationRpc, manifest: ExplainerManifest, block_hash: str, submitter: str | None, facts: dict[str, str]) -> None:
    if submitter is None or manifest.view("hasRole(bytes32,address)") is None:
        return
    if rpc.stopped:
        return
    result, error = _view(rpc, manifest, "hasRole(bytes32,address)", [_BOT, submitter], block_hash)
    if error or not isinstance(result, str):
        facts["caller_bot_role"] = "unavailable"
        return
    try:
        (flag,) = decode(["bool"], bytes.fromhex(result[2:]))
    except Exception:
        facts["caller_bot_role"] = "unavailable"
        return
    facts["caller_bot_role"] = "verified" if bool(flag) else "contradicted"


def _decode_call(manifest: ExplainerManifest, data: Any, submitter: str | None, to: str | None) -> tuple[str | None, str | None, int | None]:
    if not isinstance(data, str) or not data.startswith("0x") or to != manifest.proxy():
        return None, None, None
    raw = data[2:]
    if len(raw) < 8:
        return None, None, None
    selector = "0x" + raw[:8]
    claim_for = manifest.call("claimWithdrawFor(address,uint256)")
    claim = manifest.call("claimWithdraw(uint256)")
    if claim_for is not None and selector == str(claim_for.get("selector")).lower():
        try:
            user, index = decode(["address", "uint256"], bytes.fromhex(raw[8:]))
        except Exception:
            return None, None, None
        return submitter, normalize_address(user), int(index)
    if claim is not None and selector == str(claim.get("selector")).lower():
        try:
            (index,) = decode(["uint256"], bytes.fromhex(raw[8:]))
        except Exception:
            return None, None, None
        return submitter, submitter, int(index)
    return None, None, None


def _encode(signature: str, args: list[Any]) -> str:
    inputs, _outputs = _VIEWS[signature]
    selector = function_signature_to_4byte_selector(signature)
    payload = encode(list(inputs), args)
    return "0x" + (selector + payload).hex()


def _header(rpc: ExplanationRpc, number: int) -> tuple[dict[str, Any] | None, str | None]:
    result, error = rpc.call("eth_getBlockByNumber", [hex(number), False])
    if error:
        return None, error
    if not isinstance(result, dict):
        return None, "checkpoint_unverified"
    block_number = _hex_int(result.get("number"))
    digest = result.get("hash")
    parent = result.get("parentHash")
    if block_number != number or not isinstance(digest, str) or not digest.startswith("0x") or len(digest) != 66:
        return None, "checkpoint_unverified"
    parent_text = parent if isinstance(parent, str) and parent.startswith("0x") and len(parent) == 66 else ""
    return {"hash": digest.lower(), "number": block_number, "parent": parent_text.lower()}, None


def _chain_id(rpc: ExplanationRpc) -> tuple[int | None, str | None]:
    result, error = rpc.call("eth_chainId", [])
    if error:
        return None, error
    value = _hex_int(result)
    if value is None:
        return None, "read_error"
    return value, None


def _collect_logs(
    ctx: Any,
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    account: str,
    start_block: int,
    end_block: int,
) -> tuple[list[FoundLog], str | None]:
    cursor = getattr(ctx, "interval_cursor", None)
    saved = [item for item in (getattr(ctx, "saved_logs", None) or []) if isinstance(item, FoundLog)]
    if cursor == -1:
        return saved, None
    found, error, next_high = discover_logs(
        rpc,
        manifest,
        account,
        start_block,
        end_block,
        window_high=None if cursor is None else int(cursor),
    )
    if error and error != "truncated_logs":
        return saved, error
    merged = union_logs(saved, found)
    saver = getattr(ctx, "save_interval", None)
    if callable(saver):
        saver(next_high, merged)
    ctx.interval_cursor = next_high
    ctx.saved_logs = merged
    return merged, error


def _remember_requests(ctx: Any, logs: list[FoundLog]) -> None:
    saver = getattr(ctx, "save_requests", None)
    if callable(saver):
        saver([item for item in logs if item.kind == "request"])


def _unique_claims(claims: list[FoundLog]) -> tuple[list[FoundLog], bool]:
    chosen: dict[tuple[str, int], FoundLog] = {}
    order: list[tuple[str, int]] = []
    conflict = False
    for claim in claims:
        key = (claim.tx_id, claim.log_index)
        prior = chosen.get(key)
        if prior is None:
            chosen[key] = claim
            order.append(key)
            continue
        if (prior.address, prior.account, prior.data_words, prior.block_number) != (
            claim.address, claim.account, claim.data_words, claim.block_number,
        ):
            conflict = True
    return [chosen[key] for key in order], conflict


def _fill_request_identity(
    ctx: Any,
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    account: str,
    old_block: int,
    slots: list[Slot],
    logs: list[FoundLog],
    gaps: list[str],
) -> None:
    if not slots or all(slot.tx_id and slot.log_index is not None for slot in slots):
        return
    known = _known_history(ctx)
    cursor = getattr(ctx, "lookback_cursor", None)
    if cursor == 0:
        _reconstruct_prior_slots(slots, known, logs, old_block)
        return
    bounds = lookback_bounds(old_block, cursor if isinstance(cursor, int) else None)
    if bounds is None:
        return
    low, high, next_cursor = bounds
    found, error = _fetch_history(rpc, manifest, account, low, high)
    if error:
        gaps.append(error)
        return
    saver = getattr(ctx, "save_requests", None)
    if callable(saver):
        saver(found)
    save_cursor = getattr(ctx, "save_lookback", None)
    if callable(save_cursor):
        save_cursor(next_cursor)
    ctx.lookback_cursor = next_cursor
    if next_cursor != 0:
        return
    _reconstruct_prior_slots(slots, _known_history(ctx), [*logs, *found], old_block)


def _known_history(ctx: Any) -> list[FoundLog]:
    getter = getattr(ctx, "known_requests", None)
    if not callable(getter):
        return []
    loaded = getter()
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, FoundLog)]


def _reconstruct_prior_slots(slots: list[Slot], known: list[FoundLog], logs: list[FoundLog], old_block: int) -> None:
    """Replay from genesis. Do not attach the only amount match in a partial window."""
    prior = [
        item for item in [*known, *logs]
        if item.kind in {"request", "claim"} and item.block_number <= old_block
    ]
    reconstruct_genesis_queue(slots, union_logs([], prior))


def _fetch_history(
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    account: str,
    low: int,
    high: int,
) -> tuple[list[FoundLog], str | None]:
    """Request and claim logs in one inclusive window. Claims are required to replay the queue."""
    found: list[FoundLog] = []
    for signature, kind in (
        ("RequestWithdraw(address,uint256)", "request"),
        ("ClaimWithdrawal(address,uint256,uint256)", "claim"),
    ):
        spec = manifest.event(signature)
        if spec is None:
            return [], "stale_abi"
        topic = str(spec["topic"]).lower()
        rows, error = rpc.call("eth_getLogs", [{
            "address": manifest.proxy(),
            "fromBlock": hex(low),
            "toBlock": hex(high),
            "topics": [topic, pad_address(account)],
        }])
        if error:
            return [], error
        if not isinstance(rows, list):
            return [], "logs_unavailable"
        for row in rows:
            parsed = _parse_log(row, kind, topic, manifest.proxy(), account)
            if parsed is not None:
                found.append(parsed)
    return found, None


def _tx_receipt_identity(tx: Mapping[str, Any], receipt: Mapping[str, Any], claim: FoundLog) -> str | None:
    tx_hash = str(tx.get("hash") or "").lower()
    if tx_hash and tx_hash != claim.tx_id:
        return "receipt_identity_mismatch"
    receipt_hash = str(receipt.get("transactionHash") or "").lower()
    if receipt_hash != claim.tx_id:
        return "receipt_identity_mismatch"
    for value in (_hex_int(receipt.get("blockNumber")), _hex_int(tx.get("blockNumber"))):
        if value is not None and value != claim.block_number:
            return "receipt_identity_mismatch"
    tx_index = _hex_int(tx.get("transactionIndex"))
    receipt_index = _hex_int(receipt.get("transactionIndex"))
    if tx_index is None or receipt_index is None or tx_index != claim.tx_index or receipt_index != claim.tx_index:
        return "transaction_index_mismatch"
    return None


def _checkpoint_identity(tx: Mapping[str, Any], receipt: Mapping[str, Any], header_hash: str) -> str | None:
    tx_block = tx.get("blockHash")
    if isinstance(tx_block, str) and tx_block.lower() != header_hash:
        return "receipt_identity_mismatch"
    receipt_block = receipt.get("blockHash")
    if not isinstance(receipt_block, str) or not receipt_block.startswith("0x") or len(receipt_block) != 66:
        return "receipt_checkpoint_unverified"
    if receipt_block.lower() != header_hash:
        return "receipt_identity_mismatch"
    return None


def _status_kind(status: Any) -> str:
    if status in {1, "0x1", "0x01"}:
        return "success"
    if status in {0, "0x0", "0x00"}:
        return "failed"
    return "unavailable"


def _bound_claim_log(
    receipt: Mapping[str, Any],
    claim: FoundLog,
    account: str,
    proxy: str,
    header_hash: str,
) -> tuple[str | None, tuple[int, ...] | None]:
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return "claim_log_not_in_receipt", None
    match = None
    expected_topic = ""
    topics = claim.raw.get("topics") if isinstance(claim.raw, dict) else None
    if isinstance(topics, list) and topics:
        expected_topic = str(topics[0]).lower()
    for row in logs:
        if not isinstance(row, dict):
            continue
        if str(row.get("transactionHash") or "").lower() != claim.tx_id:
            continue
        if _hex_int(row.get("logIndex")) != claim.log_index:
            continue
        row_topics = row.get("topics")
        if not isinstance(row_topics, list) or not row_topics or str(row_topics[0]).lower() != expected_topic:
            continue
        match = row
        break
    if match is None:
        return "claim_log_not_in_receipt", None
    if _removed(match) or normalize_address(match.get("address")) != proxy:
        return "claim_evidence_mismatch", None
    row_topics = match.get("topics")
    beneficiary = _topic_address(row_topics[1]) if isinstance(row_topics, list) and len(row_topics) > 1 else None
    if beneficiary != account:
        return "claim_evidence_mismatch", None
    words = _data_words(match.get("data"), 2)
    if words is None or words != claim.data_words:
        return "claim_evidence_mismatch", None
    logged_block = _hex_int(match.get("blockNumber"))
    if logged_block is not None and logged_block != claim.block_number:
        return "receipt_identity_mismatch", None
    logged_index = _hex_int(match.get("transactionIndex"))
    if logged_index is None or logged_index != claim.tx_index:
        return "transaction_index_mismatch", None
    logged_hash = match.get("blockHash")
    if isinstance(logged_hash, str) and logged_hash.lower() != header_hash:
        return "receipt_identity_mismatch", None
    return None, words


def _removed(row: Mapping[str, Any]) -> bool:
    return row.get("removed") in {True, 1, "true", "0x1", "0x01"}


def _receipt_contains(receipt: Mapping[str, Any], claim: FoundLog) -> bool:
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return False
    for row in logs:
        if not isinstance(row, dict):
            continue
        if str(row.get("transactionHash") or "").lower() != claim.tx_id:
            continue
        if _hex_int(row.get("logIndex")) != claim.log_index:
            continue
        topics = row.get("topics")
        if isinstance(topics, list) and topics and str(topics[0]).lower() == str(claim.raw["topics"][0]).lower():
            return True
    return False


def _empty_queue_gap(rpc: ExplanationRpc, manifest: ExplainerManifest, account: str, block_hash: str, gaps: list[str]) -> list[str]:
    if rpc.stopped or not block_hash:
        return []
    rows, error = _queue(rpc, manifest, account, block_hash)
    if error:
        return [] if error in gaps else [error]
    if rows == []:
        return ["empty_request_array"]
    return []


def _transfer_block(alert: Mapping[str, Any]) -> int | None:
    raw = alert.get("block_number")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw >= 1:
        return raw
    if isinstance(raw, str) and raw.isdigit() and int(raw) >= 1:
        return int(raw)
    return None


def _transfer_tx(alert: Mapping[str, Any]) -> str | None:
    raw = alert.get("tx_id")
    if isinstance(raw, str) and raw.startswith("0x") and len(raw) == 66:
        return raw.lower()
    return None


def _transfer_amount(alert: Mapping[str, Any]) -> int | None:
    raw = alert.get("raw_amount")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw >= 0:
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _checkpoints(alert: Mapping[str, Any]) -> tuple[tuple[int, str] | None, tuple[int, str] | None]:
    return _point(alert.get("old_checkpoint")), _point(alert.get("new_checkpoint"))


def _point(value: Any) -> tuple[int, str] | None:
    if not isinstance(value, dict):
        return None
    number = value.get("block_number")
    digest = value.get("block_hash")
    if isinstance(number, str) and number.startswith("0x"):
        parsed = _hex_int(number)
    elif isinstance(number, str) and number.isdigit():
        parsed = int(number)
    elif isinstance(number, int) and not isinstance(number, bool):
        parsed = number
    else:
        parsed = None
    if parsed is None or not isinstance(digest, str) or not digest.startswith("0x") or len(digest) != 66:
        return None
    return parsed, digest.lower()


def _residual(alert: Mapping[str, Any], explained: int) -> dict[str, str]:
    observed = observed_raw_amount(alert)
    return {
        "asset": str(alert.get("asset") or "evm-native:56"),
        "explained_raw_amount": str(explained),
        "observed_raw_amount": str(observed),
        "raw_amount": str(observed - explained),
    }


def _keccak_hex(value: str) -> str | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError:
        return None
    return "0x" + keccak(raw).hex()


def _hex_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str) or not value.startswith(("0x", "0X")):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _sealed(
    rpc: ExplanationRpc,
    event_id: str,
    manifest: ExplainerManifest | None,
    verdict: str,
    facts: dict[str, str],
    gaps: list[str],
    contradictions: list[str],
    established: list[str],
    parties: dict[str, str | None],
    amounts: dict[str, str | None],
    effects: list[dict[str, Any]],
    residual: dict[str, str],
    refs: list[str],
    checkpoints: dict[str, str],
    identity: str | None,
    request_uuid: str | None,
    retry: str | None,
    calculation: str | None = None,
    evidence_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    late = rpc.stopped == "deadline" or rpc.remaining_seconds() <= 0
    if late:
        if "deadline" not in gaps:
            gaps.append("deadline")
        if verdict == "MATCHES_OWN_REQUEST":
            verdict = "UNVERIFIED"
            facts = dict(facts)
            facts["beneficiary_payment"] = "unavailable"
            facts["implementation_at_execution"] = "unverified"
            facts["request_linkage"] = "unavailable"
            established = []
            effects = []
        retry = "deadline"
    if "truncated_logs" in gaps or "discovery_incomplete" in gaps:
        if verdict == "MATCHES_OWN_REQUEST":
            verdict = "PARTIAL"
        if retry is None:
            retry = "truncated_logs" if "truncated_logs" in gaps else "discovery_incomplete"
    return _finish(
        event_id, manifest, verdict, facts, gaps, contradictions, established,
        parties, amounts, effects, residual, refs, checkpoints, identity, request_uuid, retry, calculation,
        evidence_blocks,
    )


def _finish(
    event_id: str,
    manifest: ExplainerManifest | None,
    verdict: str,
    facts: dict[str, str],
    gaps: list[str],
    contradictions: list[str],
    established: list[str],
    parties: dict[str, str | None],
    amounts: dict[str, str | None],
    effects: list[dict[str, Any]],
    residual: dict[str, str],
    refs: list[str],
    checkpoints: dict[str, str],
    identity: str | None,
    request_uuid: str | None,
    retry: str | None,
    calculation: str | None = None,
    evidence_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if "request_linkage" in facts and facts["request_linkage"] == "verified":
        established.append("request_linkage")
    return document(
        event_id=event_id,
        event_revision=1,
        revision=0,
        supersedes=None,
        explainer_id=None if manifest is None else manifest.explainer_id,
        explainer_version=None if manifest is None else manifest.version,
        manifest_hash=None if manifest is None else manifest.manifest_hash,
        verdict=verdict,
        facts=facts,
        gaps=gaps,
        contradictions=contradictions,
        established=established,
        parties=parties,
        amounts=amounts,
        effects=effects,
        residual=residual,
        evidence_refs=refs,
        checkpoints=checkpoints,
        request_identity=identity,
        attempt_state="complete" if retry is None else "retry",
        retry_reason=retry,
        calculation_version=calculation,
        request_uuid=request_uuid,
        evidence_blocks=evidence_blocks,
    )
