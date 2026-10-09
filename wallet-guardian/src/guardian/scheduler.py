"""Health watchdog and the plan `guardian monitor start` would print.

The watchdog never disables the schedule and never deletes state. It does not
start a daemon or install cron.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from guardian.configuration import PublicConfig, PrivateInputs
from guardian.delivery import CANARY_STATUS
from guardian.errors import ConfigError
from guardian.rules import dedup_key
from guardian.storage import Database

_BETTER = frozenset({"complete_in_declared_scope", "partial"})
_WORSE = frozenset({"error", "blocked"})


class Watchdog:
    """One blind alert per episode and one recovered alert when it ends."""

    def __init__(self, database: Database, tenant: str, public_config: PublicConfig) -> None:
        execution = public_config.document.get("execution")
        if not isinstance(execution, dict) or execution.get("auto_disable_schedule") is not False:
            raise ConfigError("auto_disable_schedule")
        if execution.get("sign") is not False or execution.get("broadcast") is not False:
            raise ConfigError("execution")
        polling = public_config.document.get("polling")
        if not isinstance(polling, dict) or not isinstance(polling.get("interval_seconds"), int):
            raise ConfigError("polling")
        self.database = database
        self.tenant = tenant
        self.public_config = public_config
        self.interval = polling["interval_seconds"]

    def evaluate(self, run_report_or_none: Any, now: str) -> dict[str, Any]:
        """Emit at most one health alert. Repeated blind evaluations emit nothing."""
        if self.public_config.document["execution"].get("auto_disable_schedule") is not False:
            raise ConfigError("auto_disable_schedule")
        moment = _parse(now)
        health = _load(self.database, self.tenant)
        blind = _blind(run_report_or_none, health, moment, self.interval)
        was = health.get("blind") is True
        emitted: list[dict[str, str]] = []
        if blind and not was:
            episode = now
            health["blind"] = True
            health["episode_start"] = episode
            emitted.append(self._alert("monitor_blind", episode, now))
        elif blind and was:
            health["blind"] = True
        elif was and not blind:
            episode = str(health.get("episode_start") or now)
            health["blind"] = False
            health["episode_start"] = None
            emitted.append(self._alert("monitor_recovered", episode, now))
        else:
            health["blind"] = False
        required_now = getattr(run_report_or_none, "required", None)
        if (
            run_report_or_none is not None
            and getattr(run_report_or_none, "status", None) == "ok"
            and not blind
            and _required_healthy(required_now)
        ):
            health["last_success_at"] = now
        required = getattr(run_report_or_none, "required", None)
        if isinstance(required, dict):
            health["required"] = {str(key): value for key, value in required.items()}
        health["checked_at"] = now
        health["schedule_disabled"] = False
        _save(self.database, self.tenant, health)
        return {
            "auto_disable_schedule": False,
            "blind": health.get("blind") is True,
            "emitted": emitted,
            "schedule_disabled": False,
        }

    def _alert(self, kind: str, episode: str, now: str) -> dict[str, str]:
        identity = episode
        key = dedup_key(self.tenant, "*", "*", kind, identity)
        payload = {
            "dedup_key": key,
            "episode_start": episode,
            "kind": kind,
            "observed_at": now,
            "tenant_id": self.tenant,
        }
        self.database.add_alert(self.tenant, key, payload)
        return {"dedup_key": key, "kind": kind}


def monitor_plan(public_config: PublicConfig, private_inputs: PrivateInputs) -> dict[str, Any]:
    """Describe the schedule. This does not start it."""
    document = public_config.document
    execution = document["execution"]
    if execution.get("auto_disable_schedule") is not False:
        raise ConfigError("auto_disable_schedule")
    polling = document["polling"]
    delivery = document["delivery"]
    sources = []
    for name in sorted(document["sources"]):
        source = document["sources"][name]
        status = private_inputs.source_status.get(name)
        sources.append({
            "adapter": source["adapter"],
            "credential_present": bool(status and status.credential_present),
            "enabled": source["enabled"] is True,
            "id": name,
        })
    profile_env = delivery.get("profile_env")
    recipient_profile_exists = _profile_exists(profile_env)
    return {
        "budgets": json.loads(json.dumps(document["budgets"])),
        "canary": CANARY_STATUS,
        "delivery": {
            "canary": CANARY_STATUS,
            "mode": delivery.get("mode"),
            "recipient_profile_exists": recipient_profile_exists,
        },
        "execution": {
            "auto_disable_schedule": False,
            "broadcast": False,
            "sign": False,
        },
        "notes": list(private_inputs.notes),
        "schedule": {
            "around_the_clock": polling.get("around_the_clock") is True,
            "interval_seconds": polling.get("interval_seconds"),
        },
        "sources": sources,
        "started": False,
        "wallets": len(private_inputs.accounts.accounts),
    }


def _profile_exists(env_name: Any) -> bool:
    if not isinstance(env_name, str) or not env_name:
        return False
    raw = os.environ.get(env_name)
    if not isinstance(raw, str) or not raw.strip():
        return False
    return Path(raw).expanduser().is_file()


def _blind(report: Any, health: dict[str, Any], now: datetime, interval: int) -> bool:
    if report is not None and _finished_stale(report, now, interval):
        return True
    status = getattr(report, "status", None) if report is not None else None
    if status in {"failed", "degraded"}:
        return True
    if report is not None and _shrunk(health.get("required"), getattr(report, "required", None)):
        return True
    if status == "ok":
        return not _required_healthy(getattr(report, "required", None))
    last = health.get("last_success_at")
    if not isinstance(last, str) or not last:
        return report is None or status != "ok"
    try:
        previous = _parse(last)
    except ValueError:
        return True
    age = (now - previous).total_seconds()
    return age > 2 * interval


def _required_healthy(required: Any) -> bool:
    if not isinstance(required, dict) or not required:
        return False
    return all(value in _BETTER for value in required.values())


def _finished_stale(report: Any, now: datetime, interval: int) -> bool:
    finished = getattr(report, "finished", None)
    if not isinstance(finished, str) or not finished:
        return False
    try:
        previous = _parse(finished)
    except ValueError:
        return True
    # Same grace as last_success_at: a scheduled run plus its own duration is not blind.
    return (now - previous).total_seconds() > 2 * interval


def _shrunk(previous: Any, current: Any) -> bool:
    """A required key that disappears is a failure, not a smaller scope."""
    if not isinstance(previous, dict) or not isinstance(current, dict) or not previous:
        return False
    for key in previous:
        if key not in current:
            return True
    for key, value in current.items():
        old = previous.get(key)
        if old in _BETTER and value in _WORSE:
            return True
    return False


def _parse(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load(database: Database, tenant: str) -> dict[str, Any]:
    row = database.conn.execute(
        "SELECT payload FROM monitor_health WHERE tenant_id=?",
        (tenant,),
    ).fetchone()
    if row is None:
        return {"blind": False, "schedule_disabled": False}
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        return {"blind": False, "schedule_disabled": False}
    return payload


def _save(database: Database, tenant: str, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    with database.conn:
        database.conn.execute(
            """
            INSERT INTO monitor_health(tenant_id, payload) VALUES(?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET payload=excluded.payload
            """,
            (tenant, encoded),
        )
