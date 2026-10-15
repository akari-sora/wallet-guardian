"""Private relations labels stored next to the state directory.

The file is `<state_dir>/relations-labels.json`. It is never sent to a host.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError
from guardian.identities import parse_address

SCHEMA = "guardian.relations.labels.v1"
HUB_KINDS = frozenset({"exchange", "protocol", "bridge"})
_KINDS = frozenset({"exchange", "protocol", "bridge", "self", "other"})
_WILDCARDS = frozenset({"evm", "tron"})
_MAX_BYTES = 1024 * 1024
_MAX_ROWS = 10_000


def load_user_labels(state_dir: Path, registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return parsed rows. A missing file means there are no user labels."""
    path = Path(state_dir) / "relations-labels.json"
    if path.is_symlink() or not path.exists():
        if path.is_symlink():
            raise ConfigError("labels_file")
        return []
    if not path.is_file():
        raise ConfigError("labels_file")
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ConfigError("labels_file_mode")
    if path.stat().st_size > _MAX_BYTES:
        raise ConfigError("labels_file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("labels_file") from None
    return _rows(payload, registry)


def match_label(
    labels: Sequence[Mapping[str, Any]],
    *,
    network: str,
    address: str,
    family: str,
) -> Mapping[str, Any] | None:
    """Prefer an exact network row over an `evm` or `tron` wildcard."""
    key = address.lower() if family == "evm" else address
    exact = None
    wildcard = None
    for entry in labels:
        if entry.get("match") != key or entry.get("family") != family:
            continue
        if entry.get("network") == network:
            if exact is None:
                exact = entry
        elif entry.get("network") in _WILDCARDS and wildcard is None:
            wildcard = entry
    return exact or wildcard


def _rows(payload: Any, registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise ConfigError("labels_file")
    rows = payload.get("labels")
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        raise ConfigError("labels_file")
    parsed: list[dict[str, Any]] = []
    for row in rows:
        parsed.append(_one(row, registry))
    return parsed


def _one(row: Any, registry: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ConfigError("labels_file")
    network = row.get("network")
    address = row.get("address")
    label = row.get("label")
    kind = row.get("kind")
    if not isinstance(network, str) or not isinstance(address, str) or kind not in _KINDS:
        raise ConfigError("labels_file")
    if not isinstance(label, str) or not _printable(label):
        raise ConfigError("labels_file")
    family = _family(network, registry)
    if family is None:
        raise ConfigError("labels_file")
    try:
        canonical = parse_address(family, address.strip()).canonical
    except ValueError:
        raise ConfigError("labels_file") from None
    return {
        "address": canonical,
        "family": family,
        "kind": kind,
        "label": label,
        "match": canonical.lower() if family == "evm" else canonical,
        "network": network,
    }


def _family(network: str, registry: Mapping[str, Any]) -> str | None:
    if network in _WILDCARDS:
        return network
    entry = registry.get(network)
    if not isinstance(entry, Mapping):
        return None
    family = entry.get("family")
    if not isinstance(family, str) or not family:
        return None
    return family


def _printable(label: str) -> bool:
    if len(label) < 1 or len(label) > 64:
        return False
    if "\n" in label or "\r" in label:
        return False
    return label.isprintable()
