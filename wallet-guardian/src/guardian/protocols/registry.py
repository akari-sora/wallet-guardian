"""Static plugin registry. Ids map to classes that already exist in this package.

There is no entry point string and no dynamic import.
"""

from __future__ import annotations

from types import MappingProxyType

from guardian.protocols.aave import AaveV3
from guardian.protocols.erc4626 import Erc4626
from guardian.protocols.kinza import Kinza
from guardian.protocols.lido import Lido
from guardian.protocols.lista import ListaSlisBnb
from guardian.protocols.stake import SolanaStake
from guardian.protocols.uxlink import UxlinkHoldings

PLUGIN_REGISTRY = MappingProxyType({
    AaveV3.plugin_id: AaveV3,
    Erc4626.plugin_id: Erc4626,
    Kinza.plugin_id: Kinza,
    Lido.plugin_id: Lido,
    ListaSlisBnb.plugin_id: ListaSlisBnb,
    SolanaStake.plugin_id: SolanaStake,
    UxlinkHoldings.plugin_id: UxlinkHoldings,
})
