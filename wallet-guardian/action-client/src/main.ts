import { digestBody } from "./canonical.mjs";
import {
  CANCEL_PENDING_CONFIRMATION,
  adoptSession,
  applyUnauthenticated,
  beginLogin,
  beginPresent,
  beginLogout,
  beginTaskLoad,
  canRedrawCapability,
  clearPresented,
  createClientFlow,
  endLogin,
  finishLogout,
  isCancelUnconfirmed,
  loginStillCurrent,
  mutationCsrf,
  noteAttemptCsrf,
  rememberPresented,
  resolveCancel,
  stampMatches,
} from "./client-flow.mjs";
import { clientConfirmDecision, eip5792Status, executionMode, simulationAllowsSend } from "./gate.mjs";
import { assertInertPayload, assertTaskRef, boundedJson, units, validatePlan } from "./plan-validation.mjs";
import { parsePrivateTaskRef, privateTaskSendAllowed, readBoundedJson } from "./private-task.mjs";
import { acceptLoginChallenge } from "./siwe-gate.mjs";

const TASK_VERSION = "guardian.task.v1";
const PLAN_VERSION = "guardian.action_plan.v1";
const CLIENT_BUILD = "action-client@0.3.0";
const REF_PATTERN = /^ref_[a-f0-9]{32}$/;
const BANNER =
  "主網簽名未授權。沒有固定 HTTPS 網域，不會發行可在手機錢包開啟的任務連結。按鈕只會說明阻塞原因，不會呼叫錢包。";

const SECRET_KEYS = new Set([
  "private_key",
  "mnemonic",
  "seed_phrase",
  "session_key",
  "session_secret",
  "walletconnect_uri",
  "pairing_uri",
  "executable_js",
  "runtime_script",
]);

type Simulation = {
  status: string;
  block_number?: number | null;
  source: string;
  effects_checked?: boolean;
  usable_as_live_evidence?: boolean;
  state_overrides?: boolean;
};

type Plan = {
  protocol_version: string;
  task_id: string;
  state?: string;
  sender: string;
  recipient: string;
  chain_id: number;
  calls: { to: string; data: string; value: string }[];
  asset_in: { symbol: string; raw_amount: string; decimals: number };
  asset_out: { symbol: string; raw_amount: string; decimals: number };
  human_summary: {
    purpose: string;
    sender: string;
    recipient: string;
    chain_label: string;
    amount_in_raw: string;
    amount_in_unit: string;
    amount_out_raw: string;
    amount_out_unit: string;
    fee_label: string;
    simulation_status: string;
    wallet_request_allowed: boolean;
  };
  simulation: Simulation;
  block_reasons?: string[];
  wallet_request_allowed: boolean;
  digest: string;
  alternatives?: { id: string; kind: string; completes_original_redeem?: boolean }[];
};

type Step = {
  id: string;
  label: string;
  required: boolean;
  separate: boolean;
  requests_7702?: boolean;
  unlimited_approval?: boolean;
  adds_admin?: boolean;
  migrates_assets?: boolean;
  upgrades_account?: boolean;
};

type TaskLink = {
  issued?: boolean;
  url?: string | null;
  execution_authorized?: boolean;
};

type Task = {
  protocol_version: string;
  client_build: string;
  ref: string;
  execution_authorized: boolean;
  mode: string;
  mode_reason?: string;
  wallet_request_allowed: boolean;
  steps: Step[];
  link?: TaskLink;
  plan: Plan;
  requests_7702?: boolean;
  unlimited_approval?: boolean;
  requires_user_wallet_confirmation?: boolean;
  preflight?: { preflight_ok?: boolean; revision_required?: boolean; changes?: string[] };
};

const view = document.querySelector<HTMLElement>("#view")!;
const banner = document.querySelector<HTMLElement>("#banner")!;
const MAINNET_SEND = import.meta.env.VITE_GUARDIAN_MAINNET_SEND === "true";
const flow = createClientFlow();

type ViewStamp = {
  generation: number;
  epoch: number;
  ref: string | null;
  revision: number | null;
};

type EthereumProvider = { request: (args: { method: string; params?: unknown[] }) => Promise<unknown> };

