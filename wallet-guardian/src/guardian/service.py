"""Doctor and run-once orchestration. Network failures stay visible as BLOCKED or LIMITED."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from guardian import __version__
from guardian.actions.lista_plan import build_lista_claim_plan
from guardian.actions.policy import CANONICAL_PLAN_KEYS, KillSwitch
from guardian.actions.workbench import CLIENT_BUILD, TASK_VERSION, evidence_status
from guardian.domain import (
    WithdrawalRequest,
    external_watcher_risk,
    monitoring_mode,
    normalize_hyperliquid,
)
from guardian.canonical import digest_body
from guardian.errors import ConfigError, RpcError, SecurityError
from guardian.providers import LISTA_PROXY, SLISBNB, RpcClient, read_lista_state, safe_get_text, safe_post_json
from guardian.security.address import parse_evm_address
from guardian.security.redact import redact
from guardian.security.rpc_guard import FORBIDDEN_RPC_METHODS
from guardian.storage import Database
from guardian.monitoring import SnapshotCycle

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
# One Etherscan key covers V2 chainlist and later account/tx/log calls. Not a second key.
MAINNET_SEND_ENV = "GUARDIAN_MAINNET_SEND"
LOCAL_ENV_KEYS = SECRET_ENV + (MAINNET_SEND_ENV,)
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
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
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    if data.get("mode") not in {None, "read_only"}:
        raise ConfigError("agent mode must stay read_only")
    deployment = data.get("deployment") or {}
    assisted = data.get("assisted_actions") or {}
    if not isinstance(deployment, dict) or not isinstance(assisted, dict):
        raise ConfigError("deployment and assisted_actions must be objects")
    pending = data.get("known_pending_actions") or []
    if not isinstance(pending, list) or any(not isinstance(row, dict) for row in pending):
        raise ConfigError("known_pending_actions must be a list of objects")
    if any(not isinstance(row.get("external_watcher") or {}, dict) for row in pending):
        raise ConfigError("external_watcher must be an object")
    if deployment.get("allow_trade_or_sign") or deployment.get("allow_public_webhook"):
        raise SecurityError("this agent build refuses trade, sign, and public webhook flags")
    if (data.get("assisted_actions") or {}).get("mainnet_test_transactions_authorized"):
        raise SecurityError("mainnet test transactions are not authorized for this build")
    wallets = []
    configured = data.get("wallets")
    if not isinstance(configured, list) or not configured:
        raise ConfigError("wallets must be a non-empty list")
    labels, addresses = set(), set()
    for wallet in configured:
        if not isinstance(wallet, dict):
            raise ConfigError("each wallet must be an object")
        label = wallet.get("label")
        if not isinstance(label, str) or not label.strip() or any(c in label for c in ":|/\\\n\r"):
            raise ConfigError("wallet labels must be non-empty and contain no path or identity separators")
        if label in labels:
            raise ConfigError("duplicate wallet label")
        labels.add(label)
        if wallet.get("family") != "evm":
            raise ConfigError("only evm watch addresses are supported")
        if not isinstance(wallet.get("address"), str):
            raise ConfigError("wallet address must be a string")
        address = parse_evm_address(wallet["address"])
        if address.lower() in addresses:
            raise ConfigError("duplicate wallet address")
        addresses.add(address.lower())
        wallets.append({**wallet, "address": address})
    data["wallets"] = wallets
    return data


def _which(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"present": bool(path), "path": path}


def load_local_env(path: Path | None = None) -> list[str]:
    """Load gitignored .env.local. Existing process environment wins. Values are not logged."""
    target = path if path is not None else ROOT / ".env.local"
    loaded: list[str] = []
    if not target.is_file():
        return loaded
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key not in LOCAL_ENV_KEYS or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
        loaded.append(key)
    return loaded


def _secret_flags() -> dict[str, str]:
    return {name: "configured" if os.environ.get(name) else "not_configured" for name in SECRET_ENV}


def etherscan_chainlist_probe(get_text=safe_get_text) -> dict[str, Any]:
    """V2 chainlist is LIVE_PASS only when ETHERSCAN_API_KEY is set and the call returns rows."""
    key = (os.environ.get("ETHERSCAN_API_KEY") or "").strip()
    if not key:
        return {
            "level": "LIMITED_DISCOVERY",
            "queried": False,
            "authenticated": False,
            "reason": "ETHERSCAN_API_KEY not configured; chainlist is not LIVE_PASS without the V2 key",
        }
    url = "https://api.etherscan.io/v2/chainlist?" + urlencode({"apikey": key})
    try:
        status, text = get_text(url)
    except Exception as exc:  # noqa: BLE001 - the key must not leak through the provider error
        raise RuntimeError(redact(str(exc), extra_secrets=[key])) from None
    if status != 200:
        return {"level": "LIVE_FAIL", "http": status, "queried": True, "authenticated": True}
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return {"level": "LIVE_FAIL", "http": status, "queried": True, "authenticated": True, "reason": "chainlist was not json"}
    rows = body.get("result") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        return {
            "level": "LIVE_FAIL",
            "http": status,
            "queried": True,
            "authenticated": True,
            "reason": "chainlist response had no rows",
        }
    mainnets = [row for row in rows if "testnet" not in str(row.get("chainname", "")).lower()]
    return {
        "level": "LIVE_PASS",
        "http": status,
        "queried": True,
        "authenticated": True,
        "reported_rows": len(rows),
        "name_classified_mainnets": len(mainnets),
        "enabled_for_monitoring": ["bnb-smart-chain:56"],
        "note": "one ETHERSCAN_API_KEY covers V2; row count is not the number of monitored mainnets",
    }


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
        return etherscan_chainlist_probe()

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
        return {
            "level": "LIMITED_DISCOVERY",
            "http": status,
            "address_count": len(set(addresses)),
            "reason": "SDN CSV included some 0x strings; it is not a complete digital-currency sanctions list",
        }

    def goplus() -> dict[str, Any]:
        if flags["GOPLUS_API_KEY"] != "configured":
            return {
                "source_id": "goplus",
                "level": "LIMITED_DISCOVERY",
                "reason": "GOPLUS_API_KEY not configured",
                "queried": False,
                "latency_ms": 0,
            }
        return {
            "source_id": "goplus",
            "level": "BLOCKED",
            "reason": "configured key path is intentionally not auto-called",
            "latency_ms": 0,
        }

    blocked = []
    for name, env_name, reason in (
        ("debank", "DEBANK_ACCESS_KEY", "no access key; discovery stays limited"),
        ("zerion", "ZERION_API_KEY", "no api key and no verified callback"),
        ("alchemy", "ALCHEMY_API_KEY", "no api key"),
        ("tenderly", "TENDERLY_ACCESS_KEY", "simulation vendor not configured"),
    ):
        if flags[env_name] == "configured":
            reason = "key configured; this provider adapter is not implemented"
        blocked.append({"source_id": name, "level": "BLOCKED", "reason": reason, "latency_ms": 0})

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
    load_local_env()
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
        "agent_may_sign": False,
        "user_mainnet_send_env": os.environ.get(MAINNET_SEND_ENV, "false").lower() == "true",
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
        if kind != "subAccounts" and (
            not isinstance(body, dict) or body.get("non_json") or
            (kind == "clearinghouseState" and (not isinstance(body.get("assetPositions"), list) or not isinstance(body.get("marginSummary"), dict))) or
            (kind == "spotClearinghouseState" and not isinstance(body.get("balances"), list))
        ):
            raise RpcError(f"hyperliquid {kind} invalid response schema")
        if kind == "subAccounts" and body is not None and not isinstance(body, list):
            raise RpcError("hyperliquid subAccounts invalid response schema")
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
        "quality": "PARTIAL",
        "coverage_limits": ["account mode unknown", "subaccount positions and HIP-3 markets not queried"],
        "raw": payloads,
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
    try:
        return _run_once(config, database)
    finally:
        database.close()


def _run_once(config: dict[str, Any], database: Database) -> dict[str, Any]:
    tenant_id = str(config.get("tenant_label") or "local")
    database.upsert_tenant(tenant_id, tenant_id)
    cycle = SnapshotCycle(database, tenant_id)  # Before any network I/O.
    rpc = RpcClient(BSC_RPC)
    successful_scopes: set[tuple[str, int]] = set()
    failed_scopes: list[str] = []
    items: list[dict[str, Any]] = []
    wallet_reports = []
    plans = []
    limitations = [
        "LIMITED_DISCOVERY: DeBank, Zerion, and Alchemy discovery adapters are not implemented",
        "official BSC dataseed disables eth_getLogs, so historical claims are not reconstructed",
        "prices are unknown and are not treated as zero",
        "notifications were not delivered to an external channel",
    ]
    for wallet in config["wallets"]:
        existing = database.conn.execute(
            "SELECT address FROM wallets WHERE tenant_id=? AND wallet_id=?",
            (tenant_id, wallet["label"]),
        ).fetchone()
        if existing and existing["address"].lower() != wallet["address"].lower():
            raise ConfigError("a wallet label cannot be rebound to a different address; use a new label")
        old_label = database.conn.execute(
            "SELECT label FROM wallets WHERE tenant_id=? AND lower(address)=? AND wallet_id<>?",
            (tenant_id, wallet["address"].lower(), wallet["label"]),
        ).fetchone()
        if old_label:
            raise ConfigError("renaming a wallet requires an explicit identity migration")
        start = len(items)
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
            age = time.time() - state["block_timestamp"]
            if age < -60 or age > 300 or not state.get("block_hash"):
                raise RpcError("BSC block is stale, in the future, or missing its hash")
            entry["bsc"] = {
                "quality": "COMPLETE",
                "scope": [
                    "native_balance",
                    "slisbnb_balanceOf",
                    "lista_withdrawal_requests",
                    "pause",
                    "erc1967_implementation",
                ],
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

            for item in items[start:]:
                item.update(
                    wallet_label=wallet["label"], wallet_address=wallet["address"],
                    quality="COMPLETE", fresh=True, observed_at=now_local(),
                    block_number=state["block_number"], block_hash=state["block_hash"],
                    block_timestamp=state["block_timestamp"], finality=state["finality"],
                    paused=state["paused"],
                )
            successful_scopes.add((wallet["label"], 56))
            for request in state["requests"]:
                plan = _fresh_lista_plan(rpc, wallet, request, database, tenant_id)
                plans.append({"wallet_label": wallet["label"], "plan": plan})
        except (RpcError, SecurityError, OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            del items[start:]
            successful_scopes.discard((wallet["label"], 56))
            failed_scopes.append(wallet["label"] + ":bsc")
            entry["bsc"] = {"quality": "ERROR", "detail": redact(str(exc))[:300], "balances": None}
            limitations.append(f"{wallet['label']} BSC read failed; previous items retained as STALE")
        try:
            entry["hyperliquid"] = _hyperliquid(wallet["address"])
        except (RpcError, SecurityError, OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            failed_scopes.append(wallet["label"] + ":hyperliquid")
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

    created = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    ofac = screen_ofac(config["wallets"])
    if ofac.get("level") == "LIVE_FAIL":
        failed_scopes.append("ofac")
    # Suppress only an explicitly identified request with a verified external
    # owner. Amounts and array indexes never establish cross-watcher identity.
    suppressed = set()
    for action in config.get("known_pending_actions") or []:
        watcher = action.get("external_watcher") or {}
        request_uuid = str(action.get("request_uuid", ""))
        if (action.get("chain_id") == 56 and action.get("protocol") in {"Lista DAO", "lista-slisbnb"}
                and request_uuid.isascii() and request_uuid.isdigit()
                and watcher.get("current_health") == "VERIFIED_HEALTHY"
                and watcher.get("suppress_expected_claim_duplicate") is True):
            suppressed.add(f"{action.get('wallet_label')}:lista:{int(request_uuid)}")
    settled = cycle.finish(
        incoming=items, successful_scopes=successful_scopes, failed_scopes=failed_scopes,
        created=created, tasks=[row["plan"] for row in plans], suppressed_claims=suppressed,
    )
    run_id = "run-" + uuid.uuid4().hex
    blind = bool(failed_scopes)
    result = {
        "status": "completed_with_limitations" if not blind else "degraded",
        "mode": "READ_ONLY",
        "monitoring_active": False,
        "monitoring_mode": "RUN_ONCE",
        "generated_at_taipei": now_local(),
        "run_id": run_id,
        "snapshot_generation": settled["snapshot_generation"],
        "diff": settled["diff"],
        "baseline": settled["baseline"],
        "source_health": settled["source_health"],
        "notifications_sent": False,
        "notification_decision": settled["notification_decision"],
        "limitations": limitations,
        "ofac": ofac,
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
    database.save_run(tenant_id, run_id, result)
    evidence = ROOT / "evidence" / hashlib.sha256(tenant_id.encode()).hexdigest()
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    (evidence / f"{run_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    full_plans = evidence / "plans"
    full_plans.mkdir(parents=True, exist_ok=True)
    for row in plans:
        (full_plans / f"{row['plan']['task_id']}.json").write_text(
            json.dumps(row["plan"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result



def _fresh_lista_plan(rpc, wallet, request, database, tenant_id):
    task_id = "lista-" + hashlib.sha256(wallet["address"].lower().encode()).hexdigest() + "-" + str(request["uuid"])
    old = database.get_task(tenant_id, task_id)
    revision = int(old["revision"]) + 1 if old else 1
    try:
        fresh = read_lista_state(rpc, wallet["address"])
        age = time.time() - fresh["block_timestamp"]
        if age < -60 or age > 300 or not fresh.get("block_hash"):
            raise RpcError("planning block is stale or missing")
        plan = build_lista_claim_plan(
            source=_SnapshotSource(_as_requests(fresh["requests"])),
            uuid=int(request["uuid"]), sender=wallet["address"], proxy=LISTA_PROXY,
            implementation=fresh["implementation"], proxy_code_sha256=fresh["proxy_code_sha256"],
            implementation_code_sha256=fresh["implementation_code_sha256"], paused=fresh["paused"],
            block_number=fresh["block_number"], block_hash=fresh["block_hash"],
            task_id=task_id, revision=revision, now_unix=int(time.time()),
            ownership_basis="user_provided_watch_address", mainnet_authorized=False, assisted_enabled=False,
        )
        if plan["state"] == "SIMULATED":
            plan["state"] = "DRAFT"
        plan["simulation"]["source"] = "not_run"
        plan["block_reasons"].append("只讀取狀態；未模擬 claimWithdraw 交易。")
        plan["digest"] = digest_body({key: plan[key] for key in CANONICAL_PLAN_KEYS})
        return plan
    except (RpcError, SecurityError, OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        return {"task_id": task_id, "revision": revision, "state": "BLOCKED",
                "calls": [], "wallet_request_allowed": False,
                "block_reasons": [redact(str(exc))[:240]], "digest": None}

def screen_ofac(wallets: list[dict[str, Any]]) -> dict[str, Any]:
    import re

    try:
        status, text = safe_get_text(OFAC_URL)
    except (OSError, RpcError, SecurityError, ValueError, httpx.HTTPError) as exc:
        return {"level": "LIVE_FAIL", "detail": redact(str(exc))[:240], "rows": []}
    found: set[str] = set()
    for item in re.findall(r"0x[a-fA-F0-9]{40}", text):
        try:
            found.add(parse_evm_address(item))
        except ConfigError:
            continue
    if status != 200 or not found:
        return {
            "level": "LIMITED_DISCOVERY",
            "http": status,
            "rows": [],
            "reason": "downloaded file did not yield a usable 0x address set",
        }
    return {
        "level": "LIMITED_DISCOVERY",
        "http": status,
        "address_count": len(found),
        "coverage": "PARTIAL",
        "reason": "SDN CSV is not a complete digital-currency sanctions list",
        "rows": [
            {"label": wallet["label"], "listed_in_downloaded_csv": wallet["address"] in found}
            for wallet in wallets
        ],
    }


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
        "workbench": {
            "protocol_version": TASK_VERSION,
            "client_build": CLIENT_BUILD,
            "mode_a": "original_address_single_step_guided_confirmation",
            "mode_b": "eip5792_only_after_capability_check_real_wallet_UNTESTED",
            "mode_c": "UNSUPPORTED_until_verified_on_chain_enforcer",
            "evidence": evidence_status(),
            "link_is_execution_authorization": False,
        },
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
