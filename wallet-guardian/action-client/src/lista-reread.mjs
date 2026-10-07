/** Independent read-only rebuild of one Lista claimWithdraw. Not send authority. */

import { simulationAllowsSend } from "./gate.mjs";
import {
  CLAIM_SIGNATURE,
  TRUSTED_LISTA,
  addressFromStorage,
  claimCalldata,
  codeSha256,
  decodeBool,
  decodeStatus,
  decodeWithdrawalRequests,
  encodePausedCall,
  encodeRequestStatusCall,
  encodeWithdrawalRequestsCall,
  hexQuantity,
  sameAddress,
  trustedQuerySources,
} from "./trusted-lista.mjs";

const REREAD_LABEL = { pass: "通過", fail: "失敗", insufficient: "資料不足" };
const REREAD_EN = { pass: "pass", fail: "fail", insufficient: "insufficient" };
const SIMULATION_LABEL = { pass: "通過", fail: "失敗", not_done: "未做" };
const SIMULATION_EN = { pass: "pass", fail: "fail", not_done: "not done" };

export const REASON_TEXT = Object.freeze({
  unbound: "還沒有綁定這次任務的鏈上觀察。",
  missing_fields: "計畫缺少部署或數量欄位，證據不足。",
  timeout: "鏈上讀取逾時，不能當成通過。",
  rpc_failed: "鏈上讀取失敗，證據不足。",
  undecodable: "鏈上回應無法依信任的 ABI 解讀。",
  stale_block: "區塊時間超出新鮮度，這次重讀不能算數。",
  stale: "這次觀察已超過有效時間。",
  block_missing: "沒有同一個區塊的編號與雜湊。",
  wrong_chain: "鏈不是客戶端信任的 BNB Smart Chain。",
  account_changed: "帳戶與原地址不一致，或帳戶已變更。",
  chain_changed: "鏈已變更，先前的重讀作廢。",
  revision_changed: "任務版本已變更，先前的重讀作廢。",
  digest_changed: "計畫內容已變更，先前的重讀作廢。",
  task_changed: "任務參考碼已變更，先前的重讀作廢。",
  claimable_missing: "鏈上沒有這筆可領請求。",
  not_claimable: "這筆請求目前不可領。",
  index_drift: "提款索引和計畫不一致。",
  uuid_not_unique: "同一個 uuid 出現超過一次，不能用金額或舊索引對。",
  amount_drift: "數量和鏈上請求不一致。",
  paused: "claimWithdraw 目前暫停。",
  recipient_changed: "接收地址不是原地址。",
  not_single_step: "不是原地址的單一步驟 claimWithdraw。",
  calldata_mismatch: "calldata 與重算的 claimWithdraw 不一致。",
  deployment_changed: "實作或程式碼與計畫記載的部署不一致。",
  deployment_unreadable: "合約程式碼無法核對。",
  mode_unsupported: "模式 B／C 不在這次唯讀重讀範圍。",
  digest_mismatch: "計畫摘要對不上。",
});

export function simulationEffectStatus(simulation) {
  if (!simulation || typeof simulation !== "object") return "not_done";
  if (simulation.status === "FAIL") return "fail";
  if (simulationAllowsSend(simulation)) return "pass";
  return "not_done";
}

/**
 * @typedef {{
 *   reread: string,
 *   simulation: string,
 *   allowSend: "closed",
 *   reasons: string[],
 *   binding: { chainId?: number, account?: string, planDigest?: string, taskRef?: string | null, revision?: number | null, freshUntil?: number } | null
 * }} RereadView
 */

/** @param {object | undefined} simulation @returns {RereadView} */
export function emptyRereadView(simulation) {
  return {
    reread: "insufficient",
    simulation: simulationEffectStatus(simulation),
    allowSend: "closed",
    reasons: [],
    binding: null,
  };
}

export function statusLines(view) {
  if (!view || view.allowSend !== "closed") throw new Error("allow send stays closed");
  if (!REREAD_LABEL[view.reread] || !SIMULATION_LABEL[view.simulation]) throw new Error("unknown status");
  return {
    reread: `獨立鏈上重讀：${REREAD_LABEL[view.reread]}（${REREAD_EN[view.reread]}）`,
    simulation: `交易效果模擬：${SIMULATION_LABEL[view.simulation]}（${SIMULATION_EN[view.simulation]}）`,
    send: "允許實際送出：關閉（closed）",
  };
}

