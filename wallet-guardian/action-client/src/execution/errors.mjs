/** Failure for the execution registry. A failure never becomes an approval. */

export class ExecutionRegistryError extends Error {
  constructor(code, detail = "") {
    super(detail === "" ? code : `${code}: ${detail}`);
    this.name = "ExecutionRegistryError";
    this.code = code;
    this.detail = detail;
  }
}
