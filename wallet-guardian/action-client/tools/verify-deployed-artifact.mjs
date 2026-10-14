/**
 * GET-only comparison of a deployed origin to one release record.
 * Does not send transactions and does not call wallet or deploy APIs.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { sha256 } from "./artifact-manifest.mjs";
import { findRelease, loadReleaseDocument, validateRelease, PATHS } from "./release-record.mjs";

const TASK_REF = `/t/ref_${"0".repeat(32)}`;

function cspFromHeadersFile(text) {
  const line = text.split("\n").map((item) => item.trim()).find((item) => item.toLowerCase().startsWith("content-security-policy:"));
  if (!line) return null;
  return line.slice(line.indexOf(":") + 1).trim().replace(/\s+/g, " ");
}

function normalizeCsp(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

function referencedAssets(indexHtml, scripts) {
  const found = new Set();
  const add = (raw) => {
    if (typeof raw !== "string" || raw.length === 0) return;
    if (/^[a-z]+:/i.test(raw) || raw.startsWith("//")) return;
    const path = raw.split("?")[0].split("#")[0];
    if (path.startsWith("/")) found.add(path.slice(1));
  };
  for (const match of indexHtml.matchAll(/\b(?:src|href)\s*=\s*["']([^"']+)["']/gi)) add(match[1]);
  for (const [from, source] of scripts) {
    const base = from.includes("/") ? from.slice(0, from.lastIndexOf("/") + 1) : "";
    for (const match of source.matchAll(/import\s*\(\s*["'](\.[^"']+)["']\s*\)/g)) {
      found.add(resolveRelative(base, match[1]));
    }
    for (const match of source.matchAll(/\bfrom\s+["'](\.[^"']+)["']/g)) {
      found.add(resolveRelative(base, match[1]));
    }
  }
  return [...found];
}

function resolveRelative(base, specifier) {
  const parts = `${base}${specifier}`.split("/");
  const out = [];
  for (const part of parts) {
    if (part === "" || part === ".") continue;
    if (part === "..") out.pop();
    else out.push(part);
  }
  return out.join("/");
}

async function getBytes(fetchImpl, origin, path) {
  const url = `${origin.replace(/\/$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
  const response = await fetchImpl(url, { method: "GET", redirect: "manual" });
  const bytes = Buffer.from(await response.arrayBuffer());
  return { response, bytes, text: bytes.toString("utf8"), url };
}

function isClientHtml(response, text) {
  const type = response.headers.get("content-type") || "";
  return type.includes("text/html") || /<html[\s>]/i.test(text);
}

/**
 * @param {{ origin: string, release: object, fetchImpl?: typeof fetch, distDir?: string | null, expectGone?: boolean }} options
 * @returns {Promise<{ ok: boolean, errors: string[] }>}
 */
