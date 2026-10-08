"""UXLINK token identities. The legacy token is not the new token and is not priced as one.

Identity is network plus address. symbol() is recorded only as an auxiliary note.
A legacy balance stays valuation_unknown: not zero, and not the new token's price.
Holding the legacy token does not establish migration eligibility.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionLeg, PositionObservation, contract_address, unknown_position
from guardian.protocols.network import expected_chain_id
from guardian.protocols.pins import UXLINK_SOURCES, UXLINK_SYMBOL_CHECK, UXLINK_TOKENS
from guardian.protocols.views import ViewPlugin, ViewSpec

_ROOT = Path(__file__).resolve().parents[3]
ASSETS_PATH = _ROOT / "config" / "assets" / "uxlink.json"
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_FORBIDDEN_TEXT = ("1:1", "redeem-only", "redeem_only", "redeemable")
_TOP_KEYS = frozenset({
    "documentation_only",
    "id",
    "notes",
    "retrieved",
    "sources",
    "symbol_check",
    "tokens",
})
_TOKEN_KEYS = frozenset({
    "address",
    "chain_id",
    "claimability",
    "migration_eligibility",
    "network",
    "price_mapping",
    "status",
    "symbol_check",
    "valuation",
})
_DOC_KEYS = frozenset({"network", "status", "subject"})
_STATUSES = frozenset({"deprecated_legacy", "documented_new_token"})
_CLAIMABILITY = frozenset({"not_assessed", "unknown"})
_SPECS = {
    "balanceOf(address)": ViewSpec(("address",), ("uint256",)),
    "decimals()": ViewSpec((), ("uint8",)),
    "symbol()": ViewSpec((), ("string",)),
}
_DOCS_ONLY = (
    {"network": "mantle-mainnet", "status": "documentation_only", "subject": "uxlink"},
    {"network": "opbnb-mainnet", "status": "documentation_only", "subject": "kinza"},
)


class AssetManifestError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class UxlinkToken:
    network: str
    chain_id: int
    address: str
    status: str
    claimability: str
    migration_eligibility: str | None
    price_mapping: str | None
    symbol_check: str

    @property
    def legacy(self) -> bool:
        return self.status == "deprecated_legacy"

    @property
    def identity(self) -> str:
        return f"{self.network}:{self.address}"


@dataclass(frozen=True)
class UxlinkBook:
    retrieved: str
    symbol_check: str
    sources: tuple[str, ...]
    tokens: tuple[UxlinkToken, ...]

    def token(self, network: str) -> UxlinkToken | None:
        for item in self.tokens:
            if item.network == network:
                return item
        return None


@dataclass(frozen=True)
class CallerQuote:
    """A caller-supplied price. The symbol is not part of the match."""

    network: str
    address: str
    price_usd: Decimal


class UxlinkHoldings(ViewPlugin):
    plugin_id = "uxlink_holdings"
    adapters = frozenset({"evm_rpc"})
    networks = frozenset(item["network"] for item in UXLINK_TOKENS)
    specs = _SPECS

    def observe(
        self,
        ctx: Any,
        *,
        wallet_id: str,
        wallet: str,
        network: str,
        manifest: Any,
        private: Mapping[str, Any] | None,
    ) -> PositionObservation:
        del private
        prior = ctx.prior if isinstance(ctx.prior, Mapping) else {}
        if network not in self.networks:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unsupported_network",
            )
        try:
            book = load_uxlink_file()
        except (AssetManifestError, OSError, json.JSONDecodeError):
            return unknown_position(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                prior=prior,
                reason="asset_manifest_invalid",
            )
        token = book.token(network)
        declared = normalize_address(contract_address(manifest, network, "token"))
        if token is None or declared != token.address:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="contract_mismatch",
            )
        owner = normalize_address(wallet)
        if owner is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="bad_address",
            )
        balance_read = ctx.eth_call(network, token.address, "balanceOf(address)", [owner])
        balance = _one(balance_read)
        if balance is None:
            return _failed(self.plugin_id, wallet_id, network, prior, balance_read.reason)
        if balance == 0:
            return PositionObservation(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                status="none_observed",
                cursor=f"clock:{ctx.clock()}",
            )
        decimals_read = ctx.eth_call(network, token.address, "decimals()", [])
        decimals = _one(decimals_read)
        if decimals is None or decimals > 255:
            return _failed(self.plugin_id, wallet_id, network, prior, decimals_read.reason)
        symbol_note = _symbol_note(ctx.eth_call(network, token.address, "symbol()", []))
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status="observed",
            legs=(PositionLeg(
                kind="underlying",
                identity=token.identity,
                raw_amount=balance,
                decimals=decimals,
                network=network,
                plugin_id=self.plugin_id,
                detail=_detail(token, symbol_note),
            ),),
            cursor=f"clock:{ctx.clock()}",
        )


def load_uxlink_file(path: Path | None = None) -> UxlinkBook:
    file = ASSETS_PATH if path is None else Path(path)
    document = json.loads(file.read_text(encoding="utf-8"))
    return load_uxlink_manifest(document)


def load_uxlink_manifest(document: Mapping[str, Any]) -> UxlinkBook:
    """Reject unknown labels and any value that would mark the legacy token redeemable."""
    if not isinstance(document, Mapping):
        raise AssetManifestError("manifest_not_object")
    _reject_tree(document)
    extra = set(document) - _TOP_KEYS
    if extra:
        raise AssetManifestError("unknown_label")
    if document.get("id") != "uxlink":
        raise AssetManifestError("id")
    retrieved = document.get("retrieved")
    if not isinstance(retrieved, str) or _DATE.fullmatch(retrieved) is None:
        raise AssetManifestError("retrieved")
    if document.get("symbol_check") != UXLINK_SYMBOL_CHECK:
        raise AssetManifestError("symbol_check")
    sources = document.get("sources")
    if not isinstance(sources, list) or set(sources) != set(UXLINK_SOURCES) or any(not isinstance(item, str) for item in sources):
        raise AssetManifestError("sources")
    notes = document.get("notes", "")
    if not isinstance(notes, str):
        raise AssetManifestError("notes")
    _documentation(document.get("documentation_only"))
    tokens = _tokens(document.get("tokens"))
    _match_pins(tokens)
    return UxlinkBook(
        retrieved=retrieved,
        symbol_check=UXLINK_SYMBOL_CHECK,
        sources=tuple(UXLINK_SOURCES),
        tokens=tokens,
    )


def parse_price_input(data: bytes) -> tuple[CallerQuote, ...]:
    """Caller-supplied quotes. A symbol-only row is ignored. Non-positive prices are ignored."""
    if not isinstance(data, (bytes, bytearray)) or not data.strip():
        raise AssetManifestError("price_unparseable")
    try:
        document = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssetManifestError("price_unparseable") from exc
    rows = document.get("quotes") if isinstance(document, Mapping) else document
    if not isinstance(rows, list):
        raise AssetManifestError("price_unparseable")
    quotes: list[CallerQuote] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise AssetManifestError("price_unparseable")
        network = row.get("network")
        address = normalize_address(row.get("address")) if isinstance(row.get("address"), str) else None
        raw_price = row.get("price_usd")
        if not isinstance(network, str) or not network or address is None or not isinstance(raw_price, str):
            continue
        try:
            price = Decimal(raw_price)
        except InvalidOperation:
            raise AssetManifestError("price_unparseable") from None
        if price <= 0:
            continue
        quotes.append(CallerQuote(network, address, price))
    return tuple(quotes)


def public_leg(leg: PositionLeg, quotes: Sequence[CallerQuote] | None = None) -> dict[str, Any]:
    """One report leg. Legacy UXLINK never receives another token's price."""
    labels = {key: value for key, value in leg.detail}
    payload: dict[str, Any] = {
        "decimals": leg.decimals,
        "identity": leg.identity,
        "kind": leg.kind,
        "labels": labels,
        "network": leg.network,
        "raw_amount": leg.raw_amount,
    }
    if leg.plugin_id != UxlinkHoldings.plugin_id:
        return payload
    valuation, priced = value_uxlink_leg(leg, quotes or ())
    payload["valuation"] = valuation
    payload["value_usd"] = None if priced is None else format(priced, "f")
    if valuation == "valuation_unknown":
        labels = dict(labels)
        labels["valuation"] = "valuation_unknown"
        payload["labels"] = labels
    return payload