function ethereumProvider(): EthereumProvider | null {
  return (window as unknown as { ethereum?: EthereumProvider }).ethereum ?? null;
}

function presentedMode(task: Task | null, plan: Plan): "A" | "B" {
  if (!task || task.mode === "C") return "A";
  return executionMode({
    capability: flow.observedCapability,
    callCount: plan.calls.length,
    sameChain: plan.chain_id === 56,
  });
}

function requestUserSend(plan: Plan): void {
  if (plan.wallet_request_allowed !== true) {
    throw new Error("計畫本身不允許提出錢包請求。不會呼叫錢包。");
  }
  if (!simulationAllowsSend(plan.simulation)) {
    throw new Error("模擬未完成。不會呼叫錢包。");
  }
  if ((plan.block_reasons ?? []).length > 0) {
    throw new Error("計畫仍有阻塞原因。不會呼叫錢包。");
  }
  if (!MAINNET_SEND) {
    throw new Error("主網送出開關是關閉的。不會呼叫錢包。");
  }
  throw new Error("這個建置沒有客戶端鏈上重讀，不會呼叫錢包。");
}

function syncAccountControls(): void {
  const logout = document.querySelector<HTMLButtonElement>("#logout");
  const cancel = document.querySelector<HTMLButtonElement>("#cancel-task");
  if (logout) logout.hidden = !flow.sessionCsrf;
  if (cancel) cancel.hidden = !flow.privateCancelAllowed;
}

function hideCancel(): void {
  flow.privateCancelAllowed = false;
  syncAccountControls();
}

function renderError(message: string): void {
  hideCancel();
  view.className = "error";
  view.textContent = message;
}

function renderEmpty(): void {
  view.className = "empty";
  view.textContent = "尚未載入任務。合成範例不是你的鏈上餘額。";
}

function containsSecret(value: unknown): boolean {
  if (Array.isArray(value)) return value.some((item) => containsSecret(item));
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).some(
      ([key, item]) => SECRET_KEYS.has(key) || containsSecret(item),
    );
  }
  return typeof value === "string" && value.startsWith("wc:");
}

function modeLine(task: Task | null, mode: "A" | "B"): string {
  if (task?.mode === "C") {
    return "模式 A：資料寫了限權執行。這個版本沒有鏈上權限合約，模式 C 保持關閉。";
  }
  if (mode === "B") {
    return "模式 B：這台錢包回報同鏈批次可用。真錢包批次仍未測，這個建置也不會送出。";
  }
  if (flow.observedCapability === "unsupported" || flow.observedCapability === "ready") {
    return "模式 A：這台錢包沒有可用的同鏈批次，或 ready 不會自動升級。改以一般確認。";
  }
  if (task?.mode_reason === "single_step_original_account") {
    return "模式 A：原地址單步領取。即使錢包支援批次，這一步也不合併。";
  }
  return "模式 A：一般錢包引導。必要交易由你在自己的錢包確認。尚未向錢包查詢批次能力。";
}

function validateTask(task: Task): string | null {
  if (containsSecret(task)) return "任務資料含有秘密或可執行腳本，已拒絕。";
  if (task.client_build !== CLIENT_BUILD) return "任務的客戶端版本與這個固定頁面不符。";
  if (task.execution_authorized !== false) return "任務資料不能把自己標成執行授權。";
  if (task.wallet_request_allowed !== false) return "這個建置的任務不能自行打開錢包請求。";
  if (!REF_PATTERN.test(task.ref)) return "任務參考碼必須是不透明的 ref_ 代碼。";
  if (task.requests_7702 || task.unlimited_approval) return "這個任務要求委託或無限授權。這個版本拒絕。";
  if (task.link?.execution_authorized) return "任務連結不是執行授權。";
  if (!Array.isArray(task.steps) || task.steps.length === 0) return "任務缺少分開的確認步驟。";
  for (const step of task.steps) {
    if (step.separate !== true) return "連接、登入、授權、批次、執行權限、升級與確認必須分開呈現。";
    if (step.requests_7702 || step.unlimited_approval || step.adds_admin || step.migrates_assets || step.upgrades_account) {
      return "這個任務要求委託、無限授權、管理員、資產遷移或帳戶升級。這個版本拒絕。";
    }
  }
  if (!task.plan) return "任務裡沒有可核對的計畫。";
  return null;
}

