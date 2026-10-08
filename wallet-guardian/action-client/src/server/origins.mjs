import policy from "../../../config/deployments/private-task-origins.json" with { type: "json" };

/**
 * Approved private-task VIEW origins, shared with the Python issuer.
 * The deployment picks an entry by matching its AUTH_URI origin; a task or caller cannot add one.
 * A view link is not execution authorization and does not set fixed_https_origin.
 */
const ENTRIES = (() => {
  if (!policy || policy.schema !== "guardian.private_task_origins.v1" || !Array.isArray(policy.deployments)) {
    return [];
  }
  return policy.deployments.filter(
    (entry) =>
      entry &&
      typeof entry.origin === "string" &&
      entry.origin.startsWith("https://") &&
      !entry.origin.endsWith("/") &&
      entry.task_path === "/t/{ref}" &&
      entry.purpose === "private_task_view" &&
      entry.execution_authorized === false,
  );
})();

export function approvedViewEntry(origin) {
  return ENTRIES.find((entry) => entry.origin === origin) ?? null;
}

export function viewLink(origin, ref) {
  const entry = approvedViewEntry(origin);
  if (!entry) {
    return {
      issued: false,
      reason: "view_origin_not_approved",
      url: null,
      ref,
      purpose: "private_task_view",
      execution_authorized: false,
      contains_private_state: false,
    };
  }
  return {
    issued: true,
    reason: null,
    url: `${entry.origin}${entry.task_path.replace("{ref}", ref)}`,
    ref,
    deployment_id: entry.deployment_id,
    purpose: "private_task_view",
    view_origin_approved: true,
    execution_authorized: false,
    contains_private_state: false,
  };
}
