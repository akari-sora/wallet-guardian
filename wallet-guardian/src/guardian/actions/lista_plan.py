"""Build one Lista claimWithdraw plan from a freshly read request list."""

from __future__ import annotations

from typing import Any, Protocol

from eth_abi import encode
from eth_utils import keccak

from guardian.actions.policy import CANONICAL_PLAN_KEYS, unstake_alternatives
from guardian.canonical import digest_body
from guardian.domain import WithdrawalRequest, classify_lista_request, resolve_idx
from guardian.errors import PolicyError

PROTOCOL_VERSION = "guardian.action_plan.v1"
CLAIM_SIGNATURE = "claimWithdraw(uint256)"


class RequestSource(Protocol):
    def read_now(self) -> list[WithdrawalRequest]: ...


def claim_calldata(idx: int) -> str:
    selector = keccak(text=CLAIM_SIGNATURE)[:4]
    return "0x" + (selector + encode(["uint256"], [idx])).hex()


def decode_claim_idx(data: str) -> int:
    raw = bytes.fromhex(data[2:])
    selector = keccak(text=CLAIM_SIGNATURE)[:4]
    if raw[:4] != selector:
        raise PolicyError("calldata is not claimWithdraw(uint256)")
    if len(raw) != 36:
        raise PolicyError("claimWithdraw calldata has an unexpected length")
    return int.from_bytes(raw[4:], "big")


def human_summary(plan_body: dict[str, Any], *, purpose: str, fee_label: str) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "sender": plan_body["sender"],
        "recipient": plan_body["recipient"],
        "chain_id": plan_body["chain_id"],
        "chain_label": "BNB Smart Chain (chain ID 56)",
        "amount_out_raw": plan_body["asset_out"]["raw_amount"],
        "amount_out_unit": plan_body["asset_out"]["symbol"],
        "amount_in_raw": plan_body["asset_in"]["raw_amount"],
        "amount_in_unit": plan_body["asset_in"]["symbol"],
        "fee_label": fee_label,
        "simulation_status": plan_body["simulation"]["status"],
        "wallet_request_allowed": plan_body["wallet_request_allowed"],
    }


