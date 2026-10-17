"""Digests of the loaded registry and its policies.

Core-bundle and dist digests belong on a release record once those bytes
exist. This module does not hash a bundle and does not authorize send.
"""

from __future__ import annotations

from typing import Any

from guardian.canonical import canonical_dumps, sha256_text


def registry_digest(registry: dict[str, Any]) -> str:
    payload = {
        "deployments": registry["deployments"],
        "identity": registry["identity"],
        "schema_version": registry["schema_version"],
        "send_enabled": registry["send_enabled"],
        "user_approved": registry["user_approved"],
    }
    return sha256_text(canonical_dumps(payload))


def policy_digest(registry: dict[str, Any]) -> str:
    payload = {"policies": registry["policies"], "schema_version": registry["schema_version"]}
    return sha256_text(canonical_dumps(payload))
