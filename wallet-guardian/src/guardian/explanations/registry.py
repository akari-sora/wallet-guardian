"""Versioned explainer manifests. A manifest cannot add a call the plugin does not already allow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from guardian.explanations.models import canonical, sha256_text
from guardian.resources import resource_path

# The plugin may decode or call only these signatures. A manifest that adds one is rejected.
ALLOWED_CALLS = frozenset({
    "claimWithdrawFor(address,uint256)",
    "claimWithdraw(uint256)",
    "requestWithdraw(uint256)",
})
ALLOWED_VIEWS = frozenset({
    "convertSnBnbToBnb(uint256)",
    "getBotUndelegateRequest(uint256)",
    "getUserRequestStatus(address,uint256)",
    "getUserWithdrawalRequests(address)",
    "hasRole(bytes32,address)",
})
ALLOWED_EVENTS = frozenset({
    "ClaimWithdrawal(address,uint256,uint256)",
    "RequestWithdraw(address,uint256)",
})


@dataclass(frozen=True)
class ExplainerManifest:
    explainer_id: str
    version: str
    manifest_hash: str
    abi_hash: str
    document: dict[str, Any]

    def chain_id(self) -> int:
        return int(self.document["chain_id"])

    def network(self) -> str:
        return str(self.document["network"])

    def proxy(self) -> str:
        return str(self.document["proxy"]["address"]).lower()

    def proxy_code_hash(self) -> str:
        return str(self.document["proxy"]["code_hash"]).lower()

    def intervals(self) -> list[dict[str, Any]]:
        rows = self.document.get("implementations")
        return list(rows) if isinstance(rows, list) else []

    def event(self, signature: str) -> dict[str, Any] | None:
        for row in self.document["abi"]["events"]:
            if row.get("signature") == signature:
                return row
        return None

    def view(self, signature: str) -> dict[str, Any] | None:
        for row in self.document["abi"]["views"]:
            if row.get("signature") == signature:
                return row
        return None

    def call(self, signature: str) -> dict[str, Any] | None:
        for row in self.document["abi"]["calls"]:
            if row.get("signature") == signature:
                return row
        return None


def load_packaged() -> list[ExplainerManifest]:
    directory = resource_path("config", "explainers")
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.json")):
        loaded = load_manifest(json.loads(path.read_text(encoding="utf-8")))
        if loaded is not None:
            found.append(loaded)
    return found


def load_manifest(document: dict[str, Any]) -> ExplainerManifest | None:
    """Return a manifest only when its ABI hash and signatures match the allowlist."""
    if not isinstance(document, dict):
        return None
    abi = document.get("abi")
    if not isinstance(abi, dict):
        return None
    expected = sha256_text(canonical(abi))
    if document.get("abi_hash") != expected:
        return None
    if not _signatures_allowed(abi):
        return None
    body = {key: value for key, value in document.items() if key != "manifest_hash"}
    digest = sha256_text(canonical(body))
    if document.get("manifest_hash") != digest:
        return None
    explainer_id = document.get("explainer_id")
    version = document.get("version")
    if not isinstance(explainer_id, str) or not isinstance(version, str):
        return None
    return ExplainerManifest(explainer_id, version, digest, expected, document)


def _signatures_allowed(abi: dict[str, Any]) -> bool:
    calls = abi.get("calls")
    views = abi.get("views")
    events = abi.get("events")
    if not isinstance(calls, list) or not isinstance(views, list) or not isinstance(events, list):
        return False
    call_names = {row.get("signature") for row in calls if isinstance(row, dict)}
    view_names = {row.get("signature") for row in views if isinstance(row, dict)}
    event_names = {row.get("signature") for row in events if isinstance(row, dict)}
    if not call_names or not view_names or not event_names:
        return False
    if not call_names <= ALLOWED_CALLS or not view_names <= ALLOWED_VIEWS or not event_names <= ALLOWED_EVENTS:
        return False
    required = {"claimWithdrawFor(address,uint256)", "ClaimWithdrawal(address,uint256,uint256)", "RequestWithdraw(address,uint256)"}
    return required <= (call_names | event_names)
