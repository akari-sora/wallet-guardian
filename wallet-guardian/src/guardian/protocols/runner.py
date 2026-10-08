"""Run one registered plugin. Chain identity is checked before any position is kept.

A missing or mismatched chain id is unknown. The prior cursor stays where it was.
"""

from __future__ import annotations

from typing import Any, Mapping

from guardian.errors import SecurityError
from guardian.protocols.context import check_evm_chain, check_solana_genesis, open_plugin_context
from guardian.protocols.manifest import Manifest, load_protocol_dir
from guardian.protocols.models import PositionObservation, unknown_position
from guardian.protocols.network import chain_family
from guardian.protocols.registry import PLUGIN_REGISTRY


def run_plugin(
    plugin_id: str,
    *,
    transport: Any,
    sources: Mapping[str, str],
    wallet_id: str,
    wallet: str,
    network: str,
    clock: int,
    prior: Mapping[str, Any] | None = None,
    private: Mapping[str, Any] | None = None,
    manifest: Manifest | None = None,
) -> PositionObservation:
    plugin_cls = PLUGIN_REGISTRY.get(plugin_id)
    if plugin_cls is None:
        return unknown_position(
            plugin_id=plugin_id,
            wallet_id=wallet_id,
            network=network,
            prior=prior,
            reason="unknown_plugin",
        )
    if manifest is None:
        manifest = load_protocol_dir()[plugin_id]
    if manifest.plugin_id != plugin_id:
        return unknown_position(
            plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unknown_plugin",
        )
    source_id = sources.get(network) if isinstance(sources, Mapping) else None
    if not isinstance(source_id, str) or not source_id:
        return unknown_position(
            plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="missing_source",
        )
    try:
        source = transport.endpoints.get(source_id)
    except SecurityError:
        return unknown_position(
            plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unregistered_source",
        )
    if source.adapter not in plugin_cls.adapters:
        return unknown_position(
            plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="adapter_mismatch",
        )
    family = chain_family(network)
    if family == "evm":
        ok, reason = check_evm_chain(transport, source_id, network)
    elif family == "solana":
        ok, reason = check_solana_genesis(transport, source_id, network)
    else:
        ok, reason = False, "unsupported_network"
    if not ok:
        return unknown_position(
            plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=reason,
        )
    with open_plugin_context(
        plugin_id,
        plugin_cls.specs,
        transport=transport,
        sources=sources,
        clock=clock,
        prior=prior,
    ) as ctx:
        return plugin_cls().observe(
            ctx,
            wallet_id=wallet_id,
            wallet=wallet,
            network=network,
            manifest=manifest,
            private=private,
        )
