"""One private shadow round. Read-only: run-once is always --no-send.

Exit 0 means the round was recorded. It does not mean the window qualified.
Degraded, blocked, fixture, and drill rounds stay non-qualifying. Raw diffs are
written under the evidence directory before any relay dedup. This tool does not
start the 24-window count; the operator does that after merge, on a host with
real DNS. A fake-IP resolver (every name → 198.18.0.1) is private_endpoint or
fetch_error, never ok.

The incremental window is the configured polling interval ending at the round
clock (3h ± 30min). See guardian.cutover.

Each round records detection-latency percentiles for the slot and a check that
outbox rows match chain events above the threshold. A missing input stays null
and does not pass that check. The 24-window qualification rules are unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import stat
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from guardian.cli import main as guardian_main
from guardian.configuration import (
    file_permission_bits,
    load_public_config,
    path_inside_work_tree,
    resolve_private_inputs,
)
from guardian.cutover import GAP_TOKENS, evaluate_rounds
from guardian.errors import ConfigError
from guardian.legacy_state import (
    rebuild_from_verified,
    resume_after_restart,
    review_poison_events,
    snapshot_legacy_readonly,
    verify_bsc_cursor,
)
from guardian.legacy_state import archive_freeform_cursors
from guardian.protocols.pins import KINZA_POOLS, KINZA_PROVIDERS
from guardian.shadow_recorder import attach_slot_metrics, detection_latency_summary, outbox_threshold_check
from guardian.sources.fixtures import PreparedCall
from guardian.storage import Database

_KINZA = {address.lower() for address in (*KINZA_POOLS.values(), *KINZA_PROVIDERS.values())}
_CRITICAL = frozenset({
    "approval",
    "approval_revoked",
    "approval_set",
    "approve",
    "asset_in",
    "asset_out",
    "transfer_in",
    "transfer_out",
})
_FRESH = frozenset({"complete_in_declared_scope", "partial"})
_BAD_STATUS = frozenset({"blocked", "degraded", "error", "failed"})


def evaluate_cutover(rounds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Same gate the round recorder persists."""
    return evaluate_rounds(rounds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcm_shadow_round")
    parser.add_argument("--mcm-config", required=True)
    parser.add_argument("--legacy-state", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--drill", choices=("restart", "source_failure", "rollback"))
    parser.add_argument("--relay", action="store_true", help="write one private mcm_shadow relay record (default off)")
    parser.add_argument("--legacy-runs-dir", default=None, help="same-host legacy capture directory (read-only)")
    parser.add_argument("--legacy-max-age-seconds", type=int, default=12600)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    backend: Any = None,
    sender: Any = None,
    clock: Callable[[], float] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_round(
            mcm_config=args.mcm_config,
            legacy_state=args.legacy_state,
            evidence_dir=args.evidence_dir,
            drill=args.drill,
            relay=bool(args.relay),
            backend=backend,
            sender=sender,
            clock=time.time if clock is None else clock,
            legacy_runs_dir=args.legacy_runs_dir,
            legacy_max_age_seconds=args.legacy_max_age_seconds,
        )
    except ConfigError as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "status": "error"}) + "\n")
        return exc.exit_code
    return 0


