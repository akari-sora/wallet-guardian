#!/usr/bin/env node
/**
 * Initialise per-wallet publish credentials for POST /api/bot/tasks.
 *
 * Token format (src/server/tasks.mjs): wgpub_<16 hex credential_id>.<64 hex secret>
 *   credential_id: 8 CSPRNG bytes; secret: 32 CSPRNG bytes;
 *   D1 stores only sha256(<64-hex secret string>) in publish_credentials.secret_hash.
 * The plaintext token is written only to a 0600 file in --store-dir (secret name WG_PUBLISH_TOKEN).
 * It is never printed, never written to D1, and never bundled into Pages.
 *
 * Re-runnable: a stored credential that D1 confirms is reused. A credential that cannot be
 * verified (store/D1 mismatch, or an active D1 row with no stored secret) stops the run instead of
 * silently issuing another token. --rotate revokes (revoked_at) and reissues explicitly.
 * --revoke checks ^[a-f0-9]{16}$ before any SQL.
 * --revoke-wallet revokes that address's credential, membership, and sessions in one D1 batch.
 * It prints a masked address and never prints the token.
 */
import { createHash, randomBytes } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { d1Execute, maskAddress, maskHex } from "./lib/d1.mjs";
import { revokeWalletAccess, sqlBatchDb, validateCredentialId, validateWalletAddress } from "./lib/revoke-access.mjs";

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const TENANT = /^tnt_[a-f0-9]{16}$/;
const DEFAULT_TTL = 24 * 60 * 60;

function fail(message, code = 2) {
  process.stderr.write(`bootstrap-publisher: ${message}\n`);
  process.exit(code);
}

const { values } = parseArgs({
  options: {
    target: { type: "string" },
    "persist-to": { type: "string" },
    config: { type: "string" },
    "wallets-file": { type: "string" },
    wallet: { type: "string", multiple: true },
    "store-dir": { type: "string" },
    "ttl-seconds": { type: "string" },
    rotate: { type: "boolean", default: false },
    revoke: { type: "string" },
    "revoke-wallet": { type: "string" },
    "confirm-remote": { type: "boolean", default: false },
  },
  strict: true,
});

// Format before any SQL, including a quote in --revoke. Validators do not build statements.
if (values.revoke !== undefined && values["revoke-wallet"] !== undefined) {
  fail("pass only one of --revoke or --revoke-wallet");
}
if (values.revoke !== undefined) {
  const parsed = validateCredentialId(values.revoke);
  if (!parsed.ok) fail(parsed.error);
}
if (values["revoke-wallet"] !== undefined) {
  const parsed = validateWalletAddress(values["revoke-wallet"]);
  if (!parsed.ok) fail(parsed.error);
}

const target = values.target;
if (target !== "local" && target !== "remote") fail("--target must be local or remote");
if (target === "remote" && !values["confirm-remote"]) fail("--confirm-remote is required for --target remote");
if (!values["store-dir"]) fail("--store-dir is required (restricted secret store, never inside the repo)");
const storeDir = resolve(values["store-dir"]);
const persistTo = values["persist-to"] ? resolve(values["persist-to"]) : undefined;
const d1opts = { persistTo, config: values.config };
const ttl = values["ttl-seconds"] === undefined ? DEFAULT_TTL : Number(values["ttl-seconds"]);
const minTtl = target === "remote" ? 60 : 1;
if (!Number.isSafeInteger(ttl) || ttl < minTtl || ttl > 7 * DEFAULT_TTL) fail(`--ttl-seconds must be ${minTtl}..${7 * DEFAULT_TTL}`);

function secureDir(dir) {
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
}

function writeSecret(path, value) {
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600, flag: "wx" });
  chmodSync(tmp, 0o600);
  renameSync(tmp, path);
}

