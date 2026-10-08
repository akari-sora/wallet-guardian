import { digestBody } from "./canonical.mjs";
import {
  CANCEL_PENDING_CONFIRMATION,
  beginLogin,
  beginLogout,
  canRedrawCapability,
  createClientFlow,
  endLogin,
  finishLogout,
  isCancelUnconfirmed,
  mutationCsrf,
  noteAttemptCsrf,
  resolveCancel,
  stampMatches,
} from "./client-flow.mjs";
import { clientConfirmDecision, eip5792Status, executionMode, simulationAllowsSend } from "./gate.mjs";
import { emptyRereadView, REASON_TEXT, reverifyListaClaim, settleReread, statusLines } from "./lista-reread.mjs";
import { createModeAFlow, guideListaModeA, outcomeAfterReread, OUTCOME_TEXT, POISON_NOTE } from "./mode-a.mjs";
import { validateLoginChallenge } from "./login-challenge.mjs";
import { assertInertPayload, assertTaskRef, boundedJson, units, validatePlan } from "./plan-validation.mjs";
import { parsePrivateTaskRef, privateTaskSendAllowed, readBoundedJson } from "./private-task.mjs";
import { acceptLoginChallenge } from "./siwe-gate.mjs";
import { fetchTrustedRpc, TRUSTED_LISTA } from "./trusted-lista.mjs";
import { createAbandonSignal, encodePersonalSignMessage, guardedRequest, signEncodingFor } from "./wallets/injected.mjs";
import { createWalletRegistry, sourceLabel } from "./wallets/registry.mjs";
import { SOURCE_WALLETCONNECT, walletConnectConfig } from "./wallets/walletconnect-config.mjs";
import { createWalletConnectConnection, reasonText as walletConnectReasonText } from "./wallets/walletconnect.mjs";
import { enhanceText, installTypographyEnhancements, splitTrailingParenthetical } from "./typography.mjs";

const TASK_VERSION = "guardian.task.v1";
const PLAN_VERSION = "guardian.action_plan.v1";
const CLIENT_BUILD = "action-client@0.3.0";
const REF_PATTERN = /^ref_[a-f0-9]{32}$/;
const BANNER =
  "主網簽名未授權。私人任務只在 /t/<ref> 由本人錢包登入後查看；查看連結不是執行授權。按鈕只會說明阻塞原因，不會呼叫錢包。";

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

type RereadView = {
  reread: string;
  simulation: string;
  allowSend: "closed";
  reasons: string[];
  binding: { chainId?: number; freshUntil?: number } | null;
};

