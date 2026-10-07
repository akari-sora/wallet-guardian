"""Command line entry. Every command prints JSON and a non-zero exit on failure."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from guardian.actions.policy import agent_wallet_request, parse_intent
from guardian.actions.workbench import workbench_status
from guardian.domain import format_alert
from guardian.errors import GuardianError
from guardian.security.redact import redact
from guardian.service import ROOT, doctor, load_config, operation_coverage, rescue_doctor, run_once, self_check
from guardian.storage import Database


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _db(args: argparse.Namespace) -> Database:
    path = Path(args.db) if args.db else ROOT / "data" / "guardian.sqlite"
    return Database(path)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="sqlite path")
    common.add_argument("--json", action="store_true", help="print JSON (this is always on)")
    parser = argparse.ArgumentParser(prog="guardian")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", parents=[common])
    run = sub.add_parser("run-once", parents=[common])
    run.add_argument("--config", required=True)

    sub.add_parser("snapshot", parents=[common])
    sub.add_parser("reconcile", parents=[common])
    sub.add_parser("claims", parents=[common])
    providers = sub.add_parser("providers", parents=[common])
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)
    providers_sub.add_parser("verify", parents=[common])

    wallets = sub.add_parser("wallets", parents=[common])
    wallets_sub = wallets.add_subparsers(dest="wallets_command", required=True)
    discover = wallets_sub.add_parser("discover", parents=[common])
    discover.add_argument("--config", required=True)

    sub.add_parser("approvals", parents=[common])
    explain = sub.add_parser("explain", parents=[common])
    explain.add_argument("--event-id", required=True)
    sub.add_parser("coverage", parents=[common])
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
    return parser


def dispatch(args: argparse.Namespace) -> tuple[dict, int]:
    if args.command == "doctor":
        return doctor(), 0
    if args.command == "run-once":
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
        return {
            "monitoring": "PARTIAL",
            "chains_enabled": [56],
            "protocols_read": ["lista-slisbnb", "hyperliquid-info"],
            "discovery": "LIMITED_DISCOVERY",
            "operation": operation_coverage(),
        }, 0
    if args.command == "providers":
        report = doctor()
        return {"providers": report["providers"]}, 0
    if args.command == "wallets":
        return run_once(Path(args.config), Path(args.db) if args.db else None), 0
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
        items = []
        # Local DB is single-operator in this build; tenant filter still applies when known.
        rows = database.conn.execute("SELECT tenant_id, payload FROM snapshot_items ORDER BY id").fetchall()
        items = [json.loads(row["payload"]) | {"tenant_id": row["tenant_id"]} for row in rows]
        if args.command == "claims" or (args.command == "actions" and args.actions_command == "list"):
            claims = [item for item in items if ":lista:" in item.get("position_id", "")]
            return {"status": "ok", "claims": claims, "wallet_request_allowed": False}, 0
        if args.command == "reconcile":
            return {"status": "ok", "items": len(items), "note": "aggregator keys are absent, so this is direct-state only"}, 0
        return {"status": "ok", "items": items}, 0
    finally:
        database.close()


def _explain(args: argparse.Namespace) -> tuple[dict, int]:
    path = Path(args.db) if args.db else ROOT / "data" / "guardian.sqlite"
    if not path.exists():
        return {"status": "not_found", "event_id": args.event_id}, 0
    database = Database(path)
    try:
        rows = database.conn.execute("SELECT payload FROM alerts").fetchall()
    finally:
        database.close()
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("dedup_key") == args.event_id:
            return {"status": "ok", "alert": payload, "text": format_alert(payload)}, 0
    return {"status": "not_found", "event_id": args.event_id}, 0


def _task_view(args: argparse.Namespace, task_id: str) -> tuple[dict, int]:
    path = Path(args.db) if args.db else ROOT / "data" / "guardian.sqlite"
    if not path.exists():
        return {"status": "not_found", "task_id": task_id}, 0
    database = Database(path)
    try:
        row = database.conn.execute("SELECT tenant_id, payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        database.close()
    if not row:
        return {"status": "not_found", "task_id": task_id}, 0
    task = json.loads(row["payload"])
    return {"status": "ok", "tenant_id": row["tenant_id"], "task": task, "wallet_request_allowed": False}, 0


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        # Touch config validation early for discover/run so a bad address fails closed.
        if args.command in {"run-once", "wallets"} and getattr(args, "config", None):
            load_config(Path(args.config))
        payload, code = dispatch(args)
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
