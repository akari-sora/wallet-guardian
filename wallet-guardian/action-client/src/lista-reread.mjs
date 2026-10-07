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
  unapproved_deployment: "鏈上部署和任務一致，但客戶端沒有核准這個實作版本。",
  approval_retired: "這個部署核准已停用或過期。",
  plan_expired: "計畫的本地有效時間已過。",
  plan_ttl_missing: "計畫沒有可用的本地有效時間，不能算完整通過。",
  approval_unbounded: "核准沒有明確期限，不能算完整通過。",
  block_identity_unsupported: "節點不接受以區塊雜湊鎖定狀態，不能標成同一個區塊。",
  block_not_canonical: "區塊雜湊已不是標準鏈上的區塊。",
  wallet_unverified: "沒有核對到連線錢包。這次只是觀察，不能算通過。",
  provider_disconnected: "錢包連線中斷，先前的重讀作廢。",
  provider_replaced: "錢包提供者已更換，先前的重讀作廢。",
  updating: "正在重讀。先前的結果先不算數。",
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

function view(status, reasons, binding, simulation, deployment = null) {
  return {
    reread: status,
    simulation: simulationEffectStatus(simulation),
    allowSend: "closed",
    reasons,
    binding,
    deployment,
  };
}

/** EIP-1898 tag. State reads use this object and do not fall back to a block number. */
export function canonicalBlockTag(blockHash) {
  if (typeof blockHash !== "string" || !/^0x[0-9a-f]{64}$/.test(blockHash)) {
    throw new Error("block hash required");
  }
  return { blockHash, requireCanonical: true };
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
  const ttlProblem = planTtlProblem(plan, input.nowUnix);
  if (ttlProblem) return view("insufficient", [ttlProblem], null, simulation);
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
  const blockHash = block && typeof block.hash === "string" ? block.hash.toLowerCase() : "";
  const blockNumber = hexQuantity(blockHex);
  const blockTimestamp = block && block.timestamp != null ? hexQuantity(block.timestamp) : null;
  const reportedNumber = block && block.number != null ? hexQuantity(block.number) : null;
  if (!/^0x[0-9a-f]{64}$/.test(blockHash) || blockTimestamp == null || reportedNumber !== blockNumber) {
    return { rejected: false, incoherent: "block_missing", chainId, blockNumber, blockHash, blockTimestamp, reportedNumber };
  }
  const blockTag = canonicalBlockTag(blockHash);
  const implementationWord = await call("eth_getStorageAt", [sources.proxy, sources.implementationSlot, blockTag]);
  const implementation = addressFromStorage(implementationWord);
  const proxyCode = await call("eth_getCode", [sources.proxy, blockTag]);
  const implementationCode = await call("eth_getCode", [implementation, blockTag]);
  const pausedWord = await call("eth_call", [{ to: sources.proxy, data: encodePausedCall() }, blockTag]);
  const requestWord = await call("eth_call", [{ to: sources.proxy, data: encodeWithdrawalRequestsCall(account) }, blockTag]);
  return {
    rejected: false,
    chainId,
    blockHex,
    blockNumber,
    blockHash,
    blockTimestamp,
    reportedNumber,
    blockTag,
    implementation,
    proxyCode,
    implementationCode,
    paused: decodeBool(pausedWord),
    requests: decodeWithdrawalRequests(requestWord),
  };
}

function planTtlProblem(plan, nowUnix) {
  const value = plan ? plan.local_ttl_unix : undefined;
  if (!Number.isSafeInteger(value)) return "plan_ttl_missing";
  if (Number.isSafeInteger(nowUnix) && nowUnix >= value) return "plan_expired";
  return null;
}

function deadlines(input, snap, approvalEntry) {
  const ttl = TRUSTED_LISTA.freshnessTtlSeconds;
  const startedAt = Number.isSafeInteger(input.startedAt) ? input.startedAt : input.nowUnix;
  const marks = [];
  if (Number.isSafeInteger(startedAt)) marks.push(startedAt + ttl);
  if (snap && Number.isSafeInteger(snap.blockTimestamp)) marks.push(snap.blockTimestamp + ttl);
  const planExpiresAt = input.plan && Number.isSafeInteger(input.plan.local_ttl_unix) ? input.plan.local_ttl_unix : null;
  if (planExpiresAt != null) marks.push(planExpiresAt);
  const approvalValidUntil = approvalEntry && Number.isSafeInteger(approvalEntry.validUntilUnix)
    ? approvalEntry.validUntilUnix
    : null;
  if (approvalValidUntil != null) marks.push(approvalValidUntil);
  return {
    startedAt: Number.isSafeInteger(startedAt) ? startedAt : null,
    planExpiresAt,
    blockTimestamp: snap && Number.isSafeInteger(snap.blockTimestamp) ? snap.blockTimestamp : null,
    approvalId: approvalEntry ? approvalEntry.id : null,
    approvalVersion: approvalEntry ? approvalEntry.registryVersion : null,
    approvalValidUntil,
    freshUntil: marks.length ? Math.min(...marks) : input.nowUnix,
  };
}

