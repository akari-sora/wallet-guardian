"""Wallet-browser task workbench.

The agent may only emit typed task data. A link is not permission to execute.
Mode C stays off unless a verified on-chain mechanism enforces the limits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from guardian.errors import PolicyError, SecurityError
from guardian.resources import resource_path

TASK_VERSION = "guardian.task.v1"
CLIENT_BUILD = "action-client@0.3.0"
REF_PATTERN = re.compile(r"^ref_[a-f0-9]{32}$")
SECRET_KEYS = {
    "private_key",
    "mnemonic",
    "seed_phrase",
    "session_key",
    "session_secret",
    "walletconnect_uri",
    "pairing_uri",
    "executable_js",
    "runtime_script",
}
ENFORCED_FIELDS = (
    "task",
    "chain",
    "original_account",
    "contract",
    "method",
    "asset",
    "amount",
    "recipient",
    "deadline",
    "count",
    "fee",
)
STEP_LABELS = {
    "connect_wallet": "連接錢包",
    "login_signature": "登入簽名",
    "erc20_approve": "代幣授權",
    "batch_confirm": "批次確認",
    "execution_permission": "限定執行權限",
    "account_upgrade": "帳戶升級",
    "execute": "確認這筆領取",
}


def evidence_status() -> dict[str, Any]:
    return {
        "mobile_wallet_inapp_browser": "UNTESTED",
        "mode_a_real_wallet": "UNTESTED",
        "mode_b_real_wallet": "UNTESTED",
        "mode_c_real_wallet": "UNSUPPORTED",
        "mode_c_reason": "沒有已驗證、能在鏈上強制任務限制的權限合約",
        "fixed_https_origin": False,
        "mainnet_execution_authorized": False,
        "build_is_execution_authorization": False,
    }


def assert_opaque_ref(ref: str) -> str:
    if not isinstance(ref, str) or not REF_PATTERN.fullmatch(ref):
        raise PolicyError("task reference must be an opaque ref_ token")
    if "0x" in ref:
        raise PolicyError("task reference must not contain an address")
    return ref


def issue_task_link(ref: str, origin: str | None = None) -> dict[str, Any]:
    """Reject a caller-supplied origin. This function does not issue a view link.

    Approved view links come only from ``issue_private_task_link``, which reads the
    versioned policy. A caller cannot pass an origin into that issuer, including an
    arbitrary https URL. ``fixed_https_origin`` stays false.
    """
    assert_opaque_ref(ref)
    if isinstance(origin, str) and ("0x" in origin or "amount=" in origin or "address=" in origin):
        raise SecurityError("task link must not carry private wallet state")
    return {
        "issued": False,
        "reason": "caller_origin_rejected" if origin else "no_fixed_https_origin",
        "url": None,
        "ref": ref,
        "execution_authorized": False,
        "contains_private_state": False,
    }


PRIVATE_TASK_ORIGINS_PATH = resource_path("config", "deployments", "private-task-origins.json")
PRIVATE_TASK_DEFAULT_DEPLOYMENT = "wg-private-task-v1"


def load_private_task_origins(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Versioned view-origin policy shared with the Pages API. Not an execution allowlist."""
    data = json.loads((path or PRIVATE_TASK_ORIGINS_PATH).read_text(encoding="utf-8"))
    if data.get("schema") != "guardian.private_task_origins.v1":
        raise PolicyError("unknown private task origin policy schema")
    out: dict[str, dict[str, Any]] = {}
    for entry in data.get("deployments", []):
        origin = entry.get("origin")
        if (
            not isinstance(origin, str)
            or not origin.startswith("https://")
            or origin.rstrip("/") != origin
            or entry.get("task_path") != "/t/{ref}"
            or entry.get("purpose") != "private_task_view"
            or entry.get("execution_authorized") is not False
        ):
            raise PolicyError("private task origin entry is not a view-only https origin")
        out[entry["deployment_id"]] = entry
    return out


