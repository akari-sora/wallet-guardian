"""Read-only plugin context. The context object has no transport and no write method.

RPC handles live in a process-local session table keyed by an opaque id. View
names come from the plugin class, never from manifest or news text.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

from guardian.chains.evm.adapter import chain_id_operation, hex_quantity
from guardian.chains.solana.adapter import (
    account_info_operation,
    genesis_hash_operation,
    multiple_accounts_operation,
)
from guardian.errors import SecurityError
from guardian.protocols.network import expected_chain_id, expected_genesis
from guardian.protocols.pins import STAKE_PROGRAM
from guardian.protocols.views import ViewSpec, decode_view, is_write, view_operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.transport import TransportError

_SESSIONS: dict[str, "_Session"] = {}


@dataclass(frozen=True)
class ViewRead:
    ok: bool
    decoded: tuple[Any, ...] | None
    raw: str | None
    reason: str


@dataclass(frozen=True)
class SolanaRead:
    ok: bool
    result: Any
    reason: str


@dataclass
class _Session:
    transport: Any
    sources: Mapping[str, str]
    specs: Mapping[str, ViewSpec]
    clock: int
    prior: Mapping[str, Any]


class PluginContext:
    """Views, Solana account reads, a caller clock, and prior state.

    ``session_id`` only names a row in the session table. It is not a transport.
    """

    __slots__ = ("plugin_id", "prior", "session_id")

    def __init__(self, plugin_id: str, prior: Mapping[str, Any], session_id: str) -> None:
        self.plugin_id = plugin_id
        self.prior = prior
        self.session_id = session_id

    def clock(self) -> int:
        return _SESSIONS[self.session_id].clock

    def eth_call(self, network: str, to: str, signature: str, args: Sequence[Any]) -> ViewRead:
        """One allowlisted view. Anything else raises and does not touch the transport."""
        session = _SESSIONS[self.session_id]
        spec = session.specs.get(signature)
        if spec is None or is_write(signature):
            raise ValueError("not_a_view_call")
        source_id = session.sources.get(network)
        if not source_id:
            return ViewRead(False, None, None, "missing_source")
        try:
            operation = view_operation(signature, spec, to, args)
        except ValueError as exc:
            if str(exc) == "not_a_view_call":
                raise
            return ViewRead(False, None, None, "bad_address")
        read = _jsonrpc(session.transport, source_id, operation)
        if not read.ok:
            return ViewRead(False, None, None, read.reason or "read_error")
        if not isinstance(read.result, str):
            return ViewRead(False, None, None, "malformed_result")
        decoded = decode_view(spec, read.result)
        if decoded is None:
            return ViewRead(False, None, read.result, "malformed_result")
        return ViewRead(True, decoded, read.result, "")

    def solana_account(self, address: str) -> SolanaRead:
        return self.solana_accounts([address])

    def solana_accounts(self, addresses: Sequence[str]) -> SolanaRead:
        session = _SESSIONS[self.session_id]
        source_id = _solana_source(session)
        if source_id is None:
            return SolanaRead(False, None, "missing_source")
        if not isinstance(addresses, Sequence) or isinstance(addresses, (str, bytes)):
            return SolanaRead(False, None, "bad_address")
        cleaned = [item for item in addresses if isinstance(item, str) and item]
        if len(cleaned) != len(addresses):
            return SolanaRead(False, None, "bad_address")
        if len(cleaned) == 1:
            operation = account_info_operation(cleaned[0])
        else:
            operation = multiple_accounts_operation(list(cleaned))
        return _jsonrpc(session.transport, source_id, operation)

    def solana_program_accounts(self, program_id: str, *, memcmp_offset: int, memcmp_bytes: str) -> SolanaRead:
        """Stake-program memcmp only. Other programs and offsets are refused before RPC."""
        if program_id != STAKE_PROGRAM:
            raise ValueError("not_a_read")
        session = _SESSIONS[self.session_id]
        source_id = _solana_source(session)
        if source_id is None:
            return SolanaRead(False, None, "missing_source")
        from guardian.protocols.stake import program_accounts_operation

        try:
            operation = program_accounts_operation(memcmp_bytes, memcmp_offset)
        except ValueError:
            raise ValueError("not_a_read") from None
        return _jsonrpc(session.transport, source_id, operation)


def check_evm_chain(transport: Any, source_id: str, network: str) -> tuple[bool, str]:
    expected = expected_chain_id(network)
    if expected is None:
        return False, "unsupported_network"
    read = _jsonrpc(transport, source_id, chain_id_operation())
    if not read.ok:
        return False, "chain_id_unreadable"
    got = hex_quantity(read.result)
    if got is None:
        return False, "chain_id_unreadable"
    if got != expected:
        return False, "chain_id_mismatch"
    return True, ""


def check_solana_genesis(transport: Any, source_id: str, network: str) -> tuple[bool, str]:
    expected = expected_genesis(network)
    if not expected:
        return False, "unsupported_network"
    read = _jsonrpc(transport, source_id, genesis_hash_operation())
    if not read.ok:
        return False, "genesis_unreadable"
    if not isinstance(read.result, str) or read.result != expected:
        return False, "genesis_mismatch" if isinstance(read.result, str) else "genesis_unreadable"
    return True, ""


@contextmanager
def open_plugin_context(
    plugin_id: str,
    specs: Mapping[str, ViewSpec],
    *,
    transport: Any,
    sources: Mapping[str, str],
    clock: int,
    prior: Mapping[str, Any] | None = None,
) -> Iterator[PluginContext]:
    if isinstance(clock, bool) or not isinstance(clock, int) or clock < 0:
        raise ValueError("clock")
    session_id = uuid4().hex
    stored_prior = dict(prior) if isinstance(prior, Mapping) else {}
    _SESSIONS[session_id] = _Session(transport, dict(sources), dict(specs), clock, stored_prior)
    try:
        yield PluginContext(plugin_id, stored_prior, session_id)
    finally:
        _SESSIONS.pop(session_id, None)


@dataclass(frozen=True)
class _Rpc:
    ok: bool
    result: Any
    reason: str


def _jsonrpc(transport: Any, source_id: str, operation: Any) -> _Rpc:
    try:
        result = transport.request(source_id, operation)
    except SecurityError as exc:
        return _Rpc(False, None, str(exc.reason or "denied"))
    except BudgetExhausted:
        return _Rpc(False, None, "budget")
    except TransportError as exc:
        return _Rpc(False, None, str(exc.reason or "read_error"))
    except Exception:
        return _Rpc(False, None, "read_error")
    if getattr(result, "blocked", False):
        return _Rpc(False, None, str(getattr(result, "reason", "blocked") or "blocked"))
    if getattr(result, "category", "") != "ok":
        return _Rpc(False, None, str(getattr(result, "reason", "read_error") or "read_error"))
    body = result.body
    if not isinstance(body, dict) or "result" not in body or "error" in body:
        return _Rpc(False, None, "malformed")
    return _Rpc(True, body.get("result"), "")


def _solana_source(session: _Session) -> str | None:
    for network, source_id in session.sources.items():
        if network == "solana-mainnet":
            return source_id
    return None