function view(status, reasons, binding, simulation) {
  return {
    reread: status,
    simulation: simulationEffectStatus(simulation),
    allowSend: "closed",
    reasons,
    binding,
  };
}

function uintString(value) {
  return typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value) && BigInt(value) < (1n << 256n);
}

function hashString(value) {
  return typeof value === "string" && /^[0-9a-fA-F]{64}$/.test(value);
}

/** Locate the uuid in this read. The plan's cached idx is not an input. */
export function locateWithdrawal(requests, uuid) {
  if (!Array.isArray(requests) || !uintString(String(uuid))) return { kind: "undecodable", statusIndexes: [] };
  const want = BigInt(uuid);
  const matches = requests.filter((row) => uintString(row.uuid) && BigInt(row.uuid) === want);
  if (matches.length === 0) return { kind: "missing", statusIndexes: [] };
  if (matches.length > 1) return { kind: "duplicate", statusIndexes: [] };
  return { kind: "one", idx: matches[0].idx, row: matches[0], statusIndexes: [matches[0].idx] };
}

function localBlock(input) {
  const { plan, task, account, connectedAccount, walletChainId, planDigest } = input;
  const simulation = plan && plan.simulation;
  if (!plan || typeof plan !== "object") return view("insufficient", ["missing_fields"], null, simulation);
  if (!sameAddress(account, account)) return view("insufficient", ["missing_fields"], null, simulation);
  if (!sameAddress(plan.sender, plan.recipient) || !sameAddress(plan.sender, plan.execution_account)) {
    return fail(["recipient_changed"], input);
  }
  if (!sameAddress(account, plan.sender) || (connectedAccount && !sameAddress(connectedAccount, account))) {
    return fail(["account_changed"], input);
  }
  if (walletChainId != null && walletChainId !== TRUSTED_LISTA.chainId) {
    return fail(["wrong_chain"], input);
  }
  if (task && task.mode && task.mode !== "A") return fail(["mode_unsupported"], input);
  if (plan.protocol_id !== TRUSTED_LISTA.protocolId || plan.chain_id !== TRUSTED_LISTA.chainId) {
    return fail(["wrong_chain"], input);
  }
  if (typeof planDigest !== "string" || plan.digest !== planDigest) {
    return fail(["digest_mismatch"], input);
  }
  if (!Number.isSafeInteger(plan.revision)) return view("insufficient", ["missing_fields"], null, simulation);
  if (input.revision != null && input.revision !== plan.revision) {
    return view("insufficient", ["revision_changed"], null, simulation);
  }
  const deployment = plan.deployment;
  const sources = trustedQuerySources(task ?? plan);
  if (!deployment || deployment.method !== CLAIM_SIGNATURE || !sameAddress(deployment.proxy, sources.proxy)) {
    return fail(["deployment_changed"], input);
  }
  if (!sameAddress(deployment.implementation, deployment.implementation) ||
      !hashString(deployment.proxy_code_sha256) || !hashString(deployment.implementation_code_sha256)) {
    return view("insufficient", ["missing_fields"], null, simulation);
  }
  if (!plan.intent || plan.intent.type !== "lista_claim_withdraw" || !uintString(String(plan.intent.uuid)) ||
      !Number.isSafeInteger(plan.intent.idx) || plan.intent.idx < 0) {
    return view("insufficient", ["missing_fields"], null, simulation);
  }
  if (!plan.asset_in || !plan.asset_out || !uintString(plan.asset_in.raw_amount) || !uintString(plan.asset_out.raw_amount)) {
    return view("insufficient", ["missing_fields"], null, simulation);
  }
  if (!Array.isArray(plan.calls) || plan.calls.length !== 1) return fail(["not_single_step"], input);
  const call = plan.calls[0];
  let expected;
  try {
    expected = claimCalldata(plan.intent.idx);
  } catch {
    return fail(["calldata_mismatch"], input);
  }
  if (!call || !sameAddress(call.to, sources.proxy) || call.value !== "0" ||
      String(call.data).toLowerCase() !== expected) {
    return fail(["calldata_mismatch"], input);
  }
  return null;
}

