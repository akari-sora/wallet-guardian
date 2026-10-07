"""Canonical JSON used for plan digests. Keep in lockstep with action-client/src/canonical.mjs."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_body(body: dict[str, Any]) -> str:
    return sha256_text(canonical_dumps(body))