function appendFacts(list: HTMLElement, rows: Array<[string, string]>): void {
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    list.append(term, detail);
  }
}

async function renderPlan(plan: Plan, task: Task | null, stamp: ViewStamp): Promise<void> {
  if (!stampMatches(flow, stamp)) return;
  validatePlan(plan);
  if (plan.protocol_version !== PLAN_VERSION) {
    renderError("協議版本不符，已拒絕這個檔案。");
    return;
  }
  const digest = await digestBody(plan);
  if (!stampMatches(flow, stamp)) return;
  if (digest !== plan.digest) {
    renderError("計畫摘要比對失敗。檔案可能被改過，不會拿去簽名。");
    return;
  }
  const summary = plan.human_summary;
  if (
    summary.amount_out_raw !== plan.asset_out.raw_amount ||
    summary.recipient !== plan.recipient ||
    summary.sender !== plan.sender
  ) {
    renderError("畫面上的金額或地址和計畫不一致。");
    return;
  }
  const modeName = presentedMode(task, plan);
  const decision = clientConfirmDecision({
    plan,
    task,
    mainnetSend: MAINNET_SEND,
    mode: modeName,
  });
  const reasons = [...(plan.block_reasons ?? [])];
  if (plan.wallet_request_allowed !== true) reasons.push("計畫本身不允許提出錢包請求。任務裡的 preflight 不能推翻這個拒絕。");
  if (!simulationAllowsSend(plan.simulation)) reasons.push("模擬未完成，不能當作可以送出的證據。");
  if (!decision.buttonEnabled) reasons.push("確認鈕關閉。任務自述的 preflight 不是客戶端鏈上重讀。");
  if (!MAINNET_SEND) reasons.push("VITE_GUARDIAN_MAINNET_SEND 沒有打開。監控程式不能簽名。");
  if (task?.mode === "C") reasons.push("模式 C 不支援。沒有鏈上權限合約。");
  if (task && !task.link?.issued) reasons.push("沒有固定 HTTPS 網域，所以沒有發行公開任務連結。");
  banner.textContent = "尚未對錢包提出交易。主網送出保持關閉。格式核對不是來源認證或安全保證；此頁沒有重新讀取鏈上狀態。";

  const alternatives = (plan.alternatives ?? [])
    .map((item) => `${item.id}${item.completes_original_redeem ? "" : "（不是完成原贖回）"}`)
    .join("、");
  const linkLabel = task?.link?.issued ? "不跟隨這份資料裡的網址。連結不是執行授權。" : "未發行";

  view.className = "card";
  view.replaceChildren();

  const mode = document.createElement("p");
  mode.className = "mode";
  mode.textContent = modeLine(task, modeName);

  const heading = document.createElement("h2");
  heading.textContent = "Lista 提款計畫預覽（尚未驗證可執行）";

  const list = document.createElement("dl");
  appendFacts(list, [
    ["來源錢包", summary.sender],
    ["鏈", summary.chain_label],
    ["原申請數量（非本次支出）", `${units(summary.amount_in_raw, plan.asset_in.decimals)} ${summary.amount_in_unit}`],
    ["計畫中的領取數量（待重查）", `${units(summary.amount_out_raw, plan.asset_out.decimals)} ${summary.amount_out_unit}`],
    ["接收地址", summary.recipient],
    ["費用", summary.fee_label],
    ["模擬", `${summary.simulation_status}（${plan.simulation.source}，區塊 ${plan.simulation.block_number ?? "未知"}）`],
    ["其他路徑", alternatives || "無"],
    ["任務參考碼", task?.ref ?? "僅本機檔案"],
    ["公開連結", linkLabel],
  ]);

  const stepHeading = document.createElement("h3");
  stepHeading.textContent = "分開確認";
  const steps = document.createElement("ol");
  steps.className = "steps";
  const sourceSteps: Step[] = task?.steps ?? [
    { id: "connect_wallet", label: "連接錢包", required: false, separate: true },
    { id: "login_signature", label: "登入簽名", required: false, separate: true },
    { id: "erc20_approve", label: "代幣授權", required: false, separate: true },
    { id: "batch_confirm", label: "批次確認", required: false, separate: true },
    { id: "execution_permission", label: "限定執行權限", required: false, separate: true },
    { id: "account_upgrade", label: "帳戶升級", required: false, separate: true },
    { id: "execute", label: "確認這筆領取", required: true, separate: true },
  ];
  for (const step of sourceSteps) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = step.label;
    const state = document.createElement("span");
    state.className = step.required ? "need" : "skip";
    state.textContent = step.required ? "需要你確認" : "這次不需要";
    item.append(name, state);
    steps.append(item);
  }

  const wait = document.createElement("p");
  wait.className = "status";
  wait.textContent = "解除質押後的等待可以追蹤，不能在這裡被消掉。交易雜湊不算完成，必須核對實際鏈上結果。";

  const reasonList = document.createElement("ul");
  reasonList.className = "reasons";
  for (const reason of reasons) {
    const item = document.createElement("li");
    item.textContent = reason;
    reasonList.append(item);
  }

  const evidenceHeading = document.createElement("h3");
  evidenceHeading.textContent = "這次建置的證據";
  const evidence = document.createElement("ul");
  evidence.className = "evidence";
  const evidenceRows = [
    "手機錢包內建瀏覽器：未測",
    "模式 A 真錢包：未測",
    "模式 B 真錢包：未測",
    "模式 C：不支援。沒有已驗證、能在鏈上強制限制的權限機制",
    "固定 HTTPS 網域：見部署紀錄；這個預覽沒有把它當成執行授權",
    "主網執行授權：沒有。建置與測試不等於獲准執行",
  ];
  for (const row of evidenceRows) {
    const item = document.createElement("li");
    item.textContent = row;
    evidence.append(item);
  }

  const actions = document.createElement("div");
  actions.className = "row actions-bar";
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "primary";
  confirm.disabled = !decision.buttonEnabled || plan.wallet_request_allowed !== true || !simulationAllowsSend(plan.simulation);
  confirm.textContent = "在我的錢包確認";
  confirm.addEventListener("click", () => {
    try {
      if (!stampMatches(flow, stamp)) return;
      requestUserSend(plan);
    } catch (error) {
      banner.textContent = error instanceof Error ? error.message : "已停止";
    }
  });
  const capability = document.createElement("button");
  capability.type = "button";
  capability.textContent = "查詢批次能力";
  capability.addEventListener("click", () => {
    void queryCapability(plan.chain_id, stamp);
  });
  actions.append(confirm, capability);

  const details = document.createElement("details");
  const summaryNode = document.createElement("summary");
  summaryNode.textContent = "技術細節";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(task ?? plan, null, 2);
  details.append(summaryNode, pre);

  if (!rememberPresented(flow, stamp, plan, task)) return;
  view.append(mode, heading, list, stepHeading, steps, wait, actions, reasonList, evidenceHeading, evidence, details);
}

