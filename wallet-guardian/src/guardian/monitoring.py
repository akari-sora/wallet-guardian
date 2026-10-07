"""Persistent monitoring cycle shared by run-once and offline regression tests.

Provider failure is not an empty portfolio. Notification candidates are committed
with the snapshot into a durable outbox; this module never delivers messages.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from guardian.amounts import format_units
from guardian.storage import Database


def item_key(item: dict[str, Any]) -> str:
    return f"{item['chain_id']}|{item['position_id']}"


def wallet_label(item: dict[str, Any]) -> str:
    # Legacy 0.3 snapshots did not record the wallet as a separate field.
    return str(item.get("wallet_label") or item["position_id"].split(":", 1)[0])


def event_key(*parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def reconcile_scopes(
    previous: list[dict[str, Any]], incoming: list[dict[str, Any]],
    successful_scopes: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    """Only a successful scope may replace its previous observations."""
    rows = {item_key(row): deepcopy(row) for row in incoming}
    if len(rows) != len(incoming):
        raise ValueError("duplicate incoming position")
    for old in previous:
        key = item_key(old)
        if key in rows:
            continue
        scope = (wallet_label(old), old["chain_id"])
        retained = deepcopy(old)
        if scope not in successful_scopes:
            retained.update(quality="STALE", fresh=False, retained_last_known_good=True)
            retained["stale_reason"] = "scope_not_successfully_observed"
        elif ":lista:" in old["position_id"]:
            # Without a matching claim receipt, a removed array element is NOT
            # evidence that funds arrived. Keep it visible but never claimable.
            retained.update(
                status="MISSING_NO_CLAIM_EVIDENCE", quality="UNKNOWN", fresh=False,
                retained_last_known_good=True, severity="ACTION",
            )
            retained["last_known_raw_amount"] = old["raw_amount"]
        else:
            continue
        rows[key] = retained
    return list(rows.values())


def state_diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[str]]:
    old, new = ({item_key(row): row for row in rows} for rows in (previous, current))
    return {
        "changed": [key for key, row in new.items() if key in old and (
            row.get("raw_amount") != old[key].get("raw_amount") or
            row.get("status") != old[key].get("status")
        )],
        "added": [key for key in new if key not in old],
        "removed": [key for key in old if key not in new],
    }


def _claim_alert(tenant: str, item: dict[str, Any], previous: dict[str, Any] | None, created: str) -> dict[str, Any]:
    missing = item["status"] == "MISSING_NO_CLAIM_EVIDENCE"
    if missing:
        what = "請求不在目前清單中；尚無領取收據，不能判定已入帳。"
    elif previous is None:
        what = "首次觀測此請求時已可領；不能推定剛剛才變成可領。"
    else:
        what = "這筆請求目前可領；上次觀測狀態為 " + str(previous.get("status")) + "。"
    return {
        "kind": "claim_missing" if missing else "claimable",
        "severity": "ACTION", "severity_label": "待核對" if missing else "待處理",
        "confidence": "狀態差異，缺收據" if missing else "鏈上讀取（未最終確認）",
        "wallet_label": wallet_label(item), "chain_label": "BNB Smart Chain",
        "subject": "Lista 提款", "what": what,
        "amount_display": format_units(item["raw_amount"], 18), "unit": "BNB",
        "price": None, "price_time": None,
        "why": "此請求屬於設定中的監控地址。",
        "step": "在自己的錢包操作前重查請求與 pause；請求消失時先核對領取交易。",
        "event_time": "未知（此時間為觀測時間，不是狀態轉移區塊）",
        "observed_at": created, "source": "https://bsc-dataseed.bnbchain.org",
        "limits": "未知價格不當作零；遺失請求的數量僅為最後已知值。未簽名、未廣播。",
        "dedup_key": event_key(tenant, item.get("wallet_address", wallet_label(item)),
                               item["position_id"], "missing" if missing else "claimable"),
    }


class SnapshotCycle:
    """Capture the parent BEFORE I/O; commit observations and decisions together."""

    def __init__(self, database: Database, tenant: str) -> None:
        self.database = database
        self.tenant = tenant
        self.parent_generation = database.latest_generation(tenant)
        self.previous = database.snapshot_items(tenant, self.parent_generation)
        self.previous_health = database.health_for(tenant)

    def finish(
        self, *, incoming: list[dict[str, Any]], successful_scopes: set[tuple[str, int]],
        failed_scopes: list[str], created: str, tasks: list[dict[str, Any]] | None = None,
        suppressed_claims: set[str] | None = None,
    ) -> dict[str, Any]:
        current = reconcile_scopes(self.previous, incoming, successful_scopes)
        previous_by_key = {item_key(row): row for row in self.previous}
        candidates = []
        legacy_keys = {alert.get("dedup_key") for alert in self.database.alerts_for(self.tenant)}
        for item in current:
            if ":lista:" not in item["position_id"]:
                continue
            old = previous_by_key.get(item_key(item))
            actionable = (
                item.get("fresh") is True and item.get("quality") == "COMPLETE" and
                item.get("status") == "CLAIMABLE" and not item.get("paused", True) and
                int(item["raw_amount"]) > 0
            )
            missing = (item.get("status") == "MISSING_NO_CLAIM_EVIDENCE" and
                       (wallet_label(item), item["chain_id"]) in successful_scopes)
            legacy_key = f"{self.tenant}|56|{item['position_id']}|claimable|baseline"
            if ((actionable and item["position_id"] not in (suppressed_claims or set())
                 and legacy_key not in legacy_keys) or missing):
                candidates.append(_claim_alert(self.tenant, item, old, created))

        failed = sorted(set(failed_scopes))
        was_blind = bool(self.previous_health.get("blind"))
        episode = int(self.previous_health.get("episode", 0)) + int(bool(failed) and not was_blind)
        health = {"blind": bool(failed), "episode": episode, "failed_scopes": failed, "checked_at": created}
        changed_failures = failed != self.previous_health.get("failed_scopes", [])
        if failed and (not was_blind or changed_failures) or was_blind and not failed:
            candidates.append({
                "kind": "monitoring_blind" if failed else "monitoring_recovered",
                "severity": "HIGH" if failed else "INFO",
                "failed_scopes": failed, "observed_at": created,
                "what": "部分來源無法讀取；舊數量保留為陳舊。" if failed else "本次設定的讀取來源已恢復；未支援的範圍仍未支援。",
                "dedup_key": event_key(self.tenant, "health", episode, failed),
            })
        emitted = self.database.commit_snapshot(
            self.tenant, self.parent_generation + 1, created, current,
            alerts=candidates, health=health, tasks=tasks,
        )
        return {
            "snapshot_generation": self.parent_generation + 1,
            "baseline": self.parent_generation == 0,
            "items": current,
            "diff": state_diff(self.previous, current),
            "notification_decision": emitted,
            "source_health": health,
        }
