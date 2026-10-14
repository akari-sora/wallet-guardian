/**
 * Production private-task view entries from the shared policy file.
 * A listed origin is a view entry point, not execution authorization.
 */
import policy from "../../config/deployments/private-task-origins.json" with { type: "json" };

export function productionViewEntries(document = policy) {
  const deployments = document && Array.isArray(document.deployments) ? document.deployments : [];
  return deployments.filter(
    (entry) =>
      entry &&
      typeof entry.origin === "string" &&
      entry.origin.startsWith("https://") &&
      !entry.origin.endsWith("/") &&
      entry.task_path === "/t/{ref}" &&
      typeof entry.data_path === "string" &&
      entry.data_path.includes("{ref}") &&
      entry.purpose === "private_task_view" &&
      entry.view_origin_approved === true &&
      entry.execution_authorized === false,
  );
}

export function productionViewOrigin(document = policy) {
  const [entry] = productionViewEntries(document);
  if (!entry) throw new Error("no production view origin");
  return entry.origin;
}
