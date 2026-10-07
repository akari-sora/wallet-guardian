"""Local operator views: one explicit tenant, one current generation.

The tenant flag selects local data. It is not authentication for a public API.
"""
from __future__ import annotations

from typing import Any

from guardian.errors import ConfigError
from guardian.monitoring import wallet_label
from guardian.storage import Database


def resolve_tenant(database: Database, requested: str | None = None) -> str | None:
    tenants = database.tenant_ids()
    if requested is not None:
        if requested not in tenants:
            raise ConfigError("the requested tenant was not found")
        return requested
    if len(tenants) > 1:
        raise ConfigError("multiple tenants in database; select one with --tenant")
    return tenants[0] if tenants else None


def latest_items(database: Database, tenant: str | None, wallet: str | None = None) -> list[dict[str, Any]]:
    if tenant is None:
        return []
    rows = database.snapshot_items(tenant)
    if wallet is not None:
        known = database.conn.execute(
            "SELECT 1 FROM wallets WHERE tenant_id=? AND label=?", (tenant, wallet),
        ).fetchone()
        if not known and not any(wallet_label(row) == wallet for row in rows):
            raise ConfigError("the requested wallet was not found in this tenant")
        rows = [row for row in rows if wallet_label(row) == wallet]
    return [{**row, "tenant_id": tenant} for row in rows]
