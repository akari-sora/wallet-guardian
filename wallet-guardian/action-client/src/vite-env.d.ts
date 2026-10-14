interface ImportMetaEnv {
  readonly PROD: boolean;
  readonly VITE_GUARDIAN_MAINNET_SEND?: string;
  /** WALLET-CONNECT-01 W-B: "true" to offer WalletConnect. Off when missing. */
  readonly VITE_WALLETCONNECT_ENABLED?: string;
  /** Public Reown client identifier (origin-allowlisted). WalletConnect stays off when missing. */
  readonly VITE_REOWN_PROJECT_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
