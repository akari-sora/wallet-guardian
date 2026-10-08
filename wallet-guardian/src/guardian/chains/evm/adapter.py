"""T0 EVM orchestration. Parsing stays here; sockets stay in the transport.

eth_chainId is checked before anything is recorded. Native history is not
inferred from eth_getLogs. A failed read keeps the previous cursor and does
not become a zero balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from guardian.chains.evm.accounting import TRANSFER_TOPIC, TokenLedger, account_logs, pad_address
from guardian.chains.evm.receipts import events_from_receipt
from guardian.coverage import CoverageObservation, build_coverage, portfolio_summary
from guardian.errors import SecurityError
from guardian.identities import IdentityError, parse_address
from guardian.security.transport_policy import Operation
from guardian.sources.etherscan import HistoryRead
from guardian.sources.router import SourceRouter

LOG_RANGE_MAX = 2_000
_PROBE = "untested"
_ETHERSCAN_ACTION = {
    "history.fungible": "tokentx",
    "history.native_external": "txlist",
    "history.native_internal": "txlistinternal",
}
_BLOCKSCOUT_TAIL = {
    "history.fungible": "token-transfers",
    "history.native_external": "transactions",
    "history.native_internal": "internal-transactions",
}


def evm_rpc(method: str, params: list[Any]) -> Operation:
    return Operation(protocol="jsonrpc", adapter="evm_rpc", method=method, params=params)


def chain_id_operation() -> Operation:
    return evm_rpc("eth_chainId", [])


def balance_operation(address: str) -> Operation:
    return evm_rpc("eth_getBalance", [address, "latest"])


def receipt_operation(tx_hash: str) -> Operation:
    return evm_rpc("eth_getTransactionReceipt", [tx_hash])


def token_log_operation(address: str, start: int, end: int) -> Operation:
    return evm_rpc("eth_getLogs", [{
        "fromBlock": hex(start),
        "toBlock": hex(end),
        "topics": [TRANSFER_TOPIC, None, pad_address(address)],
    }])


def hex_quantity(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith(("0x", "0X")):
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def to_observation(
    read: HistoryRead,
    *,
    source: str,
    scope_from: str | None = None,
    scope_to: str | None = None,
    declared: str = "block_range",
) -> CoverageObservation:
    complete = read.category in {"ok", "empty"} and read.pagination_complete and not read.gaps and not read.blocked
    return CoverageObservation(
        category=read.category,
        pagination_complete=read.pagination_complete,
        gap_detection_complete=read.gap_detection_complete,
        gaps=read.gaps,
        cursor=read.cursor,
        declared=declared,
        scope_from=scope_from,
        scope_to=scope_to,
        sources=(source,),
        trust="single_provider" if complete else "unverified",
        reason_codes=read.reason_codes,
    )


@dataclass(frozen=True)
class IdentityCheck:
    ok: bool
    reason: str
    chain_id: int | None
    probe: str = _PROBE


@dataclass(frozen=True)
class BalanceRead:
    raw_wei: str | None
    price: None
    observation: CoverageObservation | None
    refused: bool
    reason: str
    probe: str = _PROBE


@dataclass(frozen=True)
class ReceiptRead:
    events: tuple[Any, ...]
    fee_wei: str | None
    fee_incomplete: bool
    refused: bool
    reason: str
    probe: str = _PROBE


@dataclass(frozen=True)
class HistoryFetch:
    capability: str
    items: tuple[Any, ...]
    observation: CoverageObservation | None
    refused: bool
    reason: str
    probe: str = _PROBE
    ledger: TokenLedger | None = None


class _RouterBody:
    def __init__(self, read: HistoryRead) -> None:
        self.body = read
        self.reason = read.reason or read.category
        self.quality = "error" if read.category == "error" or read.blocked else "partial"
        self.ok = read.category in {"ok", "empty"} and read.pagination_complete and not read.blocked and not read.gaps


class EvmAdapter:
    def __init__(
        self,
        entry: Mapping[str, Any],
        transport: Any,
        *,
        rpc_source_id: str,
        etherscan: Any = None,
        blockscout: Any = None,
        nodereal: Any = None,
    ) -> None:
        chain_id = entry.get("evm_chain_id")
        native = entry.get("native_asset")
        if entry.get("family") != "evm" or isinstance(chain_id, bool) or not isinstance(chain_id, int):
            raise ValueError("not_evm")
        if not isinstance(native, Mapping) or not isinstance(native.get("identity"), str):
            raise ValueError("native_asset")
        policy = entry.get("finality_policy")
        if not isinstance(policy, str) or not policy.strip():
            raise ValueError("finality_policy")
        network = entry.get("network")
        if not isinstance(network, str) or not network:
            raise ValueError("network")
        self.entry = entry
        self.network = network
        self.chain_id = chain_id
        self.native_asset = native["identity"]
        self.finality_policy = policy
        self.transport = transport
        self.rpc_source_id = rpc_source_id
        self.etherscan = etherscan
        self.blockscout = blockscout
        self.nodereal = nodereal

    def open(self) -> EvmSession:
        try:
            result = self.transport.request(self.rpc_source_id, chain_id_operation())
        except SecurityError:
            raise
        except Exception:
            return EvmSession(self, IdentityCheck(False, "identity_unreadable", None))
        if not getattr(result, "ok", False) or not isinstance(result.body, dict):
            return EvmSession(self, IdentityCheck(False, "identity_unreadable", None))
        return EvmSession(self, interpret_chain_id(result.body.get("result"), self.chain_id))

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


class EvmSession:
    def __init__(self, adapter: EvmAdapter, identity: IdentityCheck) -> None:
        self.adapter = adapter
        self.identity = identity
        self.probe = _PROBE

    def native_balance(self, address: str) -> BalanceRead:
        if not self.identity.ok:
            return BalanceRead(None, None, None, True, self.identity.reason)
        wallet = _wallet(address)
        if wallet is None:
            return BalanceRead(None, None, _point_error("bad_address"), False, "bad_address")
        result = self.adapter.transport.request(self.adapter.rpc_source_id, balance_operation(wallet))
        if not getattr(result, "ok", False) or not isinstance(result.body, dict):
            return BalanceRead(None, None, _point_error("read_error"), False, "read_error")
        raw = hex_quantity(result.body.get("result"))
        if raw is None:
            return BalanceRead(None, None, _point_error("malformed_balance"), False, "malformed_balance")
        observation = CoverageObservation(
            category="ok",
            pagination_complete=True,
            gap_detection_complete=True,
            gaps=(),
            cursor="latest",
            declared="latest_balance",
            sources=("evm-rpc",),
            trust="single_provider",
        )
        return BalanceRead(str(raw), None, observation, False, "")

    def read_receipt(
        self,
        tx_hash: str,
        *,
        tenant_id: str,
        wallet_id: str,
        wallet: str,
        observed_at: str,
    ) -> ReceiptRead:
        if not self.identity.ok:
            return ReceiptRead((), None, False, True, self.identity.reason)
        owner = _wallet(wallet)
        if owner is None:
            return ReceiptRead((), None, True, False, "bad_address")
        result = self.adapter.transport.request(self.adapter.rpc_source_id, receipt_operation(tx_hash))
        if not getattr(result, "ok", False) or not isinstance(result.body, dict):
            return ReceiptRead((), None, True, False, "read_error")
        receipt = result.body.get("result")
        if not isinstance(receipt, dict):
            return ReceiptRead((), None, True, False, "receipt_missing")
        body = dict(receipt)
        got = body.get("transactionHash")
        if isinstance(got, str) and got.lower() != tx_hash.lower():
            return ReceiptRead((), None, True, True, "receipt_hash_mismatch")
        if "transactionHash" not in body:
            body["transactionHash"] = tx_hash
        effects = events_from_receipt(
            body,
            network=self.adapter.network,
            family="evm",
            finality_policy=self.adapter.finality_policy,
            chain_id=self.adapter.chain_id,
            native_asset=self.adapter.native_asset,
            tenant_id=tenant_id,
            wallet_id=wallet_id,
            wallet=owner,
            observed_at=observed_at,
        )
        return ReceiptRead(effects.events, effects.fee_wei, effects.fee_incomplete, False, effects.reason)

    def read_etherscan(
        self,
        capability: str,
        address: str,
        start_block: int,
        end_block: int,
        *,
        previous_cursor: str | None = None,
        offset: int = 1000,
        standard: str | None = None,
        routing: list[Mapping[str, Any]] | None = None,
        sources: Mapping[str, str] | None = None,
        catalog: Any = None,
        page_cap: int = 20,
        item_cap: int = 50_000,
    ) -> HistoryFetch:
        if not self.identity.ok:
            return _refused(capability, self.identity.reason)
        wallet = _wallet(address)
        if wallet is None:
            return _error_fetch(capability, previous_cursor, "bad_address")
        action = _etherscan_action(capability, standard)
        if action is None:
            return _error_fetch(capability, previous_cursor, "unknown_capability")
        if self.adapter.etherscan is None:
            return _error_fetch(capability, previous_cursor, "no_client")
        if routing is None:
            read = self._etherscan_read(action, wallet, start_block, end_block, previous_cursor, offset, page_cap, item_cap)
            return _pack(read, "etherscan", start_block, end_block)
        return self._routed(
            capability,
            routing,
            sources or {},
            catalog,
            previous_cursor,
            start_block,
            end_block,
            lambda: self._etherscan_read(action, wallet, start_block, end_block, previous_cursor, offset, page_cap, item_cap),
            "etherscan",
        )

    def read_blockscout(
        self,
        capability: str,
        address: str,
        *,
        previous_cursor: str | None = None,
    ) -> HistoryFetch:
        if not self.identity.ok:
            return _refused(capability, self.identity.reason)
        wallet = _wallet(address)
        tail = _BLOCKSCOUT_TAIL.get(capability)
        if wallet is None or tail is None or self.adapter.blockscout is None:
            return _error_fetch(capability, previous_cursor, "unknown_capability" if tail is None else "bad_address")
        path = f"/api/v2/addresses/{wallet}/{tail}"
        read = self.adapter.blockscout.walk(path, capability=capability, previous_cursor=previous_cursor)
        return _pack(read, "blockscout", None, None)

    def read_nodereal(
        self,
        category: str,
        address: str,
        start_block: int,
        end_block: int,
        *,
        side: str = "to",
        previous_cursor: str | None = None,
    ) -> HistoryFetch:
        if not self.identity.ok:
            return _refused(category, self.identity.reason)
        wallet = _wallet(address)
        if wallet is None or self.adapter.nodereal is None:
            return _error_fetch(category, previous_cursor, "bad_address")
        read = self.adapter.nodereal.transfers(
            category=category,
            address=wallet,
            start_block=start_block,
            end_block=end_block,
            side=side,
            previous_cursor=previous_cursor,
        )
        return _pack(read, "nodereal", start_block, end_block)

    def rpc_native_history(self, capability: str, *, previous_cursor: str | None = None) -> HistoryFetch:
        """RPC has no address index for native transfers. The gap is not a zero history."""
        if capability not in {"history.native_external", "history.native_internal"}:
            return _error_fetch(capability, previous_cursor, "unknown_capability")
        if not self.identity.ok:
            return _refused(capability, self.identity.reason)
        read = HistoryRead(
            capability,
            (),
            False,
            True,
            ("rpc_has_no_address_history",),
            previous_cursor,
            "error",
            False,
            "rpc_has_no_address_history",
            ("rpc_has_no_address_history",),
            "",
        )
        return _pack(read, "evm-rpc", None, None, declared="rpc_fallback")

    def token_history_from_logs(
        self,
        address: str,
        start_block: int,
        end_block: int,
        *,
        previous_cursor: str | None = None,
    ) -> HistoryFetch:
        """Bounded eth_getLogs token history. This does not fill native history."""
        capability = "history.fungible"
        if not self.identity.ok:
            return _refused(capability, self.identity.reason)
        wallet = _wallet(address)
        if wallet is None:
            return _error_fetch(capability, previous_cursor, "bad_address")
        if isinstance(start_block, bool) or isinstance(end_block, bool):
            return _error_fetch(capability, previous_cursor, "bad_range")
        if not isinstance(start_block, int) or not isinstance(end_block, int) or end_block < start_block:
            return _error_fetch(capability, previous_cursor, "bad_range")
        if end_block - start_block > LOG_RANGE_MAX:
            read = HistoryRead(
                capability,
                (),
                False,
                True,
                ("log_range_exceeds_bound",),
                previous_cursor,
                "partial",
                False,
                "log_range_exceeds_bound",
                ("log_range_exceeds_bound",),
                "",
            )
            return _pack(read, "evm-rpc", start_block, end_block, declared="bounded_logs")
        result = self.adapter.transport.request(
            self.adapter.rpc_source_id,
            token_log_operation(wallet, start_block, end_block),
        )
        if not getattr(result, "ok", False) or not isinstance(result.body, dict):
            return _error_fetch(capability, previous_cursor, "read_error")
        logs = result.body.get("result")
        if not isinstance(logs, list):
            return _error_fetch(capability, previous_cursor, "malformed_logs")
        ledger = account_logs([item for item in logs if isinstance(item, Mapping)], wallet)
        category = "empty" if not ledger.movements else "ok"
        read = HistoryRead(
            capability,
            ledger.movements,
            True,
            False,
            (),
            str(end_block),
            category,
            False,
            "",
            (),
            "",
        )
        fetched = _pack(read, "evm-rpc", start_block, end_block, declared="bounded_logs")
        return HistoryFetch(
            fetched.capability,
            fetched.items,
            fetched.observation,
            fetched.refused,
            fetched.reason,
            fetched.probe,
            ledger,
        )

    def _etherscan_read(
        self,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        previous_cursor: str | None,
        offset: int,
        page_cap: int,
        item_cap: int,
    ) -> HistoryRead:
        return self.adapter.etherscan.account_history(
            chain_id=self.adapter.chain_id,
            action=action,
            address=address,
            start_block=start_block,
            end_block=end_block,
            offset=offset,
            page_cap=page_cap,
            item_cap=item_cap,
            previous_cursor=previous_cursor,
        )

    def _routed(
        self,
        capability: str,
        routing: list[Mapping[str, Any]],
        sources: Mapping[str, str],
        catalog: Any,
        previous_cursor: str | None,
        start_block: int,
        end_block: int,
        read_direct,
        source: str,
    ) -> HistoryFetch:
        def send(source_id: str, _marker: Any) -> _RouterBody:
            if sources.get(source_id) != "etherscan_v2":
                return _RouterBody(_error_read(capability, previous_cursor, "fallback_not_etherscan"))
            return _RouterBody(read_direct())

        fetched = SourceRouter(routing, sources, catalog, send).fetch(
            self.adapter.network,
            capability,
            lambda _source_id: "account",
        )
        if isinstance(fetched.body, HistoryRead):
            return _pack(fetched.body, source, start_block, end_block)
        reason = fetched.reason_codes[0] if fetched.reason_codes else "error"
        return _pack(_error_read(capability, previous_cursor, reason, fetched.reason_codes), source, start_block, end_block)


def interpret_chain_id(result: Any, expected: int) -> IdentityCheck:
    got = hex_quantity(result)
    if got is None:
        return IdentityCheck(False, "identity_unreadable", None)
    if got != expected:
        return IdentityCheck(False, "chain_id_mismatch", got)
    return IdentityCheck(True, "", got)


def _wallet(address: str) -> str | None:
    try:
        return parse_address("evm", address).identity_key
    except IdentityError:
        return None


def _etherscan_action(capability: str, standard: str | None) -> str | None:
    if capability == "history.nft":
        if standard == "erc1155":
            return "token1155tx"
        if standard in {None, "erc721"}:
            return "tokennfttx"
        return None
    return _ETHERSCAN_ACTION.get(capability)


def _point_error(reason: str) -> CoverageObservation:
    return CoverageObservation(
        category="error",
        pagination_complete=False,
        gap_detection_complete=False,
        reason_codes=(reason,),
        sources=("evm-rpc",),
        trust="unverified",
        declared="latest_balance",
    )


def _refused(capability: str, reason: str) -> HistoryFetch:
    return HistoryFetch(capability, (), None, True, reason)


def _error_read(
    capability: str,
    previous: str | None,
    reason: str,
    codes: tuple[str, ...] = (),
) -> HistoryRead:
    return HistoryRead(capability, (), False, False, (), previous, "error", False, reason, codes or ((reason,) if reason else ()), "")


def _error_fetch(capability: str, previous: str | None, reason: str) -> HistoryFetch:
    read = _error_read(capability, previous, reason)
    return _pack(read, "evm-rpc", None, None)


def _pack(
    read: HistoryRead,
    source: str,
    start: int | None,
    end: int | None,
    *,
    declared: str = "block_range",
) -> HistoryFetch:
    observation = to_observation(
        read,
        source=source,
        scope_from=None if start is None else str(start),
        scope_to=None if end is None else str(end),
        declared=declared,
    )
    return HistoryFetch(read.capability, read.items, observation, False, read.reason, _PROBE, None)