function bindingFor(input, snap, approvalEntry) {
  const account = (input.connectedAccount ?? input.account);
  const observedAt = input.nowUnix ?? Math.floor(Date.now() / 1000);
  return {
    taskRef: input.task && input.task.ref ? input.task.ref : null,
    revision: input.plan.revision,
    planDigest: input.planDigest,
    account: typeof account === "string" ? account.toLowerCase() : null,
    chainId: snap && snap.chainId ? snap.chainId : (Number.isSafeInteger(input.walletChainId) ? input.walletChainId : null),
    blockNumber: snap ? snap.blockNumber : null,
    blockHash: snap ? snap.blockHash : null,
    observedAt,
    ...deadlines(input, snap, approvalEntry),
  };
}

function walletBoundThisRound(input) {
  return input.walletIdentityVerified === true &&
    sameAddress(input.connectedAccount, input.account) &&
    input.walletChainId === TRUSTED_LISTA.chainId;
}

function approvalsFor(input) {
  if (input && Object.prototype.hasOwnProperty.call(input, "testApprovals")) {
    return Array.isArray(input.testApprovals) ? input.testApprovals : [];
  }
  return TRUSTED_LISTA.approvals;
}

function entryTuple(entry) {
  if (!entry || entry.registryVersion !== TRUSTED_LISTA.approvalRegistryVersion) return null;
  if (typeof entry.id !== "string" || entry.id.length === 0) return null;
  if (typeof entry.provenance !== "string" || entry.provenance.length === 0) return null;
  if (!sameAddress(entry.implementation, entry.implementation)) return null;
  if (!hashString(entry.proxyCodeSha256) || !hashString(entry.implementationCodeSha256)) return null;
  return {
    implementation: entry.implementation,
    proxyCodeSha256: entry.proxyCodeSha256.toLowerCase(),
    implementationCodeSha256: entry.implementationCodeSha256.toLowerCase(),
  };
}

function sameTuple(left, right) {
  return sameAddress(left.implementation, right.implementation) &&
    left.proxyCodeSha256 === right.proxyCodeSha256 &&
    left.implementationCodeSha256 === right.implementationCodeSha256;
}

function entryState(entry, now) {
  if (entry.retired === true) return "retired";
  if (typeof entry.validFromUnix === "number" && now < entry.validFromUnix) return "inactive";
  if (Number.isSafeInteger(entry.validUntilUnix)) {
    return now >= entry.validUntilUnix ? "retired" : "active";
  }
  if (entry.permanent === true && entry.validUntilUnix == null) return "active";
  return "unbounded";
}

function classifyApproval(entries, tuple, now) {
  let retired = false;
  let unbounded = false;
  for (const entry of Array.isArray(entries) ? entries : []) {
    const pinned = entryTuple(entry);
    if (!pinned || !sameTuple(pinned, tuple)) continue;
    const state = entryState(entry, now);
    if (state === "active") return { active: true, retired: false, unbounded: false, entry };
    if (state === "retired") retired = true;
    if (state === "unbounded") unbounded = true;
  }
  return { active: false, retired, unbounded, entry: null };
}

export function blockTagFailure(error) {
  if (!error || error.name === "TimeoutError" || error.name === "AbortError") return null;
  const rpcError = error.rpcError;
  const message = `${error.message || ""} ${rpcError && rpcError.message ? rpcError.message : ""}`;
  if ((rpcError && rpcError.code === -32602) || /invalid params|unsupported/i.test(message)) {
    return "block_identity_unsupported";
  }
  if (/non-canonical|noncanonical|requireCanonical|unknown block|header not found|block not found/i.test(message)) {
    return "block_not_canonical";
  }
  return null;
}

function fail(reasons, input) {
  return view("fail", reasons, bindingFor(input, null), input.plan && input.plan.simulation);
}

