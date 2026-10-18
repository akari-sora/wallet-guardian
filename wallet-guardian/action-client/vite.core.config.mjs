/**
 * Client core build (S-1b decision 1). A separate output from the page build:
 * dist/core/wg-core.js plus dist/core/wg-core.digest.json (sha256 of the bundle bytes).
 * `npm run build` runs this after the page build, then the marker scans.
 */
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

export const CORE_ENTRY = "src/execution/core/index.mjs";
export const CORE_FILE = "wg-core.js";
export const CORE_DIGEST_FILE = "wg-core.digest.json";

function coreDigest() {
  return {
    name: "wallet-guardian-core-digest",
    apply: "build",
    generateBundle(_options, bundle) {
      const chunk = bundle[CORE_FILE];
      if (!chunk || chunk.type !== "chunk") {
        this.error(`core build did not produce ${CORE_FILE}`);
        return;
      }
      const others = Object.keys(bundle).filter((name) => name !== CORE_FILE);
      if (others.length > 0) this.error(`core build must be one file, got extra: ${others.join(", ")}`);
      const bytes = Buffer.from(chunk.code, "utf8");
      const digest = {
        schema: "guardian.core_bundle_digest.v1",
        file: CORE_FILE,
        entry: CORE_ENTRY,
        bytes: bytes.length,
        sha256: createHash("sha256").update(bytes).digest("hex"),
      };
      this.emitFile({ type: "asset", fileName: CORE_DIGEST_FILE, source: `${JSON.stringify(digest, null, 2)}\n` });
    },
  };
}

export default defineConfig({
  publicDir: false,
  logLevel: "warn",
  build: {
    outDir: "dist/core",
    emptyOutDir: true,
    minify: false,
    sourcemap: false,
    copyPublicDir: false,
    lib: {
      entry: fileURLToPath(new URL(`./${CORE_ENTRY}`, import.meta.url)),
      formats: ["es"],
      fileName: () => CORE_FILE,
    },
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
  plugins: [coreDigest()],
});
