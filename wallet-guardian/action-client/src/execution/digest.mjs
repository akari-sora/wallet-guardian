/** Digests of a loaded registry and its policies. This does not hash a bundle. */

import { createHash } from "node:crypto";

import { canonicalStringify } from "../canonical.mjs";

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function registryDigest(registry) {
  return sha256(canonicalStringify({
    deployments: registry.deployments,
    identity: registry.identity,
    schema_version: registry.schema_version,
    send_enabled: registry.send_enabled,
    user_approved: registry.user_approved,
  }));
}

export function policyDigest(registry) {
  return sha256(canonicalStringify({
    policies: registry.policies,
    schema_version: registry.schema_version,
  }));
}