def run_round(
    *,
    mcm_config: str,
    legacy_state: str,
    evidence_dir: str,
    drill: str | None,
    relay: bool,
    backend: Any,
    sender: Any,
    clock: Callable[[], float],
    legacy_runs_dir: str | None = None,
    legacy_max_age_seconds: int = 12600,
) -> dict[str, Any]:
    evidence = prepare_evidence_dir(Path(evidence_dir))
    moment = datetime.fromtimestamp(clock(), timezone.utc)
    taken_at = _iso(moment)
    before_inventory = _inventory(mcm_config)
    snapshot = snapshot_legacy_readonly(legacy_state, evidence, taken_at=taken_at)
    legacy = json.loads(Path(snapshot["snapshot_path"]).read_text(encoding="utf-8"))
    if not isinstance(legacy, dict):
        raise ConfigError("legacy state must be an object")
    transport = CountingBackend(backend, fail_first=drill == "source_failure")
    guarded = _GuardedSender(sender)
    commands = evidence / "commands"
    run_argv = ["run-once", "--mcm-config", mcm_config, "--no-send"]
    if "--no-send" not in run_argv:
        raise ConfigError("shadow run-once requires --no-send")
    run_code, run_out, _run_err = _capture(commands, "run-once", run_argv, backend=transport, sender=guarded, clock=clock)
    _capture(commands, "watchdog", ["watchdog", "--no-send", "--mcm-config", mcm_config], backend=transport, sender=guarded, clock=clock)
    _capture(commands, "coverage", ["coverage", "--mcm-config", mcm_config], backend=transport, sender=guarded, clock=clock)
    _register_labels(mcm_config)
    compare_argv = [
        "compare-legacy",
        "--mcm-config", mcm_config,
        "--legacy-state", snapshot["snapshot_path"],
    ]
    if legacy_runs_dir:
        public = load_public_config(mcm_config)
        interval = int(public.document["polling"]["interval_seconds"])
        compare_argv.extend([
            "--legacy-runs-dir", legacy_runs_dir,
            "--slot-start", _iso(moment - timedelta(seconds=interval)),
            "--slot-end", _iso(moment),
            "--legacy-max-age-seconds", str(legacy_max_age_seconds),
        ])
    _compare_code, compare_out, _compare_err = _capture(
        commands, "compare-legacy", compare_argv, backend=transport, sender=guarded, clock=clock,
    )
    # Raw diff before relay dedup. An already-alerted marker must not be the only copy.
    _write_text(evidence / "diffs" / "raw-compare.json", compare_out if compare_out.endswith("\n") else compare_out + "\n")
    compared = _object(compare_out)
    poison = review_poison_events(legacy.get("poison_notes"), _events(legacy.get("poison_events")))
    _write_json(evidence / "poison-review.json", poison)
    cursors = inspect_cursors(legacy, lambda height: _block_hash(transport, height))
    _write_json(evidence / "cursor-report.json", cursors)
    if drill == "restart":
        _write_json(evidence / "restart-proof.json", _restart_proof(legacy))
    budget = transport.snapshot()
    try:
        reported = json.loads(run_out)
    except json.JSONDecodeError:
        reported = None
    if isinstance(reported, dict) and isinstance(reported.get("budget"), dict):
        budget["run_once"] = reported["budget"]
    _write_json(evidence / "budget.json", budget)
    leaks = _leak_scan(mcm_config, evidence)
    _write_json(evidence / "leak-scan.json", {"leaks": leaks})
    if relay:
        _relay(evidence, compared)
    if legacy_runs_dir:
        _write_json(evidence / "legacy-evidence.json", {
            "compare_reason": compared.get("compare_reason") if isinstance(compared.get("compare_reason"), str) else "legacy_evidence_missing",
            "compare_valid": compared.get("compare_valid") is True,
            "legacy_run_id": compared.get("legacy_run_id") if isinstance(compared.get("legacy_run_id"), str) else "",
        })
    record = _round_record(
        mcm_config=mcm_config,
        evidence=evidence,
        taken_at=taken_at,
        moment=moment,
        drill=drill,
        run_code=run_code,
        run_out=run_out,
        compared=compared,
        leaks=leaks,
        guarded=guarded,
        injected_backend=backend is not None,
        legacy=legacy,
        legacy_compare=compared if legacy_runs_dir else None,
    )
    if drill == "source_failure" and "fetch_error" not in record["required_gaps"]:
        record["required_gaps"].append("fetch_error")
        record["required_fresh"] = False
    events, outbox_rows = _round_scope(mcm_config, before_inventory)
    metrics = detection_latency_summary(events)
    record["trigger_count"] = metrics["trigger_count"]
    record["detect_latency_p50_seconds"] = metrics["detect_latency_p50_seconds"]
    record["detect_latency_p95_seconds"] = metrics["detect_latency_p95_seconds"]
    record["outbox_threshold"] = outbox_threshold_check(outbox_rows, events)
    attach_slot_metrics(evidence, taken_at, metrics)
    saved = _append_round(evidence, record)
    decision = evaluate_cutover(saved)
    _write_json(evidence / "cutover.json", decision)
    _write_json(evidence / "round.json", record)
    return {"cutover": decision, "round": record, "snapshot": snapshot}


def prepare_evidence_dir(path: Path) -> Path:
    """Refuse a repo path and a directory looser than 0700."""
    resolved = Path(path).expanduser().resolve()
    if path_inside_work_tree(resolved):
        raise ConfigError("evidence dir is inside the repo")
    if resolved.exists():
        if not resolved.is_dir():
            raise ConfigError("evidence dir is not a directory")
        mode = file_permission_bits(resolved)
        if mode is not None and mode & 0o077:
            raise ConfigError("evidence dir perms looser than 0700")
    else:
        resolved.mkdir(parents=True, mode=0o700)
    if os.name == "posix":
        os.chmod(resolved, 0o700)
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode & 0o077:
            raise ConfigError("evidence dir perms looser than 0700")
    return resolved


