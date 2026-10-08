"""SPL delegate and authority diffs. Current account state wins.

A missing prior is first_observed, never a fresh grant. A failed current read
stays unknown and is not a revoke. Closing an account is closed, which may be
rent reclaim, not theft. Structural flags are not malicious labels. An unknown
Token-2022 extension means the permission coverage is not complete. This module
does not encode approve, revoke, setAuthority, or closeAccount.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from guardian.chains.solana.adapter import (
    DEFAULT_COMMITMENT,
    PROBE_UNTESTED,
    SPL_TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    UNKNOWN_EXTENSION,
    SolanaSession,
    canonical_address,
    make_observation,
    multiple_accounts_operation,
    parse_raw_amount,
)
from guardian.chains.solana.transactions import InstructionView
from guardian.coverage import CoverageObservation

_TOKEN_PROGRAMS = frozenset({SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM})
_KNOWN_EXTENSIONS = frozenset({
    "cpiGuard",
    "defaultAccountState",
    "immutableOwner",
    "interestBearingConfig",
    "memoTransfer",
    "metadataPointer",
    "mintCloseAuthority",
    "nonTransferable",
    "permanentDelegate",
    "tokenMetadata",
    "transferFeeAmount",
    "transferFeeConfig",
    "transferHook",
})
_CONFIDENTIAL_PREFIX = "confidentialTransfer"
_AUTHORITY_FIELDS = frozenset({
    "close_authority",
    "delegate",
    "freeze_authority",
    "mint_authority",
    "owner",
    "permanent_delegate",
})


@dataclass(frozen=True)
class TokenExtension:
    name: str
    known: bool
    delegate: str | None = None


@dataclass(frozen=True)
class PriorAuthority:
    known: bool
    delegate: str | None = None
    amount: int | None = None
    close_authority: str | None = None
    owner: str | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    permanent_delegate: str | None = None


@dataclass(frozen=True)
class AuthorityChange:
    subject: str
    field: str
    change: str
    previous: str | None
    current: str | None
    previous_amount: int | None
    current_amount: int | None
    same_amount: bool
    status: str
    reasons: tuple[str, ...]
    structural_flags: tuple[str, ...]
    label: str
    instruction_kinds: tuple[str, ...]


@dataclass(frozen=True)
class MintView:
    address: str
    mint_authority: str | None
    freeze_authority: str | None
    decimals: int | None
    supply: int | None
    extensions: tuple[TokenExtension, ...]
    unknown_extensions: tuple[str, ...]
    permanent_delegate: str | None


@dataclass(frozen=True)
class AuthorityReport:
    changes: tuple[AuthorityChange, ...]
    instructions: tuple[InstructionView, ...]
    complete: bool
    reasons: tuple[str, ...]
    cursor: str | None
    probe: str
    observation: CoverageObservation
    status: str
    mints: tuple[MintView, ...] = ()


@dataclass(frozen=True)
class MintRead:
    mints: tuple[MintView, ...]
    complete: bool
    reasons: tuple[str, ...]
    cursor: str | None
    probe: str
    blocked: bool


def parse_extensions(items: Any) -> tuple[TokenExtension, ...]:
    if items is None:
        return ()
    if not isinstance(items, list):
        return (TokenExtension("malformed", False),)
    found: list[TokenExtension] = []
    for item in items:
        if not isinstance(item, Mapping):
            found.append(TokenExtension("malformed", False))
            continue
        name = item.get("extension", item.get("type"))
        if not isinstance(name, str) or not name:
            found.append(TokenExtension("unnamed", False))
            continue
        known = name in _KNOWN_EXTENSIONS or name.startswith(_CONFIDENTIAL_PREFIX)
        delegate = None
        state = item.get("state")
        if isinstance(state, Mapping):
            delegate = canonical_address(state.get("delegate"))
        found.append(TokenExtension(name, known, delegate))
    return tuple(found)


def unknown_extension_names(extensions: Sequence[TokenExtension]) -> tuple[str, ...]:
    return tuple(item.name for item in extensions if not item.known)


def parse_mint_account(address: str, value: Any) -> MintView | None:
    """One jsonParsed mint. A null account is absent, not a cleared authority."""
    if value is None:
        return None
    info = _parsed_info(value, "mint")
    if info is None:
        return None
    extensions = parse_extensions(info.get("extensions"))
    permanent = next((item.delegate for item in extensions if item.name == "permanentDelegate"), None)
    decimals = info.get("decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        decimals = None
    return MintView(
        address=address,
        mint_authority=_optional_pubkey(info.get("mintAuthority")),
        freeze_authority=_optional_pubkey(info.get("freezeAuthority")),
        decimals=decimals,
        supply=parse_raw_amount(info.get("supply")),
        extensions=extensions,
        unknown_extensions=unknown_extension_names(extensions),
        permanent_delegate=permanent,
    )


def fetch_mints(
    session: SolanaSession,
    mints: Sequence[str],
    *,
    commitment: str = DEFAULT_COMMITMENT,
    previous_cursor: str | None = None,
) -> MintRead:
    addresses = []
    for mint in mints:
        canonical = canonical_address(mint)
        if canonical is None:
            return MintRead((), False, ("bad_address",), previous_cursor, PROBE_UNTESTED, False)
        addresses.append(canonical)
    if not addresses:
        return MintRead((), True, (), previous_cursor, PROBE_UNTESTED, False)
    if len(addresses) > 100:
        return MintRead((), False, ("bad_batch",), previous_cursor, PROBE_UNTESTED, False)
    try:
        operation = multiple_accounts_operation(addresses, commitment)
    except ValueError as exc:
        return MintRead((), False, (str(exc),), previous_cursor, PROBE_UNTESTED, False)
    outcome = session.rpc(operation)
    if not outcome.ok:
        return MintRead((), False, (outcome.reason or "read_error",), previous_cursor, outcome.probe, outcome.blocked)
    return _mints_from_result(addresses, outcome.result, previous_cursor)


def diff_authorities(
    accounts: Sequence[Any],
    *,
    prior: Mapping[str, PriorAuthority] | None,
    instructions: Sequence[InstructionView] = (),
    mints: Sequence[MintView] = (),
    labels: Mapping[str, str] | None = None,
    current_read_ok: bool = True,
    previous_cursor: str | None = None,
    read_reason: str = "",
) -> AuthorityReport:
    """Prior unknown is first_observed. Current state overrides instruction history."""
    token_instructions = tuple(
        item for item in instructions
        if item.program_id in _TOKEN_PROGRAMS and item.ix_type in {
            "approve", "approveChecked", "closeAccount", "revoke", "setAuthority",
        }
    )
    if not current_read_ok:
        observation = _observation(
            category="error",
            complete=False,
            gaps=(read_reason or "read_error",),
            cursor=previous_cursor,
            reasons=(read_reason or "read_error",),
        )
        return AuthorityReport(
            (), token_instructions, False, (read_reason or "read_error",), previous_cursor,
            PROBE_UNTESTED, observation, "unknown", tuple(mints),
        )
    changes: list[AuthorityChange] = []
    reasons: list[str] = []
    seen = {str(account.address) for account in accounts}
    for account in accounts:
        changes.extend(_account_changes(account, prior, token_instructions, labels or {}))
    for item in token_instructions:
        if item.ix_type == "closeAccount" and item.account and item.account not in seen:
            changes.append(AuthorityChange(
                subject=item.account,
                field="account",
                change="closed",
                previous=None,
                current=None,
                previous_amount=None,
                current_amount=None,
                same_amount=False,
                status="closed",
                reasons=("rent_reclaim_possible",),
                structural_flags=(),
                label="unlabeled",
                instruction_kinds=("closeAccount",),
            ))
            seen.add(item.account)
    for mint in mints:
        changes.extend(_mint_changes(mint, prior, token_instructions, labels or {}))
        if mint.unknown_extensions:
            reasons.append(UNKNOWN_EXTENSION)
    for account in accounts:
        unknown = tuple(getattr(account, "unknown_extensions", ()) or ())
        if unknown:
            reasons.append(UNKNOWN_EXTENSION)
    if any("event_state_mismatch" in change.reasons for change in changes):
        reasons.append("event_state_mismatch")
    complete = UNKNOWN_EXTENSION not in reasons and not any(change.status == "unknown" for change in changes)
    gap = (UNKNOWN_EXTENSION,) if UNKNOWN_EXTENSION in reasons else ()
    observation = _observation(
        category="ok" if complete else "partial",
        complete=complete,
        gaps=gap,
        cursor=previous_cursor if not complete else previous_cursor,
        reasons=tuple(dict.fromkeys(reasons)),
    )
    # A complete permission read may advance only when the caller supplies the next cursor.
    # This diff does not invent one; the scan keeps or replaces it.
    status = "unknown" if any(change.status == "unknown" for change in changes) else "observed"
    return AuthorityReport(
        tuple(changes), token_instructions, complete, tuple(dict.fromkeys(reasons)),
        previous_cursor, PROBE_UNTESTED, observation, status, tuple(mints),
    )


def _account_changes(
    account: Any,
    prior_map: Mapping[str, PriorAuthority] | None,
    instructions: Sequence[InstructionView],
    labels: Mapping[str, str],
) -> list[AuthorityChange]:
    subject = str(account.address)
    owner = canonical_address(getattr(account, "owner", None))
    related = [item for item in instructions if subject in {
        item.account, item.source_account, item.new_account,
    }]
    prior = None if prior_map is None else prior_map.get(subject)
    closed = any(item.ix_type == "closeAccount" and item.account == subject for item in instructions)
    rows = [
        _field_change(
            subject, "delegate", prior, None if prior is None else prior.delegate, account.delegate,
            None if prior is None else prior.amount, account.delegated_amount, related, labels, closed, owner,
        ),
        _field_change(
            subject, "close_authority", prior, None if prior is None else prior.close_authority,
            account.close_authority, None, None, related, labels, closed, owner,
        ),
        _field_change(
            subject, "owner", prior, None if prior is None else prior.owner, account.owner,
            None, None, related, labels, closed, owner,
        ),
    ]
    return [row for row in rows if row is not None]


def _mint_changes(
    mint: MintView,
    prior_map: Mapping[str, PriorAuthority] | None,
    instructions: Sequence[InstructionView],
    labels: Mapping[str, str],
) -> list[AuthorityChange]:
    subject = mint.address
    related = [item for item in instructions if item.account == subject or item.mint == subject]
    prior = None if prior_map is None else prior_map.get(subject)
    rows = [
        _field_change(
            subject, "mint_authority", prior, None if prior is None else prior.mint_authority,
            mint.mint_authority, None, None, related, labels, False, None,
        ),
        _field_change(
            subject, "freeze_authority", prior, None if prior is None else prior.freeze_authority,
            mint.freeze_authority, None, None, related, labels, False, None,
        ),
        _field_change(
            subject, "permanent_delegate", prior, None if prior is None else prior.permanent_delegate,
            mint.permanent_delegate, None, None, related, labels, False, None,
        ),
    ]
    return [row for row in rows if row is not None]


def _field_change(
    subject: str,
    field: str,
    prior: PriorAuthority | None,
    previous: str | None,
    current: str | None,
    previous_amount: int | None,
    current_amount: int | None,
    instructions: Sequence[InstructionView],
    labels: Mapping[str, str],
    closed: bool,
    owner: str | None,
) -> AuthorityChange | None:
    if field not in _AUTHORITY_FIELDS:
        return None
    prior_known = prior is not None and prior.known
    interesting = current is not None or (prior_known and previous is not None) or closed or _instructions_touch(field, instructions)
    if not interesting:
        return None
    reasons: list[str] = []
    kinds = tuple(item.ix_type for item in instructions if item.ix_type and _instructions_touch(field, (item,)))
    if closed:
        status = "closed"
        change = "closed"
        reasons.append("rent_reclaim_possible")
    elif not prior_known:
        change = "first_observed"
        status = "active" if current is not None else "inactive"
    elif previous != current:
        if previous and current and field == "delegate":
            change = "replaced"
        elif previous and current is None:
            change = "revoked"
        elif previous is None and current:
            change = "new"
        elif previous and current:
            change = "replaced"
        else:
            change = "revoked" if current is None else "new"
        status = "active" if current is not None else "revoked"
    elif field == "delegate" and previous_amount is not None and current_amount is not None and current_amount != previous_amount:
        change = "increased" if current_amount > previous_amount else "decreased"
        status = "active"
    else:
        change = "unchanged"
        status = "active" if current is not None else "inactive"
    if _mismatches_current(field, current, instructions):
        reasons.append("event_state_mismatch")
        if current is not None and change == "revoked":
            change = "unchanged"
            status = "active"
    label = _label(labels, current if field in {"delegate", "permanent_delegate", "close_authority", "freeze_authority"} else None)
    flags = _flags(field, current, label, owner)
    same_amount = (
        field == "delegate"
        and previous_amount is not None
        and current_amount is not None
        and previous_amount == current_amount
        and previous is not None
        and current is not None
        and previous != current
    )
    return AuthorityChange(
        subject=subject,
        field=field,
        change=change,
        previous=None if not prior_known else previous,
        current=current,
        previous_amount=None if not prior_known else previous_amount,
        current_amount=current_amount,
        same_amount=same_amount,
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        structural_flags=flags,
        label=label,
        instruction_kinds=kinds,
    )


def _instructions_touch(field: str, instructions: Sequence[InstructionView]) -> bool:
    for item in instructions:
        if field == "delegate" and item.ix_type in {"approve", "approveChecked", "revoke"}:
            return True
        if item.ix_type != "setAuthority":
            continue
        if field == "owner" and item.authority_type == "accountOwner":
            return True
        if field == "close_authority" and item.authority_type == "closeAccount":
            return True
        if field == "mint_authority" and item.authority_type == "mintTokens":
            return True
        if field == "freeze_authority" and item.authority_type == "freezeAccount":
            return True
    return False


def _mismatches_current(field: str, current: str | None, instructions: Sequence[InstructionView]) -> bool:
    relevant = [item for item in instructions if _instructions_touch(field, (item,))]
    if not relevant:
        return False
    last = relevant[-1]
    if field == "delegate":
        if last.ix_type == "revoke":
            return current is not None
        if last.ix_type in {"approve", "approveChecked"}:
            return last.delegate is not None and last.delegate != current
    if last.ix_type == "setAuthority":
        if last.new_authority_is_null:
            return current is not None
        if last.new_authority is not None:
            return last.new_authority != current
    return False


def _flags(field: str, current: str | None, label: str, owner: str | None) -> tuple[str, ...]:
    flags: list[str] = []
    if field == "delegate" and current is not None and current != owner:
        flags.append("external_delegate")
    if field == "delegate" and current is not None and label == "unlabeled":
        flags.append("unlabeled_delegate")
    if field == "close_authority" and current is not None and current != owner:
        flags.append("external_close_authority")
    if field == "permanent_delegate" and current is not None:
        flags.append("permanent_delegate")
    if field == "freeze_authority" and current is not None:
        flags.append("freeze_authority_present")
    return tuple(dict.fromkeys(flags))


def _label(labels: Mapping[str, str], address: str | None) -> str:
    if not address:
        return "unlabeled"
    value = labels.get(address)
    if not isinstance(value, str) or not value.strip():
        return "unlabeled"
    return value.strip()


def _mints_from_result(addresses: Sequence[str], result: Any, previous_cursor: str | None) -> MintRead:
    if not isinstance(result, Mapping) or not isinstance(result.get("value"), list):
        return MintRead((), False, ("malformed",), previous_cursor, PROBE_UNTESTED, False)
    values = result["value"]
    if len(values) != len(addresses):
        return MintRead((), False, ("malformed",), previous_cursor, PROBE_UNTESTED, False)
    found: list[MintView] = []
    reasons: list[str] = []
    for address, value in zip(addresses, values):
        if value is None:
            reasons.append("mint_account_missing")
            continue
        mint = parse_mint_account(address, value)
        if mint is None:
            return MintRead((), False, ("malformed_mint",), previous_cursor, PROBE_UNTESTED, False)
        found.append(mint)
        if mint.unknown_extensions:
            reasons.append(UNKNOWN_EXTENSION)
    complete = not reasons
    return MintRead(tuple(found), complete, tuple(dict.fromkeys(reasons)), previous_cursor, PROBE_UNTESTED, False)


def _parsed_info(value: Any, expected_type: str) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    data = value.get("data")
    if not isinstance(data, Mapping):
        data = value
    parsed = data.get("parsed") if isinstance(data, Mapping) else None
    if not isinstance(parsed, Mapping):
        return None
    if parsed.get("type") != expected_type or not isinstance(parsed.get("info"), Mapping):
        return None
    return parsed["info"]


def _optional_pubkey(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_address(value)


def _observation(
    *,
    category: str,
    complete: bool,
    gaps: tuple[str, ...],
    cursor: str | None,
    reasons: tuple[str, ...],
) -> CoverageObservation:
    return make_observation(
        category=category,
        pagination_complete=complete,
        gap_detection_complete=complete,
        gaps=gaps,
        cursor=cursor,
        declared="spl_authorities",
        reason_codes=reasons,
    )
