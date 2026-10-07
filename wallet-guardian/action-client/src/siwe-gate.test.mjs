import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { LOGIN_STATEMENT } from "./login-statement.mjs";
import { buildSiweMessage, checksumAddress } from "./siwe-message.mjs";
import { LOGIN_CHAIN_ID, LOGIN_DOMAIN, LOGIN_URI, acceptLoginChallenge, acceptLoginMessage } from "./siwe-gate.mjs";

const ADDRESS = checksumAddress("0x1111111111111111111111111111111111111111");
const NOW = Date.parse("2026-10-07T00:00:00.000Z");
const NONCE = "ab".repeat(16);

function fields(overrides = {}) {
  return {
    domain: LOGIN_DOMAIN,
    address: ADDRESS,
    uri: LOGIN_URI,
    chainId: LOGIN_CHAIN_ID,
    nonce: NONCE,
    issuedAt: new Date(NOW).toISOString(),
    expirationTime: new Date(NOW + 600_000).toISOString(),
    ...overrides,
  };
}

function message(overrides) {
  return buildSiweMessage(fields(overrides));
}

function challenge(overrides = {}, messageOverrides = {}) {
  const built = message(messageOverrides);
  const parsed = fields(messageOverrides);
  return {
    message: built,
    csrf_token: "ef".repeat(32),
    expires_at: Math.floor(Date.parse(parsed.expirationTime) / 1000),
    domain: parsed.domain,
    uri: parsed.uri,
    chain_id: parsed.chainId,
    address: parsed.address,
    erc1271: "unsupported",
    execution_authorized: false,
    send_authorized: false,
    ...overrides,
  };
}

test("a rebuilt fixed-audience message is the only one personal_sign may see", () => {
  const body = challenge();
  assert.equal(acceptLoginChallenge(body, ADDRESS.toLowerCase(), NOW), body.message);
  assert.equal(acceptLoginMessage(body.message, ADDRESS, NOW).message, body.message);
});

test("substring decoys with the login phrase are not signed", () => {
  const decoys = [
    message({ domain: "evil.example" }),
    message({ uri: "https://evil.example" }),
    message({ chainId: 1 }),
    message({ address: checksumAddress("0x2222222222222222222222222222222222222222") }),
    message({ expirationTime: new Date(NOW - 1000).toISOString() }),
    `${message()}\nResources:\n- https://evil.example/steal`,
    `${message()}\nNonce: ${"cd".repeat(16)}`,
    message().replace("Version: 1", "Version: 1\nNot Before: 2020-01-01T00:00:00.000Z"),
  ];
  for (const decoy of decoys) {
    assert.equal(decoy.includes(LOGIN_STATEMENT) || decoy.includes("Sign in to Wallet Guardian"), true);
    assert.equal(decoy.includes("wants you to sign in with your Ethereum account:"), true);
    assert.equal(acceptLoginMessage(decoy, ADDRESS, NOW), null);
    assert.equal(acceptLoginChallenge({ ...challenge(), message: decoy }, ADDRESS, NOW), null);
  }
  const extra = challenge({ request_id: "abc" });
  assert.equal(acceptLoginChallenge(extra, ADDRESS, NOW), null);
  const send = challenge({ send_authorized: true });
  assert.equal(acceptLoginChallenge(send, ADDRESS, NOW), null);
});

test("the page asks the gate before personal_sign", () => {
  const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  assert.equal(source.includes("acceptLoginChallenge"), true);
  assert.equal(source.includes("siwe-gate.mjs"), true);
  assert.equal(source.includes(".includes(LOGIN_STATEMENT)"), false);
  assert.equal(source.includes("wants you to sign in with your Ethereum account:"), false);
  assert.equal(source.includes("personal_sign"), true);
  const signAt = source.indexOf("personal_sign");
  const gateAt = source.indexOf("acceptLoginChallenge");
  assert.equal(gateAt >= 0 && gateAt < signAt, true);
});
