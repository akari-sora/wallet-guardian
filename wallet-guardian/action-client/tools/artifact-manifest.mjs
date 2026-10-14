/**
 * Hash a built action-client dist. GET-free and offline.
 * Prints { files, headers_sha256 } as JSON. Does not deploy.
 */
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";

export function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

export function manifestFromDirectory(dir) {
  const files = {};
  const walk = (current) => {
    for (const name of readdirSync(current)) {
      const absolute = join(current, name);
      const info = statSync(absolute);
      if (info.isDirectory()) {
        walk(absolute);
        continue;
      }
      const rel = relative(dir, absolute).split("\\").join("/");
      files[rel] = sha256(readFileSync(absolute));
    }
  };
  walk(dir);
  if (!Object.prototype.hasOwnProperty.call(files, "_headers")) {
    throw new Error("dist is missing _headers");
  }
  return { files, headers_sha256: files._headers };
}

function main() {
  const dir = process.argv[2];
  if (!dir) {
    console.error("usage: node tools/artifact-manifest.mjs <dist-dir>");
    process.exit(2);
  }
  try {
    process.stdout.write(`${JSON.stringify(manifestFromDirectory(dir), null, 2)}\n`);
  } catch (error) {
    console.error(error instanceof Error ? error.message : "manifest failed");
    process.exit(1);
  }
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) main();
