/**
 * Mode A WP-2 route contract.
 * The production view origin is read from the policy file. This test does not hardcode it.
 *
 * AC5 allowlist (files that may contain that origin):
 * - config/deployments/private-task-origins.json and the packaged copy under
 *   src/guardian/_resources: the versioned policy is the source of the origin.
 * - action-client/wrangler.jsonc: Pages config cannot import the policy. AC1 compares
 *   AUTH_URI and AUTH_DOMAIN to the policy instead of generating wrangler.jsonc.
 * - docs/ including docs/reviews: operator-facing addresses, not the issuer or client.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { parsePrivateTaskRef, privateTaskPathFormatError } from "../src/private-task.mjs";
import { handleApi } from "../src/server/handle.mjs";
import { viewLink } from "../src/server/origins.mjs";
import { LOGIN_DOMAIN, LOGIN_URI } from "../src/siwe-gate.mjs";
import { productionViewEntries } from "../src/view-origin.mjs";
import { checkPublishedView } from "../tools/publish-private-task.mjs";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const CANON = "^ref_[a-f0-9]{32}$";
const TOKEN = "ref_[a-f0-9]{32}";
const REF = `ref_${"ab".repeat(16)}`;
const POLICY_PATH = join(ROOT, "config/deployments/private-task-origins.json");
const PACKAGED_PATH = join(ROOT, "src/guardian/_resources/config/deployments/private-task-origins.json");
const ALLOW = new Set([
  "config/deployments/private-task-origins.json",
  "src/guardian/_resources/config/deployments/private-task-origins.json",
  "action-client/wrangler.jsonc",
]);
const SKIP_DIRS = new Set([
  "node_modules", "dist", ".git", "__pycache__", ".pytest_cache", ".venv", "coverage", ".mypy_cache",
]);

function read(path) {
  return readFileSync(path, "utf8");
}

function readJsonc(path) {
  return JSON.parse(read(path).replace(/^\s*\/\/.*$/gm, ""));
}

function refTokens(source) {
  return [...source.matchAll(/ref_\[[^\]]+\](?:\{[0-9,]+\})?/g)].map((match) => match[0]);
}

function assertSameRefToken(source, label) {
  const found = refTokens(source);
  assert.ok(found.length > 0, label);
  for (const item of found) assert.equal(item, TOKEN, `${label} uses ${item}`);
}

function taskGetRoute(source) {
  const line = source.split("\n").find((item) => item.includes('method: "GET"') && item.includes("tasks"));
  assert.ok(line, "GET /api/tasks route missing");
  const marker = "pattern: /";
  const start = line.indexOf(marker);
  assert.notEqual(start, -1);
  const body = line.slice(start + marker.length);
  const end = body.indexOf("/,");
  assert.notEqual(end, -1);
  return new RegExp(body.slice(0, end).replace(/\\\//g, "/"));
}

function allowed(rel) {
  const posix = rel.split(sep).join("/");
  return ALLOW.has(posix) || posix.startsWith("docs/");
}

function originHits() {
  const policy = JSON.parse(read(POLICY_PATH));
  const needles = policy.deployments.map((entry) => entry.origin);
  const hits = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir)) {
      if (SKIP_DIRS.has(entry)) continue;
      const path = join(dir, entry);
      const info = statSync(path);
      if (info.isDirectory()) {
        walk(path);
        continue;
      }
      if (!info.isFile() || info.size > 2_000_000) continue;
      const rel = relative(ROOT, path);
      if (allowed(rel)) continue;
      const bytes = readFileSync(path);
      if (bytes.includes(0)) continue;
      const text = bytes.toString("utf8");
      for (const needle of needles) {
        if (text.includes(needle)) hits.push(rel.split(sep).join("/"));
      }
    }
  };
  walk(ROOT);
  return hits;
}

const policyText = read(POLICY_PATH);
const policy = JSON.parse(policyText);
const wrangler = readJsonc(join(ROOT, "action-client/wrangler.jsonc"));
const sources = {
  "src/guardian/actions/workbench.py": read(join(ROOT, "src/guardian/actions/workbench.py")),
  "action-client/src/server/tasks.mjs": read(join(ROOT, "action-client/src/server/tasks.mjs")),
  "action-client/src/server/handle.mjs": read(join(ROOT, "action-client/src/server/handle.mjs")),
  "action-client/src/private-task.mjs": read(join(ROOT, "action-client/src/private-task.mjs")),
  "action-client/tools/publish-private-task.mjs": read(join(ROOT, "action-client/tools/publish-private-task.mjs")),
};
const mainSource = read(join(ROOT, "action-client/src/main.ts"));

test("ref syntax is the same token in the issuer, API, and client", () => {
  assert.equal(read(PACKAGED_PATH), policyText);
  assert.equal(policy.schema, "guardian.private_task_origins.v1");
  for (const [label, source] of Object.entries(sources)) assertSameRefToken(source, label);
  assert.equal(sources["src/guardian/actions/workbench.py"].includes(CANON), true);
  assert.equal(sources["action-client/src/server/tasks.mjs"].includes(CANON), true);
  assert.equal(sources["action-client/src/private-task.mjs"].includes(CANON), true);
  assert.equal(mainSource.includes(CANON), true);
  assert.match(mainSource, /const REF_PATTERN = \/\^ref_\[a-f0-9\]\{32\}\$\//);
  assert.equal(sources["action-client/tools/publish-private-task.mjs"].includes(CANON), true);
  assert.match(sources["action-client/src/server/handle.mjs"], /\^\\\/api\\\/tasks\\\/\(ref_\[a-f0-9\]\{32\}\)\$/);
  assert.equal(sources["action-client/src/private-task.mjs"].includes("?ref="), false);
  assert.equal(sources["action-client/src/server/handle.mjs"].includes('error: "rpc_not_supported"'), true);
});

test("AC1 production view entries match wrangler, viewLink, redirects, and data_path", () => {
  const route = taskGetRoute(sources["action-client/src/server/handle.mjs"]);
  const redirects = read(join(ROOT, "action-client/public/_redirects"));
  const routes = JSON.parse(read(join(ROOT, "action-client/public/_routes.json")));
  assert.match(redirects, /\/t\/\*/);
  assert.equal(routes.include.includes("/api/*"), true);
  assert.ok(policy.deployments.length > 0);
  for (const entry of policy.deployments) {
    const auth = new URL(wrangler.vars.AUTH_URI);
    assert.equal(auth.origin, entry.origin);
    assert.equal(wrangler.vars.AUTH_DOMAIN, auth.host);
    assert.equal(wrangler.vars.AUTH_DOMAIN, new URL(entry.origin).host);
    const link = viewLink(entry.origin, REF);
    assert.equal(link.issued, true);
    assert.equal(link.url, entry.origin + entry.task_path.replace("{ref}", REF));
    assert.equal(link.execution_authorized, false);
    assert.equal(parsePrivateTaskRef(new URL(link.url).pathname), REF);
    assert.equal(typeof entry.data_path, "string");
    const concrete = entry.data_path.replace("{ref}", REF);
    assert.equal(route.test(concrete), true);
    assert.equal(route.test(entry.data_path.replace("{ref}", `ref_${"AB".repeat(16)}`)), false);
    assert.equal(route.test(entry.data_path.replace("{ref}", `ref_${"a".repeat(31)}`)), false);
    assert.equal(route.test(`${concrete}/x`), false);
  }
  assert.equal(LOGIN_URI, wrangler.vars.AUTH_URI);
  assert.equal(LOGIN_DOMAIN, wrangler.vars.AUTH_DOMAIN);
  assert.equal(viewLink("https://evil.example", REF).issued, false);
  assert.equal(viewLink("https://evil.example", REF).url, null);
  assert.deepEqual(productionViewEntries().map((entry) => entry.origin), policy.deployments.map((entry) => entry.origin));
});

