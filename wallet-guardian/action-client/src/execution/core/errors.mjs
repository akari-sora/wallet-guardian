/** Core refusal. `reason` is a stable code that tests and callers match on. */
export class CoreRefusal extends Error {
  constructor(reason, detail = "") {
    super(detail ? `core refused: ${reason}: ${detail}` : `core refused: ${reason}`);
    this.name = "CoreRefusal";
    this.reason = reason;
    this.detail = detail;
  }
}

export function refuse(reason, detail = "") {
  throw new CoreRefusal(reason, detail);
}