async function queryCapability(chainId: number, stamp: ViewStamp): Promise<void> {
  if (!stampMatches(flow, stamp)) return;
  const ethereum = ethereumProvider();
  if (!ethereum) {
    if (!stampMatches(flow, stamp)) return;
    banner.textContent = "找不到瀏覽器錢包，無法查詢 EIP-5792。維持模式 A，不會送出交易。";
    return;
  }
  try {
    const capabilities = await ethereum.request({ method: "wallet_getCapabilities" });
    if (!canRedrawCapability(flow, stamp)) return;
    flow.observedCapability = eip5792Status(capabilities, chainId);
  } catch {
    if (!canRedrawCapability(flow, stamp)) return;
    flow.observedCapability = "unknown";
    banner.textContent = "批次能力查詢失敗。維持模式 A，不會送出交易。";
  }
  const plan = flow.lastPlan as Plan | null;
  const task = flow.lastTask as Task | null;
  if (!plan || !canRedrawCapability(flow, stamp)) return;
  await renderPlan(plan, task, stamp);
}

async function renderPayload(payload: unknown, stamp: ViewStamp): Promise<void> {
  if (!stampMatches(flow, stamp)) return;
  assertInertPayload(payload);
  if (!payload || typeof payload !== "object") {
    renderError("這個檔案不是可讀的任務 JSON。");
    return;
  }
  const record = payload as { protocol_version?: string };
  if (record.protocol_version === TASK_VERSION) {
    const task = payload as Task;
    const problem = validateTask(task);
    if (problem) {
      renderError(problem);
      return;
    }
    await renderPlan(task.plan, task, stamp);
    return;
  }
  if (record.protocol_version === PLAN_VERSION) {
    await renderPlan(payload as Plan, null, stamp);
    return;
  }
  renderError("協議版本不符，已拒絕這個檔案。");
}

