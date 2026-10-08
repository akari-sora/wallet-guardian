#!/usr/bin/env node
/**
 * Publish one private task through POST /api/bot/tasks with a publish credential.
 * The API derives tenant and wallet from the credential and generates the ref.
 *
 * Token source: env WG_PUBLISH_TOKEN (with --wallet), or --credential-file written by bootstrap-publisher.
 * The token is never printed. Default task is a synthetic, non-executable TEST task.
 */
import { readFileSync } from "node:fs";
import https from "node:https";
import http from "node:http";
import { parseArgs } from "node:util";
import { digestBody } from "../src/canonical.mjs";
import { validatePlan } from "../src/plan-validation.mjs";
import { maskAddress } from "./lib/d1.mjs";
import { buildTestTask } from "./lib/test-task.mjs";

const POLICY = JSON.parse(readFileSync(new URL("../../config/deployments/private-task-origins.json", import.meta.url), "utf8"));
const TOKEN = /^wgpub_[a-f0-9]{16}\.[a-f0-9]{64}$/;

function fail(message, code = 2) {
  process.stderr.write(`publish-private-task: ${message}\n`);
  process.exit(code);
}

const { values } = parseArgs({
  options: {
    deployment: { type: "string", default: "wg-private-task-v1" },
    "api-base": { type: "string" },
    "insecure-local": { type: "boolean", default: false },
    "credential-file": { type: "string" },
    wallet: { type: "string" },
    "task-file": { type: "string" },
    label: { type: "string", default: "D1-LIVE-01" },
    "ttl-seconds": { type: "string", default: String(24 * 60 * 60) },
  },
  strict: true,
});

const entry = (POLICY.deployments ?? []).find((item) => item.deployment_id === values.deployment);
if (!entry || entry.execution_authorized !== false || entry.purpose !== "private_task_view") fail("deployment is not in the approved view-origin policy");
let base = entry.origin;
if (values["api-base"]) {
  const url = new URL(values["api-base"]);
  const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
  if (!loopback && url.origin !== entry.origin) fail("--api-base must be the approved origin or a loopback test server");
  base = url.origin;
}
const insecure = values["insecure-local"] === true;
if (insecure && !/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) fail("--insecure-local is only allowed for loopback");

let token = process.env.WG_PUBLISH_TOKEN ?? "";
let wallet = values.wallet ?? "";
if (values["credential-file"]) {
  const record = JSON.parse(readFileSync(values["credential-file"], "utf8"));
  if (record.status !== "active") fail("credential record is not active");
  token = record.token;
  wallet = record.wallet_address;
}
if (!TOKEN.test(token)) fail("no publish token (set WG_PUBLISH_TOKEN or --credential-file)");
if (!/^0x[0-9a-fA-F]{40}$/.test(wallet)) fail("wallet for the task sender is missing");

const ttl = Number(values["ttl-seconds"]);
if (!Number.isSafeInteger(ttl) || ttl < 60 || ttl > 7 * 24 * 60 * 60) fail("--ttl-seconds must be 60..604800");
const now = Math.floor(Date.now() / 1000);

let task;
if (values["task-file"]) {
  task = JSON.parse(readFileSync(values["task-file"], "utf8"));
  if (String(task?.plan?.sender ?? "").toLowerCase() !== wallet.toLowerCase()) fail("task sender does not match the credential wallet");
  task.plan.digest = await digestBody(task.plan);
  validatePlan(task.plan);
} else {
  task = await buildTestTask(wallet, { label: values.label, now });
}

function post(url, body) {
  const target = new URL(url);
  const lib = target.protocol === "https:" ? https : http;
  const payload = Buffer.from(JSON.stringify(body));
  return new Promise((resolvePromise, reject) => {
    const req = lib.request(
      target,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": payload.length,
          authorization: `Bearer ${token}`,
        },
        rejectUnauthorized: !insecure,
        timeout: 15000,
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => resolvePromise({ status: res.statusCode, headers: res.headers, text: Buffer.concat(chunks).toString("utf8") }));
      },
    );
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", reject);
    req.end(payload);
  });
}

const response = await post(`${base}/api/bot/tasks`, { expires_at: now + ttl, task });
let body;
try {
  body = JSON.parse(response.text);
} catch {
  fail(`non-json response, status ${response.status}`, 3);
}
const out = {
  tool: "publish-private-task",
  status: response.status,
  cache_control: response.headers["cache-control"] ?? null,
  wallet: maskAddress(wallet),
  test_task: !values["task-file"],
  ref: body.ref ?? null,
  view_url: body.view_url ?? null,
  expires_at: body.expires_at ?? null,
  expires_at_iso: body.expires_at ? new Date(body.expires_at * 1000).toISOString() : null,
  execution_authorized: body.execution_authorized,
  send_authorized: body.send_authorized,
  error: body.error ?? null,
};
process.stdout.write(`${JSON.stringify(out, null, 2)}\n`);
process.exit(response.status === 201 ? 0 : 1);
