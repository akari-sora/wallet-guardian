# Free API keys

Free read-only API keys improve history coverage and reduce shared rate-limit failures. Each user should create their own key for personal use and check current terms before relying on a provider. Store only free, read-only keys that you can revoke. Provider coverage and free-plan recommendations below were checked 2026-10-18.

## Recommended order

1. **[Etherscan](https://etherscan.io/apis)** for Ethereum, Arbitrum, Polygon, and the other free-plan chains listed in [api-keys.md](api-keys.md#etherscan). Wallet Guardian reads `ETHERSCAN_API_KEY`.
2. **[Alchemy](https://www.alchemy.com)** for Base, BSC, and Avalanche transfer service. Only `ALCHEMY_API_KEY` is needed for Base; `ALCHEMY_TRANSFERS_URL` is an optional override of the derived Base endpoint. This code's Alchemy adapter is currently Base-only; BSC and Avalanche are not used yet.
3. **[Blockscout PRO](https://dev.blockscout.com)** for supported chains such as Optimism and Gnosis. Wallet Guardian reads `BLOCKSCOUT_API_KEY`. Featured-chain and unsupported-chain refusals remain gaps; check current terms.
4. **[NodeReal](https://nodereal.io)** for BSC transfers. Wallet Guardian reads `NODEREAL_API_KEY` and the private keyed URL in `NODEREAL_BSC_URL`.
5. **[Helius](https://dashboard.helius.dev/signup)** for Solana parsed transactions. Wallet Guardian reads `HELIUS_API_KEY` and `HELIUS_PARSED_EVENTS_URL`, but `run-once` does not use the Helius adapter yet.

Tronscan and TON Center keys are optional provider choices. They are not used by the current code, so there is no Wallet Guardian environment variable for either one. `TRON_PRO_API_KEY` belongs to the current TronGrid integration; it is not a Tronscan key. Do not invent a TON Center variable.

## Store a key in Grok Bot

Use Grok Bot's secure secret card. Create a secret with the exact environment-variable name above and enter the value in the masked field. Never paste a key into chat.

According to the Grok Bot help page and an account test on 2026-10-18:

- the secret value is never shown to the bot;
- the bot receives it as an environment variable in its shell;
- other bots on the same account can read it; and
- shared templates do not carry secrets.

These are platform observations, not Wallet Guardian guarantees. Remove or revoke a key when you no longer need it.

## Without any key

Wallet Guardian can still read configured public RPC balances and supported logs, keyless Routescan history for Ethereum and Avalanche, public Esplora Bitcoin data, the CoinGecko public tier, and any local OFAC file you supply. Some legacy Blockscout hosts answered keyless on 2026-10-18, but may stop at any time. Rate limits, unsupported methods, blocked hosts, and missing history remain explicit coverage gaps; they are never treated as an empty wallet.
