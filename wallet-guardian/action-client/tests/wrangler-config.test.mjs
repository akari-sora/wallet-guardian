import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { productionViewEntries } from "../src/view-origin.mjs";

// Keys `wrangler pages deploy` accepts in a Pages config (workers-utils validation-pages.ts).
const PAGES_KEYS = new Set([
  "$schema", "pages_build_output_dir", "name", "compatibility_date", "compatibility_flags", "send_metrics",
  "no_bundle", "limits", "placement", "vars", "durable_objects", "kv_namespaces", "queues", "r2_buckets",
  "d1_databases", "vectorize", "hyperdrive", "services", "analytics_engine_datasets", "ai", "version_metadata",
  "dev", "mtls_certificates", "browser", "upload_source_maps", "env",
]);

function readJsonc(url) {
  const text = readFileSync(url, "utf8").replace(/^\s*\/\/.*$/gm, "");
  return JSON.parse(text);
}

test("wrangler.jsonc only uses keys Pages deploy accepts, binds the real D1, and keeps preview off", () => {
  const config = readJsonc(new URL("../wrangler.jsonc", import.meta.url));
  for (const key of Object.keys(config)) assert.equal(PAGES_KEYS.has(key), true, `unsupported Pages key: ${key}`);
  assert.deepEqual(Object.keys(config.env ?? {}).filter((name) => name !== "preview" && name !== "production"), []);
  const [db] = config.d1_databases;
  assert.equal(config.d1_databases.length, 1);
  assert.equal(db.binding, "DB");
  assert.equal(db.database_name, "wallet-guardian-tasks");
  assert.match(db.database_id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  assert.notEqual(db.database_id, "00000000-0000-4000-8000-000000000000");
  assert.equal(db.migrations_dir, "migrations");
  const entry = productionViewEntries()[0];
  assert.ok(entry);
  assert.deepEqual(config.vars, {
    AUTH_DOMAIN: new URL(entry.origin).host,
    AUTH_URI: entry.origin,
    AUTH_CHAIN_ID: "56",
    AUTH_CHALLENGE_LIMIT: "10",
    AUTH_CHALLENGE_GLOBAL_LIMIT: "200",
  });
  for (const name of Object.keys(config.vars)) assert.doesNotMatch(name, /SECRET|TOKEN|SEND/);
  assert.deepEqual(config.env.preview.d1_databases, []);
  assert.deepEqual(config.env.preview.vars, {});
});
