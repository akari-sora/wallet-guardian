/**
 * S-1b client core: allowlist loading, method check, transaction shape, roles,
 * built-in Lista entry, and core isolation (no provider, no wallet, no send names).
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  assertSendable,
  BUILTIN_CONTEXT,
  buildListaClaimTransaction,
  buildTransaction,
  checkTransaction,
  CORE_ROLES,
  CoreRefusal,
  createCoreContext,
  decodeCall,
  encodeCall,
  isAllowedSendMethod,
  LISTA_CLAIM_FULL_SIGNATURE,
  loadAllowlist,
  parseSignature,
  SEND_METHOD_KECCAK,
} from "../src/execution/core/index.mjs";
import { keccakHex } from "../src/execution/core/hex.mjs";
import { DEPLOYMENT_APPROVALS, TRUSTED_LISTA } from "../src/execution/core/lista.mjs";
import { REGISTRY_ROLES, SELF_ROLES } from "../src/execution/model.mjs";
import { CORE_MARKER } from "../tools/scan-send-markers.mjs";

const CORE_DIR = fileURLToPath(new URL("../src/execution/core/", import.meta.url));
const SEND_METHOD = "eth_sendTransaction";
const SELF = "0x00000000000000000000000000000000000a11ce";
const OTHER = "0x000000000000000000000000000000000000bad0";
const TOKEN = "0x0000000000000000000000000000000000007001";
const VAULT = "0x0000000000000000000000000000000000007003";

function refusal(reason) {
  return (error) => {
    assert.ok(error instanceof CoreRefusal, `expected CoreRefusal, got ${error}`);
    assert.equal(error.reason, reason);
    return true;
  };
}

function entry(permission_type, signature, roles = {}) {
  return { permission_type, signature, selector: parseSignature(signature).selector, roles };
}

const APPROVE20 = entry("erc20_allowance", "approve(address spender,uint256 amount)", { spender: "revoked" });
const REDEEM = entry("erc4626_vault", "redeem(uint256 shares,address receiver,address owner)", { receiver: "receiver", owner: "owner" });

function context(extra = []) {
  return createCoreContext({
    deployments: [
      { chain_id: 56, address: TOKEN, permission_type: "erc20_allowance" },
      { chain_id: 56, address: VAULT, permission_type: "erc4626_vault" },
    ],
    allowlist: [APPROVE20, REDEEM, ...extra],
  });
}

function tx(overrides = {}) {
  return {
    from: SELF,
    to: TOKEN,
    data: encodeCall("approve(address,uint256)", [OTHER, 0]),
    value: "0x0",
    chainId: "0x38",
    ...overrides,
  };
}

const APPROVE_INTENT = { account: SELF, signature: "approve(address spender,uint256 amount)" };

// --- method ---------------------------------------------------------------

test("send method check uses a keccak constant equal to the one allowed method", () => {
  assert.equal(SEND_METHOD_KECCAK, keccakHex(SEND_METHOD));
  assert.equal(isAllowedSendMethod(SEND_METHOD), true);
  for (const method of ["eth_sendRawTransaction", "eth_signTransaction", "eth_sign", "personal_sign", "eth_signTypedData_v4", "wallet_sendCalls", "ETH_SENDTRANSACTION", "", null, 1]) {
    assert.equal(isAllowedSendMethod(method), false, String(method));
  }
});

test("assertSendable refuses any method that is not the send method", () => {
  for (const method of ["eth_signTransaction", "wallet_sendCalls", "eth_sendRawTransaction", "personal_sign", undefined]) {
    assert.throws(() => assertSendable({ method, params: [tx()] }, APPROVE_INTENT, context()), refusal("method_not_allowed"));
  }
  assert.ok(assertSendable({ method: SEND_METHOD, params: [tx()] }, APPROVE_INTENT, context()));
});

test("no batch shape: exactly one transaction per request", () => {
  assert.throws(() => assertSendable({ method: SEND_METHOD, params: [tx(), tx()] }, APPROVE_INTENT, context()), refusal("bad_request"));
  assert.throws(() => assertSendable({ method: SEND_METHOD, params: [] }, APPROVE_INTENT, context()), refusal("bad_request"));
  assert.throws(() => assertSendable({ method: SEND_METHOD, params: [{ calls: [tx()] }] }, APPROVE_INTENT, context()), refusal("unknown_field"));
});

// --- transaction shape ------------------------------------------------------

test("value must be exactly zero in every accepted form", () => {
  for (const value of ["0x0", "0x00", 0, 0n]) assert.ok(checkTransaction(tx({ value }), APPROVE_INTENT, context()), String(value));
  for (const value of ["0x1", 1, 1n, "0xde0b6b3a7640000"]) {
    assert.throws(() => checkTransaction(tx({ value }), APPROVE_INTENT, context()), refusal("nonzero_value"), String(value));
  }
  const missing = tx();
  delete missing.value;
  assert.throws(() => checkTransaction(missing, APPROVE_INTENT, context()), refusal("value_missing"));
  assert.throws(() => checkTransaction(tx({ value: "1" }), APPROVE_INTENT, context()), refusal("bad_value"));
});

test("from must be the account, to must be registered on the same chain and not self", () => {
  assert.throws(() => checkTransaction(tx({ from: OTHER }), APPROVE_INTENT, context()), refusal("from_not_self"));
  assert.throws(() => checkTransaction(tx({ to: OTHER }), APPROVE_INTENT, context()), refusal("to_not_registered"));
  assert.throws(() => checkTransaction(tx({ chainId: "0x1" }), APPROVE_INTENT, context()), refusal("to_not_registered"));
  assert.throws(() => checkTransaction(tx({ to: SELF }), APPROVE_INTENT, context()), refusal("to_is_self"));
});

test("unknown fields, EIP-7702 fields, and non-canonical calldata are refused", () => {
  assert.throws(() => checkTransaction(tx({ gas: "0x1" }), APPROVE_INTENT, context()), refusal("unknown_field"));
  assert.throws(() => checkTransaction(tx({ authorizationList: [] }), APPROVE_INTENT, context()), refusal("eip7702_refused"));
  assert.throws(() => checkTransaction(tx({ type: "0x4" }), APPROVE_INTENT, context()), refusal("eip7702_refused"));
  const data = tx().data;
  assert.throws(() => checkTransaction(tx({ data: data + "00" }), APPROVE_INTENT, context()), refusal("bad_calldata"));
  const dirty = data.slice(0, 10) + "ff" + data.slice(12);
  assert.throws(() => checkTransaction(tx({ data: dirty }), APPROVE_INTENT, context()), refusal("bad_calldata"));
  assert.throws(() => checkTransaction(tx({ data: "0x095ea7" }), APPROVE_INTENT, context()), refusal("bad_calldata"));
});

test("default deny: a function that is not on the allowlist is refused", () => {
  const data = encodeCall("deposit(uint256,address)", [1, SELF]);
  assert.throws(
    () => checkTransaction(tx({ to: VAULT, data }), { account: SELF, signature: "deposit(uint256 assets,address receiver)" }, context()),
    refusal("function_not_allowlisted"),
  );
  assert.throws(() => checkTransaction(tx(), APPROVE_INTENT, createCoreContext({ deployments: [{ chain_id: 56, address: TOKEN, permission_type: "erc20_allowance" }] })), refusal("function_not_allowlisted"));
});

// --- selector collisions ----------------------------------------------------

test("known 4-byte collisions are real (test premise)", () => {
  assert.equal(parseSignature("burn(uint256)").selector, parseSignature("collate_propagate_storage(bytes16)").selector);
  assert.equal(parseSignature("transferFrom(address,address,uint256)").selector, "0x23b872dd");
});

test("load time: two entries with the same selector and different canonical signatures are refused", () => {
  assert.throws(
    () => loadAllowlist([entry("erc20_allowance", "burn(uint256 amount)"), entry("erc4626_vault", "collate_propagate_storage(bytes16 x)")]),
    refusal("selector_collision"),
  );
  // Same canonical signature under two permission types (ERC-20 vs ERC-721 approve) is not a collision.
  assert.ok(loadAllowlist([APPROVE20, entry("erc721_token", "approve(address to,uint256 tokenId)", { to: "revoked" })]));
});

test("load time: a declared selector that does not match the signature is refused", () => {
  assert.throws(() => loadAllowlist([{ ...APPROVE20, selector: "0x095ea7b4" }]), refusal("selector_mismatch"));
  assert.throws(() => loadAllowlist([{ ...APPROVE20, selector: parseSignature("burn(uint256)").selector }]), refusal("selector_mismatch"));
});

test("run time: same selector but a different full signature is refused", () => {
  const ctx = context([entry("erc20_allowance", "burn(uint256 amount)")]);
  const data = encodeCall("collate_propagate_storage(bytes16)", ["0x" + "00".repeat(15) + "01"]);
  assert.throws(() => checkTransaction(tx({ data }), { account: SELF, signature: "collate_propagate_storage(bytes16 x)" }, ctx), refusal("signature_mismatch"));
  // ERC-20 approve shape to an ERC-721 deployment.
  const nft = createCoreContext({
    deployments: [{ chain_id: 56, address: TOKEN, permission_type: "erc721_token" }],
    allowlist: [entry("erc721_token", "approve(address to,uint256 tokenId)", { to: "revoked" })],
  });
  assert.throws(() => checkTransaction(tx(), APPROVE_INTENT, nft), refusal("signature_mismatch"));
  // ERC-20 deployment with only an ERC-721 entry for that selector.
  const token = createCoreContext({
    deployments: [{ chain_id: 56, address: TOKEN, permission_type: "erc20_allowance" }],
    allowlist: [entry("erc721_token", "approve(address to,uint256 tokenId)", { to: "revoked" })],
  });
  assert.throws(() => checkTransaction(tx(), APPROVE_INTENT, token), refusal("signature_mismatch"));
});

// --- allowlist hygiene --------------------------------------------------------

test("load time: multicall, execute, aggregate, permit and other denied functions cannot be allowlisted (name and selector)", () => {
  const cases = [
    "multicall(bytes[] data)",
    "multicallWithValue(bytes[] data)",
    "execute(address target,uint256 value,bytes data)",
    "executeBatch(address[] targets,uint256[] values,bytes[] datas)",
    "aggregate3((address target,bool allowFailure,bytes callData)[] calls)",
    "tryAggregate(bool requireSuccess,(address target,bytes callData)[] calls)",
    "permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)",
    "permitTransferFrom(uint256 x)",
    "increaseAllowance(address spender,uint256 addedValue)",
    "transfer(address to,uint256 amount)",
    "transferOwnership(address newOwner)",
    "upgradeToAndCall(address newImplementation,bytes data)",
  ];
  for (const signature of cases) {
    const roles = {};
    for (const name of ["spender", "to", "target", "targets", "newOwner", "newImplementation", "owner", "calls.target"]) {
      if (signature.includes(`address ${name.split(".").pop()}`) || signature.includes(`address[] ${name}`)) roles[name] = "target";
    }
    assert.throws(() => loadAllowlist([entry("erc20_allowance", signature, roles)]), refusal("denied_function"), signature);
  }
  // Renamed function whose selector is on the deny list (selector check, not just name).
  const renamed = { permission_type: "erc20_allowance", signature: "transfer(address to,uint256 amount)", selector: "0xa9059cbb", roles: { to: "target" } };
  assert.throws(() => loadAllowlist([renamed]), refusal("denied_function"));
  assert.throws(() => loadAllowlist([{ ...APPROVE20, permission_type: "permit2" }]), refusal("denied_function"));
});

test("load time: every address parameter needs a role; parameters need names", () => {
  assert.throws(() => loadAllowlist([entry("erc4626_vault", "redeem(uint256 shares,address receiver,address owner)", { receiver: "receiver" })]), refusal("address_role_missing"));
  assert.throws(() => loadAllowlist([entry("erc4626_vault", "redeem(uint256,address,address)")]), refusal("bad_allowlist"));
  assert.throws(() => loadAllowlist([entry("erc20_allowance", "approve(address spender,uint256 amount)", { spender: "anyone" })]), refusal("bad_allowlist"));
  assert.throws(() => loadAllowlist([{ ...APPROVE20, extra: true }]), refusal("bad_allowlist"));
  assert.throws(() => loadAllowlist([APPROVE20, APPROVE20]), refusal("duplicate_entry"));
});

test("core roles stay aligned with the S-1a model", () => {
  for (const role of [...SELF_ROLES, ...REGISTRY_ROLES]) assert.ok(CORE_ROLES.includes(role), role);
});

// --- roles -------------------------------------------------------------------

test("receiver, owner must be the account; spender/target must be registered", () => {
  const redeem = (receiver, owner) => tx({ to: VAULT, data: encodeCall("redeem(uint256,address,address)", [1, receiver, owner]) });
  const intent = { account: SELF, signature: REDEEM.signature };
  assert.ok(checkTransaction(redeem(SELF, SELF), intent, context()));
  assert.throws(() => checkTransaction(redeem(VAULT, SELF), intent, context()), refusal("receiver_not_self"));
  assert.throws(() => checkTransaction(redeem(TOKEN, SELF), intent, context()), refusal("receiver_not_self"));
  assert.throws(() => checkTransaction(redeem(SELF, VAULT), intent, context()), refusal("owner_not_self"));
  const spenderEntry = entry("erc4626_vault", "requestRedeem(uint256 shares,address controller,address spender)", { controller: "owner", spender: "spender" });
  const ctx = context([spenderEntry]);
  const data = (spender) => encodeCall("requestRedeem(uint256,address,address)", [1, SELF, spender]);
  const spenderIntent = { account: SELF, signature: spenderEntry.signature };
  assert.ok(checkTransaction(tx({ to: VAULT, data: data(TOKEN) }), spenderIntent, ctx));
  assert.throws(() => checkTransaction(tx({ to: VAULT, data: data(OTHER) }), spenderIntent, ctx), refusal("spender_not_registered"));
});

// --- builder -------------------------------------------------------------------

test("buildTransaction encodes, sets value 0, and runs the checks", () => {
  const built = buildTransaction({ account: SELF, chainId: 56, to: TOKEN, signature: APPROVE20.signature, args: [OTHER, 0] }, context());
  assert.equal(built.value, "0x0");
  assert.deepEqual(decodeCall(parseSignature(APPROVE20.signature), built.data), [OTHER, 0n]);
  assert.throws(() => buildTransaction({ account: SELF, chainId: 56, to: TOKEN, signature: APPROVE20.signature, args: [OTHER, 1] }, context()), refusal("nonzero_approve"));
  assert.throws(() => buildTransaction({ account: SELF, chainId: 56, to: VAULT, signature: REDEEM.signature, args: [1, VAULT, SELF] }, context()), refusal("receiver_not_self"));
});

// --- built-in Lista -------------------------------------------------------------

test("built-in Lista entry: claim passes the general checks; tampering is refused", () => {
  const claim = buildListaClaimTransaction({ account: SELF, idx: 4 });
  const intent = { account: SELF, signature: LISTA_CLAIM_FULL_SIGNATURE };
  assert.ok(assertSendable({ method: SEND_METHOD, params: [claim] }, intent, BUILTIN_CONTEXT));
  assert.throws(() => assertSendable({ method: SEND_METHOD, params: [{ ...claim, value: "0x1" }] }, intent, BUILTIN_CONTEXT), refusal("nonzero_value"));
  assert.throws(() => assertSendable({ method: SEND_METHOD, params: [{ ...claim, to: OTHER }] }, intent, BUILTIN_CONTEXT), refusal("to_not_registered"));
  assert.throws(() => assertSendable({ method: SEND_METHOD, params: [{ ...claim, chainId: "0x1" }] }, intent, BUILTIN_CONTEXT), refusal("to_not_registered"));
  const other = { ...claim, data: encodeCall("requestWithdraw(uint256)", [1]) };
  assert.throws(() => assertSendable({ method: SEND_METHOD, params: [other] }, { account: SELF, signature: "requestWithdraw(uint256 amount)" }, BUILTIN_CONTEXT), refusal("function_not_allowlisted"));
  assert.throws(() => assertSendable({ method: "wallet_sendCalls", params: [claim] }, intent, BUILTIN_CONTEXT), refusal("method_not_allowed"));
});

test("built-in Lista entry does not approve the deployment or open send", () => {
  assert.equal(DEPLOYMENT_APPROVALS.entries.length, 0);
  assert.equal(TRUSTED_LISTA.approvals.length, 0);
  const registry = JSON.parse(readFileSync(new URL("../../config/execution/registry.json", import.meta.url), "utf8"));
  assert.equal(registry.send_enabled, false);
  assert.equal(registry.user_approved, false);
});

// --- isolation --------------------------------------------------------------------

function coreSources() {
  return readdirSync(CORE_DIR)
    .filter((name) => name.endsWith(".mjs"))
    .map((name) => ({ name, text: readFileSync(join(CORE_DIR, name), "utf8") }));
}

test("core sources carry no wallet, signing, or send markers", () => {
  for (const { name, text } of coreSources()) assert.deepEqual(text.match(CORE_MARKER) ?? [], [], name);
});

test("core imports stay inside the core, the S-1a model, and @noble/hashes", () => {
  const allowed = [/^\.\/[a-z-]+\.mjs$/, /^\.\.\/model\.mjs$/, /^@noble\/hashes\/sha3\.js$/];
  for (const { name, text } of coreSources()) {
    const specs = [
      ...text.matchAll(/\bfrom\s+["']([^"']+)["']/g),
      ...text.matchAll(/\bimport\s*\(\s*["']([^"']+)["']/g),
      ...text.matchAll(/^import\s+["']([^"']+)["']/gm),
    ].map((match) => match[1]);
    assert.ok(specs.length > 0 || name === "errors.mjs", `${name} has imports`);
    for (const spec of specs) assert.ok(allowed.some((pattern) => pattern.test(spec)), `${name} imports ${spec}`);
    assert.equal(/\bfetch\s*\(|XMLHttpRequest|WebSocket|navigator\.|document\.|localStorage|indexedDB/.test(text), false, name);
  }
});

// --- F1 (review 21): `revoked` role and permission-style names need a zero rule ---------

const EVIL = OTHER;
const ME = SELF;
const MAX = (1n << 256n) - 1n;

/** Load the entry, then build the call. Both steps must happen for the call to be accepted. */
function loadAndBuild(permission_type, signature, roles, args) {
  const ctx = createCoreContext({
    deployments: [{ chain_id: 56, address: TOKEN, permission_type }],
    allowlist: [entry(permission_type, signature, roles)],
  });
  return buildTransaction({ account: ME, chainId: 56, to: TOKEN, signature, args }, ctx);
}

