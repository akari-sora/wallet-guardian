"""Shadow report against a legacy monitor_state.json file.

The legacy file is read and never written. Output keys are wallet ids and
masked transaction ids. Raw addresses are not copied out of the file.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError
from guardian.legacy_state import LEGACY_CHAINS, _parse_alert, convert_legacy_document
from guardian.storage import Database

_ADDRESS = re.compile(r"(?i)0x[0-9a-f]{40}")
_LATEST = re.compile(r"^([^_]+)_([^_]+)_(.+)$")


def compare_with_legacy(
    legacy_state_path: str | Path,
    database: Database,
    tenant: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Compare one shared window. Alerted history is dedup, not the miss set."""
    path = Path(legacy_state_path)
    try:
        raw = path.read_bytes()
    except OSError:
        raise ConfigError("legacy state was not found") from None
    try:
        legacy = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ConfigError("legacy state is not valid json") from None
    if not isinstance(legacy, dict):
        raise ConfigError("legacy state must be an object")
    labels = _labels(database, tenant)
    _ignored_rows, unmapped, legacy_networks = _legacy_rows(legacy, labels)
    del _ignored_rows
    legacy_events, uncomparable, dedup_txs = _legacy_events(legacy, labels)
    mcm_events, mcm_networks = _mcm_events(database, tenant)
    window = _window(since, until, legacy_events, mcm_events)
    legacy_in, mcm_in, dropped = _inside(legacy_events, mcm_events, window)
    uncomparable.extend(dropped)
    legacy_by = {row["key"]: row for row in legacy_in}
    mcm_by = {row["key"]: row for row in mcm_in}
    matched: list[dict[str, str]] = []
    legacy_only: list[dict[str, str]] = []
    mcm_only: list[dict[str, str]] = []
    awaiting: list[dict[str, str]] = []
    for key in sorted(set(legacy_by) | set(mcm_by)):
        left = legacy_by.get(key)
        right = mcm_by.get(key)
        if left is not None and right is not None:
            bucket = matched if _settled(left) and _settled(right) else awaiting
            bucket.append(_public_event(left))
            continue
        only = left if left is not None else right
        if only is None:
            continue
        if not _settled(only):
            awaiting.append(_public_event(only))
            continue
        if left is None and (only["network"], only["tx"]) in dedup_txs:
            continue
        if left is not None:
            legacy_only.append(_public_event(only))
        else:
            mcm_only.append(_public_event(only))
    never = sorted(set(mcm_networks) - set(LEGACY_CHAINS.values()))
    uncomparable.sort(key=lambda row: (row.get("reason", ""), row.get("network", ""), row.get("tx", ""), row.get("wallet_id", "")))
    report = {
        "awaiting_finality": awaiting,
        "counts": {
            "awaiting_finality": len(awaiting),
            "legacy_only": len(legacy_only),
            "matched": len(matched),
            "matched_tx_level": 0,
            "mcm_only": len(mcm_only),
            "uncomparable": len(uncomparable),
            "unmapped_legacy": len(unmapped),
        },
        "coverage_differences": {
            "legacy_networks": sorted(legacy_networks),
            "mcm_networks": sorted(mcm_networks),
            "networks_legacy_never_checked": never,
        },
        "legacy_coverage_windows": [],
        "legacy_only": legacy_only,
        "matched": matched,
        "matched_tx_level": [],
        "mcm_only": mcm_only,
        "uncomparable": uncomparable,
        "unmapped_legacy": unmapped,
        "window": {"since": window[0], "until": window[1]},
    }
    _apply_tx_level(report, legacy, labels, mcm_events, since, until)
    return report


