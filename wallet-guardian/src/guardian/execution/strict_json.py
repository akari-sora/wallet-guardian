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

    def reject_constant(value: str) -> float:
        raise ExecutionRegistryError("float_rejected", value)

    def parse_int(raw: str) -> int:
        # Longer than 16 digits cannot be a JS safe integer. Reject before int()
        # so CPython's digit limit cannot turn this into ValueError.
        digits = raw[1:] if raw.startswith("-") else raw
        if len(digits) > 16:
            raise ExecutionRegistryError("integer_out_of_range", raw)
        number = int(raw)
        if number > _JS_MAX_SAFE or number < -_JS_MAX_SAFE:
            raise ExecutionRegistryError("integer_out_of_range", raw)
        return number

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, item in items:
            if key in parsed:
                raise ExecutionRegistryError("duplicate_key", key)
            parsed[key] = item
        return parsed

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=reject_float,
            parse_constant=reject_constant,
            parse_int=parse_int,
        )
    except ExecutionRegistryError:
        raise
    except json.JSONDecodeError as exc:
        raise ExecutionRegistryError("bad_json", exc.msg) from exc