type SessionBody = {
  authenticated?: boolean;
  csrf_token?: string;
  execution_authorized?: boolean;
  send_authorized?: boolean;
};

type PrivateTaskBody = {
  revision?: number;
  cancel_allowed?: boolean;
  send_authorized?: boolean;
  execution_authorized?: boolean;
  tx_hash_verified?: boolean;
  on_chain_revoked?: boolean;
  task?: Task;
};

function api(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
    const token = mutationCsrf(flow, path === "/api/auth/verify" ? "verify" : "session");
    if (token) headers.set("x-csrf-token", token);
  }
  return fetch(path, {
    ...init,
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  });
}

function applySession(body: unknown): boolean {
  if (!body || typeof body !== "object") return false;
  const record = body as SessionBody;
  if (record.execution_authorized === true || record.send_authorized === true) {
    flow.sessionCsrf = null;
    syncAccountControls();
    return false;
  }
  if (record.authenticated === true && typeof record.csrf_token === "string") {
    const adopted = adoptSession(flow, record.csrf_token);
    syncAccountControls();
    return adopted;
  }
  flow.sessionCsrf = null;
  syncAccountControls();
  return false;
}

async function loadPrivateTask(ref: string, stamp: ViewStamp): Promise<void> {
  try {
    const response = await api(`/api/tasks/${ref}`, { signal: AbortSignal.timeout(10000) });
    const body = (await readBoundedJson(response)) as PrivateTaskBody;
    if (!stampMatches(flow, stamp)) return;
    if (response.status === 401) {
      applyUnauthenticated(flow, stamp);
      syncAccountControls();
      renderError("登入已失效。請重新登入。不會送出交易。");
      return;
    }
    if (response.status === 404) {
      renderError("找不到這筆任務，或這個登入不能讀取。");
      return;
    }
    if (response.status === 409) {
      renderError("任務版本已更新。舊版本不能使用，也不會送出交易。");
      return;
    }
    if (response.status === 410) {
      renderError("任務已過期、取消或被取代。舊資料不能使用。這不是鏈上撤銷，也不會送出交易。");
      return;
    }
    if (!response.ok || !body.task) {
      renderError("無法讀取任務。");
      return;
    }
    if (body.send_authorized !== false || body.execution_authorized !== false || body.tx_hash_verified !== false || body.on_chain_revoked === true) {
      renderError("任務回應試圖授權送出，或把交易雜湊當成完成。已拒絕。");
      return;
    }
    if (privateTaskSendAllowed(body.task, false)) {
      renderError("任務內容不能打開交易送出。已拒絕。");
      return;
    }
    assertTaskRef(body.task, ref);
    const revision = typeof body.revision === "number" ? body.revision : null;
    if (stamp.revision != null && revision !== stamp.revision) {
      if (!stampMatches(flow, stamp)) return;
      renderError("任務版本已更新。舊版本不能使用，也不會送出交易。");
      return;
    }
    if (!stampMatches(flow, stamp)) return;
    flow.privateRevision = revision;
    flow.privateCancelAllowed = body.cancel_allowed === true && flow.privateRevision != null;
    syncAccountControls();
    await renderPayload(body.task, stamp);
  } catch {
    if (stampMatches(flow, stamp)) renderError("無法載入私人任務。不跟隨重新導向，也不會送出交易。");
  }
}

async function openPrivateTask(ref: string): Promise<void> {
  const stamp = beginTaskLoad(flow, ref) as ViewStamp;
  if (!flow.sessionCsrf) {
    renderError("這個任務要先登入才能讀。登入只簽一則讀取訊息，不會送出交易。");
    return;
  }
  await loadPrivateTask(ref, stamp);
}

