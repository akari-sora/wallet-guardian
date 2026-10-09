"""Read-only Esplora adapter for a Bitcoin address set.

GET only. Confirmed balance is chain_stats funded_txo_sum minus spent_txo_sum,
in integer satoshi. Mempool stats are recorded separately and are never added
into that number. History walks /address/{address}/txs/chain, 25 transactions
per page, and stops at a hard page cap. A short page is complete. A capped walk
is partial and keeps the oldest txid as the cursor. Fee is not an address
event. Six confirmations are the alerting threshold (finality ``confirmed``);
shallower and mempool transactions stay ``observed`` and may be replaced.
Nothing here signs, builds, or broadcasts. POST /tx and /tx/push are not called.

offline fixtures; live probe: see docs/coverage-matrix.md
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from guardian.coverage import CoverageObservation
from guardian.errors import SecurityError
from guardian.events import ChainEvent, finality_for
from guardian.identities import IdentityError, parse_address
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

PAGE_SIZE = 25
PAGE_CAP = 4
FINAL_CONFIRMATIONS = 6
DECLARED_SCOPE = "address_set_not_hd_wallet"
_TXID = re.compile(r"^[0-9a-fA-F]{64}$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_GAPS = (
    ("balances.fungible", "no_tokens"),
    ("balances.nft", "no_ordinals"),
    ("history.fungible", "no_runes"),
    ("history.nft", "no_ordinals"),
    ("history.native_internal", "no_xpub"),
    ("positions", "no_lightning"),
    ("withdrawals", "no_xpub"),
    ("bridges", "unsupported"),
)


@dataclass(frozen=True)
class _Got:
    ok: bool
    body: Any
    reason: str
    blocked: bool


@dataclass(frozen=True)
class BitcoinRead:
    """One address. ``confirmed_sats`` is None when the source did not answer."""

    confirmed_sats: int | None
    balance: CoverageObservation
    history: CoverageObservation
    events: tuple[ChainEvent, ...]
    cursor: dict[str, str] | None


def bitcoin_gap_observations() -> dict[str, CoverageObservation]:
    """Capabilities this adapter does not read. Explicit, never a completed scan."""
    found = {}
    for capability, reason in _GAPS:
        codes = ("unsupported",) if reason == "unsupported" else ("unsupported", reason)
        found[capability] = CoverageObservation(
            category="empty",
            pagination_complete=False,
            gap_detection_complete=False,
            declared="unsupported",
            reason_codes=codes,
            trust="unverified",
            price_quality="unknown",
        )
    return found


def read_bitcoin_address(
    transport: Any,
    source_id: str,
    address: str,
    entry: Mapping[str, Any],
    *,
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    page_cap: int = PAGE_CAP,
) -> BitcoinRead:
    """Read one watch address. A bad address returns before any request."""
    try:
        canonical = parse_address("bitcoin", address).canonical
    except IdentityError as exc:
        return _refused(exc.reason or "bad_address", source_id)
    if canonical != address or "/" in canonical or ".." in canonical:
        return _refused("bad_address", source_id)
    prefix, prefix_reason = _api_prefix(transport, source_id)
    if prefix_reason:
        blocked = prefix_reason in {"endpoint_missing", "endpoint_path_unsupported"}
        return _refused(prefix_reason, source_id, blocked=blocked)
    client = _Client(transport, source_id, prefix)
    height, height_read = client.tip_height()
    tip_hash = client.tip_hash()
    if not height_read.ok or height is None:
        stats, stats_read = client.address_stats(canonical)
        balance_sats, balance = _balance(stats if stats_read.ok else None, stats_read, source_id, height, tip_hash)
        history = _error_observation(height_read.reason or "scan_head_unreadable", source_id, blocked=height_read.blocked)
        return BitcoinRead(balance_sats, balance, history, (), None)
    stats, stats_read = client.address_stats(canonical)
    balance_sats, balance = _balance(stats if stats_read.ok else None, stats_read, source_id, height, tip_hash)
    if not stats_read.ok:
        history = _error_observation(stats_read.reason or "read_error", source_id, blocked=stats_read.blocked)
        return BitcoinRead(None, balance, history, (), None)
    mempool, mempool_read = client.mempool(canonical)
    if not mempool_read.ok or not isinstance(mempool, list):
        history = _error_observation(
            mempool_read.reason or "mempool_unreadable",
            source_id,
            blocked=mempool_read.blocked,
        )
        return BitcoinRead(balance_sats, balance, history, (), None)
    walked = client.confirmed_pages(canonical, page_cap)
    if walked.error:
        history = _error_observation(walked.reason, source_id, blocked=walked.blocked)
        return BitcoinRead(balance_sats, balance, history, (), None)
    built = _events_from(
        walked.transactions,
        mempool,
        canonical,
        entry,
        height,
        tenant_id=tenant_id,
        wallet_id=wallet_id,
        observed_at=observed_at,
        source_id=source_id,
    )
    pending = built.mempool_count > 0 or built.shallow_count > 0
    partial = not walked.complete or built.malformed or pending
    reasons: list[str] = []
    gaps: list[str] = []
    if not walked.complete:
        reasons.append("page_cap")
        gaps.append("page_cap")
    if built.malformed:
        reasons.append("malformed_amount")
        gaps.append("malformed_amount")
    if pending:
        reasons.append("pending")
    cursor_token = walked.resume_txid if not walked.complete else (walked.newest_txid or f"height:{height}")
    history = CoverageObservation(
        category="partial" if partial else ("empty" if not built.events else "ok"),
        pagination_complete=walked.complete and not built.malformed,
        gap_detection_complete=walked.complete and not built.malformed and not pending,
        gaps=tuple(gaps),
        cursor=cursor_token,
        declared=DECLARED_SCOPE,
        checkpoint_kind="height",
        checkpoint_number=str(height),
        checkpoint_hash=tip_hash,
        checkpoint_status="observed",
        sources=(source_id,),
        trust="single_provider",
        price_quality="unknown",
        reason_codes=tuple(reasons),
    )
    cursor = {
        "block_number": str(height),
        "capability": "history.native_external",
        "cursor": cursor_token,
        "network": _network(entry),
        "wallet_id": wallet_id,
    }
    return BitcoinRead(balance_sats, balance, history, built.events, cursor)


@dataclass(frozen=True)
class _Walk:
    transactions: tuple[Mapping[str, Any], ...]
    complete: bool
    malformed: bool
    resume_txid: str | None
    newest_txid: str | None
    error: bool
    reason: str
    blocked: bool


@dataclass(frozen=True)
class _Built:
    events: tuple[ChainEvent, ...]
    mempool_count: int
    shallow_count: int
    malformed: bool


class _Client:
    def __init__(self, transport: Any, source_id: str, prefix: str) -> None:
        self.transport = transport
        self.source_id = source_id
        self.prefix = prefix
        self.clock: Callable[[], float] = lambda: 0.0
        self.sleep: Callable[[float], None] = lambda _seconds: None

    def tip_height(self) -> tuple[int | None, _Got]:
        got = self._get(self.prefix + "/blocks/tip/height")
        if not got.ok:
            return None, got
        text = got.body.strip() if isinstance(got.body, str) else ""
        if not text.isdigit():
            return None, _Got(False, None, "scan_head_unreadable", False)
        return int(text), got

    def tip_hash(self) -> str | None:
        got = self._get(self.prefix + "/blocks/tip/hash")
        if not got.ok or not isinstance(got.body, str):
            return None
        text = got.body.strip().lower()
        if _HASH.fullmatch(text) is None:
            return None
        return text

    def address_stats(self, address: str) -> tuple[Any, _Got]:
        got = self._get(f"{self.prefix}/address/{address}")
        return got.body, got

    def mempool(self, address: str) -> tuple[Any, _Got]:
        got = self._get(f"{self.prefix}/address/{address}/txs/mempool")
        return got.body, got

    def confirmed_pages(self, address: str, page_cap: int) -> _Walk:
        if isinstance(page_cap, bool) or not isinstance(page_cap, int) or page_cap < 1:
            return _Walk((), False, False, None, None, True, "bad_page_cap", False)
        pages = 0
        last: str | None = None
        rows: list[Mapping[str, Any]] = []
        newest: str | None = None
        while pages < page_cap:
            path = f"{self.prefix}/address/{address}/txs/chain"
            if last is not None:
                path = f"{path}/{last}"
            got = self._get(path)
            if not got.ok:
                return _Walk((), False, False, last, newest, True, got.reason or "read_error", got.blocked)
            if not isinstance(got.body, list) or any(not isinstance(item, dict) for item in got.body):
                return _Walk((), False, False, last, newest, True, "bad_history", False)
            pages += 1
            batch = [item for item in got.body if isinstance(item, dict)]
            if newest is None and batch:
                token = batch[0].get("txid")
                if isinstance(token, str) and _TXID.fullmatch(token):
                    newest = token.lower()
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                return _Walk(tuple(rows), True, False, last, newest, False, "", False)
            token = batch[-1].get("txid")
            if not isinstance(token, str) or _TXID.fullmatch(token) is None:
                return _Walk((), False, False, last, newest, True, "bad_cursor", False)
            last = token.lower()
        return _Walk(tuple(rows), False, False, last, newest, False, "", False)

    def _get(self, path: str) -> _Got:
        if not path.startswith("/") or any(token in path for token in ("..", "%", "//", "?", "#")):
            return _Got(False, None, "bad_path", True)
        if path.rstrip("/").endswith("/tx") or "tx/push" in path:
            return _Got(False, None, "broadcast_denied", True)
        operation = Operation(protocol="rest", adapter="esplora", http_method="GET", path=path)

        def attempt() -> Any:
            return self.transport.request(self.source_id, operation)

        try:
            attempts = self.transport.retry_attempts(self.source_id)
        except Exception:
            attempts = 0
        try:
            result = read_with_retry(
                attempt,
                retry_attempts=attempts,
                sleep=self.sleep,
                random=lambda: 0.0,
                clock=self.clock,
            )
        except BudgetExhausted as exc:
            return _Got(False, None, exc.reason or "budget", True)
        except (TransportError, SecurityError) as exc:
            reason = getattr(exc, "reason", "") or "read_error"
            blocked = reason in {"endpoint_missing", "credential_missing"} or getattr(exc, "blocked", False)
            return _Got(False, None, reason, bool(blocked))
        if not getattr(result, "ok", False):
            reason = getattr(result, "reason", "") or "read_error"
            return _Got(False, None, reason, bool(getattr(result, "blocked", False)))
        return _Got(True, result.body, "", False)


def _events_from(
    confirmed: tuple[Mapping[str, Any], ...],
    mempool: list[Any],
    address: str,
    entry: Mapping[str, Any],
    tip_height: int,
    *,
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    source_id: str,
) -> _Built:
    events: list[ChainEvent] = []
    seen: set[str] = set()
    shallow = 0
    malformed = False
    for tx in confirmed:
        if not isinstance(tx, dict):
            malformed = True
            continue
        txid = _txid_of(tx)
        if txid is None:
            malformed = True
            continue
        rows = _movements(tx, address)
        if rows is None:
            malformed = True
            continue
        seen.add(txid)
        if not rows:
            continue
        finality, block_number, block_hash = _chain_finality(tx, tip_height, entry)
        if finality == "observed":
            shallow += 1
        _append_events(
            events, rows, txid, finality, block_number, block_hash,
            tx_status="success", entry=entry, tenant_id=tenant_id, wallet_id=wallet_id,
            observed_at=observed_at, source_id=source_id,
        )
    mempool_count = 0
    for tx in mempool:
        if not isinstance(tx, dict):
            malformed = True
            continue
        txid = _txid_of(tx)
        if txid is None or txid in seen:
            continue
        rows = _movements(tx, address)
        if rows is None:
            malformed = True
            continue
        if not rows:
            seen.add(txid)
            continue
        mempool_count += 1
        seen.add(txid)
        _append_events(
            events, rows, txid, "observed", None, None,
            tx_status="unknown", entry=entry, tenant_id=tenant_id, wallet_id=wallet_id,
            observed_at=observed_at, source_id=source_id,
        )
    return _Built(tuple(events), mempool_count, shallow, malformed)


def _append_events(
    events: list[ChainEvent],
    rows: list[tuple[str, str, int]],
    txid: str,
    finality: str,
    block_number: str | None,
    block_hash: str | None,
    *,
    tx_status: str,
    entry: Mapping[str, Any],
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    source_id: str,
) -> None:
    asset = _asset(entry)
    network = _network(entry)
    chain_uid = _chain_uid(entry, network)
    for kind, sub_index, amount in rows:
        if amount <= 0:
            continue
        try:
            event = ChainEvent(
                tenant_id=tenant_id,
                wallet_id=wallet_id,
                network=network,
                chain_uid=chain_uid,
                tx_id=txid,
                sub_index=sub_index,
                kind=kind,
                asset=asset,
                raw_amount=amount,
                block_number=block_number,
                block_hash=block_hash,
                tx_status=tx_status,
                finality=finality,
                source_ids=(source_id,),
                observed_at=observed_at,
            )
        except ValueError:
            continue
        if any(item.identity == event.identity for item in events):
            continue
        events.append(event)


def _movements(tx: Mapping[str, Any], address: str) -> list[tuple[str, str, int]] | None:
    """In and out from vin/vout. Fee is ignored. Change to another address is not ours."""
    vins = tx.get("vin")
    vouts = tx.get("vout")
    if not isinstance(vins, list) or not isinstance(vouts, list):
        return None
    rows: list[tuple[str, str, int]] = []
    for index, vin in enumerate(vins):
        if not isinstance(vin, dict):
            return None
        prevout = vin.get("prevout")
        if prevout is None:
            continue
        if not isinstance(prevout, dict):
            return None
        if prevout.get("scriptpubkey_address") != address:
            continue
        amount = _sats(prevout.get("value"))
        if amount is None:
            return None
        if amount > 0:
            rows.append(("asset_out", f"vin:{index}", amount))
    for index, vout in enumerate(vouts):
        if not isinstance(vout, dict):
            return None
        if vout.get("scriptpubkey_address") != address:
            continue
        amount = _sats(vout.get("value"))
        if amount is None:
            return None
        if amount > 0:
            rows.append(("asset_in", f"vout:{index}", amount))
    return rows


def _chain_finality(
    tx: Mapping[str, Any],
    tip_height: int,
    entry: Mapping[str, Any],
) -> tuple[str, str | None, str | None]:
    status = tx.get("status") if isinstance(tx.get("status"), dict) else {}
    block_hash = status.get("block_hash") if isinstance(status, dict) else None
    digest = block_hash.strip().lower() if isinstance(block_hash, str) else None
    if digest is not None and _HASH.fullmatch(digest) is None:
        digest = None
    if not isinstance(status, dict) or status.get("confirmed") is not True:
        return "observed", None, None
    height = _sats(status.get("block_height"))
    if height is None:
        return "unknown", None, digest
    text = entry.get("finality_policy")
    network = _network(entry)
    if not isinstance(text, str) or not text.strip():
        return "unknown", str(height), digest
    policy = {
        "confirmation_threshold": FINAL_CONFIRMATIONS,
        "family": "bitcoin",
        "finality_policy": text,
        "mode": "confirmation_threshold",
        "network": network,
    }
    try:
        finality = finality_for(network, height, tip_height, None, policy)
    except (TypeError, ValueError):
        finality = "unknown"
    if finality == "finalized":
        finality = "confirmed"
    return finality, str(height), digest


def _balance(
    stats: Any,
    read: _Got,
    source_id: str,
    height: int | None,
    tip_hash: str | None,
) -> tuple[int | None, CoverageObservation]:
    if not read.ok or not isinstance(stats, dict):
        return None, _error_observation(read.reason or "read_error", source_id, blocked=read.blocked)
    chain = stats.get("chain_stats")
    mempool = stats.get("mempool_stats")
    if not isinstance(chain, dict):
        return None, _error_observation("bad_balance", source_id, blocked=False)
    funded = _sats(chain.get("funded_txo_sum"))
    spent = _sats(chain.get("spent_txo_sum"))
    if funded is None or spent is None or spent > funded:
        return None, _error_observation("bad_balance", source_id, blocked=False)
    confirmed = funded - spent
    evidence: list[str] = []
    if isinstance(mempool, dict):
        mempool_funded = _sats(mempool.get("funded_txo_sum"))
        mempool_spent = _sats(mempool.get("spent_txo_sum"))
        if mempool_funded is None or mempool_spent is None:
            evidence.append("mempool_unknown")
        else:
            evidence.append(f"mempool_funded_sats:{mempool_funded}")
            evidence.append(f"mempool_spent_sats:{mempool_spent}")
    else:
        evidence.append("mempool_unknown")
    observation = CoverageObservation(
        category="ok",
        pagination_complete=True,
        gap_detection_complete=True,
        declared=DECLARED_SCOPE,
        checkpoint_kind="height",
        checkpoint_number=None if height is None else str(height),
        checkpoint_hash=tip_hash,
        checkpoint_status="observed" if height is not None else "unknown",
        sources=(source_id,),
        trust="single_provider",
        price_quality="unknown",
        evidence_refs=tuple(evidence),
    )
    return confirmed, observation


def _refused(reason: str, source_id: str, *, blocked: bool = False) -> BitcoinRead:
    observation = _error_observation(reason, source_id, blocked=blocked)
    return BitcoinRead(None, observation, observation, (), None)


def _error_observation(reason: str, source_id: str, *, blocked: bool) -> CoverageObservation:
    codes = [reason or "read_error"]
    if blocked or reason in {"rate_limited", "http_5xx", "endpoint_missing", "budget"}:
        if "blocked" not in codes:
            codes.append("blocked")
    sources = (source_id,) if source_id else ()
    return CoverageObservation(
        category="error",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="not_read",
        reason_codes=tuple(codes),
        trust="unverified",
        price_quality="unknown",
        sources=sources,
    )


def _api_prefix(transport: Any, source_id: str) -> tuple[str, str]:
    try:
        endpoint = transport.endpoints.get(source_id).endpoint
    except Exception:
        return "", "endpoint_missing"
    if not isinstance(endpoint, str) or not endpoint.strip():
        return "", "endpoint_missing"
    path = urlsplit(endpoint.strip()).path.rstrip("/")
    if path in {"", "/api"}:
        return path, ""
    return "", "endpoint_path_unsupported"


def _sats(value: Any) -> int | None:
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _txid_of(tx: Mapping[str, Any]) -> str | None:
    token = tx.get("txid")
    if not isinstance(token, str) or _TXID.fullmatch(token) is None:
        return None
    return token.lower()


def _network(entry: Mapping[str, Any]) -> str:
    network = entry.get("network")
    if isinstance(network, str) and network:
        return network
    return "bitcoin-mainnet"


def _asset(entry: Mapping[str, Any]) -> str:
    native = entry.get("native_asset")
    if isinstance(native, Mapping):
        identity = native.get("identity")
        if isinstance(identity, str) and identity:
            return identity
    return "bitcoin-native:BTC"


def _chain_uid(entry: Mapping[str, Any], network: str) -> str:
    identity = entry.get("identity")
    if isinstance(identity, Mapping):
        expected = identity.get("expected_value")
        if isinstance(expected, str) and expected.strip():
            return expected
    return network