def value_uxlink_leg(leg: PositionLeg, quotes: Sequence[CallerQuote]) -> tuple[str, Decimal | None]:
    """Legacy value stays unknown. A new-token quote matches network and address only."""
    labels = {key: value for key, value in leg.detail}
    if labels.get("status") == "deprecated_legacy" or labels.get("price_mapping") == "separate_from_new_token":
        return "valuation_unknown", None
    if leg.decimals is None or ":" not in leg.identity:
        return "valuation_unknown", None
    address = leg.identity.split(":", 1)[1]
    match = next(
        (
            quote for quote in quotes
            if quote.network == leg.network and quote.address == address and quote.price_usd > 0
        ),
        None,
    )
    if match is None:
        return "valuation_unknown", None
    priced = (Decimal(leg.raw_amount) * match.price_usd) / (Decimal(10) ** leg.decimals)
    if priced <= 0:
        return "valuation_unknown", None
    return "valued", priced.normalize()


def _detail(token: UxlinkToken, symbol_note: str) -> tuple[tuple[str, str], ...]:
    rows = [
        ("claimability", token.claimability),
        ("status", token.status),
        ("symbol_check", token.symbol_check),
        ("symbol_observed", symbol_note),
    ]
    if token.migration_eligibility is not None:
        rows.append(("migration_eligibility", token.migration_eligibility))
    if token.price_mapping is not None:
        rows.append(("price_mapping", token.price_mapping))
    if token.legacy:
        rows.append(("valuation", "valuation_unknown"))
    return tuple(rows)


