import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv, type Plugin } from "vite";
import { pagesHeaders } from "./src/csp.mjs";
import { walletConnectConfig } from "./src/wallets/walletconnect-config.mjs";

// Default builds stay send_off. build:send-variant.mjs sets both variables only after
// WG_SEND_VARIANT, WG_RELEASE_AUTH_ID, and an approved_by_user mode_a inventory entry
// all pass. This package does not add that entry, and npm run build does not set them.
const sendVariantBuild = process.env.WG_SEND_VARIANT === "mode_a" && process.env.WG_SEND_VARIANT_BUILD === "mode_a";
const SEND_VARIANT = sendVariantBuild ? "mode_a" : "off";

// WALLET-CONNECT-01 W-B5: dist/_headers (CSP) follows the actual build. WalletConnect endpoints are
// only allowed when this build offers WalletConnect (both public settings present).
function pagesHeadersPlugin(walletConnect: boolean): Plugin {
  return {
    name: "wallet-guardian-pages-headers",
    apply: "build",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "_headers", source: pagesHeaders({ walletConnect }) });
    },
  };
}

function forbidSendModules(variant: string): Plugin {
  return {
    name: "wallet-guardian-forbid-send-modules",
    apply: "build",
    generateBundle() {
      if (variant !== "off") return;
      const found: string[] = [];
      for (const id of this.getModuleIds()) {
        const normalized = id.split("\\").join("/");
        if (normalized.includes("/src/send/")) found.push(normalized);
      }
      if (found.length > 0) {
        this.error(`send variant off cannot include src/send modules: ${found.join(", ")}`);
      }
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), "VITE_"), ...process.env };
  const wc = walletConnectConfig({ enabled: env.VITE_WALLETCONNECT_ENABLED, projectId: env.VITE_REOWN_PROJECT_ID });
  return {
    server: {
      host: "127.0.0.1",
      port: 43127,
      strictPort: true,
    },
    resolve: {
      alias: {
        // Only reachable with showQrModal=true, which this client never sets.
        "@reown/appkit/core": fileURLToPath(new URL("./src/wallets/appkit-not-shipped.mjs", import.meta.url)),
      },
    },
    define: {
      __WG_SEND_VARIANT__: JSON.stringify(SEND_VARIANT),
    },
    plugins: [pagesHeadersPlugin(wc.enabled), forbidSendModules(SEND_VARIANT)],
    ...(sendVariantBuild
      ? {
          build: {
            rollupOptions: {
              input: {
                main: fileURLToPath(new URL("./index.html", import.meta.url)),
                sendVariant: fileURLToPath(new URL("./src/send/send-variant-entry.mjs", import.meta.url)),
              },
            },
          },
        }
      : {}),
  };
});