def _legacy_rows(legacy: dict[str, Any], labels: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, str]], set[str]]:
    rows: list[dict[str, str]] = []
    unmapped: list[dict[str, str]] = []
    networks: set[str] = set()
    alerted = legacy.get("alerted")
    if "alerted" in legacy and not isinstance(alerted, list):
        unmapped.append({"reason": "bad_type"})
        alerted = []
    for entry in alerted or []:
        if not isinstance(entry, str):
            unmapped.append({"reason": "unparseable"})
            continue
        parsed = _parse_alert(entry)
        if parsed is None:
            unmapped.append({"reason": "free_text", "entry": _safe(entry)})
            continue
        network = LEGACY_CHAINS.get(parsed["chain"])
        if network is None or not parsed["tx"].startswith("0x") or len(parsed["tx"]) != 66:
            unmapped.append({"reason": "non_standard", "entry": _safe(entry.split(" ", 1)[0])})
            continue
        networks.add(network)
        wallet_id = labels.get(parsed["tag"], "")
        rows.append({
            "label": _safe(parsed["tag"]),
            "network": network,
            "tx": parsed["tx"].lower(),
            "tx_key": f"{network}:{parsed['tx'].lower()}",
            "wallet_id": wallet_id,
        })
    latest = legacy.get("latest_seen")
    if isinstance(latest, dict):
        for key in latest:
            if not isinstance(key, str):
                unmapped.append({"reason": "free_text"})
                continue
            match = _LATEST.fullmatch(key)
            if match is None:
                unmapped.append({"reason": "free_text", "entry": _safe(key)})
                continue
            network = LEGACY_CHAINS.get(match.group(1))
            if network is None:
                unmapped.append({"reason": "non_standard", "entry": _safe(key)})
            else:
                networks.add(network)
    elif "latest_seen" in legacy:
        unmapped.append({"reason": "bad_type"})
    for key in legacy:
        if key not in {"alerted", "events", "latest_seen", "nonces", "bsc_last_block", "_note", "last_run_taipei", "sources", "ignored_spam", "news_checked", "runs"}:
            unmapped.append({"reason": "unknown_field", "entry": _safe(str(key))})
    return rows, unmapped, networks


_SETTLED = frozenset({"confirmed", "finalized"})
_EVENT_FIELDS = ("wallet_id", "network", "tx", "position", "kind", "subject")
EXPLAINED_REASONS = frozenset({
    "awaiting_finality",
    "legacy_free_text",
    "outside_legacy_scope",
    "source_gap_declared",
    "spam_suppressed_rule",
})


def _legacy_events(
    legacy: dict[str, Any],
    labels: dict[str, str],
) -> tuple[list[dict[str, str]], list[dict[str, str]], set[tuple[str, str]]]:
    """Full events compare. ``alerted`` is a dedup set, not a miss list."""
    events: list[dict[str, str]] = []
    uncomparable: list[dict[str, str]] = []
    dedup: set[tuple[str, str]] = set()
    alerted = legacy.get("alerted")
    if isinstance(alerted, list):
        for entry in alerted:
            if not isinstance(entry, str):
                uncomparable.append({"reason": "missing_event_identity"})
                continue
            parsed = _parse_alert(entry)
            network = ""
            tx = ""
            wallet_id = ""
            label = ""
            if parsed is not None:
                network = LEGACY_CHAINS.get(parsed["chain"], "")
                label = _safe(parsed["tag"])
                wallet_id = labels.get(parsed["tag"], "")
                if parsed["tx"].startswith("0x") and len(parsed["tx"]) == 66:
                    tx = parsed["tx"].lower()
            if network and tx:
                dedup.add((network, tx))
            uncomparable.append({
                "label": label,
                "network": network,
                "reason": "missing_event_identity",
                "tx": _mask(tx) if tx else "",
                "wallet_id": wallet_id,
            })
    raw = legacy.get("events")
    if isinstance(raw, list):
        for item in raw:
            events.append(_coerce_legacy_event(item, labels))
    elif "events" in legacy:
        uncomparable.append({"reason": "bad_type"})
    return events, uncomparable, dedup