async function readAtBlock(call, account) {
  const sources = trustedQuerySources(null);
  const chainHex = await call("eth_chainId", []);
  const chainId = hexQuantity(chainHex);
  if (chainId !== TRUSTED_LISTA.chainId) return { rejected: true, chainId };
  const blockHex = await call("eth_blockNumber", []);
  const block = await call("eth_getBlockByNumber", [blockHex, false]);
  const implementationWord = await call("eth_getStorageAt", [sources.proxy, sources.implementationSlot, blockHex]);
  const implementation = addressFromStorage(implementationWord);
  const proxyCode = await call("eth_getCode", [sources.proxy, blockHex]);
  const implementationCode = await call("eth_getCode", [implementation, blockHex]);
  const pausedWord = await call("eth_call", [{ to: sources.proxy, data: encodePausedCall() }, blockHex]);
  const requestWord = await call("eth_call", [{ to: sources.proxy, data: encodeWithdrawalRequestsCall(account) }, blockHex]);
  return {
    rejected: false,
    chainId,
    blockHex,
    blockNumber: hexQuantity(blockHex),
    blockHash: block && typeof block.hash === "string" ? block.hash.toLowerCase() : "",
    blockTimestamp: block && block.timestamp != null ? hexQuantity(block.timestamp) : null,
    reportedNumber: block && block.number != null ? hexQuantity(block.number) : null,
    implementation,
    proxyCode,
    implementationCode,
    paused: decodeBool(pausedWord),
    requests: decodeWithdrawalRequests(requestWord),
  };
}

function bindingFor(input, snap) {
  const account = (input.connectedAccount ?? input.account).toLowerCase();
  const observedAt = input.nowUnix ?? Math.floor(Date.now() / 1000);
  return {
    taskRef: input.task && input.task.ref ? input.task.ref : null,
    revision: input.plan.revision,
    planDigest: input.planDigest,
    account,
    chainId: snap && snap.chainId ? snap.chainId : (input.walletChainId ?? TRUSTED_LISTA.chainId),
    blockNumber: snap ? snap.blockNumber : null,
    blockHash: snap ? snap.blockHash : null,
    observedAt,
    freshUntil: observedAt + TRUSTED_LISTA.freshnessTtlSeconds,
  };
}

function fail(reasons, input) {
  return view("fail", reasons, bindingFor(input, null), input.plan && input.plan.simulation);
}

function judge(input, snap, located, status) {
  const simulation = input.plan.simulation;
  const reasons = [];
  if (snap.chainId !== TRUSTED_LISTA.chainId) reasons.push("wrong_chain");
  if (!snap.blockHash || !/^0x[0-9a-f]{64}$/.test(snap.blockHash) || snap.blockTimestamp == null ||
      snap.reportedNumber !== snap.blockNumber) {
    return view("insufficient", ["block_missing"], null, simulation);
  }
  const skew = snap.blockTimestamp - input.nowUnix;
  if (skew > TRUSTED_LISTA.maxClockSkewSeconds) reasons.push("stale_block");
  if (input.nowUnix - snap.blockTimestamp > TRUSTED_LISTA.freshnessTtlSeconds) reasons.push("stale_block");
  if (snap.proxyCode === "0x" || snap.implementationCode === "0x") reasons.push("deployment_unreadable");
  if (!sameAddress(snap.implementation, input.plan.deployment.implementation)) reasons.push("deployment_changed");
  if (snap.proxyCodeSha256 !== input.plan.deployment.proxy_code_sha256.toLowerCase() ||
      snap.implementationCodeSha256 !== input.plan.deployment.implementation_code_sha256.toLowerCase()) {
    reasons.push("deployment_changed");
  }
  if (snap.paused === true) reasons.push("paused");
  if (located.kind === "missing") reasons.push("claimable_missing");
  if (located.kind === "duplicate") reasons.push("uuid_not_unique");
  if (located.kind === "undecodable") reasons.push("undecodable");
  if (located.kind === "one") {
    if (located.idx !== input.plan.intent.idx) reasons.push("index_drift");
    if (located.row.amountIn !== input.plan.asset_in.raw_amount) reasons.push("amount_drift");
    if (!status) reasons.push("claimable_missing");
    else if (!status.isClaimable) reasons.push("not_claimable");
    else if (status.bnbAmount === "0") reasons.push("claimable_missing");
    else if (status.bnbAmount !== input.plan.asset_out.raw_amount) reasons.push("amount_drift");
    const rebuilt = claimCalldata(located.idx);
    if (String(input.plan.calls[0].data).toLowerCase() !== rebuilt) reasons.push("calldata_mismatch");
  }
  const stale = reasons.includes("stale_block");
  const hard = reasons.filter((reason) => reason !== "stale_block");
  if (reasons.length === 0) return view("pass", [], bindingFor(input, snap), simulation);
  if (stale && hard.length === 0) return view("insufficient", reasons, null, simulation);
  if (hard.length === 0) return view("insufficient", reasons, null, simulation);
  return view("fail", reasons, bindingFor(input, snap), simulation);
}

