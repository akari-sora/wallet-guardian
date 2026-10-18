# Free API keys

Free read-only API keys improve history coverage and reduce shared rate-limit failures. Each user should create their own key for personal use and check current terms before relying on a provider. Store only free, read-only keys that you can revoke. Provider coverage and free-plan recommendations below were checked 2026-10-18.

## Recommended order

1. **[Etherscan](https://etherscan.io/apis)** for Ethereum, Arbitrum, Polygon, and the other free-plan chains listed in [api-keys.md](api-keys.md#etherscan). Wallet Guardian reads `ETHERSCAN_API_KEY`. You also set `ETHERSCAN_API_BASE` to the API origin; the client calls `/v2/api`. There is no built-in Etherscan origin, so an unset variable skips that source.
2. **[Alchemy](https://www.alchemy.com)** for Base transfer history. `guardian init` adds `alchemy-base` when Base is enabled. Only `ALCHEMY_API_KEY` is needed; `ALCHEMY_TRANSFERS_URL` is an optional override of the derived Base endpoint. A missing key is a `credential_missing` gap, not a failed run. This adapter is Base-only. BSC and Avalanche Alchemy transfers are not used.
3. **[Blockscout](https://dev.blockscout.com)** for the per-chain host an init profile names. Init does not read `BLOCKSCOUT_API_KEY`. It reads `BLOCKSCOUT_BASE_API_KEY` on Base, `BLOCKSCOUT_OPTIMISM_API_KEY` on Optimism, and `BLOCKSCOUT_<NETWORK>_API_KEY` on every other EVM network it adds, where `<NETWORK>` is the network id in uppercase with hyphens as underscores (Ethereum: `BLOCKSCOUT_ETHEREUM_MAINNET_API_KEY`, Avalanche C-Chain: `BLOCKSCOUT_AVALANCHE_C_API_KEY`). `BLOCKSCOUT_API_KEY` is the Blockscout PRO key for `examples/monitor.json` only (`blockscout-pro` on `api.blockscout.com`). Saving that name does not fill Optimism history on an init profile. Gnosis is not a configured network. Featured-chain and unsupported-chain refusals remain gaps; check current terms.
4. **[NodeReal](https://nodereal.io)** for BSC transfers. Wallet Guardian reads `NODEREAL_API_KEY` and the private keyed URL in `NODEREAL_BSC_URL`.
5. **[Helius](https://dashboard.helius.dev/signup)** for Solana parsed transactions. Wallet Guardian reads `HELIUS_API_KEY` and `HELIUS_PARSED_EVENTS_URL`, but `run-once` does not use the Helius adapter yet.

Tronscan and TON Center keys are optional provider choices. They are not used by the current code, so there is no Wallet Guardian environment variable for either one. `TRON_PRO_API_KEY` belongs to the current TronGrid integration; it is not a Tronscan key. Do not invent a TON Center variable.

## Store a key in Grok Bot

Use Grok Bot's secure secret card. Create a secret with the environment-variable name that the profile you run actually reads (init names and `examples/monitor.json` names differ; see the list above) and enter the value in the masked field. Never paste a key into chat.

According to the Grok Bot help page and an account test on 2026-10-18:

- the secret value is never shown to the bot;
- the bot receives it as an environment variable in its shell;
- other bots on the same account can read it; and
- shared templates do not carry secrets.

These are platform observations, not Wallet Guardian guarantees. Remove or revoke a key when you no longer need it.

## Without any key

Wallet Guardian can still read configured public RPC balances and supported logs, keyless Routescan history for Ethereum and Avalanche, public Esplora Bitcoin data, the CoinGecko public tier, and any local OFAC file you supply. `guardian init` includes that Routescan source for Ethereum and Avalanche, and `examples/monitor.json` and `examples/monitor.nokeys.json` include it too. When `ROUTESCAN_API_BASE` is unset, the client uses `https://api.routescan.io`. Set `ROUTESCAN_API_BASE` only to override that origin. `ROUTESCAN_API_KEY` stays optional. Etherscan still needs `ETHERSCAN_API_BASE` or that source is skipped. Blockscout PRO (`BLOCKSCOUT_API_KEY` plus `BLOCKSCOUT_PRO_API_BASE`) and the example keyless Blockscout hosts are example-profile sources; init does not add them. Some legacy Blockscout hosts answered keyless on 2026-10-18, but may stop at any time. Rate limits, unsupported methods, blocked hosts, and missing history remain explicit coverage gaps; they are never treated as an empty wallet.
