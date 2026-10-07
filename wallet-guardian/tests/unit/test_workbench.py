"""Wallet-browser workbench cases. Real-wallet and phone evidence stay UNTESTED."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from guardian.actions.workbench import (
    CLIENT_BUILD,
    TaskDirectory,
    apply_task_event,
    assert_original_account,
    build_lista_task,
    choose_mode,
    evidence_status,
    issue_task_link,
    lista_claim_steps,
    mode_c_decision,
    preflight_lista,
    track_wait,
    workspace_export,
)
from guardian.errors import PolicyError, SecurityError

REF = "ref_" + "ab" * 16
SENDER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


def _plan() -> dict:
    return {
        "sender": SENDER,
        "recipient": SENDER,
        "intent": {"idx": 0, "uuid": "1", "type": "lista_claim_withdraw"},
        "deployment": {"implementation": "0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf"},
        "asset_out": {"raw_amount": "10"},
        "calls": [{"to": "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6", "data": "0xb13acedd", "value": "0"}],
    }


def _task() -> dict:
    return build_lista_task(_plan(), ref=REF)


def _enforcer(fields: list[str] | None = None, **extra):
    base = {
        "verified": True,
        "enforces_on_chain": True,
        "enforced_by": "verified_contract",
        "enforced_fields": list(fields or []),
    }
    base.update(extra)
    return base


def test_w01_link_is_not_execution_authorization():
    link = issue_task_link(REF, "https://wallet.example")
    assert link["execution_authorized"] is False
    assert _task()["execution_authorized"] is False


def test_w02_link_does_not_carry_private_state():
    link = issue_task_link(REF, "https://wallet.example")
    assert link["url"].endswith("/t/" + REF)
    assert "0x" not in link["url"]
    assert link["contains_private_state"] is False
    with pytest.raises(SecurityError):
        issue_task_link(REF, "https://wallet.example/?address=0x1111111111111111111111111111111111111111")


def test_w03_other_tenant_cannot_open_the_ref():
    book = TaskDirectory()
    book.put("tenant-a", _task())
    with pytest.raises(SecurityError):
        book.get("tenant-b", REF)


def test_w04_agent_output_must_be_the_fixed_task_version():
    task = _task()
    assert workspace_export(task)["client_build"] == CLIENT_BUILD
    task["protocol_version"] = "guardian.task.draft"
    with pytest.raises(PolicyError):
        workspace_export(task)


def test_w05_runtime_signing_script_is_rejected():
    task = _task()
    task["runtime_script"] = "ethereum.request({method:'eth_sendTransaction'})"
    with pytest.raises(SecurityError):
        workspace_export(task)


def test_w06_session_secrets_stay_out_of_the_workspace():
    task = _task()
    task["session_key"] = "secret"
    with pytest.raises(SecurityError):
        workspace_export(task)
    task = _task()
    task["note"] = "wc:example-pairing"
    with pytest.raises(SecurityError):
        workspace_export(task)


def test_w07_lista_claim_does_not_request_7702():
    assert all(step["requests_7702"] is False for step in lista_claim_steps())
    assert _task()["requests_7702"] is False


def test_w08_lista_claim_does_not_request_unlimited_approval():
    assert all(step["unlimited_approval"] is False for step in lista_claim_steps())


def test_w09_no_admin_migration_or_upgrade_for_a_single_claim():
    for step in lista_claim_steps():
        assert step["adds_admin"] is False
        assert step["migrates_assets"] is False
        assert step["upgrades_account"] is False


def test_w10_consents_are_separate_and_only_execution_is_required():
    steps = lista_claim_steps()
    assert [step["separate"] for step in steps] == [True] * len(steps)
    required = [step["id"] for step in steps if step["required"]]
    assert required == ["execute"]
    assert {step["id"] for step in steps} >= {
        "connect_wallet",
        "login_signature",
        "erc20_approve",
        "batch_confirm",
        "execution_permission",
        "account_upgrade",
        "execute",
    }


def test_w11_unsupported_batch_falls_back_to_mode_a():
    chosen = choose_mode(
        capability="unsupported",
        same_chain=True,
        call_count=1,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert chosen["mode"] == "A"
    assert chosen["wallet_request_allowed"] is False


def test_w12_supported_multicall_can_select_mode_b_without_sending():
    chosen = choose_mode(
        capability="supported",
        same_chain=True,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert chosen["mode"] == "B"
    assert chosen["wallet_request_allowed"] is False
    assert chosen["real_wallet_evidence"] == "UNTESTED"


def test_w13_cross_chain_or_unknown_capability_does_not_batch():
    cross = choose_mode(
        capability="supported",
        same_chain=False,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    unknown = choose_mode(
        capability="unknown",
        same_chain=True,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert cross["mode"] == "A"
    assert unknown["mode"] == "A"


def test_w14_mode_c_requires_on_chain_enforcement():
    assert mode_c_decision(None)["fallback"] == "A"
    assert mode_c_decision({"verified": True, "enforces_on_chain": True, "enforced_by": "ui", "enforced_fields": []})[
        "reason"
    ] == "off_chain_check_is_not_enforcement"
    full = _enforcer(
        ["task", "chain", "original_account", "contract", "method", "asset", "amount", "recipient", "deadline", "count", "fee"]
    )
    assert mode_c_decision(full)["enabled"] is True


def test_w15_partial_enforcer_cannot_enable_mode_c():
    decision = mode_c_decision(_enforcer(["amount", "recipient"]))
    assert decision["enabled"] is False
    assert "deadline" in decision["missing"]


def test_w16_new_account_cannot_claim_the_original_position():
    with pytest.raises(PolicyError):
        assert_original_account(original=SENDER, proposed_sender=OTHER, position_holder=SENDER)
    with pytest.raises(PolicyError):
        choose_mode(
            capability="supported",
            same_chain=True,
            call_count=1,
            enforcer=None,
            original=SENDER,
            proposed_sender=OTHER,
        )


def test_w17_single_lista_step_stays_on_mode_a_even_if_batch_exists():
    chosen = choose_mode(
        capability="supported",
        same_chain=True,
        call_count=1,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert chosen["mode"] == "A"
    assert chosen["reason"] == "single_step_original_account"


def test_w18_preflight_stops_when_idx_or_deployment_changes():
    stopped = preflight_lista(
        _plan(),
        {"idx": 1, "implementation": _plan()["deployment"]["implementation"], "recipient": SENDER, "amount_out": "10", "claimable": True},
    )
    assert stopped["continue_to_wallet"] is False
    assert "idx" in stopped["changes"]
    upgraded = preflight_lista(
        _plan(),
        {"idx": 0, "implementation": OTHER, "recipient": SENDER, "amount_out": "10", "claimable": True},
    )
    assert "implementation" in upgraded["changes"] and upgraded["revision_required"] is True


def test_w19_amount_recipient_expiry_and_pause_require_a_new_confirmation():
    fresh = preflight_lista(
        _plan(),
        {
            "idx": 0,
            "implementation": _plan()["deployment"]["implementation"],
            "recipient": OTHER,
            "amount_out": "11",
            "expired": True,
            "paused": True,
            "claimable": False,
        },
    )
    assert {"recipient", "amount", "expired", "paused", "not_claimable"} <= set(fresh["changes"])


def test_w20_waiting_period_is_tracked_and_not_skipped():
    waiting = track_wait(claimable=False)
    assert waiting["skip_wait"] is False
    assert waiting["continuation_tracked"] is True
    assert waiting["state"] == "AWAITING_UNLOCK"


def test_w21_rejection_does_not_retry():
    updated = apply_task_event({"state": "WALLET_REQUESTED"}, "user_rejected")
    assert updated["state"] == "USER_REJECTED"
    assert updated["auto_retry"] is False


def test_w22_partial_success_stays_partial():
    assert apply_task_event({"state": "SUBMITTED"}, "partial")["state"] == "PARTIAL"


def test_w23_unknown_submission_does_not_create_another_transaction():
    updated = apply_task_event({"state": "WALLET_REQUESTED"}, "submission_unknown")
    assert updated["state"] == "SUBMISSION_UNKNOWN"
    assert updated["new_transaction"] is False
    assert updated["complete"] is False


def test_w24_replacement_is_not_completion():
    updated = apply_task_event({"state": "SUBMITTED"}, "replaced")
    assert updated["previous_hash_is_completion"] is False
    assert updated["new_hash_is_completion"] is False


def test_w25_duplicate_request_does_not_submit_again():
    updated = apply_task_event({"state": "SUBMITTED"}, "duplicate")
    assert updated["new_transaction"] is False
    assert updated["state"] == "SUBMITTED"


def test_w26_revoke_and_expiry_do_not_recover_assets_or_keep_mode_c():
    expired = apply_task_event({"state": "READY_FOR_USER", "mode": "C"}, "permission_expired")
    assert expired["mode"] == "A" and expired["mode_c_usable"] is False
    revoked = apply_task_event({"state": "READY_FOR_USER"}, "revoke_permission")
    assert revoked["recovers_transferred_assets"] is False


def test_w27_tx_hash_alone_is_not_completion():
    submitted = apply_task_event({"state": "WALLET_REQUESTED"}, "submitted", tx_hash="0xabc")
    assert submitted["complete"] is False
    assert submitted["tx_hash_is_completion"] is False
    with pytest.raises(PolicyError):
        apply_task_event({"state": "SUBMITTED"}, "postconditions_ok", effects_checked=False)
    done = apply_task_event({"state": "SUBMITTED"}, "postconditions_ok", effects_checked=True)
    assert done["complete"] is True


def test_w28_phone_and_mode_evidence_are_separate():
    evidence = evidence_status()
    assert evidence["mobile_wallet_inapp_browser"] == "UNTESTED"
    assert evidence["mode_a_real_wallet"] == "UNTESTED"
    assert evidence["mode_b_real_wallet"] == "UNTESTED"
    assert evidence["mode_c_real_wallet"] == "UNSUPPORTED"


def test_w29_building_the_workbench_does_not_authorize_mainnet():
    evidence = evidence_status()
    assert evidence["mainnet_execution_authorized"] is False
    assert evidence["build_is_execution_authorization"] is False
    assert _task()["wallet_request_allowed"] is False


def test_w30_without_a_fixed_origin_no_public_link_is_issued():
    blocked = issue_task_link(REF, None)
    local = issue_task_link(REF, "http://127.0.0.1:43127")
    assert blocked["issued"] is False
    assert local["issued"] is False
    assert local["url"] is None


def test_w31_ready_capability_does_not_upgrade_the_account():
    chosen = choose_mode(
        capability="ready",
        same_chain=True,
        call_count=2,
        enforcer=None,
        original=SENDER,
        proposed_sender=SENDER,
    )
    assert chosen["mode"] == "A"
    assert chosen["auto_upgrade"] is False
    assert lista_claim_steps()[-2]["id"] == "account_upgrade"
    assert lista_claim_steps()[-2]["required"] is False


def test_catalog_has_workbench_cases():
    names = {name for name in globals() if name.startswith("test_w")}
    assert len(names) == 31


def test_public_sample_task_is_not_authorization():
    path = Path(__file__).resolve().parents[2] / "action-client" / "public" / "sample-task.json"
    task = json.loads(path.read_text(encoding="utf-8"))
    accepted = workspace_export(task)
    assert accepted["mode"] == "A"
    assert accepted["execution_authorized"] is False
    assert accepted["wallet_request_allowed"] is False
    assert accepted["link"]["issued"] is False
    assert accepted["link"]["url"] is None
    assert accepted["plan"]["human_summary"]["purpose"] == "合成範例：領取已解鎖的 Lista 提款"
    assert accepted["plan"]["human_summary"]["fee_label"] == "費用未知"
    assert accepted["plan"]["sender"] == "0x1111111111111111111111111111111111111111"
    assert accepted["client_build"] == CLIENT_BUILD


def test_cli_workbench_reports_untested_evidence():
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "guardian.cli", "workbench", "--json"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["wallet_request_allowed"] is False
    assert payload["default_mode"] == "A"
    assert payload["evidence"]["mobile_wallet_inapp_browser"] == "UNTESTED"
    assert payload["evidence"]["mode_a_real_wallet"] == "UNTESTED"
    assert payload["evidence"]["mode_b_real_wallet"] == "UNTESTED"
    assert payload["evidence"]["mode_c_real_wallet"] == "UNSUPPORTED"
    assert payload["evidence"]["mainnet_execution_authorized"] is False
    assert payload["link_is_execution_authorization"] is False
    assert payload["runtime_signing_js"] is False
