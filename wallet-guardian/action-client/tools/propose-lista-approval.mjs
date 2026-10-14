#!/usr/bin/env node
/**
 * Read-only Lista pin candidate. Prints JSON and does not write config.
 * provenance and validUntil are left blank for a human. A chain read is not an approval.
 */
import { pathToFileURL } from "node:url";
import { canonicalBlockTag } from "../src/lista-reread.mjs";
import {
  TRUSTED_LISTA,
  addressFromStorage,
  codeSha256,
  fetchTrustedRpc,
  hexQuantity,
} from "../src/trusted-lista.mjs";

export async function proposeListaApproval({ fetchImpl } = {}) {
  const call = (method, params) => fetchTrustedRpc(method, params, { fetchImpl });
  const chainId = hexQuantity(await call("eth_chainId", []));
  if (chainId !== TRUSTED_LISTA.chainId) throw new Error("wrong chain");
  const blockHex = await call("eth_blockNumber", []);
  const blockNumber = hexQuantity(blockHex);
  const block = await call("eth_getBlockByNumber", [blockHex, false]);
  const blockHash = block && typeof block.hash === "string" ? block.hash.toLowerCase() : "";
  const blockTag = canonicalBlockTag(blockHash);
  const implementation = addressFromStorage(await call("eth_getStorageAt", [
    TRUSTED_LISTA.proxy,
    TRUSTED_LISTA.implementationSlot,
    blockTag,
  ]));
  const proxyCode = await call("eth_getCode", [TRUSTED_LISTA.proxy, blockTag]);
  const implementationCode = await call("eth_getCode", [implementation, blockTag]);
  return {
    candidateOnly: true,
    registryVersion: TRUSTED_LISTA.approvalRegistryVersion,
    chainId,
    proxy: TRUSTED_LISTA.proxy,
    implementation,
    proxyCodeSha256: await codeSha256(proxyCode),
    implementationCodeSha256: await codeSha256(implementationCode),
    blockNumber,
    blockHash,
    id: "",
    provenance: "",
    validUntil: null,
    validUntilUnix: null,
    permanent: false,
    retired: false,
  };
}

function invokedDirectly() {
  const entry = process.argv[1];
  if (!entry) return false;
  return import.meta.url === pathToFileURL(entry).href;
}

if (invokedDirectly()) {
  proposeListaApproval().then((candidate) => {
    process.stdout.write(`${JSON.stringify(candidate, null, 2)}\n`);
  }).catch((error) => {
    process.stderr.write(`propose-lista-approval: ${error instanceof Error ? error.message : "failed"}\n`);
    process.exitCode = 1;
  });
}
