"""Simulated Mode A lifecycle and EIP-5792 fallback. No wallet and no mainnet send."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from guardian.actions.policy import agent_wallet_request
from guardian.actions.workbench import (
    apply_task_event,
    confirm_gate,
    eip5792_status,
    mode_c_decision,
    preflight_lista,
    select_execution_mode,
)
from guardian.errors import PolicyError, SecurityError
from guardian.service import etherscan_chainlist_probe, load_local_env

SENDER = "0x1111111111111111111111111111111111111111"
IMPL = "0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf"
ROOT = Path(__file__).resolve().parents[2]


def _plan() -> dict:
    return {
        "sender": SENDER,
        "recipient": SENDER,
        "intent": {"idx": 0, "uuid": "1", "type": "lista_claim_withdraw"},
        "deployment": {"implementation": IMPL},
        "asset_out": {"raw_amount": "10"},
        "calls": [{"to": "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6", "data": "0xb13acedd", "value": "0"}],
    }


def _fresh(**overrides):
    base = {
        "idx": 0,
        "implementation": IMPL,
        "recipient": SENDER,
        "amount_out": "10",
        "claimable": True,
        "paused": False,
        "expired": False,
    }
    base.update(overrides)
    return base


def test_mode_a_button_requires_user_confirmation_and_preflight():
    matched = preflight_lista(_plan(), _fresh())
    assert matched["preflight_ok"] is True
    assert matched["continue_to_wallet"] is False
    closed = confirm_gate(
        requires_user_wallet_confirmation=False,
        preflight_ok=True,
        mainnet_send=True,
    )
    assert closed["button_enabled"] is False
    stale = preflight_lista(_plan(), _fresh(idx=1))
    blocked = confirm_gate(
        requires_user_wallet_confirmation=True,
        preflight_ok=stale["preflight_ok"],
        mainnet_send=True,
    )
    assert blocked["button_enabled"] is False
    assert blocked["reason"] == "preflight_not_ok"
    armed = confirm_gate(
        requires_user_wallet_confirmation=True,
        preflight_ok=True,
        mainnet_send=False,
    )
    assert armed["button_enabled"] is True
    assert armed["send_allowed"] is False
    assert armed["reason"] == "mainnet_send_disabled"
    assert armed["agent_may_sign"] is False


def test_mainnet_send_flag_does_not_let_the_agent_sign(monkeypatch):
    monkeypatch.setenv("GUARDIAN_MAINNET_SEND", "true")
    gate = confirm_gate(requires_user_wallet_confirmation=True, preflight_ok=True, mainnet_send=True)
    assert gate["send_allowed"] is True
    assert gate["agent_may_sign"] is False
    with pytest.raises(SecurityError):
        agent_wallet_request("eth_sendTransaction")
    with pytest.raises(SecurityError):
        agent_wallet_request("wallet_sendCalls")


def test_simulated_reject_unknown_replaced_and_completion():
    rejected = apply_task_event({"state": "READY_FOR_USER"}, "user_rejected")
    assert rejected["state"] == "USER_REJECTED"
    assert rejected["auto_retry"] is False
    assert rejected["complete"] is False
    unknown = apply_task_event({"state": "WALLET_REQUESTED"}, "submission_unknown")
    assert unknown["state"] == "SUBMISSION_UNKNOWN"
    assert unknown["new_transaction"] is False
    replaced = apply_task_event({"state": "SUBMITTED", "tx_hash": "0xold"}, "replaced")
    assert replaced["previous_hash_is_completion"] is False
    assert replaced["new_hash_is_completion"] is False
    assert replaced["complete"] is False
    submitted = apply_task_event({"state": "WALLET_REQUESTED"}, "submitted", tx_hash="0xabc")
    assert submitted["complete"] is False
    assert submitted["tx_hash_is_completion"] is False
    with pytest.raises(PolicyError):
        apply_task_event(submitted, "postconditions_ok", effects_checked=False)
    done = apply_task_event(submitted, "postconditions_ok", effects_checked=True)
    assert done["complete"] is True
    assert done["state"] == "POSTCONDITIONS_VERIFIED"
    assert done["tx_hash_is_completion"] is False


def test_unsupported_eip5792_falls_back_to_mode_a():
    capabilities = {"0x38": {"atomic": {"status": "unsupported"}}}
    assert eip5792_status(capabilities, 56) == "unsupported"
    chosen = select_execution_mode(
        capabilities=capabilities,
        chain_id=56,
        same_chain=True,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert chosen["mode"] == "A"
    assert chosen["agent_may_sign"] is False
    other_chain = select_execution_mode(
        capabilities={"0x1": {"atomic": {"status": "supported"}}},
        chain_id=56,
        same_chain=True,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert other_chain["mode"] == "A"
    assert other_chain["capability"] == "unsupported"


def test_supported_batch_selects_b_without_sending_and_single_step_stays_a():
    capabilities = {"0x38": {"atomic": {"status": "supported"}}}
    batch = select_execution_mode(
        capabilities=capabilities,
        chain_id=56,
        same_chain=True,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert batch["mode"] == "B"
    assert batch["wallet_request_allowed"] is False
    single = select_execution_mode(
        capabilities=capabilities,
        chain_id=56,
        same_chain=True,
        call_count=1,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert single["mode"] == "A"
    assert single["reason"] == "single_step_original_account"
    ready = select_execution_mode(
        capabilities={"0x38": {"atomic": {"status": "ready"}}},
        chain_id=56,
        same_chain=True,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert ready["mode"] == "A"
    assert ready["auto_upgrade"] is False


def test_claimed_enforcer_cannot_enable_mode_c():
    decision = mode_c_decision(
        {
            "verified": True,
            "enforces_on_chain": True,
            "enforced_by": "verified_contract",
            "enforced_fields": [
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
            ],
        }
    )
    assert decision["enabled"] is False
    chosen = select_execution_mode(
        capabilities={"0x38": {"atomic": {"status": "supported"}}},
        chain_id=56,
        same_chain=True,
        call_count=1,
        enforcer={
            "verified": True,
            "enforces_on_chain": True,
            "enforced_by": "verified_contract",
            "enforced_fields": ["amount"],
        },
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert chosen["mode"] == "A"


def test_env_local_configures_etherscan_without_leaking(tmp_path, monkeypatch):
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.delenv("GUARDIAN_MAINNET_SEND", raising=False)
    secret = "unit-test-not-a-real-key"
    env_file = tmp_path / ".env.local"
    env_file.write_text(f'ETHERSCAN_API_KEY="{secret}"\nGUARDIAN_MAINNET_SEND=false\n', encoding="utf-8")
    loaded = load_local_env(env_file)
    assert loaded == ["ETHERSCAN_API_KEY", "GUARDIAN_MAINNET_SEND"]
    assert os.environ["ETHERSCAN_API_KEY"] == secret
    seen: dict[str, str] = {}

    def get_text(url: str):
        seen["url"] = url
        body = {"result": [{"chainname": "BNB Smart Chain"}, {"chainname": "Sepolia Testnet"}]}
        return 200, json.dumps(body)

    probe = etherscan_chainlist_probe(get_text)
    assert probe["level"] == "LIVE_PASS"
    assert probe["authenticated"] is True
    assert probe["reported_rows"] == 2
    assert secret in seen["url"]
    assert secret not in json.dumps(probe)
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    skipped = etherscan_chainlist_probe(lambda _url: (_ for _ in ()).throw(AssertionError("must not query")))
    assert skipped["level"] == "LIMITED_DISCOVERY"
    assert skipped["queried"] is False


def test_client_gate_matches_python():
    completed = subprocess.run(
        ["node", "src/gate.test.mjs"],
        cwd=ROOT / "action-client",
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