def _coerce_legacy_event(item: Any, labels: dict[str, str]) -> dict[str, str]:
    if not isinstance(item, dict):
        return {"comparable": "", "reason": "missing_event_identity"}
    label = item.get("label") if isinstance(item.get("label"), str) else ""
    wallet_id = item.get("wallet_id") if isinstance(item.get("wallet_id"), str) else ""
    if not wallet_id and label:
        wallet_id = labels.get(label, "")
    network = item.get("network") if isinstance(item.get("network"), str) else ""
    if not network and isinstance(item.get("chain"), str):
        network = LEGACY_CHAINS.get(item["chain"], "")
    tx = item.get("tx") if isinstance(item.get("tx"), str) else ""
    if tx.startswith("0x"):
        tx = tx.lower()
    position = item.get("position")
    position_text = str(position) if isinstance(position, int | str) and not isinstance(position, bool) and str(position) else ""
    kind = item.get("kind") if isinstance(item.get("kind"), str) else ""
    subject = item.get("asset") or item.get("spender") or item.get("position_id") or ""
    subject_text = subject if isinstance(subject, str) else ""
    finality = item.get("finality") if isinstance(item.get("finality"), str) else "unknown"
    observed = item.get("observed_at") if isinstance(item.get("observed_at"), str) else ""
    row = {
        "comparable": "yes",
        "finality": finality,
        "kind": kind,
        "label": _safe(label),
        "network": network,
        "observed_at": observed,
        "position": position_text,
        "reason": "",
        "subject": subject_text,
        "tx": tx,
        "wallet_id": wallet_id,
    }
    if not _complete(row) or not tx.startswith("0x") or len(tx) != 66:
        row["comparable"] = ""
        row["reason"] = "missing_event_identity"
    row["key"] = _event_key(row)
    return row


def _mcm_events(database: Database, tenant: str) -> tuple[list[dict[str, str]], set[str]]:
    rows: list[dict[str, str]] = []
    networks: set[str] = set()
    for payload in database.chain_events_for(tenant):
        network = payload.get("network") if isinstance(payload.get("network"), str) else ""
        if network:
            networks.add(network)
        tx = payload.get("tx_id") if isinstance(payload.get("tx_id"), str) else ""
        if tx.startswith("0x"):
            tx = tx.lower()
        position = payload.get("sub_index")
        position_text = str(position) if position is not None and not isinstance(position, bool) else ""
        kind = payload.get("kind") if isinstance(payload.get("kind"), str) else ""
        subject = payload.get("asset") if isinstance(payload.get("asset"), str) else ""
        wallet_id = payload.get("wallet_id") if isinstance(payload.get("wallet_id"), str) else ""
        finality = payload.get("finality") if isinstance(payload.get("finality"), str) else "unknown"
        observed = payload.get("observed_at") if isinstance(payload.get("observed_at"), str) else ""
        row = {
            "comparable": "yes",
            "finality": finality,
            "kind": kind,
            "label": "",
            "network": network,
            "observed_at": observed,
            "position": position_text,
            "reason": "",
            "subject": subject,
            "tx": tx,
            "wallet_id": wallet_id,
        }
        if not _complete(row) or not tx.startswith("0x") or len(tx) != 66:
            row["comparable"] = ""
            row["reason"] = "missing_event_identity"
        row["key"] = _event_key(row)
        rows.append(row)
    from guardian.news.fetch import NEWS_STATE_WALLET

    for state in database.capability_state_for(tenant):
        if state.get("wallet_id") == NEWS_STATE_WALLET:
            continue  # news feed state is not a monitored network
        network = state.get("network")
        if isinstance(network, str):
            networks.add(network)
    return rows, networks


def _complete(row: dict[str, str]) -> bool:
    return all(row.get(field) for field in _EVENT_FIELDS)


def _event_key(row: dict[str, str]) -> str:
    return "\x1f".join(row.get(field, "") for field in _EVENT_FIELDS)


