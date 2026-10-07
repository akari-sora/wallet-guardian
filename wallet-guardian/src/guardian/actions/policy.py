"""Action policy. These checks are independent of any LLM narration."""

from __future__ import annotations

from typing import Any

from guardian.errors import PolicyError, SecurityError
from guardian.security.rpc_guard import FORBIDDEN_RPC_METHODS

DIAGNOSIS_SIGNALS = (
    "website_reachable",
    "chain_ok",
    "contract_ok",
    "indexer_ok",
    "default_rpc_ok",
    "approved_fallback_rpc_ok",
    "walletconnect_relay_ok",
    "injected_provider_available",
    "guardian_backend_ok",
    "action_client_independent",
    "method_paused",
    "unlock_ready",
    "queueing",
    "liquidity_ok",
    "chain_producing_blocks",
    "force_inclusion_verified",
    "offchain_proof_available",
    "exploit_suspected",
)

PROXY_TYPES = {"EIP1967", "EIP1967Proxy"}
CANONICAL_PLAN_KEYS = (
    "protocol_version",
    "task_id",
    "revision",
    "chain_namespace",
    "chain_id",
    "sender",
    "execution_account",
    "execution_account_type",
    "recipient",
    "protocol_id",
    "deployment",
    "intent",
    "calls",
    "asset_in",
    "asset_out",
    "preconditions",
    "bounds",
    "max_fee_wei",
    "simulation",
    "wallet_request_allowed",
)


def diagnose(signals: dict[str, Any]) -> dict[str, Any]:
    missing = [name for name in DIAGNOSIS_SIGNALS if name not in signals]
    if missing:
        return {"status": "INCOMPLETE", "missing": missing, "findings": []}
    findings: list[dict[str, Any]] = []
    if signals["website_reachable"] is False and signals["chain_ok"] and signals["contract_ok"]:
        findings.append(
            {
                "id": "website_down_chain_ok",
                "available": ["verified_action_client"],
                "forbidden_inferences": ["protocol_hacked", "global_outage_from_one_probe"],
            }
        )
    if signals["indexer_ok"] is False:
        findings.append(
            {
                "id": "indexer_down",
                "available": ["approved_rpc_views"],
                "forbidden_inferences": ["zero_assets"],
            }
        )
    if signals["default_rpc_ok"] is False:
        choice = "approved_fallback" if signals["approved_fallback_rpc_ok"] else "wait"
        findings.append(
            {
                "id": "rpc_down",
                "available": [choice],
                "forbidden_inferences": ["search_result_rpc"],
            }
        )
    if signals["walletconnect_relay_ok"] is False:
        available = ["injected_provider"] if signals["injected_provider_available"] else ["wait"]
        findings.append(
            {
                "id": "relay_down",
                "available": available,
                "forbidden_inferences": ["switching_site_fixes_relay"],
            }
        )
    if signals["guardian_backend_ok"] is False:
        available = ["fixed_action_client"] if signals["action_client_independent"] else ["not_independent"]
        findings.append(
            {
                "id": "backend_down",
                "available": available,
                "forbidden_inferences": ["same_api_counts_as_backup"],
            }
        )
    if signals["method_paused"] is True:
        findings.append(
            {
                "id": "method_paused",
                "available": ["wait_and_monitor"],
                "forbidden_inferences": ["bypass_access_control"],
                "block_action": True,
            }
        )
    if signals["unlock_ready"] is False or signals["queueing"] is True or signals["liquidity_ok"] is False:
        findings.append(
            {
                "id": "not_ready_to_redeem",
                "available": ["show_stage", "explicit_market_exit"],
                "forbidden_inferences": ["sell_lst_completes_redeem", "market_exit_guaranteed"],
            }
        )
    if signals["chain_producing_blocks"] is False:
        available = ["verified_force_inclusion"] if signals["force_inclusion_verified"] else ["wait"]
        findings.append(
            {
                "id": "chain_halted",
                "available": available,
                "forbidden_inferences": ["generic_escape_hatch"],
            }
        )
    if signals["offchain_proof_available"] is False:
        findings.append(
            {
                "id": "proof_missing",
                "available": [],
                "status": "BLOCKED",
                "forbidden_inferences": ["abi_implies_valid_proof"],
            }
        )
    if signals["exploit_suspected"] is True:
        findings.append(
            {
                "id": "exploit_suspected",
                "available": ["evidence_only_options"],
                "forbidden_inferences": ["blind_sign_unknown_contract"],
                "block_unverified_writes": True,
            }
        )
    return {"status": "OK", "missing": [], "findings": findings}


def resolve_proxy(*, proxy_type: str | None, implementation: str | None) -> dict[str, Any]:
    if proxy_type not in PROXY_TYPES or not implementation:
        return {"implementation": None, "guessed": False, "status": "UNKNOWN_PROXY"}
    return {"implementation": implementation, "guessed": False, "status": "RESOLVED"}