export async function reverifyListaClaim(input) {
  const blocked = localBlock(input);
  if (blocked) return blocked;
  if (typeof input.nowUnix !== "number" || !Number.isSafeInteger(input.nowUnix)) {
    return view("insufficient", ["missing_fields"], null, input.plan.simulation);
  }
  try {
    const snap = await readAtBlock(input.call, input.account);
    if (snap.rejected) return view("fail", ["wrong_chain"], bindingFor(input, snap), input.plan.simulation);
    snap.proxyCodeSha256 = await codeSha256(snap.proxyCode);
    snap.implementationCodeSha256 = await codeSha256(snap.implementationCode);
    const located = locateWithdrawal(snap.requests, input.plan.intent.uuid);
    let status = null;
    if (located.kind === "one") {
      if (located.statusIndexes.length !== 1 || located.statusIndexes[0] !== located.idx) {
        return view("insufficient", ["undecodable"], null, input.plan.simulation);
      }
      const word = await input.call("eth_call", [{
        to: trustedQuerySources(null).proxy,
        data: encodeRequestStatusCall(input.account, located.idx),
      }, snap.blockHex]);
      status = decodeStatus(word);
    }
    return judge(input, snap, located, status);
  } catch (error) {
    const name = error && error.name;
    if (name === "TimeoutError" || name === "AbortError") {
      return view("insufficient", ["timeout"], null, input.plan.simulation);
    }
    if (error instanceof Error && /bad |short |unexpected |truncated |word overflow/.test(error.message)) {
      return view("insufficient", ["undecodable"], null, input.plan.simulation);
    }
    return view("insufficient", ["rpc_failed"], null, input.plan.simulation);
  }
}

/** @returns {RereadView} */
export function settleReread(observation, context) {
  const simulation = simulationEffectStatus(context && context.simulation);
  if (!observation || !observation.binding || (observation.reread !== "pass" && observation.reread !== "fail")) {
    return {
      reread: "insufficient",
      simulation,
      allowSend: "closed",
      reasons: observation && Array.isArray(observation.reasons) ? observation.reasons : ["unbound"],
      binding: null,
    };
  }
  const binding = observation.binding;
  if (typeof binding.freshUntil !== "number" || typeof context.nowUnix !== "number") {
    return { reread: "insufficient", simulation, allowSend: "closed", reasons: ["unbound"], binding: null };
  }
  const reasons = [];
  if (context.nowUnix >= binding.freshUntil) reasons.push("stale");
  if (!sameAddress(context.account, binding.account)) reasons.push("account_changed");
  if (context.chainId !== binding.chainId) reasons.push("chain_changed");
  if (context.planDigest !== binding.planDigest) reasons.push("digest_changed");
  if ((context.taskRef ?? null) !== binding.taskRef) reasons.push("task_changed");
  if (context.revision !== binding.revision) reasons.push("revision_changed");
  if (reasons.length) {
    return { reread: "insufficient", simulation, allowSend: "closed", reasons, binding: null, invalidated: true };
  }
  return {
    reread: observation.reread,
    simulation,
    allowSend: "closed",
    reasons: observation.reasons ?? [],
    binding,
  };
}
