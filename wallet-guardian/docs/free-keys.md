# Free API keys

Free read-only API keys improve history coverage and reduce shared rate-limit failures. Each user should create their own key for personal use and check current terms before relying on a provider. Store only free, read-only keys that you can revoke. Provider coverage and free-plan recommendations below were checked 2026-10-18.

## Recommended order

1. **[Etherscan](https://etherscan.io/apis)** for Ethereum, Arbitrum, Polygon, and the other free-plan chains listed in [api-keys.md](api-keys.md#etherscan). Wallet Guardian reads `ETHERSCAN_API_KEY`. When `ETHERSCAN_API_BASE` is unset or blank, the client uses `https://api.etherscan.io` and calls `/v2/api`. Set `ETHERSCAN_API_BASE` only to override that origin. A missing `ETHERSCAN_API_KEY` is `credential_missing` and does not call Etherscan.
2. **[Alchemy](https://www.alchemy.com)** for Base transfer history. `guardian init` adds `alchemy-base` when Base is enabled. Only `ALCHEMY_API_KEY` is needed; `ALCHEMY_TRANSFERS_URL` is an optional override of the derived Base endpoint. A missing key is a `credential_missing` gap, not a failed run. This adapter is Base-only. BSC and Avalanche Alchemy transfers are not used.
3. **Per-chain Blockscout explorer keys** for the host an `init` profile names. Create each key in the account settings on that chain's own Blockscout explorer (for example `https://base.blockscout.com` for `BLOCKSCOUT_BASE_URL`, `https://explorer.optimism.io` for `BLOCKSCOUT_OPTIMISM_URL`, and `https://eth.blockscout.com` for `BLOCKSCOUT_ETHEREUM_MAINNET_URL`), not on the multichain PRO portal. Init does not read `BLOCKSCOUT_API_KEY`. It reads `BLOCKSCOUT_BASE_API_KEY` (`BLOCKSCOUT_BASE_URL`) on Base, `BLOCKSCOUT_OPTIMISM_API_KEY` (`BLOCKSCOUT_OPTIMISM_URL`) on Optimism, and `BLOCKSCOUT_<NETWORK>_API_KEY` (`BLOCKSCOUT_<NETWORK>_URL`) on every other EVM network it adds, where `<NETWORK>` is the network id in uppercase with hyphens as underscores (`BLOCKSCOUT_ETHEREUM_MAINNET_API_KEY` / `BLOCKSCOUT_ETHEREUM_MAINNET_URL`, `BLOCKSCOUT_ARBITRUM_ONE_API_KEY` / `BLOCKSCOUT_ARBITRUM_ONE_URL`, `BLOCKSCOUT_POLYGON_POS_API_KEY` / `BLOCKSCOUT_POLYGON_POS_URL`, `BLOCKSCOUT_AVALANCHE_C_API_KEY` / `BLOCKSCOUT_AVALANCHE_C_URL`). Separate from those per-chain keys, <https://dev.blockscout.com> issues `BLOCKSCOUT_API_KEY` for the multichain Blockscout PRO service (`blockscout-pro` with `BLOCKSCOUT_PRO_API_BASE` on `api.blockscout.com`), which is used only by `examples/monitor.json`. Saving `BLOCKSCOUT_API_KEY` does not fill Optimism history on an init profile. Gnosis is not a configured network. Featured-chain and unsupported-chain refusals remain gaps; check current terms.
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

Wallet Guardian can still read configured public RPC balances and supported logs, keyless Routescan history for Ethereum and Avalanche, public Esplora Bitcoin data, the CoinGecko public tier, and any local OFAC file you supply. `guardian init` includes that Routescan source for Ethereum and Avalanche, and `examples/monitor.json` and `examples/monitor.nokeys.json` include it too. When `ROUTESCAN_API_BASE` is unset, the client uses `https://api.routescan.io`. Set `ROUTESCAN_API_BASE` only to override that origin. `ROUTESCAN_API_KEY` stays optional. When `ETHERSCAN_API_BASE` is unset or blank, Etherscan uses `https://api.etherscan.io`. A missing `ETHERSCAN_API_KEY` stays `credential_missing` and does not call that source. Blockscout PRO (`BLOCKSCOUT_API_KEY` plus `BLOCKSCOUT_PRO_API_BASE`) and the example keyless Blockscout hosts are example-profile sources; init does not add them. Some legacy Blockscout hosts answered keyless on 2026-10-18, but may stop at any time. Rate limits, unsupported methods, blocked hosts, and missing history remain explicit coverage gaps; they are never treated as an empty wallet.
