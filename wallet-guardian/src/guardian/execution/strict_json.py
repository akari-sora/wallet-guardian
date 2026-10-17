"""JSON parser that rejects floats, duplicate keys, and non-standard constants.

Amounts in this registry are canonical integer strings. A JSON number with a
fraction or an exponent is rejected before schema checks run. Duplicate keys
are rejected instead of keeping the last one.
"""

from __future__ import annotations

import json
from typing import Any

from guardian.execution.errors import ExecutionRegistryError

_JS_MAX_SAFE = 9007199254740991


def parse_strict_json(text: str) -> Any:
    """Parse one JSON value. Integers must fit in a JS safe integer."""
    if not isinstance(text, str):
        raise ExecutionRegistryError("bad_json", "type")
    if text.startswith("\ufeff"):
        raise ExecutionRegistryError("bad_json", "bom")

    def reject_float(value: str) -> float:
        raise ExecutionRegistryError("float_rejected", value)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, item in items:
            if key in parsed:
                raise ExecutionRegistryError("duplicate_key", key)
            parsed[key] = item
        return parsed

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_float=reject_float)
    except ExecutionRegistryError:
        raise
    except json.JSONDecodeError as exc:
        raise ExecutionRegistryError("bad_json", exc.msg) from exc
    _reject_unsafe_integers(value)
    return value


def _reject_unsafe_integers(value: Any) -> None:
    if type(value) is int:
        if value > _JS_MAX_SAFE or value < -_JS_MAX_SAFE:
            raise ExecutionRegistryError("integer_out_of_range", str(value))
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_unsafe_integers(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_unsafe_integers(item)