def issue_private_task_link(
    ref: str,
    deployment_id: str = PRIVATE_TASK_DEFAULT_DEPLOYMENT,
    *,
    policy: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """View link for a private task. The origin comes only from the approved policy file.

    Environment or caller may pick a deployment_id; neither can supply a new origin.
    Approving a view origin does not set fixed_https_origin or authorize execution.
    """
    assert_opaque_ref(ref)
    entries = load_private_task_origins() if policy is None else policy
    entry = entries.get(deployment_id)
    if entry is None:
        return {
            "issued": False,
            "reason": "view_origin_not_approved",
            "url": None,
            "ref": ref,
            "purpose": "private_task_view",
            "execution_authorized": False,
            "contains_private_state": False,
        }
    return {
        "issued": True,
        "reason": None,
        "url": entry["origin"] + entry["task_path"].replace("{ref}", ref),
        "ref": ref,
        "deployment_id": deployment_id,
        "purpose": "private_task_view",
        "view_origin_approved": True,
        "execution_authorized": False,
        "contains_private_state": False,
    }


def accept_typed_task(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PolicyError("task payload must be an object")
    _reject_secret_keys(payload)
    if payload.get("protocol_version") != TASK_VERSION:
        raise PolicyError("task protocol version is not the fixed build")
    if payload.get("client_build") != CLIENT_BUILD:
        raise PolicyError("task client build does not match the fixed version")
    if payload.get("executable_js") or payload.get("runtime_script"):
        raise SecurityError("the agent cannot attach executable signing script")
    assert_opaque_ref(payload["ref"])
    if payload.get("execution_authorized") is not False:
        raise SecurityError("a task payload cannot mark itself as execution authorization")
    return payload


def _reject_secret_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SECRET_KEYS:
                raise SecurityError("refusing secret or executable material in task data")
            _reject_secret_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_keys(item)
    elif isinstance(value, str) and value.startswith("wc:"):
        raise SecurityError("wallet session URIs do not belong in the shared workspace")


def workspace_export(task: dict[str, Any]) -> dict[str, Any]:
    accept_typed_task(task)
    return task


class TaskDirectory:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def put(self, tenant_id: str, task: dict[str, Any]) -> None:
        accept_typed_task(task)
        self._rows[task["ref"]] = {"tenant_id": tenant_id, "task": task}

    def get(self, tenant_id: str, ref: str) -> dict[str, Any]:
        assert_opaque_ref(ref)
        row = self._rows.get(ref)
        if row is None or row["tenant_id"] != tenant_id:
            raise SecurityError("task was not found for this tenant")
        return row["task"]


def lista_claim_steps() -> list[dict[str, Any]]:
    """One original-address claim. Other consents stay visible and are not required."""
    steps = []
    for kind in STEP_LABELS:
        required = kind == "execute"
        steps.append(
            {
                "id": kind,
                "label": STEP_LABELS[kind],
                "required": required,
                "separate": True,
                "status": "needs_user_confirm" if required else "not_required",
                "requests_7702": False,
                "unlimited_approval": False,
                "adds_admin": False,
                "migrates_assets": False,
                "upgrades_account": False,
            }
        )
    return steps


def assert_original_account(*, original: str, proposed_sender: str, position_holder: str) -> None:
    if proposed_sender != original or proposed_sender != position_holder:
        raise PolicyError("a new smart account cannot operate a position held by the original address")


# This repository does not ship a permission contract. A caller-supplied dict is not enforcement.
ON_CHAIN_PERMISSION_ENFORCER = None


def _assess_enforcer(enforcer: dict[str, Any] | None) -> dict[str, Any]:
    if not enforcer or not enforcer.get("verified") or not enforcer.get("enforces_on_chain"):
        return {"enabled": False, "fallback": "A", "reason": "no_verified_on_chain_enforcer"}
    if enforcer.get("enforced_by") in {"prompt", "ui", "backend_if"}:
        return {"enabled": False, "fallback": "A", "reason": "off_chain_check_is_not_enforcement"}
    enforced = set(enforcer.get("enforced_fields") or [])
    missing = [name for name in ENFORCED_FIELDS if name not in enforced]
    if missing:
        return {"enabled": False, "fallback": "A", "reason": "limits_not_enforced", "missing": missing}
    return {"enabled": True, "fallback": None, "reason": None}


def mode_c_decision(enforcer: dict[str, Any] | None) -> dict[str, Any]:
    assessed = _assess_enforcer(enforcer)
    if ON_CHAIN_PERMISSION_ENFORCER is None:
        return {
            "enabled": False,
            "fallback": "A",
            "reason": "no_verified_on_chain_enforcer",
            "missing": assessed.get("missing"),
            "caller_claim_ignored": True,
        }
    return assessed


def choose_mode(
    *,
    capability: str,
    same_chain: bool,
    call_count: int,
    enforcer: dict[str, Any] | None,
    original: str,
    proposed_sender: str,
) -> dict[str, Any]:
    assert_original_account(original=original, proposed_sender=proposed_sender, position_holder=original)
    if capability not in {"supported", "ready", "unsupported", "unknown"}:
        raise PolicyError("wallet capability must be checked before choosing a mode")
    limited = mode_c_decision(enforcer)
    if limited["enabled"] and capability == "supported" and same_chain:
        return {"mode": "C", "wallet_request_allowed": False, "real_wallet_evidence": "UNSUPPORTED"}
    if limited["enabled"]:
        return {"mode": "A", "fallback_from": "C", "reason": "enforcer_present_but_wallet_path_unverified", "wallet_request_allowed": False}
    if capability == "supported" and same_chain and call_count > 1:
        return {
            "mode": "B",
            "wallet_request_allowed": False,
            "real_wallet_evidence": "UNTESTED",
            "reason": "same_chain_batch_capability_supported",
        }
    reason = "single_step_original_account" if call_count == 1 else "batch_unavailable"
    if capability == "ready":
        reason = "ready_does_not_auto_upgrade"
    elif capability in {"unsupported", "unknown"} or not same_chain:
        reason = "fallback_to_guided_confirmation"
    return {
        "mode": "A",
        "fallback_from": "B" if capability != "supported" or not same_chain else None,
        "reason": reason,
        "wallet_request_allowed": False,
        "real_wallet_evidence": "UNTESTED",
        "auto_upgrade": False,
    }


def preflight_lista(plan: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    changes = []
    if str(fresh["idx"]) != str(plan["intent"]["idx"]):
        changes.append("idx")
    if fresh["implementation"] != plan["deployment"]["implementation"]:
        changes.append("implementation")
    if fresh["recipient"] != plan["recipient"]:
        changes.append("recipient")
    if str(fresh["amount_out"]) != str(plan["asset_out"]["raw_amount"]):
        changes.append("amount")
    if fresh.get("paused"):
        changes.append("paused")
    if fresh.get("expired"):
        changes.append("expired")
    if not fresh.get("claimable", True):
        changes.append("not_claimable")
    if changes:
        return {"continue_to_wallet": False, "preflight_ok": False, "revision_required": True, "changes": changes}
    return {
        "continue_to_wallet": False,
        "preflight_ok": True,
        "revision_required": False,
        "changes": [],
        "reason": "state_reread_matches",
    }


def confirm_gate(
    *,
    requires_user_wallet_confirmation: bool,
    preflight_ok: bool,
    mainnet_send: bool,
    wallet_request_allowed: bool = False,
    simulation_complete: bool = False,
    plan_blocked: bool = True,
) -> dict[str, Any]:
    """The button is separate from the send flag. Python never signs or broadcasts.

    ``preflight_ok`` is not send authority. A false wallet-request flag, incomplete
    simulation, or outstanding block reasons deny send even when preflight and the
    mainnet flag are both true. Defaults fail closed.
    """
    if not requires_user_wallet_confirmation or not preflight_ok:
        reason = "requires_user_wallet_confirmation_false" if not requires_user_wallet_confirmation else "preflight_not_ok"
        return {"button_enabled": False, "send_allowed": False, "reason": reason, "agent_may_sign": False}
    if wallet_request_allowed is not True:
        return {"button_enabled": False, "send_allowed": False, "reason": "wallet_request_denied", "agent_may_sign": False}
    if simulation_complete is not True:
        return {"button_enabled": False, "send_allowed": False, "reason": "simulation_incomplete", "agent_may_sign": False}
    if plan_blocked:
        return {"button_enabled": False, "send_allowed": False, "reason": "plan_blocked", "agent_may_sign": False}
    if not mainnet_send:
        return {
            "button_enabled": True,
            "send_allowed": False,
            "reason": "mainnet_send_disabled",
            "agent_may_sign": False,
        }
    return {"button_enabled": True, "send_allowed": True, "reason": None, "agent_may_sign": False}


def eip5792_status(capabilities: dict[str, Any] | None, chain_id: int) -> str:
    """Read a wallet_getCapabilities result. This does not call a wallet."""
    if capabilities is None:
        return "unknown"
    if not isinstance(capabilities, dict):
        raise PolicyError("wallet capability must be an object")
    entry = capabilities.get(hex(chain_id))
    if entry is None:
        entry = capabilities.get(str(chain_id))
    if not isinstance(entry, dict):
        return "unsupported"
    atomic = entry.get("atomic")
    if not isinstance(atomic, dict):
        return "unsupported"
    status = atomic.get("status")
    if status not in {"supported", "ready", "unsupported"}:
        return "unknown"
    return str(status)


def select_execution_mode(
    *,
    capabilities: dict[str, Any] | None,
    chain_id: int,
    same_chain: bool,
    call_count: int,
    enforcer: dict[str, Any] | None,
    original: str,
    proposed_sender: str,
) -> dict[str, Any]:
    status = eip5792_status(capabilities, chain_id)
    chosen = choose_mode(
        capability=status,
        same_chain=same_chain,
        call_count=call_count,
        enforcer=enforcer,
        original=original,
        proposed_sender=proposed_sender,
    )
    chosen["capability"] = status
    chosen["agent_may_sign"] = False
    return chosen


def track_wait(*, claimable: bool) -> dict[str, Any]:
    return {
        "skip_wait": False,
        "continuation_tracked": True,
        "state": "READY_FOR_USER" if claimable else "AWAITING_UNLOCK",
    }


def apply_task_event(task: dict[str, Any], event: str, **details: Any) -> dict[str, Any]:
    updated = dict(task)
    updated["complete"] = False
    if event == "user_rejected":
        updated["state"] = "USER_REJECTED"
        updated["auto_retry"] = False
        return updated
    if event == "submitted":
        updated["state"] = "SUBMITTED"
        updated["tx_hash"] = details["tx_hash"]
        updated["tx_hash_is_completion"] = False
        return updated
    if event == "submission_unknown":
        updated["state"] = "SUBMISSION_UNKNOWN"
        updated["new_transaction"] = False
        return updated
    if event == "replaced":
        updated["state"] = "REPLACED"
        updated["previous_hash_is_completion"] = False
        updated["new_hash_is_completion"] = False
        return updated
    if event == "duplicate":
        updated["new_transaction"] = False
        return updated
    if event == "partial":
        updated["state"] = "PARTIAL"
        return updated
    if event == "postconditions_ok":
        if not details.get("effects_checked"):
            raise PolicyError("a transaction hash is not completion")
        updated["state"] = "POSTCONDITIONS_VERIFIED"
        updated["complete"] = True
        updated["tx_hash_is_completion"] = False
        return updated
    if event == "permission_expired":
        updated["state"] = "EXPIRED"
        updated["mode"] = "A"
        updated["mode_c_usable"] = False
        return updated
    if event == "revoke_permission":
        updated["state"] = "CANCEL_REQUESTED"
        updated["recovers_transferred_assets"] = False
        return updated
    raise PolicyError("unknown task event")


def build_lista_task(plan: dict[str, Any], *, ref: str) -> dict[str, Any]:
    """Build a typed Lista task. The view link comes from the origin policy, not the caller."""
    mode = choose_mode(
        capability="unknown",
        same_chain=True,
        call_count=len(plan.get("calls") or []),
        enforcer=None,
        original=plan["sender"],
        proposed_sender=plan["sender"],
    )
    task = {
        "protocol_version": TASK_VERSION,
        "client_build": CLIENT_BUILD,
        "ref": assert_opaque_ref(ref),
        "execution_authorized": False,
        "mode": mode["mode"],
        "mode_reason": mode["reason"],
        "wallet_request_allowed": False,
        "steps": lista_claim_steps(),
        "link": issue_private_task_link(ref),
        "plan": plan,
        "evidence": evidence_status(),
        "requests_7702": False,
        "unlimited_approval": False,
    }
    return accept_typed_task(task)


def workbench_status() -> dict[str, Any]:
    """Operator view. A link and a passing test are not mainnet authorization."""
    return {
        "protocol_version": TASK_VERSION,
        "client_build": CLIENT_BUILD,
        "wallet_request_allowed": False,
        "default_mode": "A",
        "mode_a": "original_address_single_step_guided_confirmation",
        "mode_b": "eip5792_only_after_capability_check_real_wallet_UNTESTED",
        "mode_c": "UNSUPPORTED_until_verified_on_chain_enforcer",
        "steps": lista_claim_steps(),
        "evidence": evidence_status(),
        "link_is_execution_authorization": False,
        "public_link_requires_fixed_https_origin": True,
        "agent_emits": "typed_task_data_only",
        "runtime_signing_js": False,
    }