function sha256Hex(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

secureDir(storeDir);
const credDir = join(storeDir, "credentials");
secureDir(credDir);
const targetKey = target === "remote" ? "remote" : `local:${persistTo ?? "default"}`;

function loadRecords() {
  const out = [];
  for (const name of readdirSync(credDir)) {
    if (!/^WG_PUBLISH_TOKEN\.[a-f0-9]{16}\.json$/.test(name)) continue;
    const record = JSON.parse(readFileSync(join(credDir, name), "utf8"));
    if (record.target_key === targetKey) out.push({ ...record, file: join(credDir, name) });
  }
  return out;
}

function saveRecord(record) {
  const { file, ...rest } = record;
  const path = file ?? join(credDir, `WG_PUBLISH_TOKEN.${record.credential_id}.json`);
  if (existsSync(path)) rmSync(path);
  writeSecret(path, rest);
  return path;
}

function tenantId() {
  const path = join(storeDir, `tenant.${target}.json`);
  if (existsSync(path)) {
    const stored = JSON.parse(readFileSync(path, "utf8"));
    if (!TENANT.test(stored.tenant_id ?? "")) fail("stored tenant id is malformed; refusing to continue");
    return stored.tenant_id;
  }
  const tenant = `tnt_${randomBytes(8).toString("hex")}`;
  writeSecret(path, { tenant_id: tenant, created_at: new Date().toISOString(), target });
  return tenant;
}

function walletList() {
  const wallets = new Set();
  for (const value of values.wallet ?? []) wallets.add(value);
  if (values["wallets-file"]) {
    const data = JSON.parse(readFileSync(values["wallets-file"], "utf8"));
    for (const entry of data.wallets ?? []) {
      if (entry && (entry.family === undefined || entry.family === "evm")) wallets.add(entry.address);
    }
  }
  const out = [];
  for (const wallet of wallets) {
    if (typeof wallet !== "string" || !ADDRESS.test(wallet)) fail("wallet list contains a malformed address");
    out.push(wallet.toLowerCase());
  }
  return [...new Set(out)];
}

function rowsFor(wallet) {
  return d1Execute(
    target,
    `SELECT credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at FROM publish_credentials WHERE wallet_address = '${wallet}'`,
    d1opts,
  );
}

function active(row, now) {
  return row.revoked_at == null && (row.expires_at == null || Number(row.expires_at) > now);
}

function revoke(id, now) {
  const parsed = validateCredentialId(id);
  if (!parsed.ok) fail(parsed.error);
  d1Execute(target, `UPDATE publish_credentials SET revoked_at = ${now} WHERE credential_id = '${parsed.id}' AND revoked_at IS NULL`, d1opts);
  const [row] = d1Execute(target, `SELECT credential_id, revoked_at FROM publish_credentials WHERE credential_id = '${parsed.id}'`, d1opts);
  for (const record of loadRecords()) {
    if (record.credential_id === id) saveRecord({ ...record, status: "revoked", revoked_at: row?.revoked_at ?? now, token: null });
  }
  return { credential_id: maskHex(id), revoked: Boolean(row && row.revoked_at != null) };
}

const now = Math.floor(Date.now() / 1000);
const report = { target, tool: "bootstrap-publisher", at: new Date(now * 1000).toISOString(), results: [] };

if (values.revoke) {
  report.results.push({ action: "revoke", ...revoke(values.revoke, now) });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exit(0);
}

if (values["revoke-wallet"]) {
  const parsed = validateWalletAddress(values["revoke-wallet"]);
  if (!parsed.ok) fail(parsed.error);
  await revokeWalletAccess(sqlBatchDb((sql) => d1Execute(target, sql, d1opts)), parsed.address, now);
  for (const record of loadRecords()) {
    if (String(record.wallet_address).toLowerCase() === parsed.address) {
      saveRecord({ ...record, status: "revoked", revoked_at: now, token: null });
    }
  }
  report.results.push({ action: "revoke-wallet", wallet: maskAddress(parsed.address), revoked: true });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exit(0);
}

const wallets = walletList();
if (wallets.length === 0) fail("no wallets given (--wallet or --wallets-file)");
const tenant = tenantId();
let blocked = false;

for (const wallet of wallets) {
  const rows = rowsFor(wallet);
  const byId = new Map(rows.map((row) => [row.credential_id, row]));
  const records = loadRecords().filter((record) => record.wallet_address === wallet && record.status === "active");
  let verified = null;
  const unverifiable = [];
  for (const record of records) {
    const row = byId.get(record.credential_id);
    const secret = typeof record.token === "string" ? record.token.split(".")[1] : "";
    const matches =
      row &&
      row.secret_hash === sha256Hex(secret) &&
      row.tenant_id === record.tenant_id &&
      String(row.wallet_address).toLowerCase() === wallet;
    if (!matches) unverifiable.push(record.credential_id);
    else if (active(row, now) && record.tenant_id === tenant) verified = { record, row };
    else saveRecord({ ...record, status: row.revoked_at != null ? "revoked" : "expired", token: null });
  }
  const knownIds = new Set(records.map((record) => record.credential_id));
  const strangers = rows.filter((row) => active(row, now) && !knownIds.has(row.credential_id)).map((row) => row.credential_id);
  const entry = { wallet: maskAddress(wallet), tenant: maskHex(tenant, 8) };

  if (!values.rotate && (unverifiable.length || strangers.length)) {
    blocked = true;
    report.results.push({
      ...entry,
      status: "unverifiable_credential",
      detail: "existing credential cannot be verified against the secret store; not overwriting. Use --rotate to revoke and reissue.",
      unverifiable: [...unverifiable, ...strangers].map((id) => maskHex(id)),
    });
    continue;
  }
  if (verified && !values.rotate) {
    report.results.push({
      ...entry,
      status: "ok_existing",
      credential_id: maskHex(verified.record.credential_id),
      expires_at: Number(verified.row.expires_at),
      store_file: verified.record.file,
    });
    continue;
  }
  if (values.rotate) {
    for (const row of rows.filter((item) => item.revoked_at == null)) revoke(row.credential_id, now);
    for (const record of loadRecords().filter((item) => item.wallet_address === wallet && item.status === "active")) {
      saveRecord({ ...record, status: "superseded", token: null });
    }
  }

  const id = randomBytes(8).toString("hex");
  const secret = randomBytes(32).toString("hex");
  const expires = now + ttl;
  const record = {
    schema: "guardian.publish_credential.v1",
    secret_name: "WG_PUBLISH_TOKEN",
    credential_id: id,
    token: `wgpub_${id}.${secret}`,
    tenant_id: tenant,
    wallet_address: wallet,
    target,
    target_key: targetKey,
    created_at: now,
    expires_at: expires,
    status: "pending",
  };
  const file = saveRecord(record);
  try {
    d1Execute(
      target,
      `INSERT INTO publish_credentials (credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at) VALUES ('${id}', '${sha256Hex(secret)}', '${tenant}', '${wallet}', ${expires}, NULL)`,
      d1opts,
    );
  } catch (error) {
    rmSync(file, { force: true });
    fail(`insert failed: ${String(error.message).slice(0, 400)}`, 3);
  }
  const [row] = d1Execute(
    target,
    `SELECT credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at FROM publish_credentials WHERE credential_id = '${id}'`,
    d1opts,
  );
  const ok =
    row &&
    row.secret_hash === sha256Hex(secret) &&
    row.tenant_id === tenant &&
    row.wallet_address === wallet &&
    Number(row.expires_at) === expires &&
    row.revoked_at == null;
  if (!ok) fail("readback after insert did not match; credential left pending in store for inspection", 4);
  saveRecord({ ...record, file, status: "active" });
  report.results.push({
    ...entry,
    status: "issued",
    credential_id: maskHex(id),
    expires_at: expires,
    readback_match: true,
    store_file: file,
  });
}

process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
process.exit(blocked ? 5 : 0);
