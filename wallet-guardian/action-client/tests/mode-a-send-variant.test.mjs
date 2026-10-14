/**
 * Mode A send-variant checks. The recorder counts connection.request calls and does not
 * broadcast. This is not a headless Chrome wallet test.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { originIsApproved } from "../src/approved-origins.mjs";
import { productionViewOrigin } from "../src/view-origin.mjs";
import { claimCalldata, TRUSTED_LISTA } from "../src/trusted-lista.mjs";
import { walletRequestDecision } from "../src/wallets/request-policy.mjs";
import { WC_METHODS } from "../src/wallets/walletconnect-config.mjs";
import {
  MODE_A_AUTH_MAX_AGE_SECONDS,
  runModeASendOrder,
} from "../src/send/mode-a-send-order.mjs";
import { requestModeAClaim } from "../src/send/mode-a-request.mjs";
import {
  expectedModeAClaimTx,
  issueModeAClaimCapability,
  modeAClaimDecision,
} from "../src/send/request-policy-allow.mjs";
import { sendVariantClaim } from "../src/send/send-variant-entry.mjs";
import { walletScreenChecklist } from "../src/send/wallet-screen.mjs";
import { sendVariantGate } from "../tools/build-send-variant.mjs";
import { scanText } from "../tools/scan-send-markers.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));
const ACCOUNT = "0x1111111111111111111111111111111111111111";
const OTHER = "0x2222222222222222222222222222222222222222";
const ORIGIN = productionViewOrigin();
const NOW = 1_700_000_000;
const IDX = 4;

function createRecorder() {
  const calls = [];
  return {
    calls,
    noBroadcast: true,
    broadcasts: false,
    async request(args) {
      calls.push(structuredClone(args));
      return `0x${"22".repeat(32)}`;
    },
  };
}

function passingIdentity(overrides = {}) {
  return {
    ok: true,
    account: ACCOUNT,
    chainId: TRUSTED_LISTA.chainId,
    origin: ORIGIN,
    authTime: NOW - 60,
    taskSource: "remote",
    ...overrides,
  };
}

function passingReread(overrides = {}) {
  return {
    ok: true,
    reread: "pass",
    account: ACCOUNT,
    chainId: TRUSTED_LISTA.chainId,
    sender: ACCOUNT,
    from: ACCOUNT,
    idx: IDX,
    calldata: claimCalldata(IDX),
    to: TRUSTED_LISTA.proxy,
    value: "0x0",
    approvalValidUntil: NOW + 3600,
    planExpiresAt: NOW + 3600,
    ...overrides,
  };
}

function matchingScreen() {
  return {
    to: TRUSTED_LISTA.proxy,
    value: 0,
    function: "claimWithdraw",
    chain: "BNB Smart Chain",
    chainId: TRUSTED_LISTA.chainId,
  };
}

function baseInput(recorder, patch = {}) {
  const guardedCalls = [];
  const input = {
    click: async () => ({ userGesture: true }),
    bindIdentity: async () => passingIdentity(),
    reread: async () => passingReread(),
    effectsCheck: async () => ({ ok: true }),
    settleReread: () => ({ ok: true, reread: "pass" }),
    store: async (record) => {
      if (record.op === "read") return { status: "empty", ok: true };
      return { ok: true };
    },
    guardedRequest: async (request) => {
      guardedCalls.push(request);
      return requestModeAClaim(recorder, request.params[0], request.capability);
    },
    now: () => NOW,
    walletScreen: matchingScreen(),
    ...patch,
  };
  return { input, guardedCalls };
}

async function sends(patch) {
  const recorder = createRecorder();
  const harness = baseInput(recorder, patch);
  const result = await runModeASendOrder(harness.input);
  return { result, recorder, guardedCalls: harness.guardedCalls };
}

function assertZero(bundle) {
  assert.equal(bundle.result.ok, false);
  assert.equal(bundle.result.sent, 0);
  assert.equal(bundle.guardedCalls.length, 0);
  assert.equal(bundle.recorder.calls.length, 0);
  assert.equal(bundle.guardedCalls.some((call) => call && call.method === "wallet_switchEthereumChain"), false);
  assert.equal(bundle.recorder.calls.some((call) => call && call.method === "wallet_switchEthereumChain"), false);
}

function exactParams(record = { from: ACCOUNT, idx: IDX }) {
  return [expectedModeAClaimTx(record)];
}

test("all bindings pass: exactly one eth_sendTransaction and no extra keys", async () => {
  const recorder = createRecorder();
  const timeline = [];
  const { input } = baseInput(recorder, {
    click: async () => {
      timeline.push("click");
      return { userGesture: true };
    },
    bindIdentity: async () => {
      timeline.push("bind");
      return passingIdentity();
    },
    reread: async () => {
      timeline.push("reread");
      return passingReread();
    },
    effectsCheck: async () => {
      timeline.push("effects");
      return { ok: true };
    },
    settleReread: (_observation, context) => {
      timeline.push(`settle:${context.nowUnix}`);
      return { ok: true, reread: "pass" };
    },
    store: async (record) => {
      timeline.push(record.op === "read" ? "read" : `write:${record.status}`);
      if (record.op === "read") return { status: "empty", ok: true };
      return { ok: true };
    },
    guardedRequest: async (request) => {
      timeline.push("wallet");
      return requestModeAClaim(recorder, request.params[0], request.capability);
    },
  });
  const result = await runModeASendOrder(input);
  const expected = {
    method: "eth_sendTransaction",
    params: [{
      from: ACCOUNT,
      to: TRUSTED_LISTA.proxy,
      data: claimCalldata(IDX),
      value: "0x0",
    }],
  };
  assert.equal(result.sent, 1);
  assert.equal(result.ok, true);
  assert.equal(recorder.calls.length, 1);
  assert.deepEqual(recorder.calls[0], expected);
  assert.deepEqual(Object.keys(recorder.calls[0].params[0]).sort(), ["data", "from", "to", "value"]);
  assert.equal(TRUSTED_LISTA.proxy, "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6");
  assert.equal(recorder.calls[0].params[0].to, "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6");
  assert.deepEqual(timeline, [
    "click",
    "bind",
    "reread",
    "reread",
    "effects",
    `settle:${NOW}`,
    "read",
    "write:requested",
    "wallet",
    "write:pending",
  ]);
  assert.equal(recorder.calls.some((call) => call.method === "wallet_sendCalls" || call.method === "wallet_switchEthereumChain"), false);
});

test("tampered calldata sends nothing", async () => {
  const tampered = `${claimCalldata(IDX).slice(0, -1)}5`;
  const bundle = await sends({ reread: async () => passingReread({ calldata: tampered }) });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "tampered_calldata");
});

test("another sender sends nothing", async () => {
  const bundle = await sends({ reread: async () => passingReread({ sender: OTHER }) });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "other_sender");
});

test("wrong chain sends nothing and does not switch chain", async () => {
  const bundle = await sends({ reread: async () => passingReread({ chainId: 1 }) });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "wrong_chain");
  const orderSource = readFileSync(new URL("../src/send/mode-a-send-order.mjs", import.meta.url), "utf8");
  assert.equal(orderSource.includes("wallet_switchEthereumChain"), false);
  assert.equal(orderSource.includes("wallet_sendCalls"), false);
});

test("account swap without an event sends nothing", async () => {
  let round = 0;
  const bundle = await sends({
    reread: async () => {
      round += 1;
      if (round === 2) return passingReread({ account: OTHER, sender: OTHER });
      return passingReread();
    },
  });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "account_swap");
  assert.equal(round, 2);
});

test("approval expired between the second reread and send sends nothing", async () => {
  let seenNow = null;
  const bundle = await sends({
    reread: async () => passingReread({ approvalValidUntil: NOW - 1 }),
    settleReread: (_observation, context) => {
      seenNow = context.nowUnix;
      return { ok: true, reread: "pass" };
    },
  });
  assert.equal(seenNow, NOW);
  assertZero(bundle);
  assert.equal(bundle.result.reason, "approval_expired");
});

test("plan TTL passed sends nothing", async () => {
  let seenNow = null;
  const bundle = await sends({
    reread: async () => passingReread({ planExpiresAt: NOW - 1 }),
    settleReread: (_observation, context) => {
      seenNow = context.nowUnix;
      return { ok: true, reread: "pass" };
    },
  });
  assert.equal(seenNow, NOW);
  assertZero(bundle);
  assert.equal(bundle.result.reason, "plan_ttl");
});

test("already pending sends nothing", async () => {
  const writes = [];
  const bundle = await sends({
    store: async (record) => {
      if (record.op === "write") writes.push(record);
      if (record.op === "read") return { status: "pending", ok: true };
      return { ok: true };
    },
  });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "already_pending");
  assert.deepEqual(writes, []);
});

test("storage unavailable sends nothing", async () => {
  const bundle = await sends({
    store: async (record) => {
      if (record.op === "read") return { status: "empty", ok: true };
      return { ok: false, reason: "storage_unavailable" };
    },
  });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "storage_unavailable");
});

test("a local-file task sends nothing", async () => {
  const bundle = await sends({
    bindIdentity: async () => passingIdentity({ taskSource: "local-file" }),
  });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "local_file_task");
});

test("an unapproved origin sends nothing", async () => {
  assert.equal(originIsApproved(ORIGIN, { production: true }), true);
  assert.equal(originIsApproved("https://unapproved.example", { production: true }), false);
  const bundle = await sends({
    bindIdentity: async () => passingIdentity({ origin: "https://unapproved.example" }),
  });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "unapproved_origin");
});

test("stale auth_time sends nothing", async () => {
  const bundle = await sends({
    bindIdentity: async () => passingIdentity({ auth_time: NOW - MODE_A_AUTH_MAX_AGE_SECONDS - 1, authTime: undefined }),
  });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "stale_auth_time");
});

test("4001 without a new click sends nothing and does not retry", async () => {
  const recorder = createRecorder();
  const { input } = baseInput(recorder, {
    click: async () => ({ userGesture: true, priorCode: 4001, newClick: false }),
  });
  const first = await runModeASendOrder(input);
  const second = await runModeASendOrder(input);
  assert.equal(first.sent, 0);
  assert.equal(second.sent, 0);
  assert.equal(first.reason, "needs_new_click");
  assert.equal(recorder.calls.length, 0);
});

test("effects failure sends nothing", async () => {
  const bundle = await sends({ effectsCheck: async () => ({ ok: false, reason: "effects" }) });
  assertZero(bundle);
  assert.equal(bundle.result.reason, "effects");
});

test("request-policy allow branch denies a bad claim and a missing capability", () => {
  const record = { from: ACCOUNT, idx: IDX };
  const capability = issueModeAClaimCapability(record);
  const exact = exactParams(record);
  assert.equal(capability, record);
  assert.equal(modeAClaimDecision("eth_sendTransaction", exact, { modeAClaim: capability }).allowed, true);

  const different = [{ ...exact[0], data: claimCalldata(IDX + 1) }];
  const extraGas = [{ ...exact[0], gas: "0x5208" }];
  const value = [{ ...exact[0], value: "0x1" }];
  for (const params of [different, extraGas, value]) {
    const decision = modeAClaimDecision("eth_sendTransaction", params, { modeAClaim: capability });
    assert.equal(decision.allowed, false);
    assert.equal(decision.reason, "params");
  }

  assert.equal(modeAClaimDecision("eth_sendTransaction", exact, {}).allowed, false);
  assert.equal(modeAClaimDecision("eth_sendTransaction", exact, {}).reason, "no_capability");
  const plain = { from: ACCOUNT, idx: IDX };
  assert.equal(modeAClaimDecision("eth_sendTransaction", exact, { modeAClaim: plain }).allowed, false);
  assert.equal(modeAClaimDecision("eth_sendTransaction", exact, { modeAClaim: structuredClone(capability) }).reason, "no_capability");

  const foreign = vm.runInNewContext("({ from: from, idx: idx })", { from: ACCOUNT, idx: IDX });
  assert.equal(modeAClaimDecision("eth_sendTransaction", exact, { modeAClaim: foreign }).reason, "no_capability");

  assert.equal(modeAClaimDecision("wallet_sendCalls", [[]], { modeAClaim: capability }).allowed, false);
  assert.equal(modeAClaimDecision("wallet_switchEthereumChain", [{ chainId: "0x38" }], { modeAClaim: capability }).allowed, false);
  assert.equal(modeAClaimDecision("eth_sendRawTransaction", ["0x"], { modeAClaim: capability }).allowed, false);
  assert.equal(modeAClaimDecision("eth_chainId", []).allowed, true);
  assert.equal(modeAClaimDecision("eth_chainId", []).reason, "read");

  const untouched = walletRequestDecision("eth_sendTransaction", exact, { modeAClaim: capability });
  assert.equal(untouched.allowed, false);
  assert.equal(untouched.reason, "method_not_allowed");
});

test("requestModeAClaim does not call the recorder unless the exact claim is allowed", async () => {
  const record = { from: ACCOUNT, idx: IDX };
  const capability = issueModeAClaimCapability(record);
  const exact = expectedModeAClaimTx(record);
  const denied = createRecorder();
  await assert.rejects(
    () => requestModeAClaim(denied, { ...exact, value: "0x1" }, capability),
    /refused/,
  );
  await assert.rejects(
    () => requestModeAClaim(denied, exact, { from: ACCOUNT, idx: IDX }),
    /refused/,
  );
  assert.equal(denied.calls.length, 0);

  const allowed = createRecorder();
  const hash = await requestModeAClaim(allowed, exact, capability);
  assert.equal(hash, `0x${"22".repeat(32)}`);
  assert.equal(allowed.calls.length, 1);
  assert.deepEqual(allowed.calls[0].params, [exact]);
});

test("wallet screen checklist shows the Lista proxy, value 0, claimWithdraw, and BSC", () => {
  const shown = walletScreenChecklist(matchingScreen());
  assert.equal(shown.ok, true);
  assert.equal(shown.refusal, null);
  assert.equal(shown.to, "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6");
  assert.equal(shown.value, 0);
  assert.equal(shown.function, "claimWithdraw");
  assert.equal(shown.chainId, 56);
  assert.deepEqual(shown.lines, [
    "to = 0x1adB950d8bB3dA4bE104211D5AB038628e477fE6",
    "value 0",
    "function claimWithdraw",
    "BNB Smart Chain (chain id 56)",
  ]);
  for (const bad of [
    { ...matchingScreen(), to: "0x0000000000000000000000000000000000000001" },
    { ...matchingScreen(), value: "0x1" },
    { ...matchingScreen(), function: "deposit" },
    { ...matchingScreen(), chain: "Ethereum", chainId: 1 },
  ]) {
    const refusal = walletScreenChecklist(bad);
    assert.equal(refusal.ok, false);
    assert.equal(refusal.refusal, "wallet_screen_mismatch");
  }
});

test("default request policy and main do not import the allow branch", () => {
  const policy = readFileSync(new URL("../src/wallets/request-policy.mjs", import.meta.url), "utf8");
  const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  const entry = readFileSync(new URL("../src/send/send-variant-entry.mjs", import.meta.url), "utf8");
  const allow = readFileSync(new URL("../src/send/request-policy-allow.mjs", import.meta.url), "utf8");
  const order = readFileSync(new URL("../src/send/mode-a-send-order.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(policy, /request-policy-allow/);
  assert.doesNotMatch(main, /request-policy-allow/);
  assert.doesNotMatch(main, /send-variant-entry/);
  assert.match(entry, /request-policy-allow/);
  assert.match(allow, /request-policy-allow/);
  assert.ok(scanText(allow).includes("request-policy-allow"));
  assert.doesNotMatch(order, /lista-reread|outcome-store|client-evidence|setInterval|setTimeout/);
  assert.doesNotMatch(main, /eth_sendTransaction|wallet_sendCalls/);
  const graph = importGraph(join(CLIENT, "src/main.ts"));
  const send = [...graph].filter((file) => file.split("\\").join("/").includes("/src/send/"));
  assert.deepEqual(send, []);
});

test("send-variant entry does not call the wallet when the build flag is off", async () => {
  const recorder = createRecorder();
  const record = { from: ACCOUNT, idx: IDX, chainId: 56 };
  await assert.rejects(
    () => sendVariantClaim(recorder, expectedModeAClaimTx(record), record),
    /send variant is not this build/,
  );
  assert.equal(recorder.calls.length, 0);
});

test("WalletConnect proposal stays personal_sign only", () => {
  assert.deepEqual([...WC_METHODS], ["personal_sign"]);
});

test("inventory has no approved mode_a entry, so the gate stays closed", () => {
  const raw = readFileSync(new URL("../deploy/releases.json", import.meta.url), "utf8");
  assert.equal(raw.includes("approved_by_user"), false);
  const document = JSON.parse(raw);
  assert.equal(sendVariantGate({
    WG_SEND_VARIANT: "mode_a",
    WG_RELEASE_AUTH_ID: "missing-inventory-test",
  }, document).ok, false);
  assert.equal(sendVariantGate({
    WG_SEND_VARIANT: "mode_a",
    WG_RELEASE_AUTH_ID: "missing-inventory-test",
  }, { releases: [{ variant: "mode_a", status: "approved_by_user" }] }).ok, true);
  assert.equal(sendVariantGate({
    WG_SEND_VARIANT: "off",
    WG_RELEASE_AUTH_ID: "missing-inventory-test",
  }, { releases: [{ variant: "mode_a", status: "approved_by_user" }] }).ok, false);
});

function spawnBuild(overrides) {
  const env = { ...process.env };
  delete env.WG_SEND_VARIANT;
  delete env.WG_RELEASE_AUTH_ID;
  delete env.WG_SEND_VARIANT_BUILD;
  delete env.VITE_GUARDIAN_MAINNET_SEND;
  Object.assign(env, overrides);
  return new Promise((resolvePromise) => {
    const child = spawn("npm", ["run", "build:send-variant", "--silent"], { cwd: CLIENT, env });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("close", (status) => resolvePromise({ status, stdout, stderr }));
  });
}

test("build:send-variant exits non-zero without an auth id or inventory entry", async () => {
  const missing = await spawnBuild({});
  assert.notEqual(missing.status, 0);
  const authOnly = await spawnBuild({
    WG_SEND_VARIANT: "mode_a",
    WG_RELEASE_AUTH_ID: "missing-inventory-test",
  });
  assert.notEqual(authOnly.status, 0);
  assert.match(`${authOnly.stdout}\n${authOnly.stderr}`, /approved_by_user/);
});

function spawnVite(overrides, args) {
  const env = { ...process.env };
  delete env.WG_SEND_VARIANT;
  delete env.WG_RELEASE_AUTH_ID;
  delete env.WG_SEND_VARIANT_BUILD;
  delete env.VITE_GUARDIAN_MAINNET_SEND;
  Object.assign(env, overrides);
  const viteBin = join(CLIENT, "node_modules/vite/bin/vite.js");
  return new Promise((resolvePromise) => {
    const child = spawn(process.execPath, [viteBin, ...args], { cwd: CLIENT, env });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("close", (status) => resolvePromise({ status, stdout, stderr }));
  });
}

function walkFiles(dir) {
  if (!existsSync(dir)) return [];
  const found = [];
  for (const name of readdirSync(dir)) {
    const absolute = join(dir, name);
    if (statSync(absolute).isDirectory()) found.push(...walkFiles(absolute));
    else found.push(absolute);
  }
  return found;
}

function sendVariantEntryPresent(dir) {
  return walkFiles(dir).some((file) => {
    const normalized = file.split("\\").join("/");
    if (normalized.includes("sendVariant") || normalized.includes("send-variant-entry")) return true;
    if (!file.endsWith(".js")) return false;
    return readFileSync(file, "utf8").includes("send variant is not this build");
  });
}

test("direct vite build with WG_SEND_VARIANT=mode_a throws without an approved inventory", async () => {
  const outDir = mkdtempSync(join(tmpdir(), "wg-vite-refuse-"));
  try {
    const missing = await spawnVite({ WG_SEND_VARIANT: "mode_a" }, ["build", "--outDir", outDir]);
    assert.notEqual(missing.status, 0);
    assert.match(`${missing.stdout}\n${missing.stderr}`, /send variant refused/);
    assert.match(`${missing.stdout}\n${missing.stderr}`, /WG_RELEASE_AUTH_ID/);
    assert.equal(sendVariantEntryPresent(outDir), false);
    const authOnly = await spawnVite({
      WG_SEND_VARIANT: "mode_a",
      WG_RELEASE_AUTH_ID: "missing-inventory-test",
    }, ["build", "--outDir", outDir]);
    assert.notEqual(authOnly.status, 0);
    assert.match(`${authOnly.stdout}\n${authOnly.stderr}`, /approved_by_user/);
    assert.equal(sendVariantEntryPresent(outDir), false);
  } finally {
    rmSync(outDir, { recursive: true, force: true });
  }
});

test("a default vite build does not emit the send-variant entry", { timeout: 120_000 }, async () => {
  const outDir = mkdtempSync(join(tmpdir(), "wg-vite-default-"));
  try {
    const built = await spawnVite({}, ["build", "--outDir", outDir, "--emptyOutDir"]);
    assert.equal(built.status, 0, `${built.stdout}\n${built.stderr}`);
    const files = walkFiles(outDir);
    assert.ok(files.some((file) => file.endsWith(".js")));
    assert.equal(sendVariantEntryPresent(outDir), false);
  } finally {
    rmSync(outDir, { recursive: true, force: true });
  }
});

function specsIn(file) {
  const text = readFileSync(file, "utf8");
  const specs = [];
  for (const pattern of [
    /from\s+["'](\.[^"']+)["']/g,
    /import\s*\(\s*["'](\.[^"']+)["']\s*\)/g,
    /import\s+["'](\.[^"']+)["']/g,
  ]) {
    for (const match of text.matchAll(pattern)) specs.push(match[1]);
  }
  return specs;
}

function resolveSpec(fromFile, spec) {
  const raw = resolve(dirname(fromFile), spec);
  const candidates = [raw, `${raw}.mjs`, `${raw}.ts`, `${raw}.js`, `${raw}.json`];
  return candidates.find((candidate) => existsSync(candidate) && statSync(candidate).isFile()) ?? null;
}

function importGraph(entry) {
  const seen = new Set();
  const queue = [entry];
  while (queue.length > 0) {
    const file = queue.pop();
    if (!file || seen.has(file)) continue;
    seen.add(file);
    if (file.endsWith(".json")) continue;
    for (const spec of specsIn(file)) {
      const next = resolveSpec(file, spec);
      if (next) queue.push(next);
    }
  }
  return seen;
}
