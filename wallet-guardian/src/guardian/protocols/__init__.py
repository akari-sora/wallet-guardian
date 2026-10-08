"""Read-only protocol plugins. Configuration selects a registry id and cannot import code."""

from guardian.protocols.context import PluginContext, open_plugin_context
from guardian.protocols.evidence import assess_bridge, assess_withdrawals
from guardian.protocols.manifest import Manifest, ManifestError, load_manifest, load_protocol_dir
from guardian.protocols.models import PositionLeg, PositionObservation
from guardian.protocols.portfolio import FungibleBalance, Portfolio, aggregate_portfolio
from guardian.protocols.protocol_coverage import build_protocol_coverage
from guardian.protocols.registry import PLUGIN_REGISTRY
from guardian.protocols.runner import run_plugin

__all__ = [
    "FungibleBalance",
    "Manifest",
    "ManifestError",
    "PLUGIN_REGISTRY",
    "PluginContext",
    "Portfolio",
    "PositionLeg",
    "PositionObservation",
    "aggregate_portfolio",
    "assess_bridge",
    "assess_withdrawals",
    "build_protocol_coverage",
    "load_manifest",
    "load_protocol_dir",
    "open_plugin_context",
    "run_plugin",
]
