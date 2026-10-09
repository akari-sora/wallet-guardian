"""Offline yield promotions. Not enabled, not scheduled, and not a sender.

Call :func:`guardian.yields.state.evaluate` with a fixture transport and a
clock. ``run-once`` does not import this package.
"""

from guardian.yields.models import ENABLED, promotions_enabled
from guardian.yields.parsers import DECOMPRESSED_CAP, MAX_DEPTH, MAX_RECORDS
from guardian.yields.state import FETCH_URLS, evaluate

__all__ = [
    "DECOMPRESSED_CAP",
    "ENABLED",
    "FETCH_URLS",
    "MAX_DEPTH",
    "MAX_RECORDS",
    "evaluate",
    "promotions_enabled",
]
