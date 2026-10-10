"""Tenant-scoped explanation jobs and revisions. SQL stays on the monitor database."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from guardian.explanations.models import canonical, fingerprint


# Process-local bookkeeping. It is not evidence that a job was abandoned.
# Abandonment is an expired row in explanation_job_leases.
_LIVE: dict[tuple[str, str], int] = {}
LEASE_SECONDS = 120


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(value: str) -> dict[str, Any]:
    body = json.loads(value)
    return body if isinstance(body, dict) else {}


def release_process_claims() -> None:
    """Drop in-process bookkeeping. An unexpired lease still owns the job."""
    _LIVE.clear()


def _deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lease_unexpired(database: Any, tenant: str, event_id: str, now: str) -> bool:
    row = database.conn.execute(
        "SELECT expires_at FROM explanation_job_leases WHERE tenant_id=? AND event_id=?",
        (tenant, event_id),
    ).fetchone()
    return row is not None and str(row["expires_at"]) > now


def _write_lease(database: Any, tenant: str, event_id: str, attempt: int) -> None:
    database.conn.execute(
        """
        INSERT INTO explanation_job_leases(tenant_id, event_id, attempt, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tenant_id, event_id) DO UPDATE SET
          attempt=excluded.attempt,
          expires_at=excluded.expires_at
        """,
        (tenant, event_id, attempt, _deadline()),
    )


def _drop_lease(database: Any, tenant: str, event_id: str) -> None:
    database.conn.execute(
        "DELETE FROM explanation_job_leases WHERE tenant_id=? AND event_id=?",
        (tenant, event_id),
    )


def _discovery_current(
    database: Any,
    tenant: str,
    event_id: str,
    attempt: int | None,
    generation: int | None,
) -> bool:
    if attempt is None or generation is None:
        return True
    row = database.conn.execute(
        "SELECT state, attempt, event_revision FROM explanation_jobs WHERE tenant_id=? AND event_id=?",
        (tenant, event_id),
    ).fetchone()
    if row is None:
        return False
    return (
        str(row["state"]) == "running"
        and int(row["attempt"]) == attempt
        and int(row["event_revision"]) == generation
    )


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


def job_for(database: Any, tenant: str, event_id: str) -> dict[str, Any] | None:
    row = database.conn.execute(
        """
        SELECT tenant_id, event_id, event_revision, state, attempt, retry_reason, hold_token,
               account, network, wallet_id, payload, updated_at
        FROM explanation_jobs WHERE tenant_id=? AND event_id=?
        """,
        (tenant, event_id),
    ).fetchone()
    if row is None:
        return None
    item = {key: row[key] for key in row.keys()}
    item["payload"] = _loads(item["payload"])
    return item


def claim_job(database: Any, tenant: str, event_id: str) -> bool:
    """One worker owns the job until its lease expires.

    A running job with an unexpired lease is not taken, including by another
    process. Process-local memory is not evidence that the job was abandoned.
    An expired or missing lease on a running job can be reclaimed.
    """
    key = (tenant, event_id)
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        row = database.conn.execute(
            "SELECT state, attempt FROM explanation_jobs WHERE tenant_id=? AND event_id=?",
            (tenant, event_id),
        ).fetchone()
        if row is None:
            database.conn.rollback()
            return False
        state = str(row["state"])
        attempt = int(row["attempt"])
        now = _now()
        if state == "running" and _lease_unexpired(database, tenant, event_id, now):
            database.conn.rollback()
            return False
        if state == "running" or state in {"pending", "retry"}:
            updated = database.conn.execute(
                """
                UPDATE explanation_jobs
                SET state='running', attempt=attempt+1, updated_at=?
                WHERE tenant_id=? AND event_id=? AND state=? AND attempt=?
                """,
                (now, tenant, event_id, state, attempt),
            )
        else:
            database.conn.rollback()
            return False
        if updated.rowcount != 1:
            database.conn.rollback()
            return False
        _write_lease(database, tenant, event_id, attempt + 1)
        database.conn.commit()
    except Exception:
        database.conn.rollback()
        raise
    _LIVE[key] = attempt + 1
    return True


def owns_attempt(tenant: str, event_id: str, attempt: int) -> bool:
    return _LIVE.get((tenant, event_id)) == attempt


def release_attempt(tenant: str, event_id: str, attempt: int) -> None:
    key = (tenant, event_id)
    if _LIVE.get(key) == attempt:
        _LIVE.pop(key, None)


def force_pending(database: Any, tenant: str, event_id: str) -> None:
    """An explicit refresh may re-enter a finished job or a job whose lease expired.

    A running job with an unexpired lease stays running.
    """
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        row = database.conn.execute(
            "SELECT state FROM explanation_jobs WHERE tenant_id=? AND event_id=?",
            (tenant, event_id),
        ).fetchone()
        if row is None:
            database.conn.rollback()
            return
        if str(row["state"]) == "running" and _lease_unexpired(database, tenant, event_id, _now()):
            database.conn.rollback()
            return
        _drop_lease(database, tenant, event_id)
        database.conn.execute(
            """
            UPDATE explanation_jobs SET state='pending', retry_reason=NULL, hold_token=NULL, updated_at=?
            WHERE tenant_id=? AND event_id=?
            """,
            (_now(), tenant, event_id),
        )
        database.conn.commit()
    except Exception:
        database.conn.rollback()
        raise


def complete_job(
    database: Any,
    tenant: str,
    event_id: str,
    body: dict[str, Any],
    evidence: str,
    *,
    attempt: int,
    event_revision: int,
    state: str,
    retry_reason: str | None,
    hold_token: str | None,
) -> str:
    """Write the revision and the job together. A stale attempt writes nothing.

    Returns ``stored``, ``stale``, or ``failed``. A write error leaves the job
    retryable and records ``persistence_failure``.
    """
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        row = database.conn.execute(
            "SELECT state, attempt, event_revision FROM explanation_jobs WHERE tenant_id=? AND event_id=?",
            (tenant, event_id),
        ).fetchone()
        if (
            row is None
            or str(row["state"]) != "running"
            or int(row["attempt"]) != attempt
            or int(row["event_revision"]) != event_revision
        ):
            database.conn.rollback()
            return "stale"
        _insert_unlocked(database, tenant, event_id, body, evidence)
        updated = database.conn.execute(
            """
            UPDATE explanation_jobs
            SET state=?, retry_reason=?, hold_token=?, updated_at=?
            WHERE tenant_id=? AND event_id=? AND state='running' AND attempt=? AND event_revision=?
            """,
            (state, retry_reason, hold_token, _now(), tenant, event_id, attempt, event_revision),
        )
        if updated.rowcount != 1:
            database.conn.rollback()
            return "stale"
        _drop_lease(database, tenant, event_id)
        database.conn.commit()
        return "stored"
    except Exception:
        database.conn.rollback()
        _mark_persistence_failure(database, tenant, event_id, attempt)
        return "failed"


def _mark_persistence_failure(database: Any, tenant: str, event_id: str, attempt: int) -> None:
    try:
        with database.conn:
            database.conn.execute(
                """
                UPDATE explanation_jobs
                SET state='retry', retry_reason='persistence_failure', updated_at=?
                WHERE tenant_id=? AND event_id=? AND state='running' AND attempt=?
                """,
                (_now(), tenant, event_id, attempt),
            )
            database.conn.execute(
                """
                INSERT INTO explanation_failures(
                  tenant_id, event_id, reason, account, network, wallet_id, payload, updated_at
                ) VALUES (?, ?, 'persistence_failure', NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(tenant_id, event_id, reason) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (tenant, event_id, _now()),
            )
            _drop_lease(database, tenant, event_id)
    except Exception:
        return


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
        revision = _insert_unlocked(database, tenant, event_id, body, evidence)
        database.conn.commit()
        return revision
    except Exception:
        database.conn.rollback()
        raise


