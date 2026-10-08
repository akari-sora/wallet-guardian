"""Solana read operations. Sockets stay in the transport.

Every RPC here is an allowlisted solana_rpc read. Nothing in this package
builds, signs, simulates, or broadcasts a transaction. A failed read keeps the
previous cursor and does not become a zero balance. getGenesisHash is checked
before anything is recorded. Commitment is explicit: processed is only observed,
confirmed is not finalized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from guardian.coverage import CoverageObservation, build_coverage, portfolio_summary
from guardian.errors import SecurityError
from guardian.events import finality_for
from guardian.identities import IdentityError, parse_address
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.transport import TransportError

SYSTEM_PROGRAM = "11111111111111111111111111111111"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
ADDRESS_LOOKUP_TABLE_PROGRAM = "AddressLookupTab1e1111111111111111111111111"
TOKEN_PROGRAMS = (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

COMMITMENTS = frozenset({"confirmed", "finalized", "processed"})
DEFAULT_COMMITMENT = "confirmed"
PROBE_UNTESTED = "untested"
PROBE_BLOCKED = "blocked"
SOURCE_SOLANA_RPC = "solana-rpc"
SIGNATURE_PAGE_MAX = 1000

CLOSED_HISTORY_GAP = "token_account_closed_history_gap"
UNKNOWN_HISTORY_GAP = "token_account_history_unknown"
UNKNOWN_EXTENSION = "token2022_unknown_extension"
PAGE_CAP_GAP = "signature_page_cap"


def solana_rpc(method: str, params: list[Any]) -> Operation:
    """One JSON-RPC read. The method is checked by the transport allowlist."""
    return Operation(protocol="jsonrpc", adapter="solana_rpc", method=method, params=params)


def _commitment(value: str) -> str:
    if value not in COMMITMENTS:
        raise ValueError("bad_commitment")
    return value


def genesis_hash_operation() -> Operation:
    return solana_rpc("getGenesisHash", [])


def slot_operation(commitment: str = DEFAULT_COMMITMENT) -> Operation:
    return solana_rpc("getSlot", [{"commitment": _commitment(commitment)}])


def balance_operation(address: str, commitment: str = DEFAULT_COMMITMENT) -> Operation:
    return solana_rpc("getBalance", [address, {"commitment": _commitment(commitment)}])


def token_accounts_operation(
    owner: str,
    program_id: str,
    commitment: str = DEFAULT_COMMITMENT,
) -> Operation:
    if program_id not in TOKEN_PROGRAMS:
        raise ValueError("token_program")
    return solana_rpc("getTokenAccountsByOwner", [
        owner,
        {"programId": program_id},
        {"commitment": _commitment(commitment), "encoding": "jsonParsed"},
    ])


def signatures_operation(
    address: str,
    *,
    limit: int,
    before: str | None = None,
    until: str | None = None,
    commitment: str = DEFAULT_COMMITMENT,
) -> Operation:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > SIGNATURE_PAGE_MAX:
        raise ValueError("bad_limit")
    options: dict[str, Any] = {"commitment": _commitment(commitment), "limit": limit}
    if before is not None:
        options["before"] = before
    if until is not None:
        options["until"] = until
    return solana_rpc("getSignaturesForAddress", [address, options])


def transaction_operation(signature: str, commitment: str = DEFAULT_COMMITMENT) -> Operation:
    return solana_rpc("getTransaction", [
        signature,
        {
            "commitment": _commitment(commitment),
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
        },
    ])


def account_info_operation(address: str, commitment: str = DEFAULT_COMMITMENT) -> Operation:
    return solana_rpc("getAccountInfo", [
        address,
        {"commitment": _commitment(commitment), "encoding": "jsonParsed"},
    ])


def multiple_accounts_operation(
    addresses: list[str],
    commitment: str = DEFAULT_COMMITMENT,
) -> Operation:
    return solana_rpc("getMultipleAccounts", [
        list(addresses),
        {"commitment": _commitment(commitment), "encoding": "jsonParsed"},
    ])


def parse_raw_amount(value: Any) -> int | None:
    """Chain amounts are integers. Floats, including uiAmount, are not amounts."""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def canonical_address(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_address("solana", value).canonical
    except IdentityError:
        return None


def commitment_finality(entry: Mapping[str, Any], slot: int | None, commitment: str) -> str:
    """Map one explicit commitment. Missing slot or an unknown name stays unknown."""
    if slot is None or isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        return "unknown"
    if commitment not in COMMITMENTS:
        return "unknown"
    policy = {
        "commitment": commitment,
        "family": "solana",
        "finality_policy": entry.get("finality_policy"),
        "mode": "solana_commitment",
        "network": entry.get("network"),
    }
    try:
        return finality_for(str(entry.get("network")), slot, slot, None, policy)
    except (TypeError, ValueError):
        return "unknown"


@dataclass(frozen=True)
class IdentityCheck:
    ok: bool
    reason: str
    genesis: str | None
    blocked: bool = False
    probe: str = PROBE_UNTESTED


@dataclass(frozen=True)
class RpcOutcome:
    ok: bool
    result: Any
    reason: str
    blocked: bool
    category: str
    probe: str = PROBE_UNTESTED


@dataclass(frozen=True)
class BalanceRead:
    raw_lamports: int | None
    slot: int | None
    observation: CoverageObservation | None
    refused: bool
    blocked: bool
    reason: str
    probe: str = PROBE_UNTESTED


def call_rpc(transport: Any, source_id: str, operation: Operation) -> RpcOutcome:
    """One read. Policy failures and a missing endpoint never look like an empty wallet."""
    try:
        result = transport.request(source_id, operation)
    except SecurityError as exc:
        reason = str(exc.reason or "denied")
        blocked = reason in {"endpoint_missing", "unregistered_source", "adapter_mismatch"}
        probe = PROBE_BLOCKED if blocked else PROBE_UNTESTED
        return RpcOutcome(False, None, reason, blocked, "error", probe)
    except BudgetExhausted:
        return RpcOutcome(False, None, "budget", False, "partial", PROBE_UNTESTED)
    except TransportError as exc:
        return RpcOutcome(False, None, str(exc.reason or "read_error"), False, "error", PROBE_UNTESTED)
    except Exception:
        return RpcOutcome(False, None, "read_error", False, "error", PROBE_UNTESTED)
    if getattr(result, "blocked", False):
        reason = str(getattr(result, "reason", "blocked") or "blocked")
        return RpcOutcome(False, getattr(result, "body", None), reason, True, "error", PROBE_BLOCKED)
    category = str(getattr(result, "category", "error"))
    if category == "rate_limited":
        return RpcOutcome(False, getattr(result, "body", None), "rate_limited", False, "partial", PROBE_UNTESTED)
    if not getattr(result, "ok", False):
        reason = str(getattr(result, "reason", "read_error") or "read_error")
        return RpcOutcome(False, getattr(result, "body", None), reason, False, "error", PROBE_UNTESTED)
    body = result.body
    if not isinstance(body, dict) or "result" not in body or "error" in body:
        return RpcOutcome(False, body, "malformed", False, "error", PROBE_UNTESTED)
    return RpcOutcome(True, body.get("result"), "", False, "ok", PROBE_UNTESTED)


def interpret_genesis(result: Any, expected: str) -> IdentityCheck:
    if not isinstance(result, str) or not result:
        return IdentityCheck(False, "identity_unreadable", None)
    if result != expected:
        return IdentityCheck(False, "genesis_mismatch", result)
    return IdentityCheck(True, "", result)


def make_observation(
    *,
    category: str,
    pagination_complete: bool,
    gap_detection_complete: bool,
    gaps: tuple[str, ...] = (),
    cursor: str | None = None,
    declared: str,
    reason_codes: tuple[str, ...] = (),
    slot: int | None = None,
    finality: str = "unknown",
    source: str = SOURCE_SOLANA_RPC,
    scope_from: str | None = None,
    scope_to: str | None = None,
    last_success_at: str | None = None,
) -> CoverageObservation:
    status = finality if finality in {"confirmed", "finalized", "observed", "orphaned", "unknown"} else "unknown"
    clean = category in {"empty", "ok"} and pagination_complete and gap_detection_complete and not gaps
    return CoverageObservation(
        category=category,
        pagination_complete=pagination_complete,
        gap_detection_complete=gap_detection_complete,
        gaps=tuple(gaps),
        cursor=cursor,
        declared=declared,
        scope_from=scope_from,
        scope_to=scope_to,
        checkpoint_kind="slot" if slot is not None else None,
        checkpoint_number=None if slot is None else str(slot),
        checkpoint_status=status,
        last_success_at=last_success_at,
        sources=(source,),
        trust="single_provider" if clean else "unverified",
        reason_codes=tuple(dict.fromkeys(code for code in reason_codes if code)),
    )


class SolanaAdapter:
    def __init__(self, entry: Mapping[str, Any], transport: Any, *, rpc_source_id: str) -> None:
        identity = entry.get("identity")
        native = entry.get("native_asset")
        if entry.get("family") != "solana":
            raise ValueError("not_solana")
        if not isinstance(identity, Mapping) or identity.get("method") != "getGenesisHash":
            raise ValueError("identity_method")
        expected = identity.get("expected_value")
        if not isinstance(expected, str) or not expected.strip():
            raise ValueError("genesis")
        if not isinstance(native, Mapping) or native.get("decimals") != 9:
            raise ValueError("native_asset")
        if not isinstance(native.get("identity"), str) or not native.get("identity"):
            raise ValueError("native_asset")
        policy = entry.get("finality_policy")
        if not isinstance(policy, str) or not policy.strip():
            raise ValueError("finality_policy")
        network = entry.get("network")
        if not isinstance(network, str) or not network:
            raise ValueError("network")
        self.entry = entry
        self.network = network
        self.expected_genesis = expected
        self.native_asset = native["identity"]
        self.finality_policy = policy
        self.transport = transport
        self.rpc_source_id = rpc_source_id

    def open(self) -> SolanaSession:
        outcome = call_rpc(self.transport, self.rpc_source_id, genesis_hash_operation())
        if not outcome.ok:
            probe = PROBE_BLOCKED if outcome.blocked else PROBE_UNTESTED
            return SolanaSession(self, IdentityCheck(False, outcome.reason, None, outcome.blocked, probe))
        return SolanaSession(self, interpret_genesis(outcome.result, self.expected_genesis))

    def portfolio(
        self,
        wallet_id: str,
        observations: Mapping[str, CoverageObservation],
        *,
        catalog: Any,
        registry: Mapping[str, Mapping[str, Any]],
        now: str,
    ) -> dict[str, Any]:
        results = {(wallet_id, self.network, name): observation for name, observation in observations.items()}
        rows = build_coverage([(wallet_id, self.network)], results, catalog, registry, now)
        return {"rows": rows, "summary": portfolio_summary(rows)}


class SolanaSession:
    def __init__(self, adapter: SolanaAdapter, identity: IdentityCheck) -> None:
        self.adapter = adapter
        self.identity = identity
        self.probe = identity.probe

    def rpc(self, operation: Operation) -> RpcOutcome:
        if not self.identity.ok:
            blocked = self.identity.blocked
            probe = PROBE_BLOCKED if blocked else PROBE_UNTESTED
            return RpcOutcome(False, None, self.identity.reason, blocked, "error", probe)
        return call_rpc(self.adapter.transport, self.adapter.rpc_source_id, operation)

    def native_balance(
        self,
        address: str,
        *,
        commitment: str = DEFAULT_COMMITMENT,
        previous_success_at: str | None = None,
    ) -> BalanceRead:
        if not self.identity.ok:
            return BalanceRead(None, None, None, True, self.identity.blocked, self.identity.reason, self.identity.probe)
        wallet = canonical_address(address)
        if wallet is None:
            observation = make_observation(
                category="error",
                pagination_complete=False,
                gap_detection_complete=False,
                declared="latest_balance",
                reason_codes=("bad_address",),
                last_success_at=previous_success_at,
            )
            return BalanceRead(None, None, observation, False, False, "bad_address")
        try:
            operation = balance_operation(wallet, commitment)
        except ValueError as exc:
            return BalanceRead(None, None, None, False, False, str(exc))
        outcome = self.rpc(operation)
        if not outcome.ok:
            observation = make_observation(
                category="error" if outcome.category != "partial" else "partial",
                pagination_complete=False,
                gap_detection_complete=False,
                declared="latest_balance",
                reason_codes=(outcome.reason,),
                last_success_at=previous_success_at,
            )
            return BalanceRead(None, None, observation, False, outcome.blocked, outcome.reason, outcome.probe)
        lamports, slot, reason = _parse_balance(outcome.result)
        if reason is not None or lamports is None:
            observation = make_observation(
                category="error",
                pagination_complete=False,
                gap_detection_complete=False,
                declared="latest_balance",
                reason_codes=(reason or "malformed_balance",),
                slot=slot,
                last_success_at=previous_success_at,
            )
            return BalanceRead(None, slot, observation, False, False, reason or "malformed_balance")
        finality = commitment_finality(self.adapter.entry, slot, commitment)
        observation = make_observation(
            category="ok",
            pagination_complete=True,
            gap_detection_complete=True,
            cursor=None if slot is None else str(slot),
            declared="latest_balance",
            slot=slot,
            finality=finality,
        )
        return BalanceRead(lamports, slot, observation, False, False, "")

    def scan(self, owner: str, **kwargs: Any) -> Any:
        from guardian.chains.solana.tokens import scan_owner

        return scan_owner(self, owner, **kwargs)

    def read_transaction(self, signature: str, **kwargs: Any) -> Any:
        from guardian.chains.solana.transactions import fetch_transaction

        return fetch_transaction(self, signature, **kwargs)

    def read_mints(self, mints: list[str], **kwargs: Any) -> Any:
        from guardian.chains.solana.authorities import fetch_mints

        return fetch_mints(self, mints, **kwargs)


def _parse_balance(result: Any) -> tuple[int | None, int | None, str | None]:
    if not isinstance(result, dict):
        return None, None, "malformed_balance"
    slot = _slot(result.get("context"))
    lamports = parse_raw_amount(result.get("value"))
    if lamports is None:
        return None, slot, "malformed_balance"
    return lamports, slot, None


def _slot(context: Any) -> int | None:
    if not isinstance(context, dict):
        return None
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        return None
    return slot
