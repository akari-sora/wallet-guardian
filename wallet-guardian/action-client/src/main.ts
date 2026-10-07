import { digestBody } from "./canonical.mjs";
import { clientConfirmDecision, eip5792Status, executionMode, simulationAllowsSend } from "./gate.mjs";
import { assertInertPayload, assertTaskRef, boundedJson, units, validatePlan } from "./plan-validation.mjs";

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
let loadGeneration = 0;
let observedCapability = "unknown";
let lastPlan: Plan | null = null;
let lastTask: Task | null = null;

type EthereumProvider = { request: (args: { method: string; params?: unknown[] }) => Promise<unknown> };

function ethereumProvider(): EthereumProvider | null {
  return (window as unknown as { ethereum?: EthereumProvider }).ethereum ?? null;
}

function presentedMode(task: Task | null, plan: Plan): "A" | "B" {
  if (!task || task.mode === "C") return "A";
  return executionMode({
    capability: observedCapability,
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

function renderError(message: string): void {
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
  if (observedCapability === "unsupported" || observedCapability === "ready") {
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

async function renderPlan(plan: Plan, task: Task | null, generation: number): Promise<void> {
  if (generation !== loadGeneration) return;
  validatePlan(plan);
  if (plan.protocol_version !== PLAN_VERSION) {
    renderError("協議版本不符，已拒絕這個檔案。");
    return;
  }
  const digest = await digestBody(plan);
  if (generation !== loadGeneration) return;
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
      if (generation !== loadGeneration) return;
      requestUserSend(plan);
    } catch (error) {
      banner.textContent = error instanceof Error ? error.message : "已停止";
    }
  });
  const capability = document.createElement("button");
  capability.type = "button";
  capability.textContent = "查詢批次能力";
  capability.addEventListener("click", () => {
    void queryCapability(plan.chain_id, generation);
  });
  actions.append(confirm, capability);

  const details = document.createElement("details");
  const summaryNode = document.createElement("summary");
  summaryNode.textContent = "技術細節";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(task ?? plan, null, 2);
  details.append(summaryNode, pre);

  lastPlan = plan;
  lastTask = task;
  view.append(mode, heading, list, stepHeading, steps, wait, actions, reasonList, evidenceHeading, evidence, details);
}

async function queryCapability(chainId: number, generation: number): Promise<void> {
  if (generation !== loadGeneration) return;
  const ethereum = ethereumProvider();
  if (!ethereum) {
    banner.textContent = "找不到瀏覽器錢包，無法查詢 EIP-5792。維持模式 A，不會送出交易。";
    return;
  }
  try {
    const capabilities = await ethereum.request({ method: "wallet_getCapabilities" });
    if (generation !== loadGeneration) return;
    observedCapability = eip5792Status(capabilities, chainId);
  } catch {
    if (generation !== loadGeneration) return;
    observedCapability = "unknown";
    banner.textContent = "批次能力查詢失敗。維持模式 A，不會送出交易。";
  }
  if (lastPlan && generation === loadGeneration) await renderPlan(lastPlan, lastTask, generation);
}

async function renderPayload(payload: unknown, generation: number): Promise<void> {
  if (generation !== loadGeneration) return;
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
    await renderPlan(task.plan, task, generation);
    return;
  }
  if (record.protocol_version === PLAN_VERSION) {
    await renderPlan(payload as Plan, null, generation);
    return;
  }
  renderError("協議版本不符，已拒絕這個檔案。");
}

async function loadSameOriginTask(ref: string): Promise<void> {
  const generation = ++loadGeneration;
  try {
    const response = await fetch(`/tasks/${ref}.json`, {
      credentials: "same-origin", redirect: "error", cache: "no-store", signal: AbortSignal.timeout(10000),
    });
    const payload = await boundedJson(response);
    if (generation !== loadGeneration) return;
    assertTaskRef(payload, ref);
    await renderPayload(payload, generation);
  } catch {
    if (generation === loadGeneration) renderError("無法載入同源任務。不跟隨重新導向，也不接受替換參考碼。");
  }
}

function inspectLocation(): void {
  const params = new URLSearchParams(window.location.search);
  let foreign = false;
  params.forEach((_value, key) => {
    if (key !== "ref") foreign = true;
  });
  if (foreign) {
    renderError("任務連結只能帶不透明參考碼，不能帶地址、金額或其他私人欄位。連結不是執行授權。");
    return;
  }
  if (params.getAll("ref").length > 1) {
    renderError("任務參考碼不能重複。");
    return;
  }
  const ref = params.get("ref");
  if (!ref) return;
  if (!REF_PATTERN.test(ref)) {
    renderError("任務參考碼格式不對。頁面不會用它向其他網站索取資料，連結也不是執行授權。");
    return;
  }
  void loadSameOriginTask(ref);
}

document.querySelector("#load-sample")!.addEventListener("click", async () => {
  const generation = ++loadGeneration;
  try {
    const response = await fetch("/sample-task.json", { redirect: "error", cache: "no-store", signal: AbortSignal.timeout(10000) });
    await renderPayload(await boundedJson(response), generation);
  } catch {
    if (generation === loadGeneration) renderError("讀不到有效合成範例。");
  }
});

document.querySelector<HTMLInputElement>("#file")!.addEventListener("change", async (event) => {
  const generation = ++loadGeneration;
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  try {
    if (file.size > 1024 * 1024) throw new Error("檔案超過大小限制");
    await renderPayload(JSON.parse(await file.text()) as unknown, generation);
  } catch {
    if (generation === loadGeneration) renderError("無效的任務資料。檢查格式、大小、摘要及固定協議規則。");
  }
});

document.querySelector("#clear")!.addEventListener("click", () => {
  ++loadGeneration;
  lastPlan = null;
  lastTask = null;
  renderEmpty();
  document.querySelector<HTMLInputElement>("#file")!.value = "";
  banner.textContent = BANNER;
});

inspectLocation();
