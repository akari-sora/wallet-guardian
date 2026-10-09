"""TRON reads. Importing this package does not open a socket."""

from guardian.chains.tron.adapter import read_tron_account

__all__ = ["read_tron_account"]