def inspect_cursors(legacy: Mapping[str, Any], block_hash_at: Callable[[int], str | None]) -> dict[str, Any]:
    """Archive free-form cursors and use a BSC cursor only when the RPC hash matches."""
    archive = archive_freeform_cursors(legacy)
    candidates: list[Any] = []
    if isinstance(legacy.get("bsc_cursor"), dict):
        candidates.append(legacy["bsc_cursor"])
    extra = legacy.get("cursor_candidates")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict):
                candidates.append(item)
            else:
                archive.append({"derived_block": None, "reason": "prose", "status": "unmapped", "used": False})
    checks: list[dict[str, Any]] = []
    verified: dict[str, Any] | None = None
    for cursor in candidates:
        result = verify_bsc_cursor(cursor, block_hash_at)
        checks.append(result)
        if verified is None and result.get("used") is True:
            verified = result
    rebuild = None
    events = legacy.get("rebuild_events")
    if verified is not None and isinstance(events, list):
        overlap = legacy.get("overlap")
        if type(overlap) is not int or isinstance(overlap, bool) or overlap < 0:
            overlap = 20
        rebuild = rebuild_from_verified(verified, events, overlap=overlap)
    return {"archive": archive, "checks": checks, "rebuild": rebuild, "verified": verified}


def scan_leaks(
    paths: Sequence[Path],
    *,
    evidence_dir: Path,
    secrets: Sequence[str],
    addresses: Sequence[str],
) -> list[dict[str, str]]:
    """Find configured secrets and private addresses outside the evidence directory."""
    leaks: list[dict[str, str]] = []
    root = evidence_dir.resolve()
    for path in paths:
        resolved = Path(path).resolve()
        if _inside(resolved, root) or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lowered = text.lower()
        for secret in secrets:
            if isinstance(secret, str) and len(secret) >= 8 and secret in text:
                leaks.append({"kind": "secret", "path": str(resolved), "sha256": _short_hash(secret)})
        for address in addresses:
            if isinstance(address, str) and len(address) >= 8 and address.lower() in lowered:
                leaks.append({"kind": "address", "path": str(resolved), "sha256": _short_hash(address.lower())})
    return leaks


class CountingBackend:
    """Count RPC, Etherscan, news GET, and Kinza eth_call traffic for one round."""

    def __init__(self, inner: Any, *, fail_first: bool = False) -> None:
        self.inner = inner
        self.fail_first = fail_first
        self.failed = False
        self.rpc_calls: Counter[str] = Counter()
        self.etherscan_calls = 0
        self.news_gets = 0
        self.kinza_eth_calls = 0
        self.logical_methods = 0
        self.http_calls = 0
        self.credits = 0

    def send(self, call: Any) -> Any:
        self._observe(call)
        if self.fail_first and not self.failed:
            self.failed = True
            raise ConnectionError("injected source failure")
        if self.inner is None:
            from guardian.pipeline import default_backend

            self.inner = default_backend()
        return self.inner.send(call)

    def snapshot(self) -> dict[str, Any]:
        return {
            "credits": self.credits,
            "etherscan_calls": self.etherscan_calls,
            "http_calls": self.http_calls,
            "kinza_eth_calls": self.kinza_eth_calls,
            "logical_methods": self.logical_methods,
            "news_gets": self.news_gets,
            "rpc_calls_by_network": dict(sorted(self.rpc_calls.items())),
        }

    def _observe(self, call: Any) -> None:
        source_id = str(getattr(call, "source_id", "") or "")
        method = str(getattr(call, "method", "") or "").upper()
        body = getattr(call, "body", b"") or b""
        payloads: list[dict[str, Any]] = []
        if body:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, list):
                payloads = [item for item in parsed if isinstance(item, dict)]
            elif isinstance(parsed, dict):
                payloads = [parsed]
        if not payloads:
            payloads = [{}]
        self.http_calls += 1
        each = 20 if "blockscout" in source_id else 1
        self.logical_methods += len(payloads)
        self.credits += each * len(payloads)
        for payload in payloads:
            rpc_method = payload.get("method") if isinstance(payload.get("method"), str) else ""
            if "etherscan" in source_id:
                self.etherscan_calls += 1
            elif source_id.startswith("rpc-"):
                self.rpc_calls[source_id.removeprefix("rpc-")] += 1
            if method == "GET" and ("news" in source_id or getattr(call, "protocol", "") == "xml"):
                self.news_gets += 1
            if rpc_method == "eth_call":
                params = payload.get("params") if isinstance(payload.get("params"), list) else []
                target = ""
                if params and isinstance(params[0], dict) and isinstance(params[0].get("to"), str):
                    target = params[0]["to"].lower()
                if target in _KINZA:
                    self.kinza_eth_calls += 1


