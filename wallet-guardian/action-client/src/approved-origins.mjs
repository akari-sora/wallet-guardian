/**
 * Build-time view origins. A listed origin is not execution authorization.
 * http://127.0.0.1:43127 is included only when the caller says this is not a production build.
 */
import policy from "../../config/deployments/private-task-origins.json" with { type: "json" };

const DEV_ORIGIN = "http://127.0.0.1:43127";

export function policyOrigins(document = policy) {
  const deployments = document && Array.isArray(document.deployments) ? document.deployments : [];
  return deployments
    .filter(
      (entry) =>
        entry &&
        typeof entry.origin === "string" &&
        entry.view_origin_approved === true &&
        entry.execution_authorized === false,
    )
    .map((entry) => entry.origin);
}

export function approvedOrigins({ production }) {
  const origins = policyOrigins();
  if (production !== true) origins.push(DEV_ORIGIN);
  return origins;
}

export function originIsApproved(origin, options) {
  return approvedOrigins(options).includes(origin);
}