test("F1: setOperator(EVIL, true) with operator=revoked is refused", () => {
  assert.throws(
    () => loadAndBuild("erc721_operator", "setOperator(address operator,bool approved)", { operator: "revoked" }, [EVIL, true]),
    refusal("revoked_role_without_zero_rule"),
  );
});

test("F1: ERC-6909 approve(EVIL, 1, max) with spender=revoked is refused", () => {
  assert.throws(
    () => loadAndBuild("erc20_allowance", "approve(address spender,uint256 id,uint256 amount)", { spender: "revoked" }, [EVIL, 1, MAX]),
    refusal("revoked_role_without_zero_rule"),
  );
});

test("F1: withdraw(5, EVIL, ME) with receiver mislabelled revoked is refused", () => {
  assert.throws(
    () => loadAndBuild("erc4626_vault", "withdraw(uint256 assets,address receiver,address owner)", { receiver: "revoked", owner: "owner" }, [5, EVIL, ME]),
    refusal("revoked_role_without_zero_rule"),
  );
});

test("F1: permission-style names without a zero rule are refused even without the revoked role", () => {
  // Counterparty labelled as a registered spender/target (TOKEN is registered), so role checks alone would pass.
  assert.throws(
    () => loadAndBuild("erc20_allowance", "approve(address spender,uint256 id,uint256 amount)", { spender: "spender" }, [TOKEN, 1, MAX]),
    refusal("permission_function_without_zero_rule"),
  );
  assert.throws(
    () => loadAndBuild("erc721_operator", "setOperator(address operator,bool approved)", { operator: "target" }, [TOKEN, true]),
    refusal("permission_function_without_zero_rule"),
  );
  for (const signature of [
    "approveAll(address spender)",
    "setApproval(address spender,bool approved)",
    "setApprovalForAllWithSig(address operator,bool approved,uint256 deadline)",
    "setOperator(address operator,bool approved)",
    "setAuthorizationWithSig(address authorized,bool flag,uint256 nonce)",
    "approveDelegationWithSig(address delegatee,uint256 amount,uint256 deadline)",
    "setDelegate(address delegate)",
    "setDelegateApproval(address delegate,bool approved)",
    "increaseApproval(address spender,uint256 addedValue)",
    "authorizeOperator(address operator)",
  ]) {
    const roles = {};
    for (const match of signature.matchAll(/address (\w+)/g)) roles[match[1]] = "target";
    assert.throws(() => loadAllowlist([entry("erc20_allowance", signature, roles)]), refusal("permission_function_without_zero_rule"), signature);
  }
});

