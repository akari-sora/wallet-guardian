"""Where a monitored address or an API credential may go.

Address-based monitoring cannot mean "never send or store a monitored address
anywhere." The operator's address has to be queried at the chain RPC or indexer
they configured, and it has to live in the private accounts file. It must not
travel to news, promotions, or a risk provider they have not consented to, and
it must not be copied into a public export, a routine diagnostic, or a package
artifact.

Logs and errors must not contain API credentials. An address belongs in a log
only when that log is an authorized private output that is supposed to name it.
Public contract pins are not monitored wallets.
"""

from __future__ import annotations

APPROVED_CHAIN = "approved_chain"
NEWS = "news"
PROMOTION = "promotion"
RISK_UNCONSENTED = "risk_unconsented"
PRIVATE_STATE = "private_state"
PUBLIC_EXPORT = "public_export"
DIAGNOSTIC = "diagnostic"
LOG = "log"

# wallet_address / credential. Log addresses are decided separately.
_DESTINATIONS = {
    APPROVED_CHAIN: (True, True),
    NEWS: (False, False),
    PROMOTION: (False, False),
    RISK_UNCONSENTED: (False, False),
    PRIVATE_STATE: (True, False),
    PUBLIC_EXPORT: (False, False),
    DIAGNOSTIC: (False, False),
    LOG: (False, False),
}


def allows_wallet_address(destination: str, *, authorized_private_output: bool = False) -> bool:
    """True when this destination may carry a monitored wallet address."""
    if destination not in _DESTINATIONS:
        return False
    if destination == LOG:
        return authorized_private_output
    return _DESTINATIONS[destination][0]


def allows_credential(destination: str) -> bool:
    """True when this destination may carry an API credential.

    A credential may be sent to the approved indexer that requires it. It is
    never written to a log, an export, or a diagnostic.
    """
    if destination not in _DESTINATIONS:
        return False
    return _DESTINATIONS[destination][1]