def assert_call_target(*, call_to: str, proxy: str, implementation: str, msg_sender_sensitive: bool) -> None:
    if call_to == implementation:
        raise PolicyError("the transaction must target the proxy, not the implementation")
    if msg_sender_sensitive and call_to != proxy:
        raise PolicyError("a msg.sender-sensitive call cannot be wrapped in another contract")


def invalidate_on_upgrade(*, plan_impl_hash: str, live_impl_hash: str) -> dict[str, Any]:
    if plan_impl_hash != live_impl_hash:
        return {"valid": False, "reason": "implementation_changed"}
    return {"valid": True, "reason": None}


def verified_source_meaning() -> dict[str, bool]:
    return {"source_verified": True, "audited": False, "safe": False}


def assert_sender(
    *,
    holder: str,
    holder_type: str,
    connected: str,
    ownership_basis: str,
) -> None:
    if ownership_basis != "user_wallet_connected_this_session":
        raise PolicyError("a watch-only address is not an execution identity")
    if holder_type == "safe" and connected != holder:
        raise PolicyError("a Safe position cannot be executed by the owner EOA")
    if connected != holder:
        raise PolicyError("connected account does not match the position holder")


def simulation_evidence(simulation: dict[str, Any]) -> dict[str, Any]:
    if simulation.get("state_overrides"):
        return {
            "usable_as_live_evidence": False,
            "label": "TEST_ONLY_OVERRIDE",
            "override_kind": simulation.get("override_kind"),
        }
    if simulation.get("success") and not simulation.get("effects_checked"):
        return {"usable_as_live_evidence": False, "status": "INCOMPLETE", "success_alone": False}
    if simulation.get("success") and simulation.get("effects_checked"):
        return {"usable_as_live_evidence": True, "status": "PASS"}
    return {"usable_as_live_evidence": False, "status": "FAIL"}


def ttl_decision(*, now: int, deadline: int, submitted: bool) -> dict[str, Any]:
    if submitted:
        return {"allow_new_wallet_request": False, "cancels_submitted": False, "reason": "already_submitted"}
    if now > deadline:
        return {"allow_new_wallet_request": False, "cancels_submitted": False, "reason": "local_ttl_expired"}
    return {"allow_new_wallet_request": True, "cancels_submitted": False, "reason": None}


def next_revision(*, revision: int, idx: int, new_idx: int, impl: str, new_impl: str) -> dict[str, Any]:
    if idx != new_idx or impl != new_impl:
        return {"revision": revision + 1, "previous_valid": False, "replaced_silently": False}
    return {"revision": revision, "previous_valid": True, "replaced_silently": False}


def deadline_disclosure(*, has_onchain_deadline: bool) -> dict[str, Any]:
    if has_onchain_deadline:
        residual = "上鏈當下的狀態仍可能改變"
    else:
        residual = "合約沒有 deadline，本地過期不能保證已簽名交易不會上鏈"
    return {"residual": residual, "claim_stale_cannot_land": False}


def batch_mode(*, capability: str, atomic_required: bool) -> dict[str, Any]:
    if capability not in {"supported", "ready", "unsupported"}:
        raise PolicyError("wallet capability must be supported, ready, or unsupported")
    if capability == "ready":
        return {"mode": "do_not_auto_upgrade", "batch": False, "downgraded": False}
    if atomic_required and capability != "supported":
        return {"mode": "blocked", "batch": False, "downgraded": False}
    if capability == "supported":
        return {
            "mode": "atomic_batch" if atomic_required else "optional_batch",
            "batch": True,
            "downgraded": False,
            "atomicRequired": atomic_required,
        }
    return {"mode": "stepwise", "batch": False, "downgraded": False}


def on_wallet_change(event: str) -> dict[str, Any]:
    if event in {"accountsChanged", "chainChanged"}:
        return {"valid": False, "revision_required": True}
    return {"valid": True, "revision_required": False}


def connect_options(*, form_factor: str) -> dict[str, Any]:
    if form_factor == "phone":
        return {
            "options": ["injected_provider", "wallet_inapp_browser", "open_in_system_browser"],
            "self_qr_only": False,
        }
    return {"options": ["injected_provider", "walletconnect_qr"], "self_qr_only": False}


def deeplink_fallback() -> dict[str, str]:
    return {
        "next": "open_in_browser",
        "reason": "深連結失敗時改以系統瀏覽器開啟同一個任務，不要求手機掃描自己的 QR。",
    }