export async function verifyDeployedArtifact(options) {
  const errors = [];
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const release = options.release;
  const structural = validateRelease(release);
  if (structural.length) return { ok: false, errors: structural };
  const origin = options.origin;
  if (typeof origin !== "string" || origin.length === 0) return { ok: false, errors: ["origin"] };

  let home;
  try {
    home = await getBytes(fetchImpl, origin, "/");
  } catch (error) {
    if (options.expectGone) return { ok: true, errors: [] };
    return { ok: false, errors: [`GET / failed: ${error instanceof Error ? error.message : "fetch"}`] };
  }

  if (options.expectGone) {
    if (home.response.status === 200 && isClientHtml(home.response, home.text)) {
      return { ok: false, errors: ["origin still returns client HTML"] };
    }
    return { ok: true, errors: [] };
  }

  const files = release.files;
  const seen = new Map();
  async function load(path) {
    if (seen.has(path)) return seen.get(path);
    const got = await getBytes(fetchImpl, origin, `/${path}`);
    seen.set(path, got);
    return got;
  }

  for (const [path, expected] of Object.entries(files)) {
    let got;
    try {
      got = await load(path);
    } catch (error) {
      errors.push(`${path}: ${error instanceof Error ? error.message : "fetch failed"}`);
      continue;
    }
    if (got.response.status !== 200) {
      errors.push(`${path}: HTTP ${got.response.status}`);
      continue;
    }
    const actual = sha256(got.bytes);
    if (actual !== expected) errors.push(`${path}: sha256 mismatch`);
  }

  const index = seen.get("index.html");
  const scripts = [];
  if (!index || index.response.status !== 200) {
    errors.push("index.html: missing from the fetched release files");
  } else {
    const htmlAssets = referencedAssets(index.text, []);
    for (const path of htmlAssets) {
      if (!Object.prototype.hasOwnProperty.call(files, path)) errors.push(`${path}: referenced by index and missing from the release`);
      if (!path.endsWith(".js")) continue;
      const body = seen.get(path);
      if (body && body.response.status === 200) scripts.push([path, body.text]);
    }
    for (const path of referencedAssets("", scripts)) {
      if (!Object.prototype.hasOwnProperty.call(files, path)) {
        errors.push(`${path}: lazy chunk missing from the release`);
      }
      const got = seen.get(path);
      if (!got || got.response.status !== 200) errors.push(`${path}: lazy chunk was not fetched`);
    }
  }

  let headersText = null;
  const fetchedHeaders = seen.get("_headers");
  if (fetchedHeaders && fetchedHeaders.response.status === 200 && sha256(fetchedHeaders.bytes) === release.headers_sha256) {
    headersText = fetchedHeaders.text;
  } else if (options.distDir) {
    try {
      const disk = readFileSync(join(options.distDir, "_headers"));
      if (sha256(disk) === release.headers_sha256) headersText = disk.toString("utf8");
      else errors.push("_headers: dist hash does not match headers_sha256");
    } catch {
      errors.push("_headers: unreadable");
    }
  } else {
    errors.push("_headers: expected CSP source was not fetched");
  }
  if (headersText != null && home.response.status === 200) {
    const expectedCsp = cspFromHeadersFile(headersText);
    const actualCsp = normalizeCsp(home.response.headers.get("content-security-policy"));
    if (!expectedCsp) errors.push("_headers: missing Content-Security-Policy");
    else if (actualCsp !== expectedCsp) errors.push("Content-Security-Policy does not match _headers");
  }

  let session;
  try {
    session = await getBytes(fetchImpl, origin, "/api/session");
  } catch (error) {
    errors.push(`/api/session: ${error instanceof Error ? error.message : "fetch failed"}`);
    session = null;
  }
  if (session) {
    const type = session.response.headers.get("content-type") || "";
    const cache = session.response.headers.get("cache-control") || "";
    if (session.response.status !== 200) errors.push(`/api/session: HTTP ${session.response.status}`);
    if (!type.includes("application/json")) errors.push("/api/session: not JSON");
    if (!/no-store/i.test(cache)) errors.push("/api/session: missing no-store");
    try {
      JSON.parse(session.text);
    } catch {
      errors.push("/api/session: body is not JSON");
    }
  }

  let rpc;
  try {
    rpc = await getBytes(fetchImpl, origin, "/api/rpc");
  } catch (error) {
    errors.push(`/api/rpc: ${error instanceof Error ? error.message : "fetch failed"}`);
    rpc = null;
  }
  if (rpc && rpc.response.status !== 404) errors.push(`/api/rpc: expected 404, got ${rpc.response.status}`);

  let task;
  try {
    task = await getBytes(fetchImpl, origin, TASK_REF);
  } catch (error) {
    errors.push(`${TASK_REF}: ${error instanceof Error ? error.message : "fetch failed"}`);
    task = null;
  }
  if (task) {
    if (task.response.status !== 200) errors.push(`${TASK_REF}: HTTP ${task.response.status}`);
    if (!isClientHtml(task.response, task.text)) errors.push(`${TASK_REF}: expected HTML`);
  }

  return { ok: errors.length === 0, errors };
}

function option(args, name) {
  const index = args.indexOf(name);
  if (index < 0 || index + 1 >= args.length) return null;
  return args[index + 1];
}

async function main() {
  const args = process.argv.slice(2);
  const origin = option(args, "--origin");
  const releaseId = option(args, "--release");
  const releasesPath = option(args, "--releases") ?? PATHS.RELEASES_PATH;
  const distDir = option(args, "--dist");
  const expectGone = args.includes("--expect-gone");
  if (!origin || !releaseId) {
    console.error("usage: node tools/verify-deployed-artifact.mjs --origin <url> --release <id> [--releases <file>] [--dist <dir>] [--expect-gone]");
    process.exit(2);
  }
  const document = loadReleaseDocument(releasesPath);
  const release = findRelease(document, releaseId);
  if (!release) {
    console.error(`release not found: ${releaseId}`);
    process.exit(1);
  }
  const result = await verifyDeployedArtifact({ origin, release, distDir, expectGone });
  if (!result.ok) {
    for (const error of result.errors) console.error(error);
    process.exit(1);
  }
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : "verify failed");
    process.exit(1);
  });
}
