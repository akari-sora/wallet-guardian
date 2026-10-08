"""Stdlib Draft 2020-12 validator for the MCM schema subset.

Unsupported keywords raise SchemaError. They are not ignored. This module does
not import jsonschema and does not open a network connection.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

IGNORED = frozenset({"$schema", "$comment", "$id"})
ALLOWED = IGNORED | frozenset({
    "$defs",
    "$ref",
    "additionalProperties",
    "anyOf",
    "const",
    "enum",
    "exclusiveMinimum",
    "format",
    "items",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "pattern",
    "properties",
    "propertyNames",
    "required",
    "type",
    "uniqueItems",
})
FORMATS = frozenset({"uuid", "date-time"})
TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class SchemaError(ValueError):
    """The schema uses a construct this validator refuses to skip."""


class ValidationError(ValueError):
    """The instance does not satisfy the schema."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def validate(instance: Any, schema: Any) -> None:
    """Validate instance. Raise SchemaError or ValidationError."""
    if not isinstance(schema, (dict, bool)):
        raise SchemaError("schema must be an object or boolean")
    _compile(schema, "$")
    _validate(instance, schema, "$", schema if isinstance(schema, dict) else {})


def _compile(schema: Any, path: str) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be an object or boolean")
    for key in schema:
        if key not in ALLOWED:
            raise SchemaError(f"{path}: unsupported keyword {key}")
    if "type" in schema:
        raw = schema["type"]
        names = [raw] if isinstance(raw, str) else raw
        if not isinstance(names, list) or not names or any(not isinstance(item, str) for item in names):
            raise SchemaError(f"{path}: type must be a string or a non-empty array of strings")
        for item in names:
            if item not in TYPES:
                raise SchemaError(f"{path}: unsupported type {item}")
    if "const" in schema:
        pass
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise SchemaError(f"{path}: enum must be a non-empty array")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise SchemaError(f"{path}: pattern must be a string")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise SchemaError(f"{path}: pattern does not compile") from exc
    for key in ("minLength", "maxLength", "minItems"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], int) or schema[key] < 0):
            raise SchemaError(f"{path}: {key} must be a non-negative integer")
    for key in ("minimum", "maximum", "exclusiveMinimum"):
        if key in schema and not _is_number(schema[key]):
            raise SchemaError(f"{path}: {key} must be a number")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise SchemaError(f"{path}: uniqueItems must be a boolean")
    if "items" in schema:
        if not isinstance(schema["items"], (dict, bool)):
            raise SchemaError(f"{path}: unsupported items form")
        _compile(schema["items"], f"{path}/items")
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise SchemaError(f"{path}: properties must be an object")
        for name, sub in schema["properties"].items():
            if not isinstance(name, str):
                raise SchemaError(f"{path}: property name must be a string")
            _compile(sub, f"{path}/properties/{name}")
    if "required" in schema:
        if not isinstance(schema["required"], list) or any(not isinstance(item, str) for item in schema["required"]):
            raise SchemaError(f"{path}: required must be an array of strings")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if isinstance(extra, dict):
            _compile(extra, f"{path}/additionalProperties")
        elif not isinstance(extra, bool):
            raise SchemaError(f"{path}: additionalProperties must be a boolean or schema")
    if "propertyNames" in schema:
        _compile(schema["propertyNames"], f"{path}/propertyNames")
    if "anyOf" in schema:
        if not isinstance(schema["anyOf"], list) or not schema["anyOf"]:
            raise SchemaError(f"{path}: anyOf must be a non-empty array")
        for index, sub in enumerate(schema["anyOf"]):
            _compile(sub, f"{path}/anyOf/{index}")
    if "format" in schema:
        if not isinstance(schema["format"], str) or schema["format"] not in FORMATS:
            raise SchemaError(f"{path}: unsupported format {schema.get('format')}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#"):
            raise SchemaError(f"{path}: only local $ref is supported")
    if "$defs" in schema:
        if not isinstance(schema["$defs"], dict):
            raise SchemaError(f"{path}: $defs must be an object")
        for name, sub in schema["$defs"].items():
            _compile(sub, f"{path}/$defs/{name}")