async function login(): Promise<void> {
  const ethereum = ethereumProvider();
  if (!ethereum) {
    banner.textContent = "找不到瀏覽器錢包，無法登入。不會送出交易。";
    return;
  }
  const attempt = beginLogin(flow);
  if (attempt == null) {
    banner.textContent = "登入已經在進行。不會再要求簽名。";
    return;
  }
  try {
    const accounts = await ethereum.request({ method: "eth_requestAccounts" });
    if (!loginStillCurrent(flow, attempt)) return;
    const address = Array.isArray(accounts) ? String(accounts[0] ?? "") : "";
    if (!/^0x[a-fA-F0-9]{40}$/.test(address)) {
      endLogin(flow);
      banner.textContent = "錢包沒有提供可用帳戶。不會送出交易。";
      return;
    }
    const challengeResponse = await api("/api/auth/challenge", {
      method: "POST",
      body: JSON.stringify({ address }),
      signal: AbortSignal.timeout(10000),
    });
    const issued = await readBoundedJson(challengeResponse);
    if (!loginStillCurrent(flow, attempt)) return;
    const message = acceptLoginChallenge(issued, address, Date.now());
    const csrf = issued && typeof issued === "object" ? (issued as { csrf_token?: unknown }).csrf_token : undefined;
    if (typeof message !== "string" || !noteAttemptCsrf(flow, csrf)) {
      endLogin(flow);
      banner.textContent = "登入訊息不是固定的讀取聲明。不會簽名。";
      return;
    }
    const signature = await ethereum.request({ method: "personal_sign", params: [message, address] });
    if (!loginStillCurrent(flow, attempt)) return;
    if (typeof signature !== "string") {
      endLogin(flow);
      banner.textContent = "錢包沒有回傳登入簽名。不會送出交易。";
      return;
    }
    const verifyResponse = await api("/api/auth/verify", {
      method: "POST",
      body: JSON.stringify({ message, signature }),
      signal: AbortSignal.timeout(10000),
    });
    const verified = await readBoundedJson(verifyResponse);
    if (!loginStillCurrent(flow, attempt)) return;
    if (!verifyResponse.ok || !applySession(verified)) {
      endLogin(flow);
      syncAccountControls();
      banner.textContent = "登入沒有完成。不會送出交易。";
      return;
    }
    banner.textContent = "已登入。這只是讀取身分，不是交易授權。主網送出保持關閉。";
    if (flow.privateRef) {
      const stamp = beginTaskLoad(flow, flow.privateRef) as ViewStamp;
      await loadPrivateTask(flow.privateRef, stamp);
    }
  } catch {
    if (!loginStillCurrent(flow, attempt)) return;
    endLogin(flow);
    banner.textContent = "登入已停止。不會送出交易。";
  }
}

