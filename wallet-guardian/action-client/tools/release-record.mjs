/**
 * Structural checks for action-client/deploy/releases.json.
 * mainnet_send must be false. An origin must be an approved private-task view origin.
 * This module does not deploy and does not fetch.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const RELEASES_PATH = join(ROOT, "action-client/deploy/releases.json");
const ORIGINS_PATH = join(ROOT, "config/deployments/private-task-origins.json");
const SCHEMA_PATH = join(ROOT, "action-client/deploy/release.schema.json");

const RELEASE_FIELDS = [
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
];

const DIGEST_FIELDS = ["execution_registry", "policy", "core_bundle", "dist"];

export function loadJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

export function approvedOriginEntries(policy = loadJson(ORIGINS_PATH)) {
  const deployments = policy && Array.isArray(policy.deployments) ? policy.deployments : [];
  return deployments.filter(
    (entry) =>
      entry &&
      typeof entry.origin === "string" &&
      entry.origin.startsWith("https://") &&
      entry.purpose === "private_task_view" &&
      entry.view_origin_approved === true &&
      entry.execution_authorized === false,
  );
}

function push(errors, path, message) {
  errors.push(path ? `${path}: ${message}` : message);
}

function isSha256(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

/** @returns {string[]} */
export function validateRelease(release, entries = approvedOriginEntries(), path = "") {
  const errors = [];
  if (!release || typeof release !== "object" || Array.isArray(release)) {
    push(errors, path, "release must be an object");
    return errors;
  }
  for (const field of RELEASE_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(release, field)) push(errors, path, `missing ${field}`);
  }
  for (const field of Object.keys(release)) {
    if (!RELEASE_FIELDS.includes(field) && field !== "digests") push(errors, path, `unexpected ${field}`);
  }
  if (Object.prototype.hasOwnProperty.call(release, "digests")) {
    const digests = release.digests;
    if (!digests || typeof digests !== "object" || Array.isArray(digests)) {
      push(errors, path, "digests");
    } else {
      for (const field of DIGEST_FIELDS) {
        if (!isSha256(digests[field])) push(errors, path, `digests ${field}`);
      }
      for (const field of Object.keys(digests)) {
        if (!DIGEST_FIELDS.includes(field)) push(errors, path, `unexpected ${field}`);
      }
    }
  }
  if (typeof release.release_id !== "string" || release.release_id.length === 0) push(errors, path, "release_id");
  if (typeof release.deployment_id !== "string" || release.deployment_id.length === 0) push(errors, path, "deployment_id");
  if (typeof release.client_build !== "string" || release.client_build.length === 0) push(errors, path, "client_build");
  if (typeof release.cf_deployment_url !== "string" || release.cf_deployment_url.length === 0) {
    push(errors, path, "cf_deployment_url");
  }
  if (typeof release.source_sha !== "string" || !/^[0-9a-f]{40}$/.test(release.source_sha)) push(errors, path, "source_sha");
  if (release.variant !== "send_off" && release.variant !== "mode_a") push(errors, path, "variant");
  if (typeof release.walletconnect !== "boolean") push(errors, path, "walletconnect");
  if (release.mainnet_send !== false) push(errors, path, "mainnet_send must be false");
  if (release.variant === "send_off" && release.mainnet_send !== false) {
    push(errors, path, "send_off cannot set mainnet_send true");
  }
  if (!isSha256(release.headers_sha256)) push(errors, path, "headers_sha256");
  if (!release.files || typeof release.files !== "object" || Array.isArray(release.files)) {
    push(errors, path, "files");
  } else {
    for (const [name, hash] of Object.entries(release.files)) {
      if (!isSha256(hash)) push(errors, path, `files ${name}`);
    }
  }
  if (release.status !== "active" && release.status !== "retired" && release.status !== "deleted") {
    push(errors, path, "status");
  }
  const known = entries.find((entry) => entry.origin === release.origin);
  if (!known) push(errors, path, "origin is not in private-task-origins.json");
  else if (release.deployment_id !== known.deployment_id) push(errors, path, "deployment_id does not match the origin entry");
  return errors;
}

/** @returns {string[]} */
export function validateReleaseDocument(document, entries = approvedOriginEntries()) {
  const errors = [];
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    return ["document must be an object"];
  }
  if (document.schema_version !== "guardian.action_client_releases.v1") errors.push("schema_version");
  for (const field of Object.keys(document)) {
    if (field !== "schema_version" && field !== "note" && field !== "releases") errors.push(`unexpected ${field}`);
  }
  if (document.note !== undefined && typeof document.note !== "string") errors.push("note");
  if (!Array.isArray(document.releases)) {
    errors.push("releases");
    return errors;
  }
  document.releases.forEach((release, index) => {
    errors.push(...validateRelease(release, entries, `releases[${index}]`));
  });
  return errors;
}

export function loadReleaseDocument(path = RELEASES_PATH) {
  return loadJson(path);
}

export function findRelease(document, releaseId) {
  const releases = document && Array.isArray(document.releases) ? document.releases : [];
  return releases.find((release) => release && release.release_id === releaseId) ?? null;
}

export const PATHS = { ROOT, RELEASES_PATH, ORIGINS_PATH, SCHEMA_PATH };
