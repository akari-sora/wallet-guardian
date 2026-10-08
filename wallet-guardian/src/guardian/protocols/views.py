"""ABI-encode allowlisted views. Write signatures are refused before any payload is built.

The allowlist is the plugin class table. A manifest cannot add a signature, and
this module never imports, evals, or execs configuration text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address
from guardian.chains.evm.permissions import eth_call_operation
from guardian.security.transport_policy import Operation

_WRITE_NAMES = frozenset({
    "approve",
    "bridge",
    "claimwithdraw",
    "claimwithdrawfor",
    "closeaccount",
    "deactivate",
    "decreaseallowance",
    "deposit",
    "execute",
    "increaseallowance",
    "multicall",
    "permit",
    "redeem",
    "repay",
    "requestwithdraw",
    "setapprovalforall",
    "setauthority",
    "transfer",
    "withdraw",
})


@dataclass(frozen=True)
class ViewSpec:
    """Argument and return types for one view. Both are code, not config."""

    args: tuple[str, ...]
    returns: tuple[str, ...]


def function_name(signature: str) -> str:
    if not isinstance(signature, str) or "(" not in signature or not signature.endswith(")"):
        return ""
    return signature.split("(", 1)[0]


def is_write(signature: str) -> bool:
    """True when the signature is not a view we are willing to encode."""
    name = function_name(signature).lower()
    if not name:
        return True
    if name in _WRITE_NAMES or name.startswith("claim"):
        return True
    return False


def encode_view(signature: str, spec: ViewSpec, args: Sequence[Any]) -> str:
    """Encode one allowlisted view. A write name is refused even if a spec is passed."""
    if is_write(signature) or not isinstance(spec, ViewSpec):
        raise ValueError("not_a_view_call")
    if len(args) != len(spec.args):
        raise ValueError("not_a_view_call")
    try:
        coerced = [_coerce(kind, value) for kind, value in zip(spec.args, args)]
    except ValueError:
        raise ValueError("not_a_view_call") from None
    body = encode(list(spec.args), coerced) if spec.args else b""
    selector = function_signature_to_4byte_selector(signature)
    return "0x" + (selector + body).hex()


def decode_view(spec: ViewSpec, raw: str) -> tuple[Any, ...] | None:
    """Decode a hex result. A short or non-hex payload is malformed, not zero."""
    if not isinstance(raw, str) or not raw.startswith(("0x", "0X")):
        return None
    try:
        payload = bytes.fromhex(raw[2:])
    except ValueError:
        return None
    if not spec.returns:
        return () if payload == b"" else None
    try:
        decoded = decode(list(spec.returns), payload)
    except Exception:
        return None
    return tuple(decoded)


def view_operation(signature: str, spec: ViewSpec, to: str, args: Sequence[Any]) -> Operation:
    """eth_call for one view. The transport still applies its own read allowlist."""
    return eth_call_operation(to, encode_view(signature, spec, args))


class ViewPlugin:
    """Base for EVM plugins. ``operation`` only encodes signatures in ``specs``."""

    specs: Mapping[str, ViewSpec]

    @classmethod
    def operation(cls, to: str, signature: str, args: Sequence[Any]) -> Operation:
        spec = cls.specs.get(signature)
        if spec is None or is_write(signature):
            raise ValueError("not_a_view_call")
        return view_operation(signature, spec, to, args)


def _coerce(kind: str, value: Any) -> Any:
    if kind == "address":
        if not isinstance(value, str):
            raise ValueError("not_a_view_call")
        normalized = normalize_address(value)
        if normalized is None:
            raise ValueError("not_a_view_call")
        return normalized
    if kind == "uint256":
        if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, int):
            raise ValueError("not_a_view_call")
        if value < 0 or value > UINT256_MAX:
            raise ValueError("not_a_view_call")
        return value
    raise ValueError("not_a_view_call")