def _settled(row: dict[str, str]) -> bool:
    return row.get("finality") in _SETTLED


def _window(
    since: str | None,
    until: str | None,
    legacy_events: list[dict[str, str]],
    mcm_events: list[dict[str, str]],
) -> tuple[str | None, str | None]:
    if since or until:
        return since or None, until or None
    left = _span(legacy_events)
    right = _span(mcm_events)
    if left and right:
        start = max(left[0], right[0])
        end = min(left[1], right[1])
        if start <= end:
            return start, end
        return None, None
    return None, None


def _span(events: list[dict[str, str]]) -> tuple[str, str] | None:
    times = sorted(row["observed_at"] for row in events if row.get("observed_at"))
    if not times:
        return None
    return times[0], times[-1]


def _inside(
    legacy_events: list[dict[str, str]],
    mcm_events: list[dict[str, str]],
    window: tuple[str | None, str | None],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    dropped: list[dict[str, str]] = []

    def take(events: list[dict[str, str]]) -> list[dict[str, str]]:
        kept: list[dict[str, str]] = []
        for row in events:
            if row.get("comparable") != "yes":
                public = _public_event(row)
                public["reason"] = row.get("reason") or "missing_event_identity"
                dropped.append(public)
                continue
            if not _in_window(row.get("observed_at", ""), window):
                if not row.get("observed_at"):
                    public = _public_event(row)
                    public["reason"] = "missing_event_identity"
                    dropped.append(public)
                continue
            kept.append(row)
        return kept

    return take(legacy_events), take(mcm_events), dropped


def _in_window(observed: str, window: tuple[str | None, str | None]) -> bool:
    start, end = window
    if start is None and end is None:
        return True
    if not observed:
        return False
    if start is not None and observed < start:
        return False
    if end is not None and observed > end:
        return False
    return True


def _public_event(row: dict[str, str]) -> dict[str, str]:
    tx = row.get("tx") or ""
    return {
        "kind": row.get("kind", ""),
        "label": row.get("label", ""),
        "network": row.get("network", ""),
        "position": row.get("position", ""),
        "subject": _mask_subject(row.get("subject", "")),
        "tx": _mask(tx) if tx.startswith("0x") else "",
        "wallet_id": row.get("wallet_id", ""),
    }


def _mask_subject(value: str) -> str:
    if not value:
        return ""
    if _ADDRESS.search(value) or (value.startswith("0x") and len(value) >= 12):
        return _mask(value)
    return value


def _labels(database: Database, tenant: str) -> dict[str, str]:
    """Current labels and aliases. A label that points at two wallets is not guessed."""
    found: dict[str, str] = {}
    ambiguous: set[str] = set()

    def add(label: str, wallet_id: str) -> None:
        if not label or label in ambiguous:
            return
        previous = found.get(label)
        if previous is not None and previous != wallet_id:
            ambiguous.add(label)
            found.pop(label, None)
            return
        found[label] = wallet_id

    for row in database.conn.execute(
        "SELECT label, wallet_id FROM wallet_identities WHERE tenant_id=?",
        (tenant,),
    ):
        add(str(row["label"]), str(row["wallet_id"]))
    for row in database.conn.execute(
        "SELECT label, wallet_id FROM wallet_label_aliases WHERE tenant_id=?",
        (tenant,),
    ):
        add(str(row["label"]), str(row["wallet_id"]))
    return found


def explain_diff(row: dict[str, str], reason: str, evidence_ref: str) -> dict[str, str]:
    """Mark one diff explained only with a fixed reason and an evidence ref."""
    if reason in EXPLAINED_REASONS and isinstance(evidence_ref, str) and evidence_ref:
        row["classification"] = "explained"
        row["classification_reason"] = reason
        row["evidence_ref"] = evidence_ref
        return row
    row["classification"] = "unexplained"
    row["classification_reason"] = ""
    row["evidence_ref"] = ""
    return row


def _apply_tx_level(
    report: dict[str, Any],
    legacy: dict[str, Any],
    labels: dict[str, str],
    mcm_events: list[dict[str, str]],
    since: str | None,
    until: str | None,
) -> None:
    """Match alerted rows at wallet+network+tx. Full-identity matches stay in ``matched``.

    A tx-level row is a miss candidate only inside a settled, concrete window.
    Raw diffs are recorded before notification dedup, so an alerted marker does
    not drop a legacy tx that MCM never saw.
    """
    converted = convert_legacy_document(legacy, labels)
    report["legacy_coverage_windows"] = list(converted["coverage_windows"])
    window = _tx_window(since, until, converted["common_since"], converted["common_until"])
    if window[0] is None and window[1] is None:
        _stamp_unexplained(report["legacy_only"])
        _stamp_unexplained(report["mcm_only"])
        return
    settled_mcm: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in mcm_events:
        if row.get("comparable") != "yes" or not row.get("wallet_id") or not _settled(row):
            continue
        if not _covered(row.get("observed_at", ""), window):
            continue
        settled_mcm.setdefault((row["wallet_id"], row["network"], row["tx"]), []).append(row)
    matched_tx: list[dict[str, str]] = []
    extra_legacy: list[dict[str, str]] = []
    matched_public: set[tuple[str, str, str]] = set()
    seen_legacy: set[tuple[str, str, str]] = set()
    for row in converted["tx_rows"]:
        if not row.get("wallet_id") or row.get("finality") not in _SETTLED:
            continue
        if not _covered(row.get("observed_at", ""), window):
            continue
        key = (row["wallet_id"], row["network"], row["tx"])
        if key in seen_legacy:
            continue
        seen_legacy.add(key)
        public = _public_event(row)
        public["tier"] = "tx"
        public["evidence_ref"] = row.get("evidence_ref", "")
        if key in settled_mcm:
            public["classification"] = "matched_tx_level"
            public["classification_reason"] = ""
            matched_tx.append(public)
            matched_public.add((public["wallet_id"], public["network"], public["tx"]))
            continue
        public["classification"] = "unexplained"
        public["classification_reason"] = ""
        extra_legacy.append(public)
    if extra_legacy:
        report["legacy_only"].extend(extra_legacy)
    if matched_public:
        report["mcm_only"] = [
            row for row in report["mcm_only"]
            if (row.get("wallet_id", ""), row.get("network", ""), row.get("tx", "")) not in matched_public
        ]
    report["matched_tx_level"] = matched_tx
    _stamp_unexplained(report["legacy_only"])
    _stamp_unexplained(report["mcm_only"])
    report["counts"]["legacy_only"] = len(report["legacy_only"])
    report["counts"]["matched_tx_level"] = len(matched_tx)
    report["counts"]["mcm_only"] = len(report["mcm_only"])


def _tx_window(since: str | None, until: str | None, common_since: str | None, common_until: str | None) -> tuple[str | None, str | None]:
    if since or until:
        return since or None, until or None
    return common_since or None, common_until or None


def _covered(observed: str, window: tuple[str | None, str | None]) -> bool:
    """Tx-level comparison needs a concrete window. An open window is not coverage."""
    if window[0] is None and window[1] is None:
        return False
    return _in_window(observed, window)


def _stamp_unexplained(rows: list[dict[str, str]]) -> None:
    for row in rows:
        row.setdefault("classification", "unexplained")
        row.setdefault("classification_reason", "")
        row.setdefault("evidence_ref", "")


def _mask(tx: str) -> str:
    digest = hashlib.sha256(tx.encode("utf-8")).hexdigest()[:12]
    if len(tx) < 12:
        return digest
    return tx[:6] + "…" + tx[-4:] + ":" + digest


def _safe(text: str) -> str:
    if _ADDRESS.search(text) or "://" in text or len(text) > 96:
        return "redacted"
    return text