def _insert_unlocked(
    database: Any,
    tenant: str,
    event_id: str,
    body: dict[str, Any],
    evidence: str,
) -> int:
    existing = database.conn.execute(
        """
        SELECT revision FROM explanation_revisions
        WHERE tenant_id=? AND event_id=? AND evidence_fingerprint=?
        """,
        (tenant, event_id, evidence),
    ).fetchone()
    if existing is not None:
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
    return revision


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
    """A reorg replaces the current verdict. The old row stays for the audit trail.

    The job generation advances in the same transaction. A worker that still
    holds the previous generation cannot write over this row.
    """
    target = block_hash.lower()
    changed = 0
    for job in jobs_for(database, tenant):
        if job["network"] != network:
            continue
        if not _supersede_one(database, tenant, job, target):
            continue
        changed += 1
    return changed


def _checkpoint_hashes(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    found: set[str] = set()
    for key in ("old_checkpoint", "new_checkpoint"):
        point = payload.get(key)
        if not isinstance(point, dict):
            continue
        digest = point.get("block_hash")
        if isinstance(digest, str) and digest.startswith("0x") and len(digest) == 66:
            found.add(digest.lower())
    return found


def _supersede_one(database: Any, tenant: str, job: dict[str, Any], target: str) -> bool:
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        current_row = database.conn.execute(
            """
            SELECT revision, payload FROM explanation_revisions
            WHERE tenant_id=? AND event_id=? ORDER BY revision DESC LIMIT 1
            """,
            (tenant, job["event_id"]),
        ).fetchone()
        if current_row is None:
            fenced = _fence_unrevised(database, tenant, job["event_id"], target)
            if not fenced:
                database.conn.rollback()
                return False
            database.conn.commit()
            return True
        current = _loads(current_row["payload"])
        current["revision"] = int(current_row["revision"])
        hashes = current.get("checkpoint_hashes") if isinstance(current.get("checkpoint_hashes"), dict) else {}
        owned = {str(value).lower() for value in hashes.values() if isinstance(value, str) and value}
        live_payload = database.conn.execute(
            "SELECT payload FROM explanation_jobs WHERE tenant_id=? AND event_id=?",
            (tenant, job["event_id"]),
        ).fetchone()
        if live_payload is not None:
            owned.update(_checkpoint_hashes(_loads(live_payload["payload"])))
        if target not in owned:
            database.conn.rollback()
            return False
        if current.get("verdict") == "UNVERIFIED" and "reorg" in (current.get("gaps") or []):
            database.conn.rollback()
            return False
        live = database.conn.execute(
            "SELECT event_revision FROM explanation_jobs WHERE tenant_id=? AND event_id=?",
            (tenant, job["event_id"]),
        ).fetchone()
        if live is None:
            database.conn.rollback()
            return False
        generation = int(live["event_revision"]) + 1
        body = dict(current)
        body["verdict"] = "UNVERIFIED"
        body["attempt_state"] = "superseded"
        body["retry_reason"] = "reorg"
        body["event_revision"] = generation
        body["supersedes"] = int(current["revision"])
        gaps = list(body.get("gaps") or [])
        gaps.append("reorg")
        body["gaps"] = sorted(set(gaps))
        body["funds_marked_safe"] = False
        body["severity_changed"] = False
        evidence = fingerprint({"reorg": target, "supersedes": current["revision"], "event_id": job["event_id"]})
        _insert_unlocked(database, tenant, job["event_id"], body, evidence)
        token = hold_token(
            database, tenant, str(job["wallet_id"]), str(job["network"]),
            {key: str(value) for key, value in hashes.items()},
            str(current.get("manifest_hash") or ""),
        )
        database.conn.execute(
            """
            UPDATE explanation_jobs
            SET state='retry', retry_reason='reorg', hold_token=?, event_revision=?, updated_at=?
            WHERE tenant_id=? AND event_id=? AND event_revision=?
            """,
            (token, generation, _now(), tenant, job["event_id"], int(live["event_revision"])),
        )
        _drop_lease(database, tenant, job["event_id"])
        database.conn.commit()
        return True
    except Exception:
        database.conn.rollback()
        raise


def _fence_unrevised(database: Any, tenant: str, event_id: str, target: str) -> bool:
    """Advance the generation when a checkpoint reorg arrives before any revision.

    No placeholder revision is inserted. A second reorg of the same hash does
    not advance the generation again.
    """
    live = database.conn.execute(
        """
        SELECT event_revision, retry_reason, wallet_id, network, payload
        FROM explanation_jobs WHERE tenant_id=? AND event_id=?
        """,
        (tenant, event_id),
    ).fetchone()
    if live is None:
        return False
    payload = _loads(live["payload"])
    if target not in _checkpoint_hashes(payload):
        return False
    if str(live["retry_reason"] or "") == "reorg":
        return False
    generation = int(live["event_revision"]) + 1
    checkpoints = {
        "new": "",
        "old": "",
    }
    raw_new = payload.get("new_checkpoint") if isinstance(payload, dict) else None
    raw_old = payload.get("old_checkpoint") if isinstance(payload, dict) else None
    if isinstance(raw_new, dict) and isinstance(raw_new.get("block_hash"), str):
        checkpoints["new"] = raw_new["block_hash"].lower()
    if isinstance(raw_old, dict) and isinstance(raw_old.get("block_hash"), str):
        checkpoints["old"] = raw_old["block_hash"].lower()
    token = hold_token(database, tenant, str(live["wallet_id"]), str(live["network"]), checkpoints, "")
    updated = database.conn.execute(
        """
        UPDATE explanation_jobs
        SET state='retry', retry_reason='reorg', hold_token=?, event_revision=?, updated_at=?
        WHERE tenant_id=? AND event_id=? AND event_revision=?
        """,
        (token, generation, _now(), tenant, event_id, int(live["event_revision"])),
    )
    if updated.rowcount != 1:
        return False
    _drop_lease(database, tenant, event_id)
    return True


def record_enqueue_failure(
    database: Any,
    tenant: str,
    event_id: str,
    *,
    account: str,
    network: str,
    wallet_id: str,
    payload: dict[str, Any],
) -> None:
    """Remember an enqueue that did not land. Recovery reads this row."""
    with database.conn:
        database.conn.execute(
            """
            INSERT INTO explanation_failures(
              tenant_id, event_id, reason, account, network, wallet_id, payload, updated_at
            ) VALUES (?, ?, 'enqueue_failure', ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, event_id, reason) DO UPDATE SET
              account=excluded.account,
              network=excluded.network,
              wallet_id=excluded.wallet_id,
              payload=excluded.payload,
              updated_at=excluded.updated_at
            """,
            (tenant, event_id, account, network, wallet_id, canonical(payload), _now()),
        )


def record_cycle_failure(database: Any, tenant: str, reason: str) -> bool:
    """A drain that could not finish is visible on the failure table."""
    text = reason if isinstance(reason, str) and reason else "read_error"
    try:
        with database.conn:
            database.conn.execute(
                """
                INSERT INTO explanation_failures(
                  tenant_id, event_id, reason, account, network, wallet_id, payload, updated_at
                ) VALUES (?, '__cycle__', ?, NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(tenant_id, event_id, reason) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (tenant, text, _now()),
            )
        return True
    except Exception:
        return False


def enqueue_failures(database: Any, tenant: str) -> list[dict[str, Any]]:
    rows = database.conn.execute(
        """
        SELECT event_id, account, network, wallet_id, payload
        FROM explanation_failures
        WHERE tenant_id=? AND reason='enqueue_failure'
        ORDER BY event_id
        """,
        (tenant,),
    ).fetchall()
    found = []
    for row in rows:
        payload = _loads(row["payload"]) if isinstance(row["payload"], str) else {}
        found.append({
            "account": row["account"],
            "event_id": row["event_id"],
            "network": row["network"],
            "payload": payload,
            "wallet_id": row["wallet_id"],
        })
    return found


def clear_enqueue_failure(database: Any, tenant: str, event_id: str) -> None:
    with database.conn:
        database.conn.execute(
            "DELETE FROM explanation_failures WHERE tenant_id=? AND event_id=? AND reason='enqueue_failure'",
            (tenant, event_id),
        )


def failure_reasons(database: Any, tenant: str, event_id: str) -> list[str]:
    rows = database.conn.execute(
        "SELECT reason FROM explanation_failures WHERE tenant_id=? AND event_id=? ORDER BY reason",
        (tenant, event_id),
    ).fetchall()
    return [str(row["reason"]) for row in rows]


def account_for_wallet(database: Any, tenant: str, wallet_id: str) -> str | None:
    row = database.conn.execute(
        """
        SELECT identity_key FROM wallet_identities
        WHERE tenant_id=? AND wallet_id=? AND family='evm'
        """,
        (tenant, wallet_id),
    ).fetchone()
    if row is None:
        return None
    key = row["identity_key"]
    if isinstance(key, str) and key.startswith("0x") and len(key) == 42:
        return key
    return None


def load_interval(database: Any, tenant: str, event_id: str) -> tuple[int | None, list[dict[str, Any]]]:
    row = database.conn.execute(
        "SELECT interval_cursor, logs_json FROM explanation_discovery WHERE tenant_id=? AND event_id=?",
        (tenant, event_id),
    ).fetchone()
    if row is None:
        return None, []
    cursor = None if row["interval_cursor"] is None else int(row["interval_cursor"])
    try:
        parsed = json.loads(row["logs_json"])
    except json.JSONDecodeError:
        parsed = []
    logs = [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    return cursor, logs


def save_interval(
    database: Any,
    tenant: str,
    event_id: str,
    cursor: int | None,
    logs: list[dict[str, Any]],
    *,
    attempt: int | None = None,
    generation: int | None = None,
) -> None:
    if not _discovery_current(database, tenant, event_id, attempt, generation):
        return
    with database.conn:
        database.conn.execute(
            """
            INSERT INTO explanation_discovery(tenant_id, event_id, interval_cursor, logs_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, event_id) DO UPDATE SET
              interval_cursor=excluded.interval_cursor,
              logs_json=excluded.logs_json
            """,
            (tenant, event_id, cursor, canonical(logs)),
        )


def load_lookback(database: Any, tenant: str, network: str, account: str, manager: str) -> int | None:
    row = database.conn.execute(
        """
        SELECT cursor_block FROM explanation_lookback
        WHERE tenant_id=? AND network=? AND account=? AND manager=?
        """,
        (tenant, network, account, manager),
    ).fetchone()
    if row is None:
        return None
    return int(row["cursor_block"])


def save_lookback(
    database: Any,
    tenant: str,
    network: str,
    account: str,
    manager: str,
    cursor: int,
    *,
    event_id: str | None = None,
    attempt: int | None = None,
    generation: int | None = None,
) -> None:
    if event_id is not None and not _discovery_current(database, tenant, event_id, attempt, generation):
        return
    with database.conn:
        database.conn.execute(
            """
            INSERT INTO explanation_lookback(tenant_id, network, account, manager, cursor_block)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, network, account, manager) DO UPDATE SET
              cursor_block=excluded.cursor_block
            """,
            (tenant, network, account, manager, cursor),
        )


def load_requests(database: Any, tenant: str, network: str, account: str, manager: str) -> list[dict[str, Any]]:
    rows = database.conn.execute(
        """
        SELECT tx_id, log_index, block_number, tx_index, slis, topic, address,
               block_hash, removed, conflict, kind, data_json
        FROM explanation_request_evidence
        WHERE tenant_id=? AND network=? AND account=? AND manager=?
        ORDER BY block_number, tx_index, log_index
        """,
        (tenant, network, account, manager),
    ).fetchall()
    found = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        try:
            parsed = json.loads(item.get("data_json") or "[]")
        except json.JSONDecodeError:
            parsed = []
        if not isinstance(parsed, list) or not parsed:
            parsed = [int(item["slis"])]
        item["data_words"] = [int(word) for word in parsed]
        item["removed"] = bool(item.get("removed"))
        item["conflict"] = bool(item.get("conflict"))
        item["kind"] = str(item.get("kind") or "request")
        item["block_hash"] = str(item.get("block_hash") or "")
        found.append(item)
    return found


def save_requests(
    database: Any,
    tenant: str,
    network: str,
    account: str,
    manager: str,
    logs: list[dict[str, Any]],
    *,
    event_id: str | None = None,
    attempt: int | None = None,
    generation: int | None = None,
) -> None:
    """Keep request and claim logs, including a removal flag and block hash.

    A second copy that disagrees does not replace the first. The row is marked
    as a conflict and is not a verified request.
    """
    if event_id is not None and not _discovery_current(database, tenant, event_id, attempt, generation):
        return
    with database.conn:
        for item in logs:
            kind = item.get("kind")
            if kind not in {"request", "claim"}:
                continue
            tx_id = item.get("tx_id")
            log_index = item.get("log_index")
            words = item.get("data_words")
            if not isinstance(tx_id, str) or isinstance(log_index, bool) or not isinstance(log_index, int):
                continue
            if not isinstance(words, list) or not words:
                continue
            removed = 1 if item.get("removed") else 0
            conflict = 1 if item.get("conflict") else 0
            block_hash = str(item.get("block_hash") or "").lower()
            data_json = canonical([int(word) for word in words])
            database.conn.execute(
                """
                INSERT INTO explanation_request_evidence(
                  tenant_id, network, account, manager, tx_id, log_index, block_number, tx_index, slis, topic, address,
                  block_hash, removed, conflict, kind, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, network, account, manager, tx_id, log_index) DO UPDATE SET
                  conflict = CASE
                    WHEN explanation_request_evidence.block_hash != excluded.block_hash
                      OR explanation_request_evidence.removed != excluded.removed
                      OR explanation_request_evidence.kind != excluded.kind
                      OR explanation_request_evidence.data_json != excluded.data_json
                      OR explanation_request_evidence.block_number != excluded.block_number
                      OR explanation_request_evidence.tx_index != excluded.tx_index
                    THEN 1
                    ELSE explanation_request_evidence.conflict
                  END
                """,
                (
                    tenant,
                    network,
                    account,
                    manager,
                    tx_id,
                    log_index,
                    int(item.get("block_number") or 0),
                    int(item.get("tx_index") or 0),
                    str(words[0]),
                    str(item.get("topic") or ""),
                    str(item.get("address") or manager),
                    block_hash,
                    removed,
                    conflict,
                    str(kind),
                    data_json,
                ),
            )