def _validate(instance: Any, schema: Any, path: str, root: dict[str, Any], seen: frozenset[str] = frozenset()) -> None:
    if isinstance(schema, bool):
        if schema:
            return
        raise ValidationError(f"{path}: schema is false")
    if "$ref" in schema:
        _validate_ref(instance, schema["$ref"], path, root, seen)
    if "type" in schema and not _matches_type(instance, schema["type"]):
        raise ValidationError(f"{path}: type")
    if "const" in schema and not _json_equal(instance, schema["const"]):
        raise ValidationError(f"{path}: const")
    if "enum" in schema and not any(_json_equal(instance, item) for item in schema["enum"]):
        raise ValidationError(f"{path}: enum")
    if "anyOf" in schema:
        matched = False
        for sub in schema["anyOf"]:
            try:
                _validate(instance, sub, path, root, seen)
            except ValidationError:
                continue
            matched = True
            break
        if not matched:
            raise ValidationError(f"{path}: anyOf")
    if isinstance(instance, str):
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise ValidationError(f"{path}: pattern")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise ValidationError(f"{path}: minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise ValidationError(f"{path}: maxLength")
        if "format" in schema and not _format_ok(instance, schema["format"]):
            raise ValidationError(f"{path}: format")
    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValidationError(f"{path}: minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise ValidationError(f"{path}: maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            raise ValidationError(f"{path}: exclusiveMinimum")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise ValidationError(f"{path}: minItems")
        if schema.get("uniqueItems") is True and not _unique(instance):
            raise ValidationError(f"{path}: uniqueItems")
        if "items" in schema:
            for index, item in enumerate(instance):
                _validate(item, schema["items"], f"{path}[{index}]", root, seen)
    if isinstance(instance, dict):
        if "propertyNames" in schema:
            for key in instance:
                _validate(key, schema["propertyNames"], f"{path}/propertyNames", root, seen)
        properties = schema.get("properties") or {}
        for key, sub in properties.items():
            if key in instance:
                _validate(instance[key], sub, f"{path}.{key}", root, seen)
        if "required" in schema:
            missing = [key for key in schema["required"] if key not in instance]
            if missing:
                raise ValidationError(f"{path}: missing {missing}")
        if "additionalProperties" in schema:
            extra = [key for key in instance if key not in properties]
            rule = schema["additionalProperties"]
            if rule is False and extra:
                raise ValidationError(f"{path}: unknown fields {sorted(extra)}")
            if isinstance(rule, dict):
                for key in extra:
                    _validate(instance[key], rule, f"{path}.{key}", root, seen)


def _validate_ref(instance: Any, ref: str, path: str, root: dict[str, Any], seen: frozenset[str]) -> None:
    if ref in seen:
        raise SchemaError(f"{path}: cyclic $ref {ref}")
    _validate(instance, _resolve(root, ref), path, root, seen | {ref})


def _resolve(root: dict[str, Any], ref: str) -> Any:
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        raise SchemaError(f"unresolved $ref {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise SchemaError(f"unresolved $ref {ref}")
    return node


def _matches_type(instance: Any, raw: Any) -> bool:
    names = [raw] if isinstance(raw, str) else raw
    return any(_one_type(instance, name) for name in names)


def _one_type(instance: Any, name: str) -> bool:
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "number":
        return _is_number(instance)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "null":
        return instance is None
    raise SchemaError(f"unsupported type {name}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    if _is_number(left) and _is_number(right):
        return left == right
    return left == right


def _unique(values: list[Any]) -> bool:
    seen: list[str] = []
    for value in values:
        token = _canon(value)
        if token in seen:
            return False
        seen.append(token)
    return True


def _canon(value: Any) -> str:
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _format_ok(value: str, name: str) -> bool:
    if name == "uuid":
        return _UUID.fullmatch(value) is not None
    if name == "date-time":
        if _DATE_TIME.fullmatch(value) is None:
            return False
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            datetime.fromisoformat(text)
        except ValueError:
            return False
        return True
    raise SchemaError(f"unsupported format {name}")
