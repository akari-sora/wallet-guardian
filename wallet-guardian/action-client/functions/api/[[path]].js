import { handleApi } from "../../src/server/handle.mjs";
import { internalError } from "../../src/server/http.mjs";

// Every /api request is handled here so it cannot fall through to static HTML.
export async function onRequest(context) {
  try {
    const response = await handleApi(context.request, context.env);
    if (!(response instanceof Response)) return internalError();
    return response;
  } catch {
    return internalError();
  }
}