test("F1: the four revocations with zero rules still load and still refuse non-zero", () => {
  const cases = [
    ["erc20_allowance", "approve(address spender,uint256 amount)", { spender: "revoked" }, [EVIL, 0], [EVIL, 1], "nonzero_approve"],
    ["erc721_operator", "setApprovalForAll(address operator,bool approved)", { operator: "revoked" }, [EVIL, false], [EVIL, true], "approval_for_all_true"],
    ["aave_debt_token", "approveDelegation(address delegatee,uint256 amount)", { delegatee: "revoked" }, [EVIL, 0], [EVIL, 1], "nonzero_delegation"],
    ["morpho_like", "setAuthorization(address authorized,bool newIsAuthorized)", { authorized: "revoked" }, [EVIL, false], [EVIL, true], "authorization_true"],
  ];
  for (const [type, signature, roles, ok, bad, reason] of cases) {
    assert.ok(loadAndBuild(type, signature, roles, ok), signature);
    assert.throws(() => loadAndBuild(type, signature, roles, bad), refusal(reason), signature);
  }
});

test("B1: ERC-721 approve requires zero recipient (any tokenId) and refuses approve(attacker, 0)", () => {
  const ZERO = "0x0000000000000000000000000000000000000000";
  const sig = "approve(address to,uint256 tokenId)";
  assert.ok(loadAndBuild("erc721_token", sig, { to: "revoked" }, [ZERO, 0]));
  assert.ok(loadAndBuild("erc721_token", sig, { to: "revoked" }, [ZERO, 5]));
  assert.throws(() => loadAndBuild("erc721_token", sig, { to: "revoked" }, [EVIL, 0]), refusal("nonzero_approve"));
  assert.throws(() => loadAndBuild("erc721_token", sig, { to: "revoked" }, [EVIL, 5]), refusal("nonzero_approve"));
  assert.throws(() => loadAndBuild("erc4626_vault", sig, { to: "revoked" }, [ZERO, 0]), refusal("revoked_role_without_zero_rule"));
});

test("load time: non-permission function names whose selector equals approve, setApprovalForAll, or ERC-6909 approve/setOperator are refused", () => {
  const collisions = [
    ["c_3a61cb3b6(uint256 a,uint256 b)", "0x095ea7b3"],
    ["c_2520f4f99(uint256 a,uint256 b)", "0xa22cb465"],
    ["c_072382414(uint256 a,uint256 b)", parseSignature("approve(address,uint256,uint256)").selector],
    ["c_00bf30176(uint256 a,uint256 b)", parseSignature("setOperator(address,bool)").selector],
    ["c_0a5027ed8(uint256 a,uint256 b)", parseSignature("approveDelegation(address,uint256)").selector],
    ["c_0091550c6(uint256 a,uint256 b)", parseSignature("setAuthorization(address,bool)").selector],
  ];
  for (const [signature, expectedSelector] of collisions) {
    assert.equal(parseSignature(signature).selector, expectedSelector, signature);
    assert.throws(() => loadAllowlist([entry("erc20_allowance", signature)]), refusal("permission_function_without_zero_rule"), signature);
  }
});


