"""Owner token-account set. History includes every owned token account.

An incoming SPL transfer does not put the owner in accountKeys. Signatures are
read for the wallet and for each token account, so that credit is still captured.
A token account that was seen before and is now missing, or whose history cannot
be read, is a gap. The cursor does not move past a gap or a failed read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from guardian.chains.solana.adapter import (
    CLOSED_HISTORY_GAP,
    DEFAULT_COMMITMENT,
    PAGE_CAP_GAP,
    PROBE_BLOCKED,
    PROBE_UNTESTED,
    SIGNATURE_PAGE_MAX,
    TOKEN_PROGRAMS,
    UNKNOWN_EXTENSION,
    UNKNOWN_HISTORY_GAP,
    SolanaSession,
    canonical_address,
    make_observation,
    parse_raw_amount,
    signatures_operation,
    token_accounts_operation,
)
from guardian.chains.solana.authorities import (
    AuthorityReport,
    PriorAuthority,
    TokenExtension,
    diff_authorities,
    parse_extensions,
    unknown_extension_names,
)
from guardian.chains.solana.transactions import ParsedTransaction, TokenDelta, fetch_transaction
from guardian.coverage import CoverageObservation


@dataclass(frozen=True)
class TokenAccountView:
    address: str
    program_id: str
    mint: str
    owner: str
    amount: int
    decimals: int
    state: str
    delegate: str | None
    delegated_amount: int | None
    close_authority: str | None
    extensions: tuple[TokenExtension, ...]
    unknown_extensions: tuple[str, ...]
    lamports: int | None


@dataclass(frozen=True)
class SignatureInfo:
    signature: str
    slot: int | None
    err: Any
    address: str
    via: str


@dataclass(frozen=True)
class SignatureWalk:
    items: tuple[SignatureInfo, ...]
    pagination_complete: bool
    gap: str | None
    cursor: str | None
    reason: str


@dataclass(frozen=True)
class CapturedTransfer:
    signature: str
    mint: str
    amount: int
    decimals: int
    account: str
    owner: str | None
    owner_in_account_keys: bool
    via: str
    delta: int


@dataclass(frozen=True)
class OwnerScan:
    accounts: tuple[TokenAccountView, ...]
    transfers: tuple[CapturedTransfer, ...]
    signatures: tuple[SignatureInfo, ...]
    observations: Mapping[str, CoverageObservation]
    native_lamports: int | None
    cursor: str | None
    gaps: tuple[str, ...]
    refused: bool
    blocked: bool
    reason: str
    probe: str
    pagination_complete: bool
    gap_detection_complete: bool
    authorities: AuthorityReport | None
    transactions: tuple[ParsedTransaction, ...]


def parse_token_account(item: Any, program_id: str) -> TokenAccountView | None:
    if not isinstance(item, Mapping):
        return None
    address = canonical_address(item.get("pubkey"))
    account = item.get("account")
    if address is None or not isinstance(account, Mapping):
        return None
    data = account.get("data")
    if not isinstance(data, Mapping):
        return None
    parsed = data.get("parsed")
    if not isinstance(parsed, Mapping) or parsed.get("type") != "account" or not isinstance(parsed.get("info"), Mapping):
        return None
    info = parsed["info"]
    mint = canonical_address(info.get("mint"))
    owner = canonical_address(info.get("owner"))
    token_amount = info.get("tokenAmount")
    if mint is None or owner is None or not isinstance(token_amount, Mapping):
        return None
    if isinstance(token_amount.get("amount"), float) or token_amount.get("amount") is None:
        return None
    amount = parse_raw_amount(token_amount.get("amount"))
    decimals = token_amount.get("decimals")
    if amount is None or isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        return None
    state = info.get("state")
    if state not in {"frozen", "initialized"}:
        state = "unknown"
    delegate = canonical_address(info.get("delegate")) if info.get("delegate") is not None else None
    delegated = None
    if delegate is not None:
        delegated_body = info.get("delegatedAmount")
        if isinstance(delegated_body, Mapping):
            delegated = parse_raw_amount(delegated_body.get("amount"))
        else:
            delegated = parse_raw_amount(delegated_body)
        if delegated is None:
            return None
    close_authority = canonical_address(info.get("closeAuthority")) if info.get("closeAuthority") is not None else None
    extensions = parse_extensions(info.get("extensions"))
    program = canonical_address(account.get("owner")) or program_id
    return TokenAccountView(
        address=address,
        program_id=program,
        mint=mint,
        owner=owner,
        amount=amount,
        decimals=decimals,
        state=state,
        delegate=delegate,
        delegated_amount=delegated,
        close_authority=close_authority,
        extensions=extensions,
        unknown_extensions=unknown_extension_names(extensions),
        lamports=parse_raw_amount(account.get("lamports")),
    )


def parse_token_account_result(result: Any, program_id: str) -> tuple[tuple[TokenAccountView, ...], str | None]:
    if not isinstance(result, Mapping) or not isinstance(result.get("value"), list):
        return (), "malformed_token_accounts"
    found: list[TokenAccountView] = []
    for item in result["value"]:
        account = parse_token_account(item, program_id)
        if account is None:
            return (), "malformed_token_account"
        found.append(account)
    return tuple(found), None


def walk_signatures(
    session: SolanaSession,
    address: str,
    *,
    limit: int = SIGNATURE_PAGE_MAX,
    page_cap: int = 20,
    commitment: str = DEFAULT_COMMITMENT,
    previous_cursor: str | None = None,
    until: str | None = None,
    via: str,
) -> SignatureWalk:
    """Walk getSignaturesForAddress until a short page. A cap keeps the previous cursor."""
    if page_cap < 1 or limit < 1 or limit > SIGNATURE_PAGE_MAX:
        return SignatureWalk((), False, PAGE_CAP_GAP, previous_cursor, "bad_limit")
    collected: list[SignatureInfo] = []
    pages = 0
    before: str | None = None
    while True:
        if pages >= page_cap:
            return SignatureWalk(tuple(collected), False, PAGE_CAP_GAP, previous_cursor, PAGE_CAP_GAP)
        try:
            operation = signatures_operation(
                address,
                limit=limit,
                before=before,
                until=until,
                commitment=commitment,
            )
        except ValueError as exc:
            return SignatureWalk(tuple(collected), False, UNKNOWN_HISTORY_GAP, previous_cursor, str(exc))
        outcome = session.rpc(operation)
        pages += 1
        if not outcome.ok:
            gap = "budget" if outcome.reason == "budget" else UNKNOWN_HISTORY_GAP
            return SignatureWalk(tuple(collected), False, gap, previous_cursor, outcome.reason or gap)
        if not isinstance(outcome.result, list):
            return SignatureWalk(tuple(collected), False, UNKNOWN_HISTORY_GAP, previous_cursor, "malformed_signatures")
        batch: list[SignatureInfo] = []
        for item in outcome.result:
            parsed = _signature(item, address, via)
            if parsed is None:
                return SignatureWalk(tuple(collected), False, UNKNOWN_HISTORY_GAP, previous_cursor, "malformed_signatures")
            batch.append(parsed)
        collected.extend(batch)
        if len(outcome.result) < limit:
            newest = collected[0].signature if collected else previous_cursor
            return SignatureWalk(tuple(collected), True, None, newest, "")
        before = batch[-1].signature


def scan_owner(
    session: SolanaSession,
    owner: str,
    *,
    prior_token_accounts: Sequence[str] | None = None,
    prior_authorities: Mapping[str, PriorAuthority] | None = None,
    previous_cursor: str | None = None,
    limit: int = 1000,
    page_cap: int = 20,
    commitment: str = DEFAULT_COMMITMENT,
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    labels: Mapping[str, str] | None = None,
    fetch_transactions: bool = True,
    previous_success_at: str | None = None,
) -> OwnerScan:
    """Enumerate both token programs, then signatures of the wallet and each token account."""
    if not session.identity.ok:
        return _failed(
            previous_cursor,
            session.identity.reason,
            session.identity.blocked,
            session.identity.probe,
            refused=True,
            native=None,
            previous_success_at=previous_success_at,
        )
    wallet = canonical_address(owner)
    if wallet is None:
        return _failed(previous_cursor, "bad_address", False, PROBE_UNTESTED, refused=False, native=None, previous_success_at=previous_success_at)
    native = session.native_balance(wallet, commitment=commitment, previous_success_at=previous_success_at)
    if native.reason and native.raw_lamports is None:
        return _failed(
            previous_cursor,
            native.reason,
            native.blocked,
            native.probe,
            refused=native.refused,
            native=None,
            native_observation=native.observation,
            previous_success_at=previous_success_at,
        )
    groups: list[TokenAccountView] = []
    for program_id in TOKEN_PROGRAMS:
        try:
            operation = token_accounts_operation(wallet, program_id, commitment)
        except ValueError as exc:
            return _failed(previous_cursor, str(exc), False, PROBE_UNTESTED, refused=False, native=native.raw_lamports, native_observation=native.observation, previous_success_at=previous_success_at)
        outcome = session.rpc(operation)
        if not outcome.ok:
            return _failed(
                previous_cursor,
                outcome.reason or "read_error",
                outcome.blocked,
                outcome.probe,
                refused=False,
                native=native.raw_lamports,
                native_observation=native.observation,
                previous_success_at=previous_success_at,
                partial_accounts=tuple(groups),
            )
        parsed, reason = parse_token_account_result(outcome.result, program_id)
        if reason:
            return _failed(
                previous_cursor, reason, False, PROBE_UNTESTED, refused=False,
                native=native.raw_lamports, native_observation=native.observation,
                previous_success_at=previous_success_at,
            )
        groups.extend(parsed)
    accounts = tuple(groups)
    gaps: list[str] = []
    unknown_extensions = tuple(dict.fromkeys(
        name for account in accounts for name in account.unknown_extensions
    ))
    if unknown_extensions:
        gaps.append(UNKNOWN_EXTENSION)
    if prior_token_accounts is None:
        gap_detection = False
        closed: tuple[str, ...] = ()
    else:
        gap_detection = True
        current = {account.address for account in accounts}
        closed = tuple(sorted(set(prior_token_accounts) - current))
        if closed:
            gaps.append(CLOSED_HISTORY_GAP)
    until = previous_cursor if _looks_like_signature(previous_cursor) else None
    walks: list[SignatureWalk] = []
    page_complete = True
    addresses = [(wallet, "wallet")]
    addresses.extend((account.address, "token_account") for account in accounts)
    addresses.extend((item, "token_account") for item in closed)
    for address, via in addresses:
        walk = walk_signatures(
            session, address, limit=limit, page_cap=page_cap, commitment=commitment,
            previous_cursor=previous_cursor, until=until, via=via,
        )
        walks.append(walk)
        if not walk.pagination_complete:
            page_complete = False
            if walk.gap == PAGE_CAP_GAP:
                gaps.append(PAGE_CAP_GAP)
            elif walk.reason == "budget":
                gaps.append("budget")
            elif via == "token_account":
                gaps.append(UNKNOWN_HISTORY_GAP)
            else:
                gaps.append(walk.gap or UNKNOWN_HISTORY_GAP)
            break
    signatures = _dedupe_signatures(tuple(item for walk in walks for item in walk.items))
    transactions: list[ParsedTransaction] = []
    transfers: list[CapturedTransfer] = []
    if fetch_transactions and page_complete:
        owned = {account.address for account in accounts} | set(closed)
        extensions = unknown_extensions
        for info in signatures:
            parsed = fetch_transaction(
                session, info.signature, commitment=commitment, owner=wallet, owned_accounts=owned,
                tenant_id=tenant_id, wallet_id=wallet_id, observed_at=observed_at,
                unknown_extensions=extensions,
            )
            transactions.append(parsed)
            if parsed.status == "unknown":
                page_complete = False
                gaps.append(UNKNOWN_HISTORY_GAP)
                break
            if parsed.status != "success":
                continue
            transfers.extend(_capture(parsed, info, wallet, owned))
    elif fetch_transactions and not page_complete:
        pass
    authorities = diff_authorities(
        accounts,
        prior=prior_authorities,
        instructions=tuple(item for tx in transactions if tx.status == "success" for item in tx.instructions),
        labels=labels,
        current_read_ok=True,
        previous_cursor=previous_cursor,
    )
    if not authorities.complete:
        for reason in authorities.reasons:
            if reason not in gaps:
                gaps.append(reason)
    gaps = tuple(dict.fromkeys(gaps))
    advanced = page_complete and not gaps and gap_detection
    cursor = _success_cursor(signatures, previous_cursor) if advanced else previous_cursor
    observations = _observations(
        native_lamports=native.raw_lamports,
        native_slot=native.slot,
        native_finality=native.observation.checkpoint_status if native.observation is not None else "unknown",
        accounts=accounts,
        transfers=tuple(transfers),
        gaps=gaps,
        cursor=cursor,
        pagination_complete=page_complete,
        gap_detection_complete=gap_detection,
        previous_success_at=previous_success_at if not advanced else None,
        authorities=authorities,
    )
    return OwnerScan(
        accounts=accounts,
        transfers=tuple(transfers),
        signatures=signatures,
        observations=observations,
        native_lamports=native.raw_lamports,
        cursor=cursor,
        gaps=gaps,
        refused=False,
        blocked=False,
        reason=gaps[0] if gaps else "",
        probe=PROBE_UNTESTED,
        pagination_complete=page_complete,
        gap_detection_complete=gap_detection,
        authorities=authorities,
        transactions=tuple(transactions),
    )


def _capture(
    parsed: ParsedTransaction,
    info: SignatureInfo,
    wallet: str,
    owned: set[str],
) -> list[CapturedTransfer]:
    found: list[CapturedTransfer] = []
    for delta in parsed.token_deltas:
        if delta.delta == 0:
            continue
        if delta.account not in owned and delta.owner != wallet:
            continue
        found.append(CapturedTransfer(
            signature=parsed.signature or info.signature,
            mint=delta.mint,
            amount=abs(delta.delta),
            decimals=delta.decimals,
            account=delta.account,
            owner=delta.owner,
            owner_in_account_keys=parsed.owner_in_account_keys,
            via=info.via,
            delta=delta.delta,
        ))
    return found


def _observations(
    *,
    native_lamports: int | None,
    native_slot: int | None,
    native_finality: str,
    accounts: Sequence[TokenAccountView],
    transfers: Sequence[CapturedTransfer],
    gaps: Sequence[str],
    cursor: str | None,
    pagination_complete: bool,
    gap_detection_complete: bool,
    previous_success_at: str | None,
    authorities: AuthorityReport,
) -> dict[str, CoverageObservation]:
    clean = pagination_complete and gap_detection_complete and not gaps
    native_category = "ok" if native_lamports is not None else "error"
    fungible_category = "empty" if clean and not accounts else "ok" if clean else "partial"
    if UNKNOWN_HISTORY_GAP in gaps or CLOSED_HISTORY_GAP in gaps or PAGE_CAP_GAP in gaps or "budget" in gaps:
        fungible_category = "partial"
    reasons = tuple(gaps)
    return {
        "balances.native": make_observation(
            category=native_category,
            pagination_complete=native_lamports is not None,
            gap_detection_complete=native_lamports is not None,
            cursor=None if native_slot is None else str(native_slot),
            declared="latest_balance",
            slot=native_slot,
            finality=native_finality,
            last_success_at=previous_success_at if native_lamports is None else None,
        ),
        "balances.fungible": make_observation(
            category=fungible_category,
            pagination_complete=pagination_complete,
            gap_detection_complete=gap_detection_complete,
            gaps=reasons,
            cursor=cursor,
            declared="owner_token_accounts",
            reason_codes=reasons,
            slot=native_slot,
            finality=native_finality,
            last_success_at=previous_success_at if not clean else None,
        ),
        "history.fungible": make_observation(
            category="ok" if clean and transfers else "empty" if clean else "partial",
            pagination_complete=pagination_complete,
            gap_detection_complete=gap_detection_complete,
            gaps=reasons,
            cursor=cursor,
            declared="token_account_signatures",
            reason_codes=reasons,
            slot=native_slot,
            finality=native_finality,
        ),
        "history.native_external": make_observation(
            category="empty" if clean else "partial",
            pagination_complete=pagination_complete,
            gap_detection_complete=gap_detection_complete,
            gaps=reasons,
            cursor=cursor,
            declared="wallet_signatures",
            reason_codes=reasons,
            slot=native_slot,
            finality=native_finality,
        ),
        "permissions.spl_delegate": authorities.observation,
        "permissions.spl_authority": authorities.observation,
    }


def _failed(
    previous_cursor: str | None,
    reason: str,
    blocked: bool,
    probe: str,
    *,
    refused: bool,
    native: int | None,
    native_observation: CoverageObservation | None = None,
    previous_success_at: str | None = None,
    partial_accounts: tuple[TokenAccountView, ...] = (),
) -> OwnerScan:
    observation = make_observation(
        category="error" if reason != "budget" else "partial",
        pagination_complete=False,
        gap_detection_complete=False,
        gaps=(reason,) if reason else (),
        cursor=previous_cursor,
        declared="owner_token_accounts",
        reason_codes=(reason,) if reason else (),
        last_success_at=previous_success_at,
    )
    native_obs = native_observation or make_observation(
        category="error" if native is None else "ok",
        pagination_complete=native is not None,
        gap_detection_complete=native is not None,
        cursor=previous_cursor,
        declared="latest_balance",
        reason_codes=() if native is not None else ((reason,) if reason else ()),
        last_success_at=previous_success_at if native is None else None,
    )
    authorities = diff_authorities(
        (),
        prior=None,
        current_read_ok=False,
        previous_cursor=previous_cursor,
        read_reason=reason or "read_error",
    )
    return OwnerScan(
        accounts=partial_accounts,
        transfers=(),
        signatures=(),
        observations={
            "balances.native": native_obs,
            "balances.fungible": observation,
            "history.fungible": observation,
            "history.native_external": observation,
            "permissions.spl_delegate": authorities.observation,
            "permissions.spl_authority": authorities.observation,
        },
        native_lamports=native,
        cursor=previous_cursor,
        gaps=(reason,) if reason else (),
        refused=refused,
        blocked=blocked,
        reason=reason,
        probe=PROBE_BLOCKED if blocked else probe,
        pagination_complete=False,
        gap_detection_complete=False,
        authorities=authorities,
        transactions=(),
    )


def _signature(item: Any, address: str, via: str) -> SignatureInfo | None:
    if not isinstance(item, Mapping):
        return None
    signature = item.get("signature")
    if not isinstance(signature, str) or not signature:
        return None
    slot = item.get("slot")
    if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or slot < 0):
        return None
    return SignatureInfo(signature, None if slot is None else slot, item.get("err"), address, via)


def _dedupe_signatures(items: Sequence[SignatureInfo]) -> tuple[SignatureInfo, ...]:
    seen: set[str] = set()
    ordered: list[SignatureInfo] = []
    for item in items:
        if item.signature in seen:
            continue
        seen.add(item.signature)
        ordered.append(item)
    return tuple(ordered)


def _success_cursor(signatures: Sequence[SignatureInfo], previous: str | None) -> str | None:
    if not signatures:
        return previous if previous is not None else "empty"
    ranked = [item for item in signatures if item.slot is not None]
    if ranked:
        return max(ranked, key=lambda item: item.slot or 0).signature
    return signatures[0].signature


def _looks_like_signature(value: str | None) -> bool:
    if not isinstance(value, str) or not value:
        return False
    # A stored cursor may be an opaque marker. Only a base58 signature is an `until`.
    from guardian.identities import IdentityError, _b58decode

    try:
        raw = _b58decode(value)
    except IdentityError:
        return False
    return len(raw) == 64
