import { internalError, stampNoStore } from "../../src/server/http.mjs";

export async function onRequest(context) {
  try {
    return stampNoStore(await context.next());
  } catch {
    return internalError();
  }
}