function judge(input, snap, located, status) {
  const simulation = input.plan.simulation;
  const reasons = [];
  if (snap.chainId !== TRUSTED_LISTA.chainId) reasons.push("wrong_chain");
  if (!snap.blockTag || snap.blockTag.requireCanonical !== true || snap.blockTag.blockHash !== snap.blockHash) {
    return view("insufficient", ["block_missing"], null, simulation);
  }
  const skew = snap.blockTimestamp - input.nowUnix;
  if (skew > TRUSTED_LISTA.maxClockSkewSeconds) reasons.push("stale_block");
  if (input.nowUnix - snap.blockTimestamp > TRUSTED_LISTA.freshnessTtlSeconds) reasons.push("stale_block");
  if (snap.proxyCode === "0x" || snap.implementationCode === "0x") reasons.push("deployment_unreadable");
  const tuple = {
    implementation: snap.implementation,
    proxyCodeSha256: snap.proxyCodeSha256,
    implementationCodeSha256: snap.implementationCodeSha256,
  };
  const taskDeployment = input.plan.deployment;
  const matchesTask = sameAddress(tuple.implementation, taskDeployment.implementation) &&
    tuple.proxyCodeSha256 === String(taskDeployment.proxy_code_sha256).toLowerCase() &&
    tuple.implementationCodeSha256 === String(taskDeployment.implementation_code_sha256).toLowerCase();
  if (!matchesTask) reasons.push("deployment_changed");
  const approval = classifyApproval(approvalsFor(input), tuple, input.nowUnix);
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
  const binding = bindingFor(input, snap, approval.entry);
  const ttlProblem = planTtlProblem(input.plan, input.nowUnix);
  if (ttlProblem) reasons.push(ttlProblem);
  if (input.nowUnix >= binding.freshUntil && !reasons.includes("stale_block") && !reasons.includes("plan_expired")) {
    reasons.push("stale");
  }
  const deployment = { matchesTask, approvedDeployment: approval.active };
  const soft = new Set(["stale_block", "plan_expired", "plan_ttl_missing", "stale"]);
  const hard = reasons.filter((reason) => !soft.has(reason));
  if (hard.length) return view("fail", reasons, binding, simulation, deployment);
  if (reasons.length) return view("insufficient", reasons, null, simulation, deployment);
  if (!approval.active) {
    const reason = approval.retired ? "approval_retired" : approval.unbounded ? "approval_unbounded" : "unapproved_deployment";
    return view("insufficient", [reason], binding, simulation, { matchesTask, approvedDeployment: false });
  }
  if (!walletBoundThisRound(input)) {
    return view("insufficient", ["wallet_unverified"], binding, simulation, deployment);
  }
  return view("pass", [], binding, simulation, deployment);
}

export async function reverifyListaClaim(input) {
  const startedAt = Number.isSafeInteger(input.startedAt) ? input.startedAt : input.nowUnix;
  const scoped = { ...input, startedAt };
  const blocked = localBlock(scoped);
  if (blocked) return blocked;
  if (typeof scoped.nowUnix !== "number" || !Number.isSafeInteger(scoped.nowUnix)) {
    return view("insufficient", ["missing_fields"], null, input.plan.simulation);
  }
  try {
    const snap = await readAtBlock(input.call, input.account);
    if (snap.rejected) return view("fail", ["wrong_chain"], bindingFor(scoped, snap), input.plan.simulation);
    if (snap.incoherent) return view("insufficient", [snap.incoherent], null, input.plan.simulation);
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
      }, snap.blockTag]);
      status = decodeStatus(word);
    }
    const completedAt = typeof input.clock === "function" ? input.clock() : scoped.nowUnix;
    const judged = { ...scoped, nowUnix: Number.isSafeInteger(completedAt) ? completedAt : scoped.nowUnix };
    return judge(judged, snap, located, status);
  } catch (error) {
    const tagReason = blockTagFailure(error);
    if (tagReason) return view("insufficient", [tagReason], null, input.plan.simulation);
    const name = error && error.name;
    if (name === "TimeoutError" || name === "AbortError") {
      return view("insufficient", ["timeout"], null, input.plan.simulation);
    }
    if (error instanceof Error && /bad |short |unexpected |truncated |word overflow|block hash required/.test(error.message)) {
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
  if (Number.isSafeInteger(binding.planExpiresAt) && context.nowUnix >= binding.planExpiresAt) reasons.push("plan_expired");
  if (Number.isSafeInteger(binding.approvalValidUntil) && context.nowUnix >= binding.approvalValidUntil) {
    reasons.push("approval_retired");
  }
  if (Number.isSafeInteger(binding.blockTimestamp) && context.nowUnix - binding.blockTimestamp > TRUSTED_LISTA.freshnessTtlSeconds) {
    reasons.push("stale_block");
  }
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