def build_lista_claim_plan(
    *,
    source: RequestSource,
    uuid: int,
    sender: str,
    proxy: str,
    implementation: str,
    proxy_code_sha256: str,
    implementation_code_sha256: str,
    paused: bool,
    block_number: int | None,
    block_hash: str | None,
    task_id: str,
    revision: int = 1,
    cached_idx: int | None = None,
    mainnet_authorized: bool = False,
    assisted_enabled: bool = False,
    effects_checked: bool = False,
    state_overrides: bool = False,
    ownership_basis: str = "user_provided_watch_address",
    now_unix: int = 0,
    ttl_seconds: int = 120,
) -> dict[str, Any]:
    requests = source.read_now()
    located = resolve_idx(requests, uuid, cached_idx=cached_idx)
    request = requests[located["idx"]]
    status = classify_lista_request(request, age_days=None, claim_evidence=False)
    calls: list[dict[str, str]] = []
    reasons: list[str] = []
    if paused:
        reasons.append("claimWithdraw 目前處於 pause，不能送出這一步")
        state = "BLOCKED"
    elif status != "CLAIMABLE":
        reasons.append("這筆請求目前不可領，系統只保留等待狀態，不提供可簽名 calldata")
        state = "AWAITING_UNLOCK"
    else:
        if request.bnb_amount <= 0:
            reasons.append("可領狀態沒有正的 BNB 數量，停止規劃")
            state = "BLOCKED"
        else:
            calls = [{"to": proxy, "data": claim_calldata(located["idx"]), "value": "0"}]
            _assert_single_claim(calls, proxy)
            state = "SIMULATED"
    if ownership_basis != "user_wallet_connected_this_session":
        reasons.append("監控地址只是 watch-only，尚未證明這次操作的錢包身份")
    if not assisted_enabled:
        reasons.append("輔助簽名尚未由使用者啟用")
    if not mainnet_authorized:
        reasons.append("這次建置沒有主網交易授權")
    if state_overrides or not effects_checked:
        reasons.append("現有模擬無法核對餘額與領取事件，不能當作可以簽名的證據")
    wallet_request_allowed = False
    if wallet_request_allowed:
        raise PolicyError("this build cannot mark a wallet request as allowed")
    simulation_status = "INCOMPLETE"
    if state_overrides:
        simulation_status = "TEST_ONLY_OVERRIDE"
    elif state == "BLOCKED":
        simulation_status = "FAIL"
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "task_id": task_id,
        "revision": revision,
        "chain_namespace": "evm",
        "chain_id": 56,
        "sender": sender,
        "execution_account": sender,
        "execution_account_type": "unproven",
        "recipient": sender,
        "protocol_id": "lista-slisbnb",
        "deployment": {
            "proxy": proxy,
            "implementation": implementation,
            "proxy_code_sha256": proxy_code_sha256,
            "implementation_code_sha256": implementation_code_sha256,
            "method": CLAIM_SIGNATURE,
        },
        "intent": {"type": "lista_claim_withdraw", "uuid": str(uuid), "idx": located["idx"]},
        "calls": calls,
        "asset_in": {
            "symbol": "slisBNB",
            "raw_amount": str(request.amount_in_slis_bnb),
            "decimals": 18,
        },
        "asset_out": {"symbol": "BNB", "raw_amount": str(request.bnb_amount), "decimals": 18},
        "preconditions": {
            "paused": paused,
            "is_claimable": request.is_claimable,
            "idx_reread": True,
            "stale_cache_rejected": located["stale_cache_rejected"],
            "request_count": len(requests),
        },
        "bounds": {
            "min_out_raw": str(request.bnb_amount) if request.is_claimable else "0",
            "slippage_bps": None,
        },
        "max_fee_wei": None,
        "simulation": {
            "status": simulation_status,
            "source": "eth_call_without_trace" if not state_overrides else "state_override",
            "effects_checked": False,
            "state_overrides": state_overrides,
            "block_number": block_number,
            "block_hash": block_hash,
        },
        "wallet_request_allowed": False,
    }
    summary = human_summary(
        body,
        purpose="領取已解鎖的 Lista 提款" if state == "SIMULATED" else "查看 Lista 提款進度",
        fee_label="費用未知",
    )
    if summary["amount_out_raw"] != body["asset_out"]["raw_amount"] or summary["recipient"] != body["recipient"]:
        raise PolicyError("human summary drifted from the verified plan")
    return {
        **body,
        "digest": digest_body({key: body[key] for key in CANONICAL_PLAN_KEYS}),
        "human_summary": summary,
        "state": state,
        "block_reasons": reasons,
        "alternatives": unstake_alternatives(claimable=request.is_claimable, paused=paused),
        "local_ttl_unix": now_unix + ttl_seconds,
        "onchain_deadline": False,
        "idx_source": "reread",
    }


def _assert_single_claim(calls: list[dict[str, str]], proxy: str) -> None:
    if len(calls) != 1:
        raise PolicyError("Lista claim is a single call")
    call = calls[0]
    if call["to"] != proxy or call["value"] != "0":
        raise PolicyError("claimWithdraw must target the proxy with zero value")
    decode_claim_idx(call["data"])


def verify_saved_plan(plan: dict[str, Any], *, expected_calldata: str | None) -> None:
    from guardian.canonical import digest_body as digest

    body = {key: plan[key] for key in CANONICAL_PLAN_KEYS}
    if digest(body) != plan["digest"]:
        raise PolicyError("plan digest mismatch")
    summary = plan["human_summary"]
    if summary["amount_out_raw"] != plan["asset_out"]["raw_amount"]:
        raise PolicyError("summary amount does not match the plan")
    if summary["recipient"] != plan["recipient"] or summary["sender"] != plan["sender"]:
        raise PolicyError("summary account does not match the plan")
    if plan["calls"] and expected_calldata is not None and plan["calls"][0]["data"] != expected_calldata:
        raise PolicyError("client calldata does not match the fixed adapter")
    if any(call["value"] != "0" for call in plan["calls"]):
        raise PolicyError("unexpected native value")