def press_button(task: dict[str, Any], *, revision: int) -> dict[str, Any]:
    if task.get("revision") == revision and task.get("state") in {
        "WALLET_REQUESTED",
        "SUBMITTED",
        "SUBMISSION_UNKNOWN",
        "INCLUDED",
        "CONFIRMED",
        "COMPLETED",
    }:
        return {"new_submission": False, "reason": "already_requested"}
    if task.get("state") == "USER_REJECTED":
        return {"new_submission": False, "reason": "rejected_no_auto_retry"}
    return {"new_submission": False, "reason": "wallet_requests_disabled"}


def recover_task(*, state: str, found_hash: bool) -> dict[str, Any]:
    if state in {"WALLET_REQUESTED", "SUBMISSION_UNKNOWN"}:
        return {
            "state": "SUBMITTED" if found_hash else "SUBMISSION_UNKNOWN",
            "new_transaction": False,
        }
    return {"state": state, "new_transaction": False}


def step_outcome(steps: list[dict[str, bool]]) -> str:
    oks = [bool(step["ok"]) for step in steps]
    if any(oks) and not all(oks):
        return "PARTIAL"
    if all(oks) and oks:
        return "AWAITING_POSTCONDITIONS"
    return "REVERTED"


def verify_claim_postconditions(
    *,
    request_gone: bool,
    claim_log: dict[str, Any] | None,
    native_delta: int | None,
    gas_used: int | None,
    concurrent_other_txs: bool,
) -> dict[str, Any]:
    if not claim_log:
        return {"verified": False, "reason": "missing_claim_event", "list_disappearance_is_enough": False}
    if concurrent_other_txs or native_delta is None or gas_used is None:
        return {"verified": False, "reason": "native_delta_ambiguous", "list_disappearance_is_enough": False}
    expected = int(claim_log["bnb_raw"]) - gas_used
    if native_delta != expected or not request_gone:
        return {"verified": False, "reason": "effect_mismatch", "list_disappearance_is_enough": False}
    return {"verified": True, "reason": None, "list_disappearance_is_enough": False}


class KillSwitch:
    def __init__(self, writes_enabled: bool) -> None:
        self.writes_enabled = writes_enabled

    def read(self) -> dict[str, bool]:
        return {"ok": True, "writes_enabled": self.writes_enabled}

    def write(self) -> None:
        if not self.writes_enabled:
            raise SecurityError("write actions are switched off")


def client_wallet_request(
    method: str,
    *,
    user_clicked: bool,
    wallet_request_allowed: bool,
    release_enabled: bool,
    mainnet_authorized: bool,
) -> None:
    """Decide whether a user-device client may ask the wallet. This never broadcasts."""
    if method in {"eth_sign", "personal_sign", "eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4"}:
        raise SecurityError("blind and typed-data signatures are not enabled")
    if method not in {"eth_sendTransaction"}:
        raise SecurityError("wallet method is not available")
    if method in FORBIDDEN_RPC_METHODS and method != "eth_sendTransaction":
        raise SecurityError("method is forbidden")
    if not user_clicked or not wallet_request_allowed or not release_enabled or not mainnet_authorized:
        raise SecurityError("wallet request is blocked by policy")
    raise SecurityError("this build has no mainnet wallet release")


def agent_wallet_request(method: str) -> None:
    if method in FORBIDDEN_RPC_METHODS or method == "eth_sendTransaction":
        raise SecurityError("the agent cannot request or broadcast a wallet transaction")
    raise SecurityError("the agent has no wallet session")


def parse_intent(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("type") == "arbitrary_contract_call" or "data" in payload or "calldata" in payload:
        raise SecurityError("arbitrary contract calls are not available")
    if payload.get("protocol_version") != "guardian.intent.v1":
        raise PolicyError("intent protocol version is not supported")
    if payload.get("type") != "lista_claim_withdraw":
        raise PolicyError("only the verified Lista claim intent is enabled")
    if "uuid" not in payload or payload.get("chain_id") != 56:
        raise PolicyError("Lista claim intent needs chain 56 and a stable uuid")
    return {
        "protocol_version": payload["protocol_version"],
        "type": "lista_claim_withdraw",
        "uuid": str(payload["uuid"]),
        "chain_id": 56,
        "wallet_label": payload.get("wallet_label"),
    }


def unstake_alternatives(*, claimable: bool, paused: bool) -> list[dict[str, Any]]:
    if paused:
        return [{"id": "wait", "kind": "wait", "completes_original_redeem": False}]
    if claimable:
        return [{"id": "claim_withdraw", "kind": "protocol_redeem", "completes_original_redeem": True}]
    return [
        {"id": "wait", "kind": "wait", "completes_original_redeem": False},
        {
            "id": "sell_lst",
            "kind": "market_exit",
            "requires_explicit_confirmation": True,
            "completes_original_redeem": False,
        },
    ]


def reorg_correction() -> dict[str, bool]:
    return {"correction_alert": True, "silent": False}