class _GuardedSender:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0

    def send(self, message: Mapping[str, Any]) -> None:
        self.calls += 1
        if self.inner is not None:
            self.inner.send(message)


def _capture(
    directory: Path,
    name: str,
    argv: list[str],
    *,
    backend: Any,
    sender: Any,
    clock: Callable[[], float],
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = 1
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = guardian_main(argv, backend=backend, sender=sender, clock=clock)
    except Exception as exc:  # noqa: BLE001 — a shadow command failure is evidence, not a crash
        stderr.write(type(exc).__name__)
        code = 1
    _write_text(directory / f"{name}.stdout", stdout.getvalue())
    _write_text(directory / f"{name}.stderr", stderr.getvalue())
    _write_text(directory / f"{name}.exit", str(code) + "\n")
    _write_text(directory / f"{name}.argv", " ".join(argv) + "\n")
    return code, stdout.getvalue(), stderr.getvalue()


def _register_labels(mcm_config: str) -> None:
    public = load_public_config(mcm_config)
    private = resolve_private_inputs(public, os.environ)
    database = Database(private.state_dir / "monitor.sqlite")
    try:
        database.upsert_tenant(private.accounts.tenant_id, private.accounts.tenant_id)
        for account in private.accounts.accounts:
            database.upsert_wallet_identity(
                private.accounts.tenant_id,
                account.wallet_id,
                account.family,
                account.normalized.identity_key,
                account.label,
            )
    finally:
        database.close()


def _round_record(
    *,
    mcm_config: str,
    evidence: Path,
    taken_at: str,
    moment: datetime,
    drill: str | None,
    run_code: int,
    run_out: str,
    compared: Mapping[str, Any],
    leaks: Sequence[Mapping[str, str]],
    guarded: _GuardedSender,
    injected_backend: bool,
    legacy: Mapping[str, Any],
    legacy_compare: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    public = load_public_config(mcm_config)
    private = resolve_private_inputs(public, os.environ)
    report = _object(run_out)
    interval = int(public.document["polling"]["interval_seconds"])
    finished = moment
    started = finished - timedelta(seconds=interval)
    required = report.get("required") if isinstance(report.get("required"), dict) else {}
    required_gaps, declared_gaps = _coverage_gaps(report.get("coverage"), required)
    status = report.get("status") if isinstance(report.get("status"), str) else ""
    fresh = bool(required) and all(isinstance(value, str) and value in _FRESH for value in required.values())
    if status in _BAD_STATUS or run_code != 0 or required_gaps:
        fresh = False
    markers: list[str] = []
    if injected_backend or report.get("live_pass") is True or report.get("live_verified") is True:
        markers.append("mock_fixture_pass")
    if guarded.calls:
        markers.append("shadow_sent")
    prior = _load_rounds(evidence)
    natural = [item for item in prior if not item.get("drill")]
    baseline = not drill and not natural
    scope = {
        "config_hash": hashlib.sha256(Path(mcm_config).read_bytes()).hexdigest(),
        "docs_only": False,
        "networks": sorted({network for account in private.accounts.accounts for network in account.networks}),
        "plugins": sorted(name for name in public.document.get("plugins", []) if isinstance(name, str)),
        "required": sorted(str(key) for key in required),
        "content_hash": content_hash(Path(__file__).resolve().parents[1]),
        "wallets": sorted(account.wallet_id for account in private.accounts.accounts),
    }
    record = {
        "baseline": baseline,
        "command_status": status or "missing",
        "declared_gaps": sorted(set(declared_gaps)),
        "dns_requirement": "real_dns_host",
        "drill": drill,
        "duplicates": _duplicate_alerts(private.state_dir / "monitor.sqlite", private.accounts.tenant_id),
        "fake_ip_reported_as": ["fetch_error", "private_endpoint"],
        "finished_at": _iso(finished),
        "fixture": injected_backend,
        "index": 0 if baseline else len(natural),
        "live_pass": False,
        "live_verified": False,
        "markers": markers,
        "no_send": guarded.calls == 0,
        "required_fresh": fresh,
        "taken_at": taken_at,
        "required_gaps": sorted(set(required_gaps)),
        "reverse_shadow": False,
        "round_id": "baseline" if baseline else f"round-{len(prior) + 1}",
        "scope": scope,
        "secret_leaks": len(leaks),
        "sender_calls": guarded.calls,
        "started_at": _iso(started),
        "unexplained_legacy_only": _unexplained(compared.get("legacy_only")),
        "unexplained_mcm_only": _unexplained(compared.get("mcm_only")),
        "watermark": legacy.get("delivery_watermark") if isinstance(legacy.get("delivery_watermark"), str) else "",
    }
    if legacy_compare is not None:
        valid = legacy_compare.get("compare_valid") is True
        reason = legacy_compare.get("compare_reason")
        record["compare_valid"] = valid
        record["compare_reason"] = "" if valid else reason if isinstance(reason, str) and reason else "legacy_evidence_missing"
    return record


def _coverage_gaps(coverage: Any, required: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    required_gaps: list[str] = []
    declared: list[str] = []
    if not isinstance(coverage, list):
        return required_gaps, declared
    for row in coverage:
        if not isinstance(row, dict):
            continue
        codes = row.get("reason_codes") if isinstance(row.get("reason_codes"), list) else []
        hits = [token for token in GAP_TOKENS if token in codes]
        if not hits:
            continue
        key = "|".join(str(row.get(part) or "") for part in ("wallet_id", "network", "capability"))
        bucket = required_gaps if key in required else declared
        bucket.extend(hits)
    return required_gaps, declared


def _relay(evidence: Path, compared: Mapping[str, Any]) -> None:
    rows = compared.get("mcm_only") if isinstance(compared.get("mcm_only"), list) else []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in _CRITICAL:
            continue
        if row.get("classification") == "explained":
            continue
        parts = (
            str(row.get("wallet_id") or ""),
            str(row.get("network") or ""),
            str(row.get("tx") or ""),
            str(row.get("kind") or ""),
            str(row.get("subject") or ""),
        )
        dedup = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        path = evidence / "relay" / f"{dedup}.json"
        if path.exists():
            continue
        _write_json(path, {
            "dedup_id": dedup,
            "kind": parts[3],
            "network": parts[1],
            "origin": "mcm_shadow",
            "tx": parts[2],
            "wallet_id": parts[0],
        })


def _restart_proof(legacy: Mapping[str, Any]) -> dict[str, Any]:
    fixture = legacy.get("restart_fixture") if isinstance(legacy.get("restart_fixture"), dict) else {}
    events = fixture.get("events") if isinstance(fixture.get("events"), list) else []
    committed = fixture.get("committed_height")
    partial = fixture.get("partial_height")
    if type(committed) is not int or type(partial) is not int:
        committed, partial = 0, 0
    resumed = resume_after_restart(committed_height=committed, partial_height=partial, events=events)
    return {"counts_toward_24": False, "drill": "restart", "resumed": resumed}


def _append_round(evidence: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
    rounds = _load_rounds(evidence)
    rounds.append(record)
    lines = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rounds)
    _write_text(evidence / "rounds.jsonl", lines)
    return rounds


def _inventory(mcm_config: str) -> dict[str, set[str] | set[int]] | None:
    """Ids already stored before this round. A missing database is an empty set."""
    try:
        public = load_public_config(mcm_config)
        private = resolve_private_inputs(public, os.environ)
        path = private.state_dir / "monitor.sqlite"
        tenant = private.accounts.tenant_id
    except ConfigError:
        raise
    except (OSError, sqlite3.Error, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not path.is_file():
        return {"events": set(), "outbox": set()}
    database = Database(path)
    try:
        event_ids = {
            str(row["identity"])
            for row in database.conn.execute(
                "SELECT identity FROM chain_events WHERE tenant_id=?",
                (tenant,),
            )
            if isinstance(row["identity"], str) and row["identity"]
        }
        outbox_ids = {
            int(row["id"])
            for row in database.conn.execute(
                "SELECT id FROM outbox WHERE tenant_id=?",
                (tenant,),
            )
        }
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        database.close()
    return {"events": event_ids, "outbox": outbox_ids}


def _round_scope(
    mcm_config: str,
    before: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """Chain events and outbox rows written during this round.

    If the prior inventory is missing, both results are null. They are not
    reported as zero.
    """
    if before is None:
        return None, None
    prior_events = before.get("events")
    prior_outbox = before.get("outbox")
    if not isinstance(prior_events, set) or not isinstance(prior_outbox, set):
        return None, None
    try:
        public = load_public_config(mcm_config)
        private = resolve_private_inputs(public, os.environ)
        path = private.state_dir / "monitor.sqlite"
        tenant = private.accounts.tenant_id
    except ConfigError:
        raise
    except (OSError, sqlite3.Error, UnicodeError, json.JSONDecodeError, ValueError):
        return None, None
    if not path.is_file():
        return [], []
    database = Database(path)
    try:
        events: list[dict[str, Any]] = []
        for row in database.conn.execute(
            "SELECT identity, payload FROM chain_events WHERE tenant_id=?",
            (tenant,),
        ):
            identity = row["identity"]
            if not isinstance(identity, str) or not identity:
                return None, None
            if identity in prior_events:
                continue
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                return None, None
            if not isinstance(payload, dict):
                return None, None
            item = dict(payload)
            item["identity"] = identity
            events.append(item)
        outbox_rows = [
            row for row in database.outbox_for(tenant)
            if isinstance(row.get("id"), int) and row["id"] not in prior_outbox
        ]
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None, None
    finally:
        database.close()
    return events, outbox_rows


def _load_rounds(evidence: Path) -> list[dict[str, Any]]:
    path = evidence / "rounds.jsonl"
    if not path.is_file():
        return []
    rounds: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rounds.append(item)
    return rounds


def _leak_scan(mcm_config: str, evidence: Path) -> list[dict[str, str]]:
    public = load_public_config(mcm_config)
    private = resolve_private_inputs(public, os.environ)
    secrets = _secret_values(public, os.environ)
    addresses = [account.normalized.display for account in private.accounts.accounts]
    return scan_leaks([Path(mcm_config)], evidence_dir=evidence, secrets=secrets, addresses=addresses)


def _secret_values(public: Any, environ: Mapping[str, str]) -> list[str]:
    name = public.document.get("private_inputs", {}).get("secret_profile_file_env")
    raw = environ.get(name) if isinstance(name, str) else None
    if not isinstance(raw, str) or not raw:
        return []
    try:
        document = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(document, dict):
        return []
    return [value for value in document.values() if isinstance(value, str)]


def _duplicate_alerts(database_path: Path, tenant: str) -> int:
    if not database_path.is_file():
        return 0
    database = Database(database_path)
    try:
        keys = []
        for row in database.outbox_for(tenant):
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            key = payload.get("dedup_key")
            if isinstance(key, str):
                keys.append(key)
    finally:
        database.close()
    return len(keys) - len(set(keys))


def _block_hash(backend: Any, height: int) -> str | None:
    body = json.dumps({
        "id": 1,
        "jsonrpc": "2.0",
        "method": "eth_getBlockByNumber",
        "params": [hex(int(height)), False],
    }).encode("utf-8")
    call = PreparedCall("rpc-bsc-mainnet", "POST", "https://bsc.example.invalid/rpc", body, "json-rpc")
    try:
        raw = backend.send(call)
    except Exception:  # noqa: BLE001 — an unreadable block is an unverified cursor
        return None
    try:
        payload = json.loads(raw.body.decode("utf-8"))
    except (AttributeError, UnicodeError, json.JSONDecodeError):
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict) and isinstance(result.get("hash"), str):
        return result["hash"]
    return None


def _events(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _unexplained(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return sum(1 for row in value if isinstance(row, dict) and row.get("classification") != "explained")


def _object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lf_bytes(data: bytes) -> bytes:
    """CRLF and lone CR become LF so Windows and Linux hash the same checkout."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def content_hash(root: Path) -> str:
    """Scope identity for the code and the config the monitor actually runs.

    The hash covers ``src/guardian``, including packaged resources under
    ``src/guardian/_resources``, with CRLF normalized to LF. Docs, tests, and
    tools stay outside it. A checkout that has no packaged config tree still
    hashes top-level ``config/`` so an unpackaged tree keeps the same rule.
    """
    digest = hashlib.sha256()
    packaged_config = root / "src" / "guardian" / "_resources" / "config"
    tops = ["src/guardian"]
    if not packaged_config.is_dir():
        tops.append("config")
    for top in tops:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            digest.update(f"{top}/{path.relative_to(base).as_posix()}".encode("utf-8"))
            digest.update(b"\0")
            digest.update(_lf_bytes(path.read_bytes()))
            digest.update(b"\0")
    return digest.hexdigest()


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(path, 0o600)


if __name__ == "__main__":
    raise SystemExit(main())
