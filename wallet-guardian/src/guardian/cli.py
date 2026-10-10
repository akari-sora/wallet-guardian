"""Command line entry. Every command prints JSON and a non-zero exit on failure."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from guardian.actions.policy import agent_wallet_request, parse_intent
from guardian.actions.workbench import workbench_status
from guardian.comparison import compare_with_legacy
from guardian.legacy_capture import (
    begin_capture,
    finish_capture,
    record_capture,
    scope_hash_for_accounts,
)
from guardian.configuration import load_public_config, resolve_private_inputs, safe_export
from guardian.domain import format_alert
from guardian.errors import GuardianError
from guardian.pipeline import default_backend, probe_sources, run_capability_pipeline, stored_coverage
from guardian.scheduler import Watchdog, monitor_plan
from guardian.security.redact import redact
from guardian.sources.key_guide import hint_for_source
from guardian.service import ROOT, doctor, load_config, load_local_env, operation_coverage, rescue_doctor, run_once, self_check
from guardian.storage import Database
from guardian.read_views import latest_items, resolve_tenant


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _db(args: argparse.Namespace) -> Database:
    path = Path(args.db) if args.db else ROOT / "data" / "guardian.sqlite"
    return Database(path)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="sqlite path")
    common.add_argument("--tenant", default=argparse.SUPPRESS, help="local tenant scope (required when ambiguous)")
    common.add_argument("--json", action="store_true", help="print JSON (this is always on)")
    parser = argparse.ArgumentParser(prog="guardian")
    parser.set_defaults(db=None)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_cmd = sub.add_parser("doctor", parents=[common])
    doctor_cmd.add_argument("--mcm-config", default=None)
    doctor_cmd.add_argument(
        "--live",
        action="store_true",
        help="live probe with a 5 second whole-call deadline per probe; omitted doctor stays offline",
    )
    run = sub.add_parser("run-once", parents=[common])
    selected = run.add_mutually_exclusive_group(required=True)
    selected.add_argument("--config")
    selected.add_argument("--mcm-config")
    run.add_argument("--no-send", action="store_true")
    run.add_argument("--news-input", default=None, help="caller-supplied RSS, Atom, or structured news; not fetched")
    run.add_argument("--price-input", default=None, help="caller-supplied price quotes; not fetched")

    sub.add_parser("snapshot", parents=[common])
    sub.add_parser("reconcile", parents=[common])
    sub.add_parser("claims", parents=[common])
    providers = sub.add_parser("providers", parents=[common])
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)
    providers_sub.add_parser(
        "verify",
        parents=[common],
        help="legacy provider probe; does not prove a selected profile's connectivity",
    )

    wallets = sub.add_parser("wallets", parents=[common])
    wallets_sub = wallets.add_subparsers(dest="wallets_command", required=True)
    discover = wallets_sub.add_parser("discover", parents=[common])
    discover.add_argument("--config", required=True)

    sub.add_parser("approvals", parents=[common])
    explain = sub.add_parser("explain", parents=[common])
    explain.add_argument("--event-id", required=True)
    coverage = sub.add_parser("coverage", parents=[common])
    coverage.add_argument("--mcm-config", default=None)
    replay = sub.add_parser("replay", parents=[common])
    replay.add_argument("--fixture", required=True)
    replay.add_argument("--no-send", action="store_true")
    alerts = sub.add_parser("alerts", parents=[common])
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)
    dry = alerts_sub.add_parser("test", parents=[common])
    dry.add_argument("--dry-run", action="store_true", required=True)
    sub.add_parser("self-check", parents=[common])

    diagnose = sub.add_parser("diagnose", parents=[common])
    diagnose.add_argument("--task", required=True)
    actions = sub.add_parser("actions", parents=[common])
    actions_sub = actions.add_subparsers(dest="actions_command", required=True)
    listed = actions_sub.add_parser("list", parents=[common])
    listed.add_argument("--wallet", required=True)

    action = sub.add_parser("action", parents=[common])
    action_sub = action.add_subparsers(dest="action_command", required=True)
    plan = action_sub.add_parser("plan", parents=[common])
    plan.add_argument("--intent-file", required=True)
    simulate = action_sub.add_parser("simulate", parents=[common])
    simulate.add_argument("--plan-id", required=True)
    export = action_sub.add_parser("export", parents=[common])
    export.add_argument("--plan-id", required=True)
    export.add_argument("--unsigned-only", action="store_true", required=True)
    status = action_sub.add_parser("status", parents=[common])
    status.add_argument("--task-id", required=True)
    reconcile = action_sub.add_parser("reconcile", parents=[common])
    reconcile.add_argument("--task-id", required=True)

    sub.add_parser("rescue", parents=[common])
    sub.add_parser("operation-coverage", parents=[common])
    sub.add_parser("workbench", parents=[common])

    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    validate = config_sub.add_parser("validate", parents=[common])
    validate.add_argument("--mcm-config", required=True)

    sources = sub.add_parser("sources")
    sources_sub = sources.add_subparsers(dest="sources_command", required=True)
    probe = sources_sub.add_parser("probe", parents=[common])
    probe.add_argument("--mcm-config", required=True)

    exported = sub.add_parser("export", parents=[common])
    exported.add_argument("--public", action="store_true")
    exported.add_argument("--mcm-config", required=True)

    compared = sub.add_parser("compare-legacy", parents=[common])
    compared.add_argument("--mcm-config", required=True)
    compared.add_argument("--legacy-state", required=True)
    compared.add_argument("--since", default=None, help="ISO window start")
    compared.add_argument("--until", default=None, help="ISO window end")
    compared.add_argument("--legacy-runs-dir", default=None)
    compared.add_argument("--slot-start", default=None)
    compared.add_argument("--slot-end", default=None)
    compared.add_argument("--legacy-max-age-seconds", type=int, default=12600)

    news = sub.add_parser("news")
    news_sub = news.add_subparsers(dest="news_command", required=True)
    health = news_sub.add_parser("health", parents=[common])
    health.add_argument("--mcm-config", required=True)
    health.add_argument("--offline", action="store_true", help="read stored feed health and do not fetch")

    capture = sub.add_parser("legacy-capture")
    capture_sub = capture.add_subparsers(dest="capture_command", required=True)
    begin = capture_sub.add_parser("begin")
    begin.add_argument("--runs-dir", required=True)
    begin.add_argument("--run-id", required=True)
    begin.add_argument("--legacy-state", required=True)
    begin.add_argument("--tool-name", required=True)
    begin.add_argument("--tool-version", required=True)
    begin.add_argument("--tool-hash", required=True)
    begin.add_argument("--host-id", required=True)
    begin.add_argument("--scope-hash", required=True)
    begin.add_argument("--account", action="append", required=True, help="label=<sha256 of the identity key>")
    begin.add_argument("--started-at", required=True)
    recorded = capture_sub.add_parser("record")
    recorded.add_argument("--runs-dir", required=True)
    recorded.add_argument("--run-id", required=True)
    recorded.add_argument("--window-json", action="append", default=[])
    recorded.add_argument("--event-json", action="append", default=[])
    finished = capture_sub.add_parser("finish")
    finished.add_argument("--runs-dir", required=True)
    finished.add_argument("--run-id", required=True)
    finished.add_argument("--legacy-state", required=True)
    finished.add_argument("--exit-code", type=int, required=True)
    finished.add_argument("--finished-at", required=True)

    watchdog = sub.add_parser("watchdog", parents=[common])
    watchdog.add_argument("--mcm-config", required=True)

    monitor = sub.add_parser("monitor")
    monitor_sub = monitor.add_subparsers(dest="monitor_command", required=True)
    plan = monitor_sub.add_parser("plan", parents=[common])
    plan.add_argument("--mcm-config", required=True)

    egress = sub.add_parser("egress-doctor", parents=[common])
    egress.add_argument("--mcm-config", default=None)
    egress.add_argument("--egress-config", default=None)
    egress.add_argument("--qualification", choices=("formal", "preview"), default="formal")
    egress.add_argument("--mode", choices=("system", "doh"), default=None)
    init = sub.add_parser("init", parents=[common], help="write a read-only monitor profile")
    init.add_argument("--dir", dest="directory", default=None, help="private directory (default: XDG config dir)")
    init.add_argument("--from", dest="answers_from", default=None, help="non-interactive answers JSON")
    init.add_argument("--yes", action="store_true", help="record data-sharing consent and write files")
    init.add_argument("--force", action="store_true", help="back up existing files first; does not touch the state database")

    service = sub.add_parser(
        "install-service",
        parents=[common],
        help="print a dry-run install plan; units stay disabled without --apply",
    )
    service.add_argument("--dry-run", action="store_true", help="print the plan and do not enable units")
    service.add_argument("--apply", action="store_true", help="install rendered units; refused when omitted")
    service.add_argument("--mcm-config", default=None)
    service.add_argument("--output", default=None, help="write rendered units only under this directory")
    service.add_argument("--executable", default=None, help="absolute path of the installed guardian executable")
    service.add_argument("--config-dir", default=None)
    service.add_argument("--state-dir", default=None)
    service.add_argument("--user", default="guardian-monitor")
    service.add_argument("--offset-minutes", type=int, default=5)
    service.add_argument("--legacy-executable", default=None)
    return parser


def dispatch(
    args: argparse.Namespace,
    *,
    backend: Any = None,
    sender: Any = None,
    clock: Callable[[], float] | None = None,
    egress_hooks: Any = None,
    notification_backend: Any = None,
    notification_resolve: Any = None,
    service_runner: Any = None,
) -> tuple[dict, int]:
    if args.command == "egress-doctor":
        return _egress_doctor(args, egress_hooks)
    if args.command == "doctor":
        return doctor(live=bool(getattr(args, "live", False)), mcm_config=getattr(args, "mcm_config", None)), 0
    if args.command == "run-once":
        if getattr(args, "mcm_config", None):
            return _mcm_run(args, backend, sender, clock, notification_backend, notification_resolve)
        result = run_once(Path(args.config), Path(args.db) if args.db else None)
        return result, 0 if result["status"] != "degraded" else 3
    if args.command == "self-check":
        result = self_check(Path(args.db) if args.db else None)
        return result, 0 if result["status"] == "pass" else 1
    if args.command == "operation-coverage":
        return operation_coverage(), 0
    if args.command == "workbench":
        return workbench_status(), 0
    if args.command == "rescue":
        return rescue_doctor(), 0
    if args.command == "coverage":
        if getattr(args, "mcm_config", None):
            return _mcm_coverage(args)
        return {
            "monitoring": "PARTIAL",
            "chains_enabled": [56],
            "protocols_read": ["lista-slisbnb", "hyperliquid-info"],
            "discovery": "LIMITED_DISCOVERY",
            "operation": operation_coverage(),
        }, 0
    if args.command == "providers":
        report = doctor(live=True, legacy_probe=True)
        return {
            "probe_kind": "legacy_provider_probe",
            "proves_selected_profile_connectivity": False,
            "note": "legacy probe; does not prove the selected profile's connectivity",
            "providers": report["providers"],
        }, 0
    if args.command == "wallets":
        result = run_once(Path(args.config), Path(args.db) if args.db else None)
        return result, 3 if result["status"] == "degraded" else 0
    if args.command in {"snapshot", "reconcile", "claims", "approvals", "actions"}:
        return _stored(args)
    if args.command == "explain":
        return _explain(args)
    if args.command == "diagnose":
        return _task_view(args, args.task)
    if args.command == "alerts":
        sample = {
            "severity": "INFO",
            "severity_label": "資訊",
            "confidence": "測試",
            "wallet_label": "範例",
            "chain_label": "BNB Smart Chain",
            "subject": "監控自我檢查",
            "what": "這是乾跑訊息，沒有送出通知。",
            "amount_display": "0",
            "unit": "BNB",
            "price": None,
            "price_time": None,
            "why": "用來確認告警格式。",
            "step": "不需要操作。",
            "event_time": "未知",
            "observed_at": "未知",
            "source": "dry-run",
            "limits": "不是鏈上事件。",
        }
        return {"delivered": False, "text": format_alert(sample)}, 0
    if args.command == "replay":
        if not args.no_send:
            raise GuardianError("replay requires --no-send")
        fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        if fixture.get("method") in {"eth_sendTransaction", "eth_sendRawTransaction"}:
            agent_wallet_request(fixture["method"])
        return {"status": "replayed", "sent": False, "quality": fixture.get("quality", "UNKNOWN")}, 0
    if args.command == "action":
        return _action(args)
    if args.command == "config":
        return _mcm_validate(args)
    if args.command == "sources":
        return _mcm_probe(args, backend)
    if args.command == "export":
        return _mcm_export(args)
    if args.command == "compare-legacy":
        return _mcm_compare(args)
    if args.command == "news":
        if getattr(args, "news_command", None) != "health":
            raise GuardianError("unknown news command")
        return _mcm_news_health(args)
    if args.command == "legacy-capture":
        return _legacy_capture(args)
    if args.command == "watchdog":
        return _mcm_watchdog(args, clock)
    if args.command == "monitor":
        return _mcm_plan(args)
    if args.command == "init":
        from guardian.onboarding import execute_init

        return execute_init(
            directory=getattr(args, "directory", None),
            answers_path=getattr(args, "answers_from", None),
            yes=bool(getattr(args, "yes", False)),
            force=bool(getattr(args, "force", False)),
            environ=os.environ,
        )
    if args.command == "install-service":
        from guardian.service_install import execute_install_service

        apply = bool(getattr(args, "apply", False)) and not bool(getattr(args, "dry_run", False))
        payload = execute_install_service(
            dry_run=not apply,
            apply=apply,
            mcm_config=getattr(args, "mcm_config", None),
            output_dir=getattr(args, "output", None),
            executable=getattr(args, "executable", None),
            config_directory=getattr(args, "config_dir", None),
            state_directory=getattr(args, "state_dir", None),
            user=getattr(args, "user", None) or "guardian-monitor",
            offset_minutes=int(getattr(args, "offset_minutes", 5)),
            legacy_executable=getattr(args, "legacy_executable", None),
            runner=service_runner,
        )
        return payload, 0 if payload.get("installed") else 1
    raise GuardianError("unknown command")


def _stored(args: argparse.Namespace) -> tuple[dict, int]:
    if not args.db and not (ROOT / "data" / "guardian.sqlite").exists():
        return {"status": "empty", "reason": "no local snapshot yet"}, 0
    database = _db(args)
    try:
        if args.command == "approvals":
            return {
                "status": "PARTIAL",
                "reason": "allowance history has not been indexed; coverage is partial",
                "items": [],
            }, 0
        tenant = resolve_tenant(database, getattr(args, "tenant", None))
        items = latest_items(database, tenant, getattr(args, "wallet", None))
        if args.command == "claims" or (args.command == "actions" and args.actions_command == "list"):
            claims = [item for item in items if ":lista:" in item.get("position_id", "")]
            return {"status": "ok", "claims": claims, "wallet_request_allowed": False}, 0
        if args.command == "reconcile":
            return {"status": "ok", "items": len(items), "note": "latest stored generation only; no live reconciliation was performed"}, 0
        return {"status": "ok", "items": items}, 0
    finally:
        database.close()


def _explain(args: argparse.Namespace) -> tuple[dict, int]:
    path = Path(args.db) if args.db else ROOT / "data" / "guardian.sqlite"
    if not path.exists():
        return {"status": "not_found", "event_id": args.event_id}, 0
    database = Database(path)
    try:
        tenant = resolve_tenant(database, getattr(args, "tenant", None))
        rows = database.alerts_for(tenant) if tenant is not None else []
    finally:
        database.close()
    for payload in rows:
        if payload.get("dedup_key") == args.event_id:
            from guardian.security.display_text import render_alert

            rendered = render_alert(payload)
            return {
                "status": "ok",
                "alert": payload,
                "csv": rendered["csv"],
                "html": rendered["html"],
                "markdown": rendered["markdown"],
                "text": rendered["plain"],
            }, 0
    return {"status": "not_found", "event_id": args.event_id}, 0


def _task_view(args: argparse.Namespace, task_id: str) -> tuple[dict, int]:
    path = Path(args.db) if args.db else ROOT / "data" / "guardian.sqlite"
    if not path.exists():
        return {"status": "not_found", "task_id": task_id}, 0
    database = Database(path)
    try:
        tenant = resolve_tenant(database, getattr(args, "tenant", None))
        task = database.get_task(tenant, task_id) if tenant is not None else None
    finally:
        database.close()
    if not task:
        return {"status": "not_found", "task_id": task_id}, 0
    return {"status": "ok", "tenant_id": tenant, "task": task, "wallet_request_allowed": False}, 0


def _action(args: argparse.Namespace) -> tuple[dict, int]:
    if args.action_command == "plan":
        intent = parse_intent(json.loads(Path(args.intent_file).read_text(encoding="utf-8")))
        return {
            "status": "BLOCKED",
            "intent": intent,
            "reason": "live planning must go through run-once so the idx is reread; this command refuses raw calldata",
            "wallet_request_allowed": False,
        }, 0
    if args.action_command == "simulate":
        found = _task_view(args, args.plan_id)[0]
        if found["status"] != "ok":
            return found, 0
        simulation = found["task"].get("simulation", {})
        return {
            "status": simulation.get("status", "INCOMPLETE"),
            "usable_as_live_evidence": False,
            "reason": "eth_call success is not a balance or event trace",
            "wallet_request_allowed": False,
        }, 0
    if args.action_command == "export":
        found = _task_view(args, args.plan_id)[0]
        if found["status"] != "ok":
            return found, 0
        task = dict(found["task"])
        task.pop("signature", None)
        task["unsigned_only"] = True
        task["wallet_request_allowed"] = False
        return task, 0
    if args.action_command in {"status", "reconcile"}:
        found = _task_view(args, args.task_id)[0]
        found["reconciled_onchain"] = False
        found["reason"] = "postconditions are not marked complete without a user-submitted receipt"
        return found, 0
    raise GuardianError("unknown action command")


def _egress_doctor(args: argparse.Namespace, hooks: Any) -> tuple[dict, int]:
    """Host diagnosis only. Accounts and secret profiles are not opened."""
    from guardian.sources.resolver import diagnose_egress

    document = None
    if getattr(args, "mcm_config", None):
        document = load_public_config(args.mcm_config).document
    return diagnose_egress(
        document=document,
        environ=os.environ,
        egress_config=getattr(args, "egress_config", None),
        qualification=args.qualification,
        mode=args.mode,
        hooks=hooks,
    )


_ENV_PATH_KEYS = (
    "MONITOR_ACCOUNTS_FILE",
    "MONITOR_DELIVERY_PROFILE",
    "MONITOR_SECRET_PROFILE",
    "MONITOR_STATE_DIR",
)


def _env_with_sibling_paths(path: str) -> dict[str, str]:
    """Process env plus the path variables `guardian init` wrote next to the monitor file.

    A scheduled job has no shell profile. `env-paths.json` holds only paths, never secrets. The process
    environment wins, and only the four path variables are taken from the file.
    """
    env = dict(os.environ)
    sibling = Path(path).expanduser().resolve().parent / "env-paths.json"
    try:
        loaded = json.loads(sibling.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return env
    if isinstance(loaded, dict):
        for key in _ENV_PATH_KEYS:
            value = loaded.get(key)
            if key not in env and isinstance(value, str) and value:
                env[key] = value
    return env


def _open_mcm(path: str):
    public = load_public_config(path)
    private = resolve_private_inputs(public, _env_with_sibling_paths(path))
    database = Database(private.state_dir / "monitor.sqlite")
    return public, private, database


def _validate_hint(public, name: str, blocked_reason: str | None) -> str | None:
    source = public.document["sources"].get(name, {})
    if not isinstance(source, dict):
        return None
    return hint_for_source(source, blocked_reason)


def _mcm_validate(args: argparse.Namespace) -> tuple[dict, int]:
    public, private, database = _open_mcm(args.mcm_config)
    database.close()
    sources = []
    for name in sorted(private.source_status):
        status = private.source_status[name]
        row = {
            "availability": status.availability,
            "blocked_reason": status.blocked_reason,
            "credential_present": status.credential_present,
            "enabled": status.enabled,
            "id": name,
        }
        hint = _validate_hint(public, name, status.blocked_reason)
        if hint:
            row["credential_hint"] = hint
        sources.append(row)
    return {
        "live_verified": False,
        "notes": list(private.notes),
        "sources": sources,
        "status": "ok",
        "wallets": len(private.accounts.accounts),
    }, 0


def _mcm_probe(args: argparse.Namespace, backend: Any) -> tuple[dict, int]:
    public, private, database = _open_mcm(args.mcm_config)
    try:
        return probe_sources(public, private, backend=_chosen_backend(backend), clock=time.time), 0
    finally:
        database.close()


def _mcm_run(
    args: argparse.Namespace,
    backend: Any,
    sender: Any,
    clock: Callable[[], float] | None,
    notification_backend: Any = None,
    notification_resolve: Any = None,
) -> tuple[dict, int]:
    public, private, database = _open_mcm(args.mcm_config)
    try:
        report = run_capability_pipeline(
            public,
            private.accounts,
            private,
            database,
            backend=_chosen_backend(backend),
            clock=time.time if clock is None else clock,
            no_send=bool(args.no_send),
            sender=sender,
            news_input=_optional_bytes(getattr(args, "news_input", None)),
            price_input=_optional_bytes(getattr(args, "price_input", None)),
            notification_backend=notification_backend,
            notification_resolve=notification_resolve,
        )
    finally:
        database.close()
    code = 2 if report.misconfigured else 1 if report.status == "failed" else 0
    return report.as_dict(), code


def _mcm_coverage(args: argparse.Namespace) -> tuple[dict, int]:
    from guardian.diagnostics import diagnostic_fields

    public, private, database = _open_mcm(args.mcm_config)
    try:
        rows = stored_coverage(database, private.accounts.tenant_id)
    finally:
        database.close()
    fields = diagnostic_fields(public.document, os.environ)
    return {
        "live_verified": False,
        "rows": rows,
        "status": "ok",
        "credentials": fields["credentials"],
        "resolver": fields["resolver"],
        "connectivity": fields["connectivity"],
        "egress_qualification": fields["egress_qualification"],
    }, 0


def _mcm_export(args: argparse.Namespace) -> tuple[dict, int]:
    if not args.public:
        raise GuardianError("export requires --public")
    public, private, database = _open_mcm(args.mcm_config)
    database.close()
    return safe_export(public, private.accounts), 0


def _mcm_news_health(args: argparse.Namespace) -> tuple[dict, int]:
    """Read stored feed health. This command does not fetch, even without --offline."""
    from guardian.news.health import stored_feed_health

    _public, private, database = _open_mcm(args.mcm_config)
    try:
        feeds = stored_feed_health(database, private.accounts.tenant_id)
    finally:
        database.close()
    return {
        "feeds": feeds,
        "live_pass": False,
        "live_verified": False,
        "offline": True,
        "status": "ok",
    }, 0


def _mcm_compare(args: argparse.Namespace) -> tuple[dict, int]:
    _public, private, database = _open_mcm(args.mcm_config)
    try:
        runs = getattr(args, "legacy_runs_dir", None)
        return compare_with_legacy(
            args.legacy_state,
            database,
            private.accounts.tenant_id,
            since=getattr(args, "since", None),
            until=getattr(args, "until", None),
            legacy_runs_dir=runs,
            legacy_scope_hash=None if not runs else scope_hash_for_accounts(private.accounts),
            slot_start=getattr(args, "slot_start", None),
            slot_end=getattr(args, "slot_end", None),
            legacy_max_age_seconds=getattr(args, "legacy_max_age_seconds", 12600),
        ), 0
    finally:
        database.close()


def _legacy_capture(args: argparse.Namespace) -> tuple[dict, int]:
    command = args.capture_command
    if command == "begin":
        return begin_capture(
            args.runs_dir,
            run_id=args.run_id,
            legacy_state=args.legacy_state,
            tool_name=args.tool_name,
            tool_version=args.tool_version,
            tool_hash=args.tool_hash,
            host_id=args.host_id,
            scope_hash=args.scope_hash,
            accounts=[_capture_account(item) for item in args.account],
            started_at=args.started_at,
        ), 0
    if command == "record":
        return record_capture(
            args.runs_dir,
            run_id=args.run_id,
            windows=[_capture_json(item, "query window") for item in args.window_json],
            events=[_capture_json(item, "sidecar event") for item in args.event_json],
        ), 0
    if command == "finish":
        return finish_capture(
            args.runs_dir,
            run_id=args.run_id,
            legacy_state=args.legacy_state,
            exit_code=args.exit_code,
            finished_at=args.finished_at,
        ), 0
    raise GuardianError("unknown command")


def _capture_account(value: str) -> dict[str, str]:
    label, sep, digest = value.partition("=")
    if not sep:
        raise GuardianError("account must be label=<sha256>")
    return {"address_hash": digest, "label": label}


def _capture_json(value: str, label: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GuardianError(f"{label} is not valid json") from exc
    if not isinstance(parsed, dict):
        raise GuardianError(f"{label} must be an object")
    return parsed


def _mcm_watchdog(args: argparse.Namespace, clock: Callable[[], float] | None) -> tuple[dict, int]:
    public, private, database = _open_mcm(args.mcm_config)
    try:
        record = database.latest_run(private.accounts.tenant_id)
        loaded = _loaded_run(record)
        report = Watchdog(database, private.accounts.tenant_id, public).evaluate(loaded, _now(clock))
    finally:
        database.close()
    report["live_verified"] = False
    report["live_pass"] = False
    return report, 0


def _loaded_run(record: dict[str, Any] | None) -> SimpleNamespace | None:
    """The persisted run, or None when this monitor has not finished one."""
    if not isinstance(record, dict):
        return None
    required = record.get("required")
    return SimpleNamespace(
        finished=record.get("finished") if isinstance(record.get("finished"), str) else "",
        required=required if isinstance(required, dict) else {},
        run_id=record.get("run_id") if isinstance(record.get("run_id"), str) else "",
        started=record.get("started") if isinstance(record.get("started"), str) else "",
        status=record.get("status") if isinstance(record.get("status"), str) else "unknown",
    )


def _mcm_plan(args: argparse.Namespace) -> tuple[dict, int]:
    public, private, database = _open_mcm(args.mcm_config)
    database.close()
    plan = monitor_plan(public, private)
    plan["live_verified"] = False
    return plan, 0


def _optional_bytes(path: str | None) -> bytes | None:
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise GuardianError("input file is not readable") from exc


def _chosen_backend(backend: Any) -> Any:
    return default_backend() if backend is None else backend


def _now(clock: Callable[[], float] | None = None) -> str:
    moment = time.time() if clock is None else clock()
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(
    argv: list[str] | None = None,
    *,
    backend: Any = None,
    sender: Any = None,
    clock: Callable[[], float] | None = None,
    egress_hooks: Any = None,
    notification_backend: Any = None,
    notification_resolve: Any = None,
    service_runner: Any = None,
) -> int:
    load_local_env()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if getattr(args, "mcm_config", None):
            # Scheduled jobs have no shell profile: fill only the unset path variables from env-paths.json.
            for key, value in _env_with_sibling_paths(args.mcm_config).items():
                if key in _ENV_PATH_KEYS:
                    os.environ.setdefault(key, value)
        # Touch config validation early for discover/run so a bad address fails closed.
        if args.command in {"run-once", "wallets"} and getattr(args, "config", None):
            load_config(Path(args.config))
        payload, code = dispatch(
            args,
            backend=backend,
            sender=sender,
            clock=clock,
            notification_backend=notification_backend,
            notification_resolve=notification_resolve,
            egress_hooks=egress_hooks,
            service_runner=service_runner,
        )
    except GuardianError as exc:
        payload = {"status": "error", "error_type": type(exc).__name__, "error": redact(str(exc))}
        code = exc.exit_code
    except Exception as exc:  # noqa: BLE001
        payload = {"status": "error", "error_type": type(exc).__name__, "error": redact(str(exc))}
        code = 1
    _print(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
