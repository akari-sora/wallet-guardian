import assert from "node:assert/strict";
import test from "node:test";
import { harness, tick } from "./client-review-harness.mjs";

test("an unapproved production origin calls no wallet methods and disables login", async () => {
  const h = await harness({
    env: { PROD: true },
    setupWindow(window) {
      window.location = new URL("https://evil.example/");
    },
  });
  assert.equal(h.elements.get("login").disabled, true);
  assert.match(h.elements.get("banner").textContent, /此網址不是核准來源/);
  assert.match(h.elements.get("view").textContent, /此網址不是核准來源/);
  h.elements.get("login").listeners.get("click")();
  await tick();
  assert.equal(h.walletCalls.length, 0);
  assert.equal(h.walletCalls.some((call) => call.method === "eth_requestAccounts"), false);
});
