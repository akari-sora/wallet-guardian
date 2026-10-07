import { digestBody } from "./canonical.mjs";

const WALLET_REQUESTS_ENABLED = false;

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
  simulation: { status: string; block_number: number | null; source: string };
  block_reasons?: string[];
  wallet_request_allowed: boolean;
  digest: string;
  alternatives?: { id: string; kind: string; completes_original_redeem?: boolean }[];
};

const view = document.querySelector<HTMLElement>("#view")!;
const banner = document.querySelector<HTMLElement>("#banner")!;

function units(raw: string, decimals: number): string {
  const negative = raw.startsWith("-");
  const digits = negative ? raw.slice(1) : raw;
  if (!/^\d+$/.test(digits)) return raw;
  const padded = digits.padStart(decimals + 1, "0");
  const whole = padded.slice(0, padded.length - decimals);
  const frac = padded.slice(padded.length - decimals).replace(/0+$/, "");
  const text = frac ? `${whole}.${frac}` : whole;
  return negative ? `-${text}` : text;
}

function requestWallet(): void {
  if (!WALLET_REQUESTS_ENABLED) {
    throw new Error("這個建置的錢包請求已關閉");
  }
  const ethereum = (window as unknown as { ethereum?: { request: (args: unknown) => Promise<unknown> } }).ethereum;
  if (!ethereum) {
    throw new Error("找不到瀏覽器錢包");
  }
  void ethereum.request;
}

function renderError(message: string): void {
  view.className = "error";
  view.textContent = message;
}

function renderEmpty(): void {
  view.className = "empty";
  view.textContent = "尚未載入計畫。合成範例不是你的鏈上餘額。";
}

async function renderPlan(plan: Plan): Promise<void> {
  if (plan.protocol_version !== "guardian.action_plan.v1") {
    renderError("協議版本不符，已拒絕這個檔案。");
    return;
  }
  const digest = await digestBody(plan);
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
  const reasons = [...(plan.block_reasons ?? [])];
  if (!plan.wallet_request_allowed) reasons.push("計畫本身不允許提出錢包請求。");
  if (!WALLET_REQUESTS_ENABLED) reasons.push("這個頁面的發布開關是關閉的。");
  banner.textContent = "尚未對錢包提出交易。下列原因需要先解除，才可能進入確認。";
  const alternatives = (plan.alternatives ?? [])
    .map((item) => `${item.id}${item.completes_original_redeem ? "" : "（不是完成原贖回）"}`)
    .join("、");
  view.className = "card";
  view.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = summary.purpose;
  const list = document.createElement("dl");
  const rows: Array<[string, string]> = [
    ["來源錢包", summary.sender],
    ["鏈", summary.chain_label],
    ["你會付出", `${units(summary.amount_in_raw, plan.asset_in.decimals)} ${summary.amount_in_unit}`],
    ["你會收到", `${units(summary.amount_out_raw, plan.asset_out.decimals)} ${summary.amount_out_unit}`],
    ["接收地址", summary.recipient],
    ["費用", summary.fee_label],
    ["模擬", `${summary.simulation_status}（${plan.simulation.source}，區塊 ${plan.simulation.block_number ?? "未知"}）`],
    ["其他路徑", alternatives || "無"],
  ];
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    list.append(term, detail);
  }
  const reasonList = document.createElement("ul");
  reasonList.className = "reasons";
  for (const reason of reasons) {
    const item = document.createElement("li");
    item.textContent = reason;
    reasonList.append(item);
  }
  const actions = document.createElement("div");
  actions.className = "row";
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "primary";
  confirm.disabled = true;
  confirm.textContent = "在我的錢包確認";
  confirm.addEventListener("click", () => {
    try {
      requestWallet();
    } catch (error) {
      banner.textContent = error instanceof Error ? error.message : "已停止";
    }
  });
  const details = document.createElement("details");
  const summaryNode = document.createElement("summary");
  summaryNode.textContent = "技術細節";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(plan, null, 2);
  details.append(summaryNode, pre);
  actions.append(confirm);
  view.append(heading, list, reasonList, actions, details);
}

document.querySelector("#load-sample")!.addEventListener("click", async () => {
  const response = await fetch("/sample-plan.json");
  if (!response.ok) {
    renderError("讀不到合成範例。");
    return;
  }
  await renderPlan((await response.json()) as Plan);
});

document.querySelector<HTMLInputElement>("#file")!.addEventListener("change", async (event) => {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  try {
    await renderPlan(JSON.parse(await file.text()) as Plan);
  } catch {
    renderError("這個檔案不是可讀的計畫 JSON。");
  }
});

document.querySelector("#clear")!.addEventListener("click", () => {
  renderEmpty();
  banner.textContent = "主網簽名未授權。按鈕只會說明阻塞原因，不會呼叫錢包。";
});
