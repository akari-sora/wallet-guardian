"""Doctor and run-once orchestration. Network failures stay visible as BLOCKED or LIMITED."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from guardian import __version__
from guardian.actions.lista_plan import build_lista_claim_plan
from guardian.actions.policy import KillSwitch
from guardian.domain import (
    WithdrawalRequest,
    baseline_alerts,
    diff_items,
    external_watcher_risk,
    format_alert,
    monitoring_mode,
    normalize_hyperliquid,
    notify_decision,
    semantic_key,
)
from guardian.actions.policy import CANONICAL_PLAN_KEYS
from guardian.canonical import digest_body
from guardian.errors import ConfigError, PolicyError, RpcError, SecurityError
from guardian.providers import LISTA_PROXY, SLISBNB, RpcClient, read_lista_state, safe_get_text, safe_post_json
from guardian.security.address import parse_evm_address
from guardian.security.redact import redact
from guardian.security.rpc_guard import FORBIDDEN_RPC_METHODS
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
TAIPEI = ZoneInfo("Asia/Taipei")
SECRET_ENV = (
    "ETHERSCAN_API_KEY",
    "GOPLUS_API_KEY",
    "DEBANK_ACCESS_KEY",
    "ZERION_API_KEY",
    "ALCHEMY_API_KEY",
    "TENDERLY_ACCESS_KEY",
)
BSC_RPC = "https://bsc-dataseed.bnbchain.org"
OFAC_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
EXPECTED_TABLES = [
    "tenants",
    "wallets",
    "chain_capabilities",
    "adapter_versions",
    "observations",
    "snapshots",
    "snapshot_items",
    "position_legs",
    "exposure_edges",
    "events",
    "cursors",
    "pending_actions",
    "allowances",
    "incidents",
    "rules",
    "alerts",
    "outbox",
    "expected_actions",
    "deliveries",
    "runs",
    "source_registry",
    "tasks",
]


def now_local() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError("config file was not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("config is not valid json") from exc
    if data.get("mode") not in {None, "read_only"}:
        raise ConfigError("agent mode must stay read_only")
    deployment = data.get("deployment") or {}
    if deployment.get("allow_trade_or_sign") or deployment.get("allow_public_webhook"):
        raise SecurityError("this agent build refuses trade, sign, and public webhook flags")
    if (data.get("assisted_actions") or {}).get("mainnet_test_transactions_authorized"):
        raise SecurityError("mainnet test transactions are not authorized for this build")
    wallets = []
    for wallet in data.get("wallets") or []:
        if wallet.get("family") != "evm":
            raise ConfigError("only evm watch addresses are supported")
        wallets.append({**wallet, "address": parse_evm_address(wallet["address"])})
    data["wallets"] = wallets
    return data


def _which(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"present": bool(path), "path": path}


def _secret_flags() -> dict[str, str]:
    return {name: "configured" if os.environ.get(name) else "not_configured" for name in SECRET_ENV}


def _pid1() -> str:
    try:
        return Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _systemd_state() -> str:
    if not shutil.which("systemctl"):
        return "absent"
    try:
        completed = subprocess.run(
            ["systemctl", "is-system-running"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (completed.stdout or completed.stderr or "unknown").strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def environment_report() -> dict[str, Any]:
    usage = shutil.disk_usage(ROOT)
    probe = Path(ROOT / "data" / ".write-probe")
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_taipei": now_local(),
        "timezone": "Asia/Taipei",
        "os": platform.platform(),
        "cpu_arch": platform.machine(),
        "python": platform.python_version(),
        "node": _which("node"),
        "uv": _which("uv"),
        "git": _which("git"),
        "pid_1": _pid1(),
        "systemd": _systemd_state(),
        "cron_daemon": "not_available_as_bot_scheduler",
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
        "workspace_writable": True,
        "persistent_workspace_assumed": False,
        "shared_computer_is_isolation": False,
        "https_webhook_receiver": False,
        "webhook_reason": "no verified public callback and the private seed disallows a public webhook",
        "credential_slots": _secret_flags(),
        "paid_subscription_changes": "not_performed",
        "guardian_version": __version__,
    }


def _probe(name: str, func) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        payload = func()
        level = payload.pop("level")
    except Exception as exc:  # noqa: BLE001 - provider failures must be reported, not hidden
        return {
            "source_id": name,
            "level": "LIVE_FAIL",
            "detail": redact(f"{type(exc).__name__}: {exc}")[:300],
            "latency_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        }
    payload.update(
        {
            "source_id": name,
            "level": level,
            "latency_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        }
    )
    return payload


def provider_probes() -> list[dict[str, Any]]:
    flags = _secret_flags()

    def etherscan() -> dict[str, Any]:
        status, text = safe_get_text("https://api.etherscan.io/v2/chainlist")
        if status != 200:
            return {"level": "LIVE_FAIL", "http": status}
        body = json.loads(text)
        rows = body.get("result") or []
        mainnets = [row for row in rows if "testnet" not in str(row.get("chainname", "")).lower()]
        return {
            "level": "LIVE_PASS",
            "http": status,
            "reported_rows": len(rows),
            "name_classified_mainnets": len(mainnets),
            "enabled_for_monitoring": ["bnb-smart-chain:56"],
            "note": "row count is not the number of monitored mainnets",
        }

    def bsc() -> dict[str, Any]:
        rpc = RpcClient(BSC_RPC)
        chain_id = int(rpc.call("eth_chainId", []), 16)
        block = rpc.call("eth_getBlockByNumber", ["latest", False])
        if chain_id != 56:
            return {"level": "LIVE_FAIL", "chain_id": chain_id}
        return {
            "level": "LIVE_PASS",
            "chain_id": chain_id,
            "block_number": int(block["number"], 16),
            "eth_getLogs": "disabled_on_official_dataseed",
        }

    def hyperliquid() -> dict[str, Any]:
        status, body = safe_post_json("https://api.hyperliquid.xyz/info", {"type": "meta"})
        ok = status == 200 and isinstance(body, dict)
        return {"level": "LIVE_PASS" if ok else "LIVE_FAIL", "http": status}

    def ofac() -> dict[str, Any]:
        status, text = safe_get_text(OFAC_URL)
        import re

        addresses = re.findall(r"0x[a-fA-F0-9]{40}", text)
        if status != 200:
            return {"level": "LIVE_FAIL", "http": status}
        if not addresses:
            return {
                "level": "LIMITED_DISCOVERY",
                "http": status,
                "reason": "downloaded SDN CSV did not contain a 0x address set, so wallets were not screened",
            }
        return {"level": "LIVE_PASS", "address_count": len(set(addresses))}

    def goplus() -> dict[str, Any]:
        if flags["GOPLUS_API_KEY"] != "configured":
            return {"level": "LIMITED_DISCOVERY", "reason": "GOPLUS_API_KEY not configured", "queried": False}
        return {"level": "BLOCKED", "reason": "configured key path is intentionally not auto-called"}

    blocked = []
    for name, env_name, reason in (
        ("debank", "DEBANK_ACCESS_KEY", "no access key; discovery stays limited"),
        ("zerion", "ZERION_API_KEY", "no api key and no verified callback"),
        ("alchemy", "ALCHEMY_API_KEY", "no api key"),
        ("tenderly", "TENDERLY_ACCESS_KEY", "simulation vendor not configured"),
    ):
        level = "BLOCKED" if flags[env_name] != "configured" else "BLOCKED"
        blocked.append({"source_id": name, "level": level, "reason": reason, "latency_ms": 0})

    return [
        _probe("etherscan_v2_chainlist", etherscan),
        _probe("bsc_official_rpc", bsc),
        _probe("hyperliquid_info", hyperliquid),
        _probe("ofac_sdn_csv", ofac),
        goplus(),
        *blocked,
        {
            "source_id": "news_rss",
            "level": "LIMITED_DISCOVERY",
            "reason": "filter skeleton only; no wallet is sent to a news search",
            "latency_ms": 0,
        },
        {
            "source_id": "webhook_receiver",
            "level": "BLOCKED",
            "reason": "public webhook is disallowed",
            "latency_ms": 0,
        },
    ]


def doctor() -> dict[str, Any]:
    env = environment_report()
    probes = provider_probes()
    report = {
        "status": "completed_with_limitations",
        "mode": "READ_ONLY",
        "monitoring_active": False,
        "monitoring_mode": monitoring_mode(scheduler_verified=False, next_run_read_back=False),
        "environment": env,
        "providers": probes,
        "agent_forbidden_methods": sorted(FORBIDDEN_RPC_METHODS),
        "assisted_actions_enabled": False,
        "mainnet_test_transactions_authorized": False,
        "writes_enabled": False,
    }
    (ROOT / "environment_report.json").write_text(json.dumps(report["environment"], indent=2) + "\n", encoding="utf-8")
    (ROOT / "source_registry.json").write_text(json.dumps({"generated_at_taipei": now_local(), "providers": probes}, indent=2) + "\n", encoding="utf-8")
    (ROOT / "provider_benchmark.json").write_text(
        json.dumps({"generated_at_taipei": now_local(), "results": probes, "stress_tests": "BLOCKED"}, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


class _SnapshotSource:
    def __init__(self, requests: list[WithdrawalRequest]) -> None:
        self.requests = requests
        self.reads = 0

    def read_now(self) -> list[WithdrawalRequest]:
        self.reads += 1
        return list(self.requests)


def _as_requests(rows: list[dict[str, Any]]) -> list[WithdrawalRequest]:
    return [
        WithdrawalRequest(
            uuid=int(row["uuid"]),
            amount_in_slis_bnb=int(row["amount_in_slis_bnb"]),
            start_time=int(row["start_time"]),
            is_claimable=bool(row["is_claimable"]),
            bnb_amount=int(row["bnb_amount"]),
        )
        for row in rows
    ]


def _hyperliquid(address: str) -> dict[str, Any]:
    payloads = {}
    for kind in ("clearinghouseState", "spotClearinghouseState", "subAccounts"):
        status, body = safe_post_json("https://api.hyperliquid.xyz/info", {"type": kind, "user": address})
        if status != 200:
            raise RpcError(f"hyperliquid {kind} http {status}")
        payloads[kind] = body
    subaccounts = payloads["subAccounts"]
    if subaccounts is None:
        parsed_subs = None
    elif isinstance(subaccounts, list):
        parsed_subs = subaccounts
    else:
        parsed_subs = None
    clearing = payloads["clearinghouseState"] if isinstance(payloads["clearinghouseState"], dict) else {}
    spot = payloads["spotClearinghouseState"] if isinstance(payloads["spotClearinghouseState"], dict) else {}
    if isinstance(spot, dict) and "portfolio_value" not in spot:
        spot = {**spot, "portfolio_value": None}
    return {
        "quality": "PARTIAL" if parsed_subs is None else "COMPLETE",
        "normalized": normalize_hyperliquid(
            clearinghouse=clearing,
            spot=spot,
            subaccounts=parsed_subs,
            mode="unknown",
        ),
        "raw_kept": True,
    }


def run_once(config_path: Path, db_path: Path | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    database = Database(db_path or (ROOT / "data" / "guardian.sqlite"))
    tenant_id = str(config.get("tenant_label") or "local")
    database.upsert_tenant(tenant_id, tenant_id)
    rpc = RpcClient(BSC_RPC)
    items: list[dict[str, Any]] = []
    wallet_reports = []
    plans = []
    limitations = [
        "LIMITED_DISCOVERY: DeBank, Zerion, and Alchemy keys are not configured",
        "official BSC dataseed disables eth_getLogs, so historical claims are not reconstructed",
        "prices are unknown and are not treated as zero",
        "notifications were not delivered to an external channel",
    ]
    blind = False
    for wallet in config["wallets"]:
        database.upsert_wallet(
            wallet["label"],
            tenant_id,
            wallet["label"],
            wallet["address"],
            wallet.get("ownership_basis", "user_provided_watch_address"),
        )
        entry: dict[str, Any] = {
            "label": wallet["label"],
            "ownership_basis": wallet.get("ownership_basis"),
            "ownership_proven": False,
        }
        try:
            state = read_lista_state(rpc, wallet["address"])
            entry["bsc"] = {
                "quality": "PARTIAL",
                "block_number": state["block_number"],
                "block_hash": state["block_hash"],
                "block_timestamp": state["block_timestamp"],
                "finality": state["finality"],
                "native_wei": state["native_wei"],
                "slisbnb_raw": state["slisbnb_raw"],
                "paused": state["paused"],
                "implementation": state["implementation"],
                "requests": state["requests"],
            }
            items.append(
                {
                    "position_id": f"{wallet['label']}:bnb:native",
                    "chain_id": 56,
                    "raw_amount": state["native_wei"],
                    "status": "wallet",
                    "symbol": "BNB",
                    "price": None,
                }
            )
            items.append(
                {
                    "position_id": f"{wallet['label']}:bsc:slisbnb",
                    "chain_id": 56,
                    "raw_amount": state["slisbnb_raw"],
                    "status": "staking",
                    "symbol": "slisBNB",
                    "price": None,
                }
            )
            for request in state["requests"]:
                items.append(
                    {
                        "position_id": f"{wallet['label']}:lista:{request['uuid']}",
                        "chain_id": 56,
                        "raw_amount": request["bnb_amount"],
                        "status": "CLAIMABLE" if request["is_claimable"] else "WAITING",
                        "symbol": "BNB",
                        "price": None,
                        "severity": "ACTION" if request["is_claimable"] else "INFO",
                    }
                )

            class _LiveReread:
                def __init__(self) -> None:
                    self.reads = 0

                def read_now(self) -> list[WithdrawalRequest]:
                    self.reads += 1
                    fresh = read_lista_state(rpc, wallet["address"])
                    self.last = fresh
                    return _as_requests(fresh["requests"])

            for request in state["requests"]:
                source = _LiveReread()
                try:
                    plan = build_lista_claim_plan(
                        source=source,
                        uuid=int(request["uuid"]),
                        sender=wallet["address"],
                        proxy=LISTA_PROXY,
                        implementation=state["implementation"],
                        proxy_code_sha256=state["proxy_code_sha256"],
                        implementation_code_sha256=state["implementation_code_sha256"],
                        paused=state["paused"],
                        block_number=state["block_number"],
                        block_hash=state["block_hash"],
                        task_id="lista-"
                        + hashlib.sha256(wallet["address"].encode()).hexdigest()[:8]
                        + "-"
                        + request["uuid"],
                        ownership_basis=wallet.get("ownership_basis", "user_provided_watch_address"),
                        mainnet_authorized=False,
                        assisted_enabled=False,
                    )
                except (PolicyError, RpcError, ValueError) as exc:
                    plans.append(
                        {
                            "wallet_label": wallet["label"],
                            "plan": {
                                "task_id": "lista-unplanned-" + request["uuid"],
                                "state": "BLOCKED",
                                "wallet_request_allowed": False,
                                "block_reasons": [redact(str(exc))[:240]],
                                "digest": None,
                            },
                        }
                    )
                    continue
                fresh = source.last
                plan["deployment"]["implementation"] = fresh["implementation"]
                plan["deployment"]["proxy_code_sha256"] = fresh["proxy_code_sha256"]
                plan["deployment"]["implementation_code_sha256"] = fresh["implementation_code_sha256"]
                plan["preconditions"]["paused"] = fresh["paused"]
                plan["simulation"]["block_number"] = fresh["block_number"]
                plan["simulation"]["block_hash"] = fresh["block_hash"]
                if fresh["implementation"] != state["implementation"] or fresh["paused"] != state["paused"]:
                    plan["state"] = "BLOCKED"
                    plan["block_reasons"].append("部署或 pause 狀態在重讀時改變，舊計畫作廢")
                plan["digest"] = digest_body({key: plan[key] for key in CANONICAL_PLAN_KEYS})
                if source.reads != 1:
                    raise RuntimeError("plan builder did not reread the request list")
                plans.append({"wallet_label": wallet["label"], "plan": plan})
                database.save_task(tenant_id, plan)
        except (RpcError, SecurityError, OSError, ValueError) as exc:
            blind = True
            entry["bsc"] = {"quality": "ERROR", "detail": redact(str(exc))[:300], "balances": None}
            limitations.append(f"{wallet['label']} BSC read failed; last known balances were not replaced with zero")
        try:
            entry["hyperliquid"] = _hyperliquid(wallet["address"])
        except (RpcError, SecurityError, OSError, ValueError) as exc:
            entry["hyperliquid"] = {"quality": "ERROR", "detail": redact(str(exc))[:300]}
            limitations.append(f"{wallet['label']} Hyperliquid read failed")
        wallet_reports.append(entry)

    pending = []
    for action in config.get("known_pending_actions") or []:
        watcher = external_watcher_risk((action.get("external_watcher")))
        pending.append(
            {
                "wallet_label": action.get("wallet_label"),
                "protocol": action.get("protocol"),
                "user_reported_status": "UNVERIFIED",
                "user_reported_amount_used_as_current_state": False,
                "onchain_identity": "not_matched_by_amount",
                "watcher": watcher,
            }
        )

    previous = database.snapshot_items(tenant_id) if database.latest_generation(tenant_id) else []
    generation = database.latest_generation(tenant_id) + 1
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if items:
        database.commit_snapshot(tenant_id, generation, created, items)
    diff = diff_items(previous, items)
    is_baseline = not previous
    candidates = baseline_alerts(items, is_baseline=is_baseline)
    alerts = []
    if candidates and is_baseline:
        for item in candidates:
            alert = {
                "severity": item["severity"],
                "severity_label": "待處理" if item["severity"] == "ACTION" else item["severity"],
                "confidence": "鏈上讀取",
                "wallet_label": item["position_id"].split(":", 1)[0],
                "chain_label": "BNB Smart Chain",
                "subject": "Lista 提款",
                "what": "首次基準觀測到目前可領的請求；這不是「剛剛才變成可領」的判斷。",
                "amount_display": item["raw_amount"],
                "unit": item["symbol"],
                "price": None,
                "price_time": None,
                "why": "這是監控地址上的 Lista 提款請求。",
                "step": "在你自己的錢包確認前，先核對 idx 與 isClaimable。此建置不會代簽。",
                "event_time": created,
                "observed_at": now_local(),
                "source": BSC_RPC,
                "limits": "模擬沒有追蹤餘額變化；主網簽名未授權。",
                "dedup_key": semantic_key(tenant_id, "56", item["position_id"], "claimable", "baseline"),
            }
            alerts.append({"text": format_alert(alert), "payload": alert})
            database.add_alert(tenant_id, alert["dedup_key"], alert)
    decision = notify_decision(
        changes=alerts,
        sources_healthy=not blind,
        blind_alert_open=False,
        recovered=False,
    )
    run_id = "run-" + hashlib.sha256(created.encode()).hexdigest()[:12]
    result = {
        "status": "completed_with_limitations" if not blind else "degraded",
        "mode": "READ_ONLY",
        "monitoring_active": False,
        "monitoring_mode": "RUN_ONCE",
        "generated_at_taipei": now_local(),
        "run_id": run_id,
        "snapshot_generation": generation if items else None,
        "diff": diff,
        "baseline": is_baseline,
        "notifications_sent": False,
        "notification_decision": decision,
        "limitations": limitations,
        "pending_actions": pending,
        "wallets": wallet_reports,
        "action_plans": [
            {
                "wallet_label": row["wallet_label"],
                "task_id": row["plan"]["task_id"],
                "state": row["plan"]["state"],
                "wallet_request_allowed": False,
                "block_reasons": row["plan"]["block_reasons"],
                "digest": row["plan"]["digest"],
            }
            for row in plans
        ],
        "operation_coverage": operation_coverage(),
        "agent_broadcast": False,
    }
    evidence = ROOT / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{run_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    full_plans = ROOT / "evidence" / "plans"
    full_plans.mkdir(parents=True, exist_ok=True)
    for row in plans:
        (full_plans / f"{row['plan']['task_id']}.json").write_text(
            json.dumps(row["plan"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    database.save_run(tenant_id, run_id, {"status": result["status"], "generation": result["snapshot_generation"]})
    database.close()
    return result


def operation_coverage() -> dict[str, Any]:
    return {
        "actions": [
            {
                "chain_id": 56,
                "protocol": "lista-slisbnb",
                "deployment": LISTA_PROXY,
                "token": SLISBNB,
                "method": "claimWithdraw(uint256)",
                "read": "READ_VERIFIED_WHEN_LIVE_CALL_SUCCEEDS",
                "write": "DRAFT",
                "release": "BLOCKED",
                "reasons": [
                    "mainnet_test_transactions_authorized=false",
                    "simulation has no balance or event trace",
                    "no fork test in this environment",
                    "no user-wallet end-to-end test",
                    "watch-only addresses are not execution identities",
                ],
            }
        ],
        "arbitrary_contract_call": "UNSUPPORTED",
        "wallet_requests_enabled": False,
    }


def self_check(db_path: Path | None = None) -> dict[str, Any]:
    switch = KillSwitch(False)
    read_ok = switch.read()["ok"]
    write_blocked = False
    try:
        switch.write()
    except SecurityError:
        write_blocked = True
    database = Database(db_path or (ROOT / "data" / "self-check.sqlite"))
    names = set(database.table_names())
    database.close()
    return {
        "status": "pass" if read_ok and write_blocked and set(EXPECTED_TABLES).issubset(names) else "fail",
        "read_while_writes_off": read_ok,
        "write_blocked": write_blocked,
        "monitoring_mode": monitoring_mode(scheduler_verified=False, next_run_read_back=False),
        "tables_present": sorted(names),
        "webhook_receiver": False,
        "agent_can_sign": False,
    }


def rescue_doctor() -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": "沒有已核准的固定版本部署、公開入口或可攜救援包發布",
        "unsigned_plan_export": "local-only",
        "contains_secrets": False,
    }
