/** Offline format/policy validation, NOT origin authentication or live simulation. */
export const LISTA_PROXY = "0x1adb950d8bb3da4be104211d5ab038628e477fe6";
const ADDRESS = /^0x[a-fA-F0-9]{40}$/;
const SECRET_KEYS = new Set(["privatekey", "mnemonic", "seedphrase", "sessionkey", "sessionsecret",
  "walletconnecturi", "pairinguri", "executablejs", "runtimescript"]);

function check(ok, message) {
  if (!ok) throw new Error(message);
}

export function assertInertPayload(value, depth = 0) {
  check(depth <= 32, "JSON 巢狀深度超過限制。");
  if (Array.isArray(value)) {
    for (const item of value) assertInertPayload(item, depth + 1);
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      check(!SECRET_KEYS.has(key.replace(/[_-]/g, "").toLowerCase()), "資料包含秘密或可執行腳本。");
      assertInertPayload(item, depth + 1);
    }
  } else if (typeof value === "number") {
    check(Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value)), "JSON 數字失去精度或不是有限值。");
  } else if (typeof value === "string") {
    check(!value.trimStart().toLowerCase().startsWith("wc:"), "不能匯入錢包連線憑證。");
  }
}

function rawAmount(raw) {
  check(typeof raw === "string" && /^(0|[1-9][0-9]{0,77})$/.test(raw), "數量不是有效整數。");
  check(BigInt(raw) < (1n << 256n), "數量超出 uint256。");
  return raw;
}

export function units(raw, decimals) {
  rawAmount(raw);
  check(Number.isInteger(decimals) && decimals >= 0 && decimals <= 255, "代幣精度不合法。");
  if (decimals === 0) return raw;
  const padded = raw.padStart(decimals + 1, "0");
  const whole = padded.slice(0, -decimals);
  const fraction = padded.slice(-decimals).replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole;
}

export function validatePlan(plan) {
  assertInertPayload(plan);
  check(plan && typeof plan === "object", "計畫必須是物件。");
  check(plan.protocol_version === "guardian.action_plan.v1" && plan.protocol_id === "lista-slisbnb",
    "不支援的協議計畫。");
  check(plan.chain_namespace === "evm" && plan.chain_id === 56, "鏈不符合固定解析器。");
  check(plan.wallet_request_allowed === false, "此建置不允許錢包請求。");
  check(ADDRESS.test(plan.sender) && ADDRESS.test(plan.recipient) && ADDRESS.test(plan.execution_account),
    "帳戶格式不正確。");
  check(plan.sender.toLowerCase() === plan.recipient.toLowerCase() &&
    plan.sender.toLowerCase() === plan.execution_account.toLowerCase(), "原帳戶與接收地址不一致。");
  const deployment = plan.deployment;
  check(deployment && typeof deployment.proxy === "string" && deployment.proxy.toLowerCase() === LISTA_PROXY &&
    deployment.method === "claimWithdraw(uint256)", "合約或方法不符合固定解析器。");
  check(plan.intent?.type === "lista_claim_withdraw" && Number.isSafeInteger(plan.intent.idx) && plan.intent.idx >= 0,
    "無效的提款索引。");
  rawAmount(plan.intent.uuid);
  for (const [asset, symbol] of [[plan.asset_in, "slisBNB"], [plan.asset_out, "BNB"]]) {
    check(asset && asset.symbol === symbol && asset.decimals === 18, "資產單位不符合固定解析器。");
    rawAmount(asset.raw_amount);
  }
  check(Array.isArray(plan.calls) && plan.calls.length <= 1, "只支援單一步驟領取。");
  if (plan.calls.length) {
    const call = plan.calls[0];
    const expected = "0xb13acedd" + BigInt(plan.intent.idx).toString(16).padStart(64, "0");
    check(call && typeof call.to === "string" && call.to.toLowerCase() === LISTA_PROXY &&
      call.data === expected && call.value === "0", "calldata、目標或原生幣金額不符合固定解析器。");
    check(plan.preconditions?.is_claimable === true && plan.preconditions?.paused === false &&
      BigInt(plan.asset_out.raw_amount) > 0n, "阻塞中的計畫不能帶領取呼叫。");
  }
  const summary = plan.human_summary;
  check(summary && summary.sender === plan.sender && summary.recipient === plan.recipient &&
    summary.chain_id === 56 && summary.chain_label === "BNB Smart Chain (chain ID 56)" &&
    summary.amount_in_raw === plan.asset_in.raw_amount && summary.amount_in_unit === "slisBNB" &&
    summary.amount_out_raw === plan.asset_out.raw_amount && summary.amount_out_unit === "BNB" &&
    summary.wallet_request_allowed === false, "畫面摘要與計畫不一致。");
  check(plan.simulation && summary.simulation_status === plan.simulation.status &&
    plan.simulation.effects_checked === false &&
    ["INCOMPLETE", "FAIL", "TEST_ONLY_OVERRIDE"].includes(plan.simulation.status), "不可宣稱已驗證模擬成功。");
  check(plan.max_fee_wei === null && summary.fee_label === "費用未知", "未驗證費用不能顯示為免費或已核准。");
}

export function assertTaskRef(payload, ref) {
  check(payload?.protocol_version === "guardian.task.v1" && payload.ref === ref, "回傳任務與要求的參考碼不符。");
}

export async function boundedJson(response, limit = 1024 * 1024) {
  check(response.ok && (response.headers.get("content-type") ?? "").includes("json"), "未取得 JSON 任務。");
  check(response.body, "任務回應沒有內容。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let size = 0;
  let text = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      check(size <= limit, "任務檔案超過大小限制。");
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return JSON.parse(text);
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
