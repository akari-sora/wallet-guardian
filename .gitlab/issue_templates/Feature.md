## What you want

Describe the read you want the monitor to show. Name the chain and the capability. The named preview chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path.

## Why this is not already a gap

Look at `wallet-guardian/docs/coverage-matrix.md`. If the cell is `partial`, `blocked`, `credential_missing`, `expansion_pending`, or `not_verified`, say what new evidence would change that cell. Do not ask the project to report an unknown read as safe, complete, or zero.

## Constraints

The monitor stays read-only. It does not sign, build a transaction, or broadcast. It does not take a seed phrase, a private key, or a keystore.

Do not paste API keys, seed phrases, private keys, keystores, keyed RPC URLs, notification recipients, or real watched addresses. Use a synthetic address such as the one in `wallet-guardian/examples/answers.example.json` if an example is required.

## Version

`environment.guardian_version` from `guardian doctor --json`, or the wheel filename. This preview's `guardian --help` does not list `guardian --version`.