test("AC3 publish rejects an evil view_url and a malformed ref", () => {
  const entry = policy.deployments[0];
  const evil = `https://evil.example/t/${REF}`;
  assert.notEqual(checkPublishedView(entry, REF, evil), 0);
  assert.equal(checkPublishedView(entry, REF, entry.origin + entry.task_path.replace("{ref}", REF)), 0);
  assert.notEqual(checkPublishedView(entry, `ref_${"AB".repeat(16)}`, `${entry.origin}/t/ref_${"AB".repeat(16)}`), 0);
  assert.notEqual(checkPublishedView(entry, `ref_${"a".repeat(31)}`, `${entry.origin}/t/ref_${"a".repeat(31)}`), 0);
});

test("AC4 uppercase, short, and extra path refs are client format errors and server 404 JSON", async () => {
  const entry = policy.deployments[0];
  const upper = `/t/ref_${"AB".repeat(16)}`;
  const short = `/t/ref_${"a".repeat(31)}`;
  const extra = `/t/${REF}/x`;
  for (const path of [upper, short, extra]) {
    assert.equal(parsePrivateTaskRef(path), null);
    assert.equal(typeof privateTaskPathFormatError(path), "string");
    assert.equal(privateTaskPathFormatError(path).includes("格式"), true);
  }
  assert.equal(privateTaskPathFormatError(`/t/${REF}`), null);
  assert.equal(parsePrivateTaskRef(`/?ref=${REF}`), null);
  assert.equal(parsePrivateTaskRef(`/tasks/${REF}.json`), null);
  assert.equal(mainSource.includes("privateTaskPathFormatError"), true);

  const env = {
    DB: { withSession() { return {}; } },
    AUTH_DOMAIN: new URL(entry.origin).host,
    AUTH_URI: entry.origin,
    AUTH_CHAIN_ID: "56",
  };
  const paths = [
    `/api/tasks/ref_${"AB".repeat(16)}`,
    `/api/tasks/ref_${"a".repeat(31)}`,
    `/api/tasks/${REF}/x`,
    extra,
  ];
  for (const path of paths) {
    const response = await handleApi(new Request(`${entry.origin}${path}`), env);
    const type = response.headers.get("content-type") ?? "";
    const text = await response.text();
    assert.equal(response.status, 404, path);
    assert.equal(type.includes("application/json"), true, path);
    assert.equal(type.includes("text/html"), false, path);
    const body = JSON.parse(text);
    assert.equal(body.error, "not_found");
    assert.equal(body.execution_authorized, false);
    assert.equal(text.includes("<html"), false);
  }
  const rpc = await handleApi(new Request(`${entry.origin}/api/rpc`), {});
  assert.equal(rpc.status, 404);
  assert.equal((await rpc.json()).error, "rpc_not_supported");
});

test("AC5 code and tests do not hardcode the production view origin", () => {
  assert.deepEqual(originHits(), []);
});
