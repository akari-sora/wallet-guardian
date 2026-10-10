"""Tenant-scoped explanation jobs and revisions. SQL stays on the monitor database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from guardian.explanations.models import canonical, fingerprint


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(value: str) -> dict[str, Any]:
    body = json.loads(value)
    return body if isinstance(body, dict) else {}


def enqueue(
    database: Any,
    tenant: str,
    event_id: str,
    *,
    account: str,
    network: str,
    wallet_id: str,
    payload: dict[str, Any],
) -> None:
    """Insert a pending job. An existing job for the same event stays put."""
    if not all(isinstance(item, str) and item for item in (tenant, event_id, account, network, wallet_id)):
        return
    encoded = canonical(payload)
    with database.conn:
        database.conn.execute(
            """
            INSERT INTO explanation_jobs(
              tenant_id, event_id, event_revision, state, attempt, retry_reason, hold_token,
              account, network, wallet_id, payload, updated_at
            ) VALUES (?, ?, 1, 'pending', 0, NULL, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, event_id) DO NOTHING
            """,
            (tenant, event_id, account, network, wallet_id, encoded, _now()),
        )


def jobs_for(database: Any, tenant: str) -> list[dict[str, Any]]:
    rows = database.conn.execute(
        """
        SELECT tenant_id, event_id, event_revision, state, attempt, retry_reason, hold_token,
               account, network, wallet_id, payload, updated_at
        FROM explanation_jobs WHERE tenant_id=? ORDER BY event_id
        """,
        (tenant,),
    ).fetchall()
    found = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["payload"] = _loads(item["payload"])
        found.append(item)
    return found


def claim_job(database: Any, tenant: str, event_id: str) -> bool:
    """One worker owns the job. A concurrent claim loses."""
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        updated = database.conn.execute(
            """
            UPDATE explanation_jobs
            SET state='running', attempt=attempt+1, updated_at=?
            WHERE tenant_id=? AND event_id=? AND state IN ('pending', 'retry')
            """,
            (_now(), tenant, event_id),
        )
        if updated.rowcount != 1:
            database.conn.rollback()
            return False
        database.conn.commit()
        return True
    except Exception:
        database.conn.rollback()
        raise


def force_pending(database: Any, tenant: str, event_id: str) -> None:
    """An explicit refresh may re-enter a finished or abandoned job."""
    with database.conn:
        database.conn.execute(
            """
            UPDATE explanation_jobs SET state='pending', retry_reason=NULL, hold_token=NULL, updated_at=?
            WHERE tenant_id=? AND event_id=? AND state!='running'
            """,
            (_now(), tenant, event_id),
        )


def finish_job(
    database: Any,
    tenant: str,
    event_id: str,
    *,
    state: str,
    retry_reason: str | None,
    hold_token: str | None,
    event_revision: int,
) -> None:
    with database.conn:
        database.conn.execute(
            """
            UPDATE explanation_jobs
            SET state=?, retry_reason=?, hold_token=?, event_revision=?, updated_at=?
            WHERE tenant_id=? AND event_id=?
            """,
            (state, retry_reason, hold_token, event_revision, _now(), tenant, event_id),
        )


def insert_revision(
    database: Any,
    tenant: str,
    event_id: str,
    body: dict[str, Any],
    evidence: str,
) -> int | None:
    """Store one revision. The same evidence fingerprint is not written twice."""
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        existing = database.conn.execute(
            """
            SELECT revision FROM explanation_revisions
            WHERE tenant_id=? AND event_id=? AND evidence_fingerprint=?
            """,
            (tenant, event_id, evidence),
        ).fetchone()
        if existing is not None:
            database.conn.commit()
            return int(existing["revision"])
        current = database.conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM explanation_revisions WHERE tenant_id=? AND event_id=?",
            (tenant, event_id),
        ).fetchone()
        revision = int(current["revision"]) + 1
        stored = dict(body)
        stored["revision"] = revision
        previous = revision - 1 if revision > 1 else None
        if body.get("supersedes") is not None:
            stored["supersedes"] = body["supersedes"]
        elif previous is not None:
            stored["supersedes"] = previous
        database.conn.execute(
            """
            INSERT INTO explanation_revisions(
              tenant_id, event_id, revision, event_revision, evidence_fingerprint, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                tenant,
                event_id,
                revision,
                int(stored.get("event_revision") or 1),
                evidence,
                canonical(stored),
            ),
        )
        database.conn.commit()
        return revision
    except Exception:
        database.conn.rollback()
        raise


def latest_revision(database: Any, tenant: str, event_id: str) -> dict[str, Any] | None:
    row = database.conn.execute(
        """
        SELECT revision, payload FROM explanation_revisions
        WHERE tenant_id=? AND event_id=? ORDER BY revision DESC LIMIT 1
        """,
        (tenant, event_id),
    ).fetchone()
    if row is None:
        return None
    body = _loads(row["payload"])
    body["revision"] = int(row["revision"])
    return body


def revision_count(database: Any, tenant: str, event_id: str) -> int:
    row = database.conn.execute(
        "SELECT COUNT(*) AS n FROM explanation_revisions WHERE tenant_id=? AND event_id=?",
        (tenant, event_id),
    ).fetchone()
    return int(row["n"])


def hold_token(database: Any, tenant: str, wallet_id: str, network: str, checkpoints: dict[str, str], manifest_hash: str) -> str:
    """Local evidence only. A new stored event changes the token; a provider call does not."""
    count = database.conn.execute(
        "SELECT COUNT(*) AS n FROM chain_events WHERE tenant_id=? AND wallet_id=? AND network=?",
        (tenant, wallet_id, network),
    ).fetchone()
    return fingerprint({
        "checkpoints": checkpoints,
        "events": int(count["n"]),
        "manifest": manifest_hash,
    })


def supersede_checkpoint(database: Any, tenant: str, network: str, block_hash: str) -> int:
    """A reorg replaces the current verdict. The old row stays for the audit trail."""
    target = block_hash.lower()
    changed = 0
    for job in jobs_for(database, tenant):
        if job["network"] != network:
            continue
        current = latest_revision(database, tenant, job["event_id"])
        if current is None:
            continue
        hashes = current.get("checkpoint_hashes") if isinstance(current.get("checkpoint_hashes"), dict) else {}
        owned = {str(value).lower() for value in hashes.values() if isinstance(value, str)}
        if target not in owned:
            continue
        if current.get("verdict") == "UNVERIFIED" and "reorg" in (current.get("gaps") or []):
            continue
        body = dict(current)
        body["verdict"] = "UNVERIFIED"
        body["attempt_state"] = "superseded"
        body["retry_reason"] = "reorg"
        body["event_revision"] = int(job["event_revision"]) + 1
        body["supersedes"] = int(current["revision"])
        gaps = list(body.get("gaps") or [])
        gaps.append("reorg")
        body["gaps"] = sorted(set(gaps))
        body["funds_marked_safe"] = False
        body["severity_changed"] = False
        evidence = fingerprint({"reorg": target, "supersedes": current["revision"], "event_id": job["event_id"]})
        insert_revision(database, tenant, job["event_id"], body, evidence)
        token = hold_token(
            database, tenant, str(job["wallet_id"]), network,
            {key: str(value) for key, value in hashes.items()},
            str(current.get("manifest_hash") or ""),
        )
        finish_job(
            database, tenant, job["event_id"],
            state="retry", retry_reason="reorg", hold_token=token, event_revision=int(job["event_revision"]) + 1,
        )
        changed += 1
    return changed
