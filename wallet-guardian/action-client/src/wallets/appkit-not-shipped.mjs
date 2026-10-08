/**
 * WALLET-CONNECT-01 W-B1: build-time stand-in for "@reown/appkit/core".
 * @walletconnect/ethereum-provider only imports AppKit when showQrModal=true. This client always
 * uses showQrModal=false and draws its own QR, so the AppKit modal (and its wallet directory,
 * analytics, swap/on-ramp/social endpoints) is not part of the bundle. Reaching this is a bug.
 */
export function createAppKit() {
  throw new Error("AppKit modal is not shipped in Wallet Guardian (showQrModal=false).");
}