type Plan = {
  protocol_version: string;
  task_id: string;
  revision?: number;
  local_ttl_unix?: number;
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
  purpose?: string;
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
let csrfToken: string | null = null;
let privateRef: string | null = null;
let privateRevision: number | null = null;
let privateCancelAllowed = false;
let authGeneration = 0;
let authBusy = false;
let sessionAddress: string | null = null;
let rereadSeq = 0;
let rereadResult: RereadView | null = null;
let rereadPending = false;
let forcedReason: string | null = null;
let expiryGeneration = 0;
let expiryTimer: ReturnType<typeof setTimeout> | null = null;
let walletEpoch = 0;
let walletAccount: string | null = null;
let walletChainId: number | null = null;
let watchedProvider: EthereumProvider | null = null;
const walletHandlers: { event: string; handler: (value: unknown) => void }[] = [];
const flow = createClientFlow();
const modeAFlow = createModeAFlow();

type ViewStamp = {
  generation: number;
  epoch: number;
  ref: string | null;
  revision: number | null;
};

function syncFlow(): void {
  flow.loadGeneration = loadGeneration;
  flow.sessionEpoch = authGeneration;
  flow.lastPlan = lastPlan;
  flow.lastTask = lastTask;
  flow.observedCapability = observedCapability;
  flow.sessionCsrf = csrfToken;
  flow.privateRef = privateRef;
  flow.privateRevision = privateRevision;
  flow.privateCancelAllowed = privateCancelAllowed;
}

function applyFlowView(): void {
  loadGeneration = flow.loadGeneration;
  lastPlan = (flow.lastPlan as Plan | null) ?? null;
  lastTask = (flow.lastTask as Task | null) ?? null;
  observedCapability = flow.observedCapability;
  csrfToken = flow.sessionCsrf;
  privateRef = flow.privateRef;
  privateRevision = flow.privateRevision;
  privateCancelAllowed = flow.privateCancelAllowed;
}

function viewStamp(generation: number, epoch: number, ref: string | null, revision: number | null): ViewStamp {
  return { generation, epoch, ref, revision };
}

function paintWalletOutcome(status: string): void {
  const key = (status in OUTCOME_TEXT ? status : "idle") as keyof typeof OUTCOME_TEXT;
  const node = document.querySelector<HTMLElement>("#wallet-outcome");
  if (!node) return;
  node.textContent = OUTCOME_TEXT[key];
  if (node.dataset) node.dataset.state = key === "confirmed" ? "pass" : key === "failed" ? "fail" : "closed";
}

function dropReread(reason: string | null = null): void {
  rereadSeq += 1;
  rereadResult = null;
  rereadPending = false;
  forcedReason = reason;
  expiryGeneration += 1;
  if (expiryTimer) clearTimeout(expiryTimer);
  expiryTimer = null;
  paintWalletOutcome(outcomeAfterReread(modeAFlow).status);
}

// A network cancellation is best-effort; generations are the authority for
// whether a late response may mutate this page after a security boundary.
function invalidatePrivateView(keepRef = true): number {
  ++loadGeneration;
  lastPlan = null;
  lastTask = null;
  observedCapability = "unknown";
  privateRevision = null;
  if (!keepRef) privateRef = null;
  dropReread();
  hideCancel();
  renderEmpty();
  return loadGeneration;
}

type EthereumProvider = {
  request: (args: { method: string; params?: unknown[] }) => Promise<unknown>;
  on?: (event: string, handler: (value: unknown) => void) => void;
  removeListener?: (event: string, handler: (value: unknown) => void) => void;
};

// WALLET-CONNECT-01 W-A. name/rdns/uuid/icon are wallet self-description for display only.
// Login identity is the SIWE-verified address plus the D1 session; nothing here feeds the ACL.
type WalletCandidate = {
  readonly id: string;
  readonly source: string;
  readonly provider: EthereumProvider;
  readonly name: string;
  readonly rdns: string | null;
  readonly uuid: string | null;
  readonly icon: string | null;
};

type WalletContext = {
  userAction?: boolean;
  selected?: boolean;
  approvedMessage?: string;
  address?: string;
};

type Abandon = ReturnType<typeof createAbandonSignal>;

const registry = createWalletRegistry({ win: window, onChange: () => onWalletsChanged() });
let selectedCandidate: WalletCandidate | null = null;
// Local connection generation. Bumped when the provider actually in use changes.
let connectionEpoch = 0;
let connectionAbandon: Abandon = createAbandonSignal();
// A provider switch with a live session must revoke it on the server before a new login.
let revokePending = false;
let revokeAfterAuth = false;
let loginActive = false;
let lastRevocation: { reason: string; confirmed: boolean; status: number | null } | null = null;

function walletCandidates(): WalletCandidate[] {
  return registry.list() as unknown as WalletCandidate[];
}

// WALLET-CONNECT-01 W-B. Offered only when both public build settings are present (fail closed).
// It is never an implicit choice: the SDK loads and the relay is contacted only after the user picks
// WalletConnect and presses login. The candidate has the same shape as an injected wallet, so
// selection, switching, guardedRequest() and the request policy are shared with W-A.
type WalletConnectStatus = {
  state: string;
  address: string | null;
  reason: string | null;
  message: string | null;
  uri: string | null;
  links: { id: string; label: string; href: string; kind: string; newTab: boolean }[];
  drawQr: ((canvas: unknown, uri: string) => boolean) | null;
  clearQr: ((canvas: unknown) => void) | null;
  extraMethods: string[];
};

const WC_CONFIG = walletConnectConfig({
  enabled: import.meta.env.VITE_WALLETCONNECT_ENABLED,
  projectId: import.meta.env.VITE_REOWN_PROJECT_ID,
});

function browserStorage(): Storage | null {
  try {
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

const walletConnect = import.meta.env.VITE_WALLETCONNECT_ENABLED === "true" && WC_CONFIG.enabled && WC_CONFIG.projectId
  ? createWalletConnectConnection({
    projectId: WC_CONFIG.projectId,
    origin: window.location.origin,
    rpcUrl: TRUSTED_LISTA.rpcUrl,
    loadSdk: () => import("./wallets/walletconnect-sdk.mjs"),
    onStatus: (status: WalletConnectStatus) => renderWalletConnectPanel(status),
    flagStorage: browserStorage(),
  })
  : null;
const wcCandidate = walletConnect ? (walletConnect.candidate as unknown as WalletCandidate) : null;

function isWalletConnect(candidate: WalletCandidate | null): boolean {
  return candidate != null && candidate.source === SOURCE_WALLETCONNECT;
}

let wcPanel: {
  root: HTMLElement;
  text: HTMLElement;
  qr: HTMLCanvasElement;
  links: HTMLElement;
  cancel: HTMLButtonElement;
  disconnect: HTMLButtonElement;
} | null = null;

function walletConnectPanel(): typeof wcPanel {
  if (wcPanel || !walletConnect) return wcPanel;
  const picker = document.querySelector<HTMLElement>("#wallet-picker");
  if (!picker) return null;
  const root = document.createElement("section");
  root.id = "wc-panel";
  root.className = "wc-panel";
  root.hidden = true;
  const text = document.createElement("p");
  text.className = "status wc-status";
  const qr = document.createElement("canvas");
  qr.className = "wc-qr";
  qr.width = 0;
  qr.height = 0;
  if (typeof qr.setAttribute === "function") qr.setAttribute("aria-label", "WalletConnect 連線 QR code（只用於唯讀登入）");
  const links = document.createElement("div");
  links.className = "wc-links";
  const note = document.createElement("p");
  note.className = "lock-note";
  note.textContent = "桌機：用手機錢包 App 掃描 QR。手機：按錢包按鈕跳到錢包 App，確認後切回這個分頁；不用掃自己的 QR。連線邀請 5 分鐘內有效，只核准 BNB Smart Chain 的 personal_sign，不送出交易。";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary";
  cancel.textContent = "取消 WalletConnect 連線";
  cancel.addEventListener("click", () => walletConnect?.cancel());
  const disconnect = document.createElement("button");
  disconnect.type = "button";
  disconnect.className = "secondary";
  disconnect.textContent = "中斷 WalletConnect";
  disconnect.addEventListener("click", () => {
    void walletConnect?.disconnect();
  });
  const row = document.createElement("div");
  row.className = "row";
  row.append(cancel, disconnect);
  root.append(text, qr, links, note, row);
  picker.append(root);
  wcPanel = { root, text, qr, links, cancel, disconnect };
  return wcPanel;
}

function wipePairing(panel: NonNullable<typeof wcPanel>, clearQr: WalletConnectStatus["clearQr"]): void {
  panel.links.replaceChildren();
  if (clearQr) clearQr(panel.qr);
  else {
    panel.qr.width = 0;
    panel.qr.height = 0;
  }
}

// The pairing URI exists only in the QR canvas and the jump links while pairing. Any other state
// removes both. It is never written to the banner, storage, the server or a log.
function renderWalletConnectPanel(status: WalletConnectStatus): void {
  const panel = walletConnectPanel();
  if (!panel) return;
  wipePairing(panel, status.clearQr);
  panel.cancel.hidden = !(status.state === "loading" || status.state === "pairing");
  panel.disconnect.hidden = !(status.state === "connected" || status.state === "signing");
  panel.root.hidden = status.state === "idle" && !status.message;
  if (status.state === "pairing" && status.uri) {
    if (status.drawQr) status.drawQr(panel.qr, status.uri);
    for (const link of status.links) {
      const anchor = document.createElement("a");
      anchor.className = `wc-link wc-link-${link.kind}`;
      anchor.href = link.href;
      anchor.rel = "noopener noreferrer";
      anchor.referrerPolicy = "no-referrer";
      if (link.newTab) anchor.target = "_blank";
      anchor.textContent = link.label;
      panel.links.append(anchor);
    }
    panel.text.textContent = "等待錢包核准 WalletConnect 連線（只要求 BNB Smart Chain 的 personal_sign）。";
    return;
  }
  const extra = status.extraMethods.length > 0
    ? `錢包另外核准了 ${status.extraMethods.length} 個方法；本頁不會使用，只用 personal_sign 簽固定登入訊息。`
    : "";
  if (status.state === "loading") panel.text.textContent = "正在載入 WalletConnect 並連線中繼（relay）…";
  else if (status.state === "connected") panel.text.textContent = `WalletConnect 已連線：${status.address ?? ""}。這不是網站登入，也不是交易授權。${extra}`;
  else if (status.state === "signing") panel.text.textContent = "已把固定登入訊息送到錢包 App。請在錢包確認；手機請切到錢包 App，確認後切回這個分頁。";
  else panel.text.textContent = status.message ?? "WalletConnect 未連線。";
}

// W-A2: one connection for every wallet operation. An explicit selection wins. Otherwise the
// provider already in use stays (another wallet's announcement is not a switch). Otherwise a
// sole candidate is used. Several candidates and no selection means no wallet calls at all.
function activeConnection(): WalletCandidate | null {
  if (selectedCandidate) return selectedCandidate;
  registry.refresh();
  const candidates = walletCandidates();
  if (watchedProvider) {
    const kept = candidates.find((item) => item.provider === watchedProvider);
    if (kept) return kept;
  }
  return candidates.length === 1 ? candidates[0] : null;
}

function ethereumProvider(): EthereumProvider | null {
  return activeConnection()?.provider ?? null;
}

function walletCall(connection: WalletCandidate, method: string, params?: unknown[], context: WalletContext = {}): Promise<unknown> {
  return guardedRequest(connection, method, params, context, connectionAbandon) as Promise<unknown>;
}

function bumpConnection(): void {
  connectionEpoch += 1;
  connectionAbandon.abandon();
  connectionAbandon = createAbandonSignal();
  walletAccount = null;
  walletChainId = null;
}

function currentVerification(): RereadView {
  if (forcedReason) return { ...emptyRereadView(lastPlan?.simulation), reasons: [forcedReason] };
  if (rereadPending) return { ...emptyRereadView(lastPlan?.simulation), reasons: ["updating"] };
  if (walletChainId != null && walletChainId !== TRUSTED_LISTA.chainId) {
    return { ...emptyRereadView(lastPlan?.simulation), reasons: ["wrong_chain"] };
  }
  if (!lastPlan || !rereadResult) return emptyRereadView(lastPlan?.simulation);
  return settleReread(rereadResult, {
    simulation: lastPlan.simulation,
    account: walletAccount ?? lastPlan.sender,
    chainId: rereadResult.binding ? rereadResult.binding.chainId : TRUSTED_LISTA.chainId,
    planDigest: lastPlan.digest,
    taskRef: lastTask?.ref ?? null,
    revision: privateRevision ?? lastPlan.revision,
    nowUnix: Math.floor(Date.now() / 1000),
  });
}

function armExpiry(until: number): void {
  if (expiryTimer) clearTimeout(expiryTimer);
  const token = ++expiryGeneration;
  const delay = Math.max(0, until * 1000 - Date.now());
  const timer = setTimeout(() => {
    if (token !== expiryGeneration) return;
    paintVerification(currentVerification());
  }, delay);
  const nodeTimer = timer as unknown as { unref?: () => void };
  if (typeof nodeTimer.unref === "function") nodeTimer.unref();
  expiryTimer = timer;
}

function paintVerification(model: RereadView): void {
  const lines = statusLines(model);
  if ((model.reread === "pass" || model.reread === "fail") && typeof model.binding?.freshUntil === "number") {
    armExpiry(model.binding.freshUntil);
  }
  const reread = document.querySelector<HTMLElement>("#verify-reread");
  const simulation = document.querySelector<HTMLElement>("#verify-simulation");
  const send = document.querySelector<HTMLElement>("#verify-send");
  const reasons = document.querySelector<HTMLElement>("#verify-reasons");
  if (reread) {
    reread.textContent = lines.reread;
    if (reread.dataset) reread.dataset.state = model.reread;
  }
  if (simulation) {
    simulation.textContent = lines.simulation;
    if (simulation.dataset) simulation.dataset.state = model.simulation;
  }
  if (send) {
    send.textContent = lines.send;
    if (send.dataset) send.dataset.state = "closed";
  }
  if (reasons) {
    reasons.replaceChildren();
    for (const code of model.reasons) {
      const item = document.createElement("li");
      item.textContent = REASON_TEXT[code as keyof typeof REASON_TEXT] ?? code;
      reasons.append(item);
    }
  }
  paintStatusChips();
}

function unlisten(provider: EthereumProvider): void {
  if (!provider.removeListener) {
    walletHandlers.length = 0;
    return;
  }
  for (const entry of walletHandlers) provider.removeListener(entry.event, entry.handler);
  walletHandlers.length = 0;
}

function listen(provider: EthereumProvider, event: string, handler: (value: unknown) => void): void {
  provider.on?.(event, handler);
  walletHandlers.push({ event, handler });
}

function watchWallet(): void {
  const provider = ethereumProvider();
  if (provider === watchedProvider) return;
  if (watchedProvider) {
    unlisten(watchedProvider);
    walletEpoch += 1;
    bumpConnection();
    dropReread("provider_replaced");
    paintVerification(currentVerification());
  }
  watchedProvider = provider;
  renderWalletPicker();
  if (!provider?.on) return;
  // Events from a provider that is no longer the connection are ignored even if it kept the listener.
  const live = () => provider === watchedProvider;
  listen(provider, "accountsChanged", (value: unknown) => {
    if (!live()) return;
    const first = Array.isArray(value) ? String(value[0] ?? "") : "";
    walletAccount = /^0x[a-fA-F0-9]{40}$/.test(first) ? first.toLowerCase() : null;
    walletEpoch += 1;
    dropReread();
    paintVerification(currentVerification());
    if (csrfToken && sessionAddress && walletAccount && walletAccount !== sessionAddress.toLowerCase()) {
      banner.textContent = "錢包帳戶已變更，和目前的讀取身分不同。要改用新帳戶，請重新登入；登入前會先撤銷舊的登入。不會送出交易。";
    }
  });
  listen(provider, "chainChanged", (value: unknown) => {
    if (!live()) return;
    const hex = typeof value === "string" ? value : "";
    const parsed = /^0x[0-9a-fA-F]+$/.test(hex) ? Number.parseInt(hex, 16) : Number.NaN;
    walletChainId = Number.isSafeInteger(parsed) ? parsed : null;
    walletEpoch += 1;
    dropReread();
    paintVerification(currentVerification());
  });
  listen(provider, "disconnect", () => {
    if (!live()) return;
    walletEpoch += 1;
    walletAccount = null;
    dropReread("provider_disconnected");
    paintVerification(currentVerification());
  });
}

function bindResumeListeners(): void {
  const resume = () => {
    walletConnect?.resume();
    if (registry.refresh()) renderWalletPicker();
    watchWallet();
    paintVerification(currentVerification());
  };
  if (typeof document.addEventListener === "function") document.addEventListener("visibilitychange", resume);
  if (typeof window.addEventListener === "function") {
    window.addEventListener("focus", resume);
    window.addEventListener("pageshow", resume);
  }
}

async function reverifyCurrent(testApprovals?: unknown[]): Promise<void> {
  const generation = loadGeneration;
  const seq = ++rereadSeq;
  const epoch = walletEpoch;
  const plan = lastPlan;
  const task = lastTask;
  const revision = privateRevision ?? plan?.revision ?? null;
  if (!plan) return;
  const startedAt = Math.floor(Date.now() / 1000);
  forcedReason = null;
  rereadPending = true;
  paintVerification(currentVerification());
  watchWallet();
  const still = () => seq === rereadSeq && epoch === walletEpoch && generation === loadGeneration && lastPlan === plan;
  let connectedAccount: string | null = null;
  let roundChain: number | null = null;
  let walletIdentityVerified = false;
  const connection = activeConnection();
  if (!connection) {
    walletAccount = null;
    walletChainId = null;
  } else {
    try {
      const accounts = await walletCall(connection, "eth_accounts");
      if (!still()) return;
      const first = Array.isArray(accounts) ? String(accounts[0] ?? "") : "";
      if (!/^0x[a-fA-F0-9]{40}$/.test(first)) throw new Error("wallet account");
      const chain = await walletCall(connection, "eth_chainId");
      if (!still()) return;
      if (typeof chain !== "string" || !/^0x[0-9a-fA-F]+$/.test(chain)) throw new Error("wallet chain");
      const parsed = Number.parseInt(chain, 16);
      if (!Number.isSafeInteger(parsed)) throw new Error("wallet chain");
      connectedAccount = first;
      roundChain = parsed;
      walletIdentityVerified = true;
      walletAccount = first.toLowerCase();
      walletChainId = parsed;
    } catch {
      if (!still()) return;
      connectedAccount = null;
      roundChain = null;
      walletIdentityVerified = false;
      walletAccount = null;
      walletChainId = null;
    }
  }
  try {
    const result = await reverifyListaClaim({
      call: (method: string, params: unknown[]) => fetchTrustedRpc(method, params, {
        fetchImpl: fetch,
        signal: AbortSignal.timeout(10000),
      }),
      plan,
      task,
      account: plan.sender,
      connectedAccount,
      walletChainId: roundChain,
      walletIdentityVerified,
      planDigest: plan.digest,
      revision,
      startedAt,
      nowUnix: startedAt,
      clock: () => Math.floor(Date.now() / 1000),
      ...(Array.isArray(testApprovals) ? { testApprovals } : {}),
    });
    if (!still() || (privateRevision ?? plan.revision) !== revision) return;
    rereadPending = false;
    rereadResult = result as RereadView;
  } catch (error) {
    if (!still()) return;
    rereadPending = false;
    const name = error instanceof Error ? error.name : "";
    const reason = name === "TimeoutError" || name === "AbortError" ? "timeout" : "rpc_failed";
    rereadResult = { ...emptyRereadView(plan.simulation), reasons: [reason] };
  }
  if (still()) paintVerification(currentVerification());
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
  throw new Error("獨立重讀不是送出授權。允許實際送出保持關閉。不會呼叫錢包。");
}

// Presentation only. Copies state this page already holds into the status chips.
// A missing chip is a no-op so the review harness, which only knows the original ids, is unchanged.
function paintStatus(rootId: string, valueId: string, value: string, state: string): void {
  const valueNode = document.querySelector<HTMLElement>(valueId);
  if (valueNode) {
    valueNode.textContent = value;
    enhanceText(valueNode);
  }
  const root = document.querySelector<HTMLElement>(rootId);
  if (root?.dataset) root.dataset.state = state;
}

function setWalletDisplayName(nameEl: HTMLElement, displayName: string): void {
  const { primary, detail } = splitTrailingParenthetical(displayName);
  nameEl.textContent = primary;
  if (detail) {
    const detailEl = document.createElement("span");
    detailEl.className = "wallet-name-detail";
    detailEl.textContent = detail;
    nameEl.append(detailEl);
  }
}

// The 401 message from loadPrivateTask() is a "please log in" state, not a failure.
function needsLoginView(): boolean {
  return view.className === "error" && (view.textContent ?? "").startsWith("這個任務要先登入");
}

function taskStatusLabel(): string {
  if (needsLoginView()) return "需要登入";
  if (view.className === "error") return "無法顯示";
  if ((view.textContent ?? "").startsWith("未執行任務已取消")) return "已取消";
  if (view.className === "empty") return "尚未載入";
  if (lastTask) return "已載入";
  if (lastPlan) return "本機計畫";
  return "—";
}

function chainStatusLabel(): string {
  const label = lastPlan?.human_summary?.chain_label;
  if (walletChainId != null && lastPlan != null && walletChainId === lastPlan.chain_id && label) return label;
  if (walletChainId != null) return String(walletChainId);
  if (label) return label;
  return "未知";
}

function walletStatusLabel(): { text: string; state: string } {
  const selectedName = selectedCandidate?.name?.trim() ?? "";
  if (selectedName.length > 0) return { text: selectedName, state: "ok" };
  const found = walletCandidates();
  const onlyName = found.length === 1 ? (found[0]?.name?.trim() ?? "") : "";
  if (onlyName.length > 0) return { text: onlyName, state: "ok" };
  if (found.length > 1 || wcCandidate) return { text: "未選擇", state: "warn" };
  return { text: "—", state: "idle" };
}

function paintStatusChips(): void {
  // Presentation must never interrupt a login, wallet, or task path.
  try {
    paintStatusChipsUnsafe();
  } catch {
    // Chips keep their previous text; the authoritative state lives in #session, #verify and #view.
  }
}

function paintStatusChipsUnsafe(): void {
  const loggedIn = Boolean(csrfToken && sessionAddress);
  paintStatus(
    "#chip-login",
    "#chip-login-value",
    authBusy ? "處理中…" : loggedIn ? "已登入" : "尚未登入",
    authBusy ? "warn" : loggedIn ? "ok" : "idle",
  );
  const wallet = walletStatusLabel();
  paintStatus("#chip-wallet", "#chip-wallet-value", wallet.text, wallet.state);
  const chainText = chainStatusLabel();
  const chainMismatch = walletChainId != null && lastPlan != null && walletChainId !== lastPlan.chain_id;
  paintStatus("#chip-chain", "#chip-chain-value", chainText, chainMismatch ? "warn" : chainText === "未知" ? "idle" : "ok");
  const taskText = taskStatusLabel();
  if (view.dataset) view.dataset.tone = needsLoginView() ? "login" : "";
  const taskState = taskText === "無法顯示" ? "danger" : taskText === "已取消" || taskText === "需要登入" ? "warn" : taskText === "已載入" || taskText === "本機計畫" ? "ok" : "idle";
  paintStatus("#chip-task", "#chip-task-value", taskText, taskState);
  const sendValue = document.querySelector<HTMLElement>("#chip-send-value");
  if (sendValue) sendValue.textContent = "允許實際送出：關閉";
}

function syncAccountControls(): void {
  const logout = document.querySelector<HTMLButtonElement>("#logout");
  const cancel = document.querySelector<HTMLButtonElement>("#cancel-task");
  const loginButton = document.querySelector<HTMLButtonElement>("#login");
  const identity = document.querySelector<HTMLElement>("#session");
  if (logout) { logout.hidden = !csrfToken; logout.disabled = authBusy; }
  if (loginButton) loginButton.disabled = authBusy;
  if (cancel) { cancel.hidden = !privateCancelAllowed; cancel.disabled = authBusy; }
  if (identity) {
    identity.textContent = csrfToken && sessionAddress
      ? `讀取身分：${sessionAddress}。不是交易授權。`
      : "尚未登入。登入只建立讀取工作階段，不是交易授權。";
    if (typeof identity.setAttribute === "function" && csrfToken && sessionAddress) identity.setAttribute("translate", "no");
    else if (typeof identity.removeAttribute === "function") identity.removeAttribute("translate");
  }
  paintStatusChips();
}

function hideCancel(): void {
  privateCancelAllowed = false;
  syncAccountControls();
}

function renderError(message: string): void {
  ++loadGeneration;
  lastPlan = null;
  lastTask = null;
  observedCapability = "unknown";
  dropReread();
  hideCancel();
  view.className = "error";
  view.textContent = message;
  paintVerification(emptyRereadView(undefined));
}

function renderEmpty(): void {
  view.className = "empty";
  view.textContent = "尚未載入任務。合成範例不是你的鏈上餘額。";
  paintVerification(emptyRereadView(undefined));
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
    if (/0x[a-fA-F0-9]{8,}/.test(value) || /^ref_[a-f0-9]{8,}$/.test(value)) {
      detail.className = "mono";
      if (typeof detail.setAttribute === "function") detail.setAttribute("translate", "no");
    }
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
  if (!decision.buttonEnabled) reasons.push("確認鈕關閉。任務自述的 preflight 不能當成重讀通過，也不能打開送出。");
  if (!MAINNET_SEND) reasons.push("VITE_GUARDIAN_MAINNET_SEND 沒有打開。監控程式不能簽名。");
  if (task?.mode === "C") reasons.push("模式 C 不支援。沒有鏈上權限合約。");
  if (task && !task.link?.issued) reasons.push("這筆任務沒有已核准的查看來源，所以沒有發行查看連結。");
  banner.textContent = "尚未對錢包提出交易。主網送出保持關閉。重讀通過不是模擬通過，也不是送出授權。鎖畫面不等於鎖鏈。";

  const alternatives = (plan.alternatives ?? [])
    .map((item) => `${item.id}${item.completes_original_redeem ? "" : "（不是完成原贖回）"}`)
    .join("、");
  const linkLabel = task?.link?.issued
    ? task.link.purpose === "private_task_view"
      ? "已發行私人查看連結（/t/<ref>，需本人登入）。只用於查看，不是執行授權。"
      : "不跟隨這份資料裡的網址。連結不是執行授權。"
    : "未發行";

  view.className = "card";
  view.replaceChildren();

  const mode = document.createElement("p");
  mode.className = "mode";
  mode.textContent = modeLine(task, modeName);

  const heading = document.createElement("h2");
  heading.textContent = "Lista 提款計畫預覽（尚未驗證可執行）";

  const list = document.createElement("dl");
  list.className = "facts";
  appendFacts(list, [
    ["來源錢包", summary.sender],
    ["鏈", summary.chain_label],
    ["原申請數量（非本次支出）", `${units(summary.amount_in_raw, plan.asset_in.decimals)} ${summary.amount_in_unit}`],
    ["計畫中的領取數量（待重查）", `${units(summary.amount_out_raw, plan.asset_out.decimals)} ${summary.amount_out_unit}`],
    ["接收地址", summary.recipient],
    ["這次確認", plan.sender.toLowerCase() === plan.recipient.toLowerCase() ? POISON_NOTE : "接收地址不是原地址。"],
    ["費用", summary.fee_label],
    ["模擬", `${summary.simulation_status}（${plan.simulation.source}，區塊 ${plan.simulation.block_number ?? "未知"}）`],
    ["其他路徑", alternatives || "無"],
    ["任務參考碼", task?.ref ?? "僅本機檔案"],
    ["查看連結", linkLabel],
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
    "模式 A：引導路徑與不廣播測試已寫入。真錢包未測，主網送出未授權",
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
    const clickGeneration = generation;
    void (async () => {
      try {
        if (clickGeneration !== loadGeneration) return;
        const guided = await guideListaModeA({
          plan,
          task,
          account: plan.sender,
          flow: modeAFlow,
          identity: () => {
            if (!lastPlan || !lastTask) return null;
            return {
              connectedAccount: walletAccount,
              walletChainId,
              walletIdentityVerified: walletAccount != null && walletChainId === TRUSTED_LISTA.chainId,
              revision: privateRevision ?? lastPlan.revision,
              planDigest: lastPlan.digest,
              taskRef: lastTask.ref,
            };
          },
          isCurrent: () => clickGeneration === loadGeneration && lastPlan != null && lastTask != null,
          clock: () => Math.floor(Date.now() / 1000),
          call: (method: string, params: unknown[]) => fetchTrustedRpc(method, params, { fetchImpl: fetch }),
          testOnlyAllowWalletRequest: false,
          mainnetSend: MAINNET_SEND,
          provider: null,
        });
        if (clickGeneration !== loadGeneration) return;
        paintWalletOutcome(outcomeAfterReread(modeAFlow).status);
        if (guided.allowSend !== "closed" || guided.walletRequested !== false || guided.broadcast !== false) {
          throw new Error("允許實際送出保持關閉。不會呼叫錢包。");
        }
        requestUserSend(plan);
      } catch (error) {
        if (clickGeneration !== loadGeneration) return;
        banner.textContent = error instanceof Error ? error.message : "已停止";
      }
    })();
  });
  const capability = document.createElement("button");
  capability.type = "button";
  capability.className = "secondary";
  capability.textContent = "查詢批次能力";
  capability.addEventListener("click", () => {
    void queryCapability(plan.chain_id, generation);
  });
  const reread = document.createElement("button");
  reread.type = "button";
  reread.className = "secondary";
  reread.textContent = "重讀鏈上狀態（唯讀）";
  reread.addEventListener("click", () => {
    if (generation !== loadGeneration) return;
    void reverifyCurrent();
  });
  actions.append(confirm, capability, reread);
  paintVerification(currentVerification());

  const details = document.createElement("details");
  details.className = "code-fold";
  const summaryNode = document.createElement("summary");
  summaryNode.textContent = "技術細節";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(task ?? plan, null, 2);
  if (typeof pre.setAttribute === "function") pre.setAttribute("translate", "no");
  details.append(summaryNode, pre);

  lastPlan = plan;
  lastTask = task;
  view.append(mode, heading, list, stepHeading, steps, wait, actions, reasonList, evidenceHeading, evidence, details);
  paintStatusChips();
}

async function queryCapability(chainId: number, generation: number): Promise<void> {
  if (generation !== loadGeneration) return;
  const stamp = viewStamp(generation, authGeneration, privateRef, privateRevision);
  const connection = activeConnection();
  if (!connection) {
    banner.textContent = walletCandidates().length > 1
      ? "請先選擇錢包，再查詢 EIP-5792。維持模式 A，不會送出交易。"
      : "找不到瀏覽器錢包，無法查詢 EIP-5792。維持模式 A，不會送出交易。";
    return;
  }
  try {
    const capabilities = await walletCall(connection, "wallet_getCapabilities", undefined, { userAction: true });
    syncFlow();
    if (!canRedrawCapability(flow, stamp) || generation !== loadGeneration) return;
    observedCapability = eip5792Status(capabilities, chainId);
  } catch {
    syncFlow();
    if (!canRedrawCapability(flow, stamp) || generation !== loadGeneration) return;
    observedCapability = "unknown";
    banner.textContent = isWalletConnect(connection)
      ? "WalletConnect 連線只核准 personal_sign，不查詢批次能力（unknown）。維持模式 A，不會送出交易。"
      : "批次能力查詢失敗。維持模式 A，不會送出交易。";
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

type SessionBody = {
  authenticated?: boolean;
  address?: string;
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
    if (!headers.has("x-csrf-token")) {
      syncFlow();
      const token = mutationCsrf(flow, path === "/api/auth/verify" ? "verify" : "session");
      if (token) headers.set("x-csrf-token", token);
    }
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
  const record = body && typeof body === "object" ? body as SessionBody : {};
  const valid = record.authenticated === true && record.execution_authorized === false &&
    record.send_authorized === false && typeof record.csrf_token === "string" &&
    /^[a-f0-9]{64}$/.test(record.csrf_token) && typeof record.address === "string" &&
    /^0x[a-fA-F0-9]{40}$/.test(record.address);
  csrfToken = valid ? record.csrf_token! : null;
  sessionAddress = valid ? record.address! : null;
  syncAccountControls();
  return valid;
}

async function loadPrivateTask(ref: string, generationOrStamp: number | ViewStamp): Promise<void> {
  const generation = typeof generationOrStamp === "number" ? generationOrStamp : generationOrStamp.generation;
  const stamp = typeof generationOrStamp === "number"
    ? viewStamp(generation, authGeneration, ref, privateRevision)
    : generationOrStamp;
  try {
    const response = await api(`/api/tasks/${ref}`, { signal: AbortSignal.timeout(10000) });
    const body = (await readBoundedJson(response)) as PrivateTaskBody;
    syncFlow();
    if (!stampMatches(flow, stamp) || generation !== loadGeneration) return;
    if (response.status === 401) {
      applySession(null);
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
    const nextRevision = typeof body.revision === "number" ? body.revision : null;
    if (nextRevision !== privateRevision) dropReread();
    privateRevision = nextRevision;
    privateCancelAllowed = body.cancel_allowed === true && privateRevision != null;
    syncAccountControls();
    await renderPayload(body.task, generation);
  } catch {
    syncFlow();
    if (stampMatches(flow, stamp) && generation === loadGeneration) {
      renderError("無法載入私人任務。不跟隨重新導向，也不會送出交易。");
    }
  }
}

async function openPrivateTask(ref: string): Promise<void> {
  const generation = ++loadGeneration;
  privateRef = ref;
  dropReread();
  paintVerification(emptyRereadView(undefined));
  hideCancel();
  if (!csrfToken) {
    renderError("這個任務要先登入才能讀。登入只簽一則讀取訊息，不會送出交易。");
    return;
  }
  await loadPrivateTask(ref, generation);
}

// W-A2: the user picks the wallet before any account request. One candidate is used directly.
function connectionForLogin(): WalletCandidate | null {
  if (selectedCandidate) return selectedCandidate;
  registry.refresh();
  const candidates = walletCandidates();
  if (candidates.length !== 1) return null;
  selectedCandidate = candidates[0];
  watchWallet();
  renderWalletPicker();
  return selectedCandidate;
}

// Server-side revocation for a wallet/account switch. Local state is cleared first; the result
// is only reported as revoked when the server answers revoked=true (or no session remains).
async function revokeServerSession(reason: string): Promise<boolean> {
  syncFlow();
  beginLogout(flow);
  applyFlowView();
  dropReread();
  hideCancel();
  renderEmpty();
  syncAccountControls();
  try {
    const response = await api("/api/auth/logout", {
      method: "POST", body: JSON.stringify({}), signal: AbortSignal.timeout(10000),
    });
    const result = await readBoundedJson(response) as { authenticated?: boolean; revoked?: boolean };
    if (response.status !== 401 && (!response.ok || result.authenticated !== false || result.revoked !== true)) {
      throw new Error("revocation unconfirmed");
    }
    finishLogout(flow);
    applyFlowView();
    applySession(null);
    revokePending = false;
    lastRevocation = { reason, confirmed: true, status: response.status };
    return true;
  } catch {
    revokePending = true;
    lastRevocation = { reason, confirmed: false, status: null };
    return false;
  }
}

// A verify answer that arrives after a switch is never applied. If it created a session, that
// session is revoked with its own CSRF so it cannot come back on reload.
async function revokeOrphanSession(body: unknown): Promise<void> {
  const record = body && typeof body === "object" ? body as SessionBody : {};
  if (typeof record.csrf_token !== "string" || !/^[a-f0-9]{64}$/.test(record.csrf_token)) return;
  try {
    const response = await api("/api/auth/logout", {
      method: "POST", headers: { "x-csrf-token": record.csrf_token },
      body: JSON.stringify({}), signal: AbortSignal.timeout(10000),
    });
    const result = await readBoundedJson(response) as { authenticated?: boolean; revoked?: boolean };
    const confirmed = response.status === 401 || (response.ok && result.authenticated === false && result.revoked === true);
    lastRevocation = { reason: "late_verify", confirmed, status: response.status };
  } catch {
    lastRevocation = { reason: "late_verify", confirmed: false, status: null };
  }
}

async function login(): Promise<void> {
  if (authBusy) return;
  const connection = connectionForLogin();
  if (!connection) {
    banner.textContent = walletCandidates().length > 1 || (walletCandidates().length === 1 && wcCandidate)
      ? "偵測到多個錢包選項。請先選擇要登入的錢包；選擇前不會向任何錢包索取帳戶。不會送出交易。"
      : wcCandidate
        ? "目前找不到瀏覽器錢包。可選擇 WalletConnect，用手機錢包掃 QR 或跳轉錢包 App 登入。不會送出交易。"
        : "目前找不到瀏覽器錢包，無法登入。頁面仍會接收晚到的錢包公告。不會送出交易。";
    renderWalletPicker();
    return;
  }
  authBusy = true;
  loginActive = true;
  revokeAfterAuth = false;
  const authEpoch = ++authGeneration;
  const connEpoch = connectionEpoch;
  invalidatePrivateView();
  syncAccountControls();
  if (beginLogin(flow) == null) {
    authBusy = false;
    loginActive = false;
    syncAccountControls();
    return;
  }
  // Same auth generation, same connection generation, same selected provider.
  const current = () => authEpoch === authGeneration && connEpoch === connectionEpoch &&
    selectedCandidate != null && selectedCandidate.provider === connection.provider;
  try {
    // W-A3: a switched provider revokes the previous site session before a new identity logs in.
    if (csrfToken && revokePending) {
      if (!(await revokeServerSession("provider_switch"))) throw new Error("舊登入撤銷未確認。");
      if (!current() || beginLogin(flow) == null) return;
    }
    if (isWalletConnect(connection)) {
      banner.textContent = "正在建立 WalletConnect 連線。請用手機錢包掃 QR，或在手機上按錢包按鈕。只會要求唯讀登入簽名，不會送出交易。";
    }
    const accounts = await walletCall(connection, "eth_requestAccounts", undefined, { userAction: true, selected: true });
    if (!current()) return;
    const address = Array.isArray(accounts) ? String(accounts[0] ?? "") : "";
    const chain = await walletCall(connection, "eth_chainId");
    if (!/^0x[a-fA-F0-9]{40}$/.test(address) || typeof chain !== "string" || !/^0x[0-9a-f]+$/i.test(chain)) {
      throw new Error("錢包沒有提供可用帳戶與鏈。");
    }
    const chainId = Number(BigInt(chain));
    if (!Number.isSafeInteger(chainId) || chainId <= 0 || !current()) return;
    // W-A3: a different account than the live session is a switch too. Revoke first.
    if (csrfToken && sessionAddress && sessionAddress.toLowerCase() !== address.toLowerCase()) {
      if (!(await revokeServerSession("account_switch"))) throw new Error("舊登入撤銷未確認。");
      if (!current() || beginLogin(flow) == null) return;
    }
    const challengeResponse = await api("/api/auth/challenge", {
      method: "POST", body: JSON.stringify({ address, chainId }), signal: AbortSignal.timeout(10000),
    });
    const issued = await readBoundedJson(challengeResponse);
    if (!current()) return;
    if (!challengeResponse.ok) throw new Error("無法取得這條鏈的唯讀登入挑戰。");
    const rebuilt = acceptLoginChallenge(issued, address, Date.now());
    const challenge = validateLoginChallenge(issued, { origin: window.location.origin, address, chainId });
    if (typeof rebuilt !== "string" || rebuilt !== challenge.message || !noteAttemptCsrf(flow, challenge.csrfToken)) {
      throw new Error("登入訊息不是固定的讀取聲明。不會簽名。");
    }
    // Also pin the wallet identity immediately before offering the message.
    const currentAccounts = await walletCall(connection, "eth_accounts");
    const currentChain = await walletCall(connection, "eth_chainId");
    if (!current() || !Array.isArray(currentAccounts) ||
        String(currentAccounts[0]).toLowerCase() !== address.toLowerCase() || currentChain !== chain) {
      throw new Error("錢包帳戶或鏈已變更，請重新登入。");
    }
    // Keep the challenge CSRF local. A rejected login must not corrupt the
    // existing session's CSRF token used by logout and task cancellation.
    // W-A4: only the fixed SIWE message, only to the selected provider. The transport encoding
    // may differ per adapter; the signed bytes may not. A refusal is final (no eth_sign fallback).
    const payload = encodePersonalSignMessage(rebuilt, signEncodingFor(connection));
    if (isWalletConnect(connection)) {
      banner.textContent = "已把固定登入訊息送到錢包 App。請在錢包確認；手機請切到錢包 App，確認後切回這個分頁。不會送出交易。";
    }
    const signature = await walletCall(connection, "personal_sign", [payload, address], {
      userAction: true, approvedMessage: rebuilt, address,
    });
    if (!current()) return;
    if (typeof signature !== "string") throw new Error("未取得登入簽名。");
    const verifyCsrf = mutationCsrf(flow, "verify");
    const verifyResponse = await api("/api/auth/verify", {
      method: "POST", headers: { "x-csrf-token": verifyCsrf ?? "" },
      body: JSON.stringify({ message: rebuilt, signature }), signal: AbortSignal.timeout(10000),
    });
    const verified = await readBoundedJson(verifyResponse);
    if (!current()) {
      if (verifyResponse.ok) await revokeOrphanSession(verified);
      return;
    }
    if (!verifyResponse.ok) throw new Error("登入沒有完成。");
    if (!applySession(verified)) throw new Error("登入回應不完整。");
    revokePending = false;
    banner.textContent = `已用「${connection.name}」登入。這只是讀取身分，不是交易授權。主網送出保持關閉。`;
    if (privateRef) {
      const generation = ++loadGeneration;
      await loadPrivateTask(privateRef, generation);
    }
  } catch (error) {
    if (current()) {
      // Duck-typed (not instanceof) so the check does not depend on the realm the facade module came from.
      const wcError = typeof error === "object" && error !== null && (error as { name?: unknown }).name === "WalletConnectError"
        ? error as { reason?: unknown }
        : null;
      const wcReason = wcError ? (typeof wcError.reason === "string" ? wcError.reason : "rejected") : null;
      banner.textContent = error instanceof Error && error.message === "舊登入撤銷未確認。"
        ? "已切換錢包，但舊登入的伺服器撤銷未確認，所以沒有用新錢包登入。請重試登入或登出；不會送出交易。"
        : wcReason
          ? `${walletConnectReasonText(wcReason)} 登入未完成；既有登入不會因此獲得新權限。不會送出交易。`
          : "登入未完成；既有登入不會因此獲得新權限。可重試或登出，不會送出交易。";
    }
  } finally {
    endLogin(flow);
    loginActive = false;
    if (revokeAfterAuth) {
      // Set only by a switch during this login, which also made this login unable to install a session.
      revokeAfterAuth = false;
      if (csrfToken) await finishSwitchRevocation();
    }
    authBusy = false;
    syncAccountControls();
  }
}

async function finishSwitchRevocation(): Promise<void> {
  const confirmed = await revokeServerSession("provider_switch");
  banner.textContent = confirmed
    ? "已切換錢包。舊登入已在伺服器撤銷（登出回應 revoked=true）。請用新選的錢包登入；不會送出交易。"
    : "已切換錢包，畫面已清除，但舊登入的伺服器撤銷未確認。請按登出重試，或登入時會先再試撤銷；不會送出交易。";
}

// W-A3: picking a different provider is a switch. A new announcement alone never calls this.
async function selectWallet(candidate: WalletCandidate): Promise<void> {
  const previous = activeConnection();
  if (previous && previous.provider === candidate.provider) {
    if (!selectedCandidate) selectedCandidate = candidate;
    renderWalletPicker();
    return;
  }
  selectedCandidate = candidate;
  // W-B: leaving WalletConnect ends its pairing and session; nothing falls back to it later.
  if (walletConnect && isWalletConnect(previous) && !isWalletConnect(candidate)) void walletConnect.disconnect();
  if (!previous && !authBusy) {
    // First choice: nothing was connected yet. No account request happens here.
    watchWallet();
    renderWalletPicker();
    banner.textContent = isWalletConnect(candidate)
      ? "已選擇 WalletConnect。按登入後才會連線中繼並顯示 QR／錢包按鈕；尚未連線。不會送出交易。"
      : `已選擇「${candidate.name}」。登入只會送到這個錢包；尚未向它索取帳戶。不會送出交易。`;
    return;
  }
  ++authGeneration;
  walletEpoch += 1;
  bumpConnection();
  invalidatePrivateView();
  watchWallet();
  renderWalletPicker();
  banner.textContent = `已切換到「${candidate.name}」。舊連線的重查結果與私人畫面已清除。不會送出交易。`;
  if (!csrfToken) return;
  revokePending = true;
  if (authBusy) {
    // A login in flight is abandoned now; revocation runs as soon as it unwinds so an in-flight
    // verify cannot re-install a cookie after the revocation. A logout in flight revokes anyway;
    // if it cannot confirm, revokePending makes the next login revoke first.
    if (loginActive) revokeAfterAuth = true;
    return;
  }
  authBusy = true;
  syncAccountControls();
  try {
    await finishSwitchRevocation();
  } finally {
    authBusy = false;
    syncAccountControls();
  }
}

function onWalletsChanged(): void {
  // Discovery only updates the menu (and the sticky implicit connection). It is not a switch.
  renderWalletPicker();
  watchWallet();
}

function renderWalletPicker(): void {
  const list = document.querySelector<HTMLElement>("#wallet-list");
  const status = document.querySelector<HTMLElement>("#wallet-status");
  const loginButton = document.querySelector<HTMLButtonElement>("#login");
  const candidates = walletCandidates();
  if (list) {
    list.replaceChildren();
    for (const candidate of candidates) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "wallet-option";
      const chosen = selectedCandidate != null && selectedCandidate.provider === candidate.provider;
      if (button.dataset) button.dataset.selected = chosen ? "true" : "false";
      if (typeof button.setAttribute === "function") button.setAttribute("aria-pressed", chosen ? "true" : "false");
      if (candidate.icon) {
        const icon = document.createElement("img");
        icon.src = candidate.icon;
        icon.alt = "";
        icon.referrerPolicy = "no-referrer";
        icon.className = "wallet-icon";
        icon.width = 32;
        icon.height = 32;
        button.append(icon);
      }
      const name = document.createElement("span");
      name.className = "wallet-name";
      setWalletDisplayName(name, candidate.name);
      const meta = document.createElement("small");
      meta.className = "wallet-meta";
      meta.textContent = sourceLabel(candidate);
      button.append(name, meta);
      button.addEventListener("click", () => {
        void selectWallet(candidate);
      });
      list.append(button);
    }
    if (wcCandidate) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "wallet-option wallet-option-wc";
      const chosen = selectedCandidate != null && selectedCandidate.provider === wcCandidate.provider;
      if (button.dataset) button.dataset.selected = chosen ? "true" : "false";
      if (button.dataset) button.dataset.source = SOURCE_WALLETCONNECT;
      if (typeof button.setAttribute === "function") button.setAttribute("aria-pressed", chosen ? "true" : "false");
      const name = document.createElement("span");
      name.className = "wallet-name";
      setWalletDisplayName(name, wcCandidate.name);
      const meta = document.createElement("small");
      meta.className = "wallet-meta";
      meta.textContent = walletConnect?.hadSession()
        ? "WalletConnect v2（Reown）。上次已連線：按登入會先恢復連線，仍要你在錢包簽固定登入訊息，不會自動簽名。"
        : "WalletConnect v2（Reown）：桌機掃 QR、手機跳轉幣安錢包等 App。按登入才會連線中繼（relay）。只用於唯讀登入。";
      button.append(name, meta);
      button.addEventListener("click", () => {
        void selectWallet(wcCandidate);
      });
      list.append(button);
    }
  }
  if (status) {
    status.textContent = candidates.length === 0
      ? wcCandidate && !selectedCandidate
        ? "尚未偵測到瀏覽器錢包（頁面會持續接收錢包公告）。可選擇 WalletConnect，用手機錢包登入。"
        : selectedCandidate
          ? `已選擇：${selectedCandidate.name}。連線與登入只送到這個錢包。`
          : "尚未偵測到瀏覽器錢包。頁面會持續接收錢包公告；這不代表你沒有錢包。"
      : selectedCandidate
        ? `已選擇：${selectedCandidate.name}。連線與登入只送到這個錢包。錢包名稱與 rdns 是錢包自述，不是身分驗證。`
        : candidates.length === 1
          ? "偵測到 1 個錢包。按登入會直接使用它。"
          : "偵測到多個錢包。請先選擇要登入的錢包；選擇前不會向任何錢包索取帳戶。";
  }
  if (loginButton) {
    loginButton.textContent = candidates.length === 1 && !selectedCandidate
      ? "使用此錢包登入（只簽讀取訊息）"
      : "登入（只簽讀取訊息）";
  }
  paintStatusChips();
}

async function logout(): Promise<void> {
  // Serialize authentication mutations so an older verify response cannot
  // install a new cookie after logout has completed.
  if (authBusy) return;
  authBusy = true;
  ++authGeneration;
  syncFlow();
  beginLogout(flow);
  applyFlowView();
  dropReread();
  hideCancel();
  renderEmpty();
  syncAccountControls();
  try {
    const response = await api("/api/auth/logout", {
      method: "POST", body: JSON.stringify({}), signal: AbortSignal.timeout(10000),
    });
    const result = await readBoundedJson(response) as { authenticated?: boolean; revoked?: boolean };
    if (response.status !== 401 && (!response.ok || result.authenticated !== false || result.revoked !== true)) {
      throw new Error("logout unconfirmed");
    }
    finishLogout(flow);
    applyFlowView();
    applySession(null);
    revokePending = false;
    banner.textContent = "已登出。此工作階段已失效。不會送出交易。";
  } catch {
    banner.textContent = "畫面已清除，但伺服器登出結果未確認。請重試登出；不會送出交易。";
  } finally {
    authBusy = false;
    syncAccountControls();
  }
}

async function cancelPrivateTask(): Promise<void> {
  if (authBusy || !privateRef || privateRevision == null || !privateCancelAllowed || !csrfToken) return;
  const ref = privateRef;
  const revision = privateRevision;
  const generation = loadGeneration;
  const stamp = viewStamp(generation, authGeneration, ref, revision);
  try {
    const response = await api(`/api/tasks/${ref}/cancel`, {
      method: "POST",
      body: JSON.stringify({ revision }),
      signal: AbortSignal.timeout(10000),
    });
    let body: unknown;
    try {
      body = await readBoundedJson(response);
    } catch (error) {
      syncFlow();
      if (!stampMatches(flow, stamp)) return;
      if (isCancelUnconfirmed(error)) {
        banner.textContent = CANCEL_PENDING_CONFIRMATION;
        await loadPrivateTask(ref, stamp);
        return;
      }
      banner.textContent = "取消結果未確認，請重新讀取任務狀態。不會送出交易。";
      return;
    }
    syncFlow();
    if (!stampMatches(flow, stamp)) return;
    const outcome = resolveCancel(flow, stamp, { ok: response.ok, body });
    if (outcome === "cancelled") {
      applyFlowView();
      dropReread();
      hideCancel();
      view.className = "card";
      view.textContent = "未執行任務已取消。這只更新讀取紀錄，不是鏈上撤銷，也沒有送出交易。";
      banner.textContent = "主網送出保持關閉。取消不是交易。";
      paintVerification(emptyRereadView(undefined));
      return;
    }
    if (outcome === "stale") return;
    banner.textContent = "任務沒有取消。取消回應不符合已取消的格式。不會把它當成已取消，也不會送出交易。";
  } catch (error) {
    syncFlow();
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

async function restoreSession(): Promise<boolean> {
  const authEpoch = authGeneration;
  const stamp = viewStamp(loadGeneration, authEpoch, privateRef, privateRevision);
  try {
    const response = await api("/api/session", { signal: AbortSignal.timeout(10000) });
    const body = await readBoundedJson(response);
    syncFlow();
    if (!stampMatches(flow, stamp) || authEpoch !== authGeneration || authBusy) return false;
    applySession(response.ok ? body : null);
  } catch {
    syncFlow();
    if (!stampMatches(flow, stamp) || authEpoch !== authGeneration || authBusy) return false;
    applySession(null);
  }
  return true;
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
  const generation = ++loadGeneration;
  dropReread();
  hideCancel();
  try {
    const response = await fetch("/sample-task.json", { redirect: "error", cache: "no-store", signal: AbortSignal.timeout(10000) });
    await renderPayload(await boundedJson(response), generation);
  } catch {
    if (generation === loadGeneration) renderError("讀不到有效合成範例。");
  }
});

document.querySelector<HTMLInputElement>("#file")!.addEventListener("change", async (event) => {
  const generation = ++loadGeneration;
  dropReread();
  hideCancel();
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
  privateRef = null;
  privateRevision = null;
  dropReread();
  hideCancel();
  renderEmpty();
  document.querySelector<HTMLInputElement>("#file")!.value = "";
  banner.textContent = BANNER;
});

installTypographyEnhancements();
registry.start();
watchWallet();
renderWalletPicker();
bindResumeListeners();
// Some wallets inject window.ethereum / binancew3w after load without an EIP-6963 announcement.
// Re-read once shortly after load and on the legacy initialized event. Not a switch by itself.
function refreshInjectedWallets(): void {
  if (registry.refresh()) {
    renderWalletPicker();
    watchWallet();
  }
}
if (typeof window.addEventListener === "function") window.addEventListener("ethereum#initialized", refreshInjectedWallets);
{
  const timer = setTimeout(refreshInjectedWallets, 1500) as unknown as { unref?: () => void };
  if (typeof timer.unref === "function") timer.unref();
}
void restoreSession().then((current) => { if (current) inspectLocation(); });
