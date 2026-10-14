import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import { originIsApproved, policyOrigins } from "../src/approved-origins.mjs";
import { PATHS, loadReleaseDocument, validateRelease, validateReleaseDocument } from "../tools/release-record.mjs";

const deployDoc = readFileSync(new URL("../../docs/deploy.md", import.meta.url), "utf8");
const schema = JSON.parse(readFileSync(PATHS.SCHEMA_PATH, "utf8"));
const origins = JSON.parse(readFileSync(PATHS.ORIGINS_PATH, "utf8"));

test("deploy doc does not recommend vercel --prod", () => {
  assert.equal(deployDoc.includes("vercel --prod"), false);
  assert.match(deployDoc, /wallet-guardian-action-client/);
  assert.match(deployDoc, /fixed_https_origin/);
  assert.match(deployDoc, /5a09b4e6\.wallet-guardian-action-client\.pages\.dev/);
  assert.doesNotMatch(deployDoc, /--project-name wallet-guardian(?!-action-client)/);
});

test("release schema and verifier exist", () => {
  assert.equal(existsSync(new URL("../tools/verify-deployed-artifact.mjs", import.meta.url)), true);
  assert.equal(existsSync(new URL("../tools/artifact-manifest.mjs", import.meta.url)), true);
  assert.equal(schema.properties.schema_version.const, "guardian.action_client_releases.v1");
  const release = schema.$defs.release;
  for (const field of [
    "release_id",
    "deployment_id",
    "origin",
    "source_sha",
    "client_build",
    "variant",
    "walletconnect",
    "mainnet_send",
    "files",
    "headers_sha256",
    "cf_deployment_url",
    "status",
  ]) {
    assert.equal(release.required.includes(field), true, field);
  }
  assert.deepEqual(release.properties.variant.enum, ["send_off", "mode_a"]);
  assert.equal(release.properties.mainnet_send.const, false);
  assert.deepEqual(release.properties.status.enum, ["active", "retired", "deleted"]);
  assert.equal(release.allOf[0].if.properties.variant.const, "send_off");
  assert.equal(release.allOf[0].then.properties.mainnet_send.const, false);
});

test("checked-in release inventory does not claim a hashed live deploy", () => {
  const document = loadReleaseDocument();
  assert.deepEqual(validateReleaseDocument(document), []);
  assert.deepEqual(document.releases, []);
  assert.match(document.note, /empty releases array/i);
});

function sampleRelease(overrides = {}) {
  return {
    release_id: "fixture-send-off",
    deployment_id: origins.deployments[0].deployment_id,
    origin: origins.deployments[0].origin,
    source_sha: "a".repeat(40),
    client_build: "action-client@0.3.0",
    variant: "send_off",
    walletconnect: false,
    mainnet_send: false,
    files: { "index.html": "b".repeat(64) },
    headers_sha256: "c".repeat(64),
    cf_deployment_url: "https://example.pages.dev",
    status: "retired",
    ...overrides,
  };
}

test("send_off with mainnet_send true fails schema checks", () => {
  const errors = validateRelease(sampleRelease({ mainnet_send: true }));
  assert.equal(errors.some((error) => error.includes("mainnet_send")), true);
});

test("origin outside private-task-origins.json fails", () => {
  const errors = validateRelease(sampleRelease({ origin: "https://evil.example" }));
  assert.equal(errors.some((error) => error.includes("private-task-origins")), true);
});

test("a structurally valid send_off record with the approved origin passes", () => {
  assert.deepEqual(validateRelease(sampleRelease()), []);
});

test("dev origin is approved only outside production builds", () => {
  for (const entry of origins.deployments) assert.equal(policyOrigins().includes(entry.origin), true);
  assert.equal(originIsApproved("http://127.0.0.1:43127", { production: false }), true);
  assert.equal(originIsApproved("http://127.0.0.1:43127", { production: true }), false);
  assert.equal(originIsApproved("https://evil.example", { production: false }), false);
  assert.equal(originIsApproved("http://localhost:43127", { production: false }), false);
});
