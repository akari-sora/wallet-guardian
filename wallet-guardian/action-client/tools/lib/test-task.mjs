/**
 * Synthetic, clearly-labelled, non-executable TEST task for the private-task acceptance check.
 * It is not a real Lista position, carries no call, and cannot open send.
 */
import { readFileSync } from "node:fs";
import { digestBody } from "../../src/canonical.mjs";
import { validatePlan } from "../../src/plan-validation.mjs";
import { checksumAddress } from "../../src/siwe-message.mjs";

const SAMPLE = new URL("../../public/sample-task.json", import.meta.url);

export async function buildTestTask(wallet, { label = "D1-LIVE-01", now = Math.floor(Date.now() / 1000) } = {}) {
  const sender = checksumAddress(wallet);
  if (!sender) throw new Error("invalid wallet address");
  const task = JSON.parse(readFileSync(SAMPLE, "utf8"));
  delete task.ref;
  task.mode = "A";
  task.mode_reason = "fallback_to_guided_confirmation";
  task.execution_authorized = false;
  task.wallet_request_allowed = false;
  task.test_task = {
    purpose: "private_task_acceptance",
    label,
    executable: false,
    real_position: false,
    note: "測試任務：不可執行，不代表真實 Lista 部位或可領取狀態。只驗證登入、查看、取消、登出。",
  };
  for (const step of task.steps) {
    if (step.id === "execute") {
      step.required = false;
      step.status = "not_required";
      step.label = "測試任務不可執行";
    }
  }
  const plan = task.plan;
  plan.notice = {
    synthetic: true,
    test_task: true,
    notice: "測試任務（不可執行）。這不是真實餘額，也不是可簽名交易。",
  };
  plan.task_id = `${label.toLowerCase()}-test-${now}`;
  plan.revision = 1;
  plan.sender = sender;
  plan.execution_account = sender;
  plan.recipient = sender;
  plan.execution_account_type = "unproven";
  plan.intent = { type: "lista_claim_withdraw", uuid: "0", idx: 0 };
  plan.calls = [];
  plan.asset_in = { symbol: "slisBNB", raw_amount: "0", decimals: 18 };
  plan.asset_out = { symbol: "BNB", raw_amount: "0", decimals: 18 };
  plan.preconditions = {
    paused: false,
    is_claimable: false,
    idx_reread: false,
    stale_cache_rejected: false,
    request_count: 0,
  };
  plan.bounds = { min_out_raw: "0", slippage_bps: null };
  plan.max_fee_wei = null;
  plan.simulation = {
    status: "INCOMPLETE",
    source: "none_test_task",
    effects_checked: false,
    state_overrides: false,
    block_number: null,
    block_hash: null,
  };
  plan.wallet_request_allowed = false;
  plan.state = "BLOCKED";
  plan.block_reasons = [
    `${label} 測試任務：不可執行，只驗證私人任務查看、取消與登出`,
    "這次建置沒有主網交易授權",
    "沒有領取呼叫，也沒有模擬證據",
  ];
  plan.alternatives = [];
  plan.human_summary = {
    purpose: `${label} 測試任務（不可執行）`,
    sender,
    recipient: sender,
    chain_id: 56,
    chain_label: "BNB Smart Chain (chain ID 56)",
    amount_out_raw: "0",
    amount_out_unit: "BNB",
    amount_in_raw: "0",
    amount_in_unit: "slisBNB",
    fee_label: "費用未知",
    simulation_status: "INCOMPLETE",
    wallet_request_allowed: false,
  };
  plan.digest = await digestBody(plan);
  validatePlan(plan);
  return task;
}