def _tokens(value: Any) -> tuple[UxlinkToken, ...]:
    if not isinstance(value, list) or len(value) != len(UXLINK_TOKENS):
        raise AssetManifestError("tokens")
    found: list[UxlinkToken] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) - _TOKEN_KEYS:
            raise AssetManifestError("unknown_label")
        network = item.get("network")
        chain_id = item.get("chain_id")
        address = normalize_address(item.get("address")) if isinstance(item.get("address"), str) else None
        status = item.get("status")
        claimability = item.get("claimability")
        if not isinstance(network, str) or address is None or status not in _STATUSES or claimability not in _CLAIMABILITY:
            raise AssetManifestError("token")
        if isinstance(chain_id, bool) or not isinstance(chain_id, int) or expected_chain_id(network) != chain_id:
            raise AssetManifestError("chain_id")
        if item.get("symbol_check") != UXLINK_SYMBOL_CHECK:
            raise AssetManifestError("symbol_check")
        migration = item.get("migration_eligibility", None)
        mapping = item.get("price_mapping", None)
        valuation = item.get("valuation", None)
        if migration is not None and migration != "unknown":
            raise AssetManifestError("migration_eligibility")
        if mapping is not None and mapping != "separate_from_new_token":
            raise AssetManifestError("price_mapping")
        if valuation is not None and valuation != "valuation_unknown":
            raise AssetManifestError("valuation")
        if status == "deprecated_legacy":
            if migration != "unknown" or claimability != "unknown" or mapping != "separate_from_new_token":
                raise AssetManifestError("legacy_labels")
            if valuation != "valuation_unknown":
                raise AssetManifestError("valuation")
        else:
            if claimability != "not_assessed" or migration is not None or mapping is not None or valuation is not None:
                raise AssetManifestError("new_token_labels")
        found.append(UxlinkToken(
            network=network,
            chain_id=chain_id,
            address=address,
            status=status,
            claimability=claimability,
            migration_eligibility=migration if isinstance(migration, str) else None,
            price_mapping=mapping if isinstance(mapping, str) else None,
            symbol_check=UXLINK_SYMBOL_CHECK,
        ))
    return tuple(found)


def _match_pins(tokens: Sequence[UxlinkToken]) -> None:
    if len({item.network for item in tokens}) != len(tokens) or len(tokens) != len(UXLINK_TOKENS):
        raise AssetManifestError("pin_mismatch")
    for pin in UXLINK_TOKENS:
        token = next((item for item in tokens if item.network == pin["network"]), None)
        if token is None or token.address != str(pin["token"]).lower():
            raise AssetManifestError("pin_mismatch")
        if token.chain_id != pin["chain_id"] or token.status != pin["status"] or token.claimability != pin["claimability"]:
            raise AssetManifestError("pin_mismatch")
        if token.migration_eligibility != pin.get("migration_eligibility"):
            raise AssetManifestError("pin_mismatch")
        if token.price_mapping != pin.get("price_mapping"):
            raise AssetManifestError("pin_mismatch")


def _documentation(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise AssetManifestError("documentation_only")
    rows = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _DOC_KEYS:
            raise AssetManifestError("unknown_label")
        if item.get("status") != "documentation_only":
            raise AssetManifestError("documentation_only")
        if "address" in item:
            raise AssetManifestError("documentation_only")
        rows.append({"network": item.get("network"), "status": item.get("status"), "subject": item.get("subject")})
    if sorted(rows, key=lambda row: (str(row["network"]), str(row["subject"]))) != list(_DOCS_ONLY):
        raise AssetManifestError("documentation_only")


def _reject_tree(value: Any) -> None:
    if isinstance(value, bool):
        raise AssetManifestError("forbidden_label")
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).strip().lower()
            if folded in {"claimable", "redeem-only", "redeem_only", "redeemable"}:
                raise AssetManifestError("forbidden_label")
            _reject_text(folded)
            _reject_tree(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_tree(item)
        return
    if isinstance(value, str):
        _reject_text(value)


def _reject_text(value: str) -> None:
    folded = value.casefold()
    if any(needle in folded for needle in _FORBIDDEN_TEXT):
        raise AssetManifestError("forbidden_label")
    if folded in {"claimable", "true"}:
        raise AssetManifestError("forbidden_label")


def _symbol_note(read: Any) -> str:
    """Auxiliary only. A match is not identity, and the raw chain string is not copied out."""
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return "not_read"
    value = read.decoded[0]
    if isinstance(value, str) and value == "UXLINK":
        return "uxlink"
    return "not_uxlink"


def _one(read: Any) -> int | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    value = read.decoded[0]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT256_MAX:
        return None
    return value


def _failed(plugin_id: str, wallet_id: str, network: str, prior: Mapping[str, Any], detail: str) -> PositionObservation:
    return unknown_position(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        prior=prior,
        reason="read_failed",
        detail=detail or "read_error",
    )
