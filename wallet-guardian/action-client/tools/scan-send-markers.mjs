/**
 * Scan built JavaScript for send-construction markers.
 * The WalletConnect SDK lazy chunk is exempt; application chunks are not.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";

export const SEND_MARKER = /eth_sendTransaction|eth_sendRawTransaction|eth_signTransaction|wallet_sendCalls|testOnlyAllowWalletRequest|noBroadcast/g;

export function isWalletConnectSdkChunk(rel) {
  return /(^|\/)walletconnect-sdk[^/]*\.js$/.test(rel);
}

export function scanText(text) {
  return text.match(SEND_MARKER) ?? [];
}

export function scanSendMarkers(dir) {
  const hits = [];
  const walk = (current) => {
    for (const name of readdirSync(current)) {
      const absolute = join(current, name);
      if (statSync(absolute).isDirectory()) {
        walk(absolute);
        continue;
      }
      if (!name.endsWith(".js")) continue;
      const rel = relative(dir, absolute).split("\\").join("/");
      if (isWalletConnectSdkChunk(rel)) continue;
      const found = scanText(readFileSync(absolute, "utf8"));
      if (found.length > 0) hits.push({ file: rel, count: found.length, markers: [...new Set(found)] });
    }
  };
  walk(dir);
  return hits;
}

function main() {
  const dir = process.argv[2];
  if (!dir) {
    console.error("usage: node tools/scan-send-markers.mjs <dist-dir>");
    process.exit(2);
  }
  const hits = scanSendMarkers(dir);
  if (hits.length === 0) {
    process.stdout.write("send markers: 0\n");
    process.exit(0);
  }
  for (const hit of hits) console.error(`${hit.file}: ${hit.markers.join(",")} (${hit.count})`);
  process.exit(1);
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) main();