async function logout(): Promise<void> {
  const csrf = beginLogout(flow);
  syncAccountControls();
  if (!csrf) {
    finishLogout(flow);
    syncAccountControls();
    banner.textContent = "已登出。伺服器會作廢這個工作階段。不會送出交易。";
    renderEmpty();
    return;
  }
  try {
    const response = await api("/api/auth/logout", {
      method: "POST",
      body: JSON.stringify({}),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      banner.textContent = "登出沒有完成。";
      return;
    }
  } catch {
    banner.textContent = "登出沒有完成。";
    return;
  }
  finishLogout(flow);
  syncAccountControls();
  banner.textContent = "已登出。伺服器會作廢這個工作階段。不會送出交易。";
  renderEmpty();
}

async function cancelPrivateTask(): Promise<void> {
  if (!flow.privateRef || flow.privateRevision == null || !flow.privateCancelAllowed || !flow.sessionCsrf) return;
  const stamp = {
    generation: flow.loadGeneration,
    epoch: flow.sessionEpoch,
    ref: flow.privateRef,
    revision: flow.privateRevision,
  } as ViewStamp;
  const ref = stamp.ref;
  if (!ref || stamp.revision == null) return;
  try {
    const response = await api(`/api/tasks/${ref}/cancel`, {
      method: "POST",
      body: JSON.stringify({ revision: stamp.revision }),
      signal: AbortSignal.timeout(10000),
    });
    let body: unknown;
    try {
      body = await readBoundedJson(response);
    } catch (error) {
      if (!stampMatches(flow, stamp)) return;
      if (isCancelUnconfirmed(error)) {
        banner.textContent = CANCEL_PENDING_CONFIRMATION;
        await loadPrivateTask(ref, stamp);
        return;
      }
      banner.textContent = "取消回應不符合已取消的格式。不會把它當成已取消，也不會送出交易。";
      return;
    }
    if (!stampMatches(flow, stamp)) return;
    const outcome = resolveCancel(flow, stamp, { ok: response.ok, body });
    if (outcome === "cancelled") {
      syncAccountControls();
      view.className = "card";
      view.textContent = "未執行任務已取消。這只更新讀取紀錄，不是鏈上撤銷，也沒有送出交易。";
      banner.textContent = "主網送出保持關閉。取消不是交易。";
      return;
    }
    if (outcome === "stale") return;
    banner.textContent = "取消回應不符合已取消的格式。不會把它當成已取消，也不會送出交易。";
  } catch (error) {
    if (!stampMatches(flow, stamp)) return;
    if (isCancelUnconfirmed(error)) {
      banner.textContent = CANCEL_PENDING_CONFIRMATION;
      await loadPrivateTask(ref, stamp);
      return;
    }
    banner.textContent = "任務沒有取消。不會送出交易。";
  }
}

function inspectLocation(): void {
  const pathRef = parsePrivateTaskRef(window.location.pathname);
  if (pathRef) {
    void openPrivateTask(pathRef);
    return;
  }
  if (window.location.pathname.startsWith("/t/")) {
    renderError("任務參考碼格式不對。頁面不會向其他路徑索取資料，連結也不是執行授權。");
    return;
  }
  const params = new URLSearchParams(window.location.search);
  const keys = [...params.keys()];
  if (keys.some((key) => key !== "ref") || params.getAll("ref").length > 1) {
    renderError("任務連結只能使用 /t/ 後面的不透明參考碼，不能帶地址、金額或其他私人欄位。連結不是執行授權。");
    return;
  }
  if (params.get("ref")) {
    renderError("私人任務請改走 /t/ 參考碼。這個頁面不會讀取公開靜態任務。連結不是執行授權。");
  }
}

async function restoreSession(): Promise<void> {
  const stamp = {
    generation: flow.loadGeneration,
    epoch: flow.sessionEpoch,
    ref: flow.privateRef,
    revision: flow.privateRevision,
  } as ViewStamp;
  try {
    const response = await api("/api/session", { signal: AbortSignal.timeout(10000) });
    const body = await readBoundedJson(response);
    if (!stampMatches(flow, stamp)) return;
    applySession(body);
  } catch {
    if (!applyUnauthenticated(flow, stamp)) return;
    syncAccountControls();
  }
}

document.querySelector("#login")!.addEventListener("click", () => {
  void login();
});

document.querySelector("#logout")!.addEventListener("click", () => {
  void logout();
});

document.querySelector("#cancel-task")!.addEventListener("click", () => {
  void cancelPrivateTask();
});

document.querySelector("#load-sample")!.addEventListener("click", async () => {
  const stamp = beginPresent(flow) as ViewStamp;
  try {
    const response = await fetch("/sample-task.json", { redirect: "error", cache: "no-store", signal: AbortSignal.timeout(10000) });
    await renderPayload(await boundedJson(response), stamp);
  } catch {
    if (stampMatches(flow, stamp)) renderError("讀不到有效合成範例。");
  }
});

document.querySelector<HTMLInputElement>("#file")!.addEventListener("change", async (event) => {
  const stamp = beginPresent(flow) as ViewStamp;
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  try {
    if (file.size > 1024 * 1024) throw new Error("檔案超過大小限制");
    await renderPayload(JSON.parse(await file.text()) as unknown, stamp);
  } catch {
    if (stampMatches(flow, stamp)) renderError("無效的任務資料。檢查格式、大小、摘要及固定協議規則。");
  }
});

document.querySelector("#clear")!.addEventListener("click", () => {
  clearPresented(flow);
  renderEmpty();
  document.querySelector<HTMLInputElement>("#file")!.value = "";
  banner.textContent = BANNER;
});

void restoreSession().then(() => inspectLocation());
