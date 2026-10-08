// Types for the lazily loaded SDK module. walletconnect-sdk.mjs is JavaScript (not type-checked,
// like every .mjs here) and is excluded from tsc so the SDK's own Node-typed .d.ts files are not
// pulled into the browser type check.
export declare class ReadOnlyLoginProvider {
  request(args: { method: string; params?: unknown[] }, expiry?: number): Promise<unknown>;
}
export declare function providerOptions(options: {
  projectId: string;
  origin: string;
  rpcUrl: string;
  relayUrl?: string;
  storage?: unknown;
  customStoragePrefix?: string;
}): Record<string, unknown>;
export declare function initProvider(options: {
  projectId: string;
  origin: string;
  rpcUrl: string;
  relayUrl?: string;
  storage?: unknown;
  customStoragePrefix?: string;
}): Promise<any>;
export declare function drawQr(canvas: unknown, uri: string): boolean;
export declare function clearQr(canvas: unknown): void;
