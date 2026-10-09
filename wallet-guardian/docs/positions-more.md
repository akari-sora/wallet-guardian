# Positions more

Opt-in reads for two Ethereum ERC-4626 vaults and the Ethereum USDC Compound III Comet. Nothing here is enabled by `examples/monitor.json`. Nothing here signs, approves, supplies, withdraws, or broadcasts.

sUSDe is not included. Unstaking burns shares and moves the claim into a cooldown silo, so a shares-only adapter can report a live claim as gone. Morpho Blue markets are not included. A market id still needs its own accrual, share conversion, rounding, and oracle accounting.

## How to opt in

Add plugin ids to the monitor profile. Leave them out and these contracts are not called.

| Plugin id | What it reads |
| --- | --- |
| `erc4626-pins` | sDAI and Steakhouse USDC on `ethereum-mainnet` |
| `compound-v3` | cUSDCv3 Comet proxy on `ethereum-mainnet` |

`erc4626` and `erc4626-registered` stay the caller-supplied vault path. That path still has an empty public registry. A missing vault list is `vaults_not_configured`, not a zero balance.

## Pins

These addresses are documented read candidates. A matching `symbol`, `name`, `asset()`, or `MORPHO()` is an identity check. It is not a safety certificate.

| Candidate | Address | Chain | Expected underlying | Expected decimals | Implementation identity |
| --- | --- | --- | --- | --- | --- |
| sDAI | `0x83F20F44975D03b1b09e64809B757c47f942BEeA` | ethereum-mainnet | DAI `0x6B175474E89094C44Da98b954EedeAC495271d0F` | shares 18, DAI 18 | SavingsDai at the vault address. [Spark sDAI docs](https://docs.spark.fi/dev/savings/sdai-token) |
| Steakhouse USDC | `0xBEEF01735c132Ada46AA9aA4c54623cAA92A64CB` | ethereum-mainnet | USDC `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | shares 18, USDC 6 | MetaMorpho at the vault address. `MORPHO()` is `0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb`. [MetaMorpho source](https://github.com/morpho-org/metamorpho) |
| cUSDCv3 | `0xc3d688B66703497DAA19211EEdff47f25384cdc3` | ethereum-mainnet | USDC, same address | base 6 | Proxy only. Launch implementation `0x42F9505a376761b180e27a01bA0554244ED1DE7D` is provenance and is not called. [Compound III docs](https://docs.compound.finance/) |

The plugin reads `decimals()` from the chain. If the chain value disagrees with the expected decimals above, the observation is `pin_mismatch`, not a corrected balance. A manifest address that disagrees with these pins is `contract_mismatch`.

## Same-block reads

sDAI, Steakhouse USDC, and Comet each take one block number from `eth_getBlockByNumber` and use that number for every `eth_call` in the observation. That numeric-height binding is not a canonical block-hash proof. The plugins do not read or pin `block.hash`, and a reorg can move the same height onto a different block.

## ERC-4626 accounting

Each opted-in vault is read at one block number. That binding is the numeric height above, not a block hash.

- The share balance is one receipt.
- `convertToAssets` is the underlying quote of those shares. It rounds down. The quote leg records that rounding and points `duplicates` at the vault, so the portfolio counts the receipt once.
- `maxWithdraw` is the withdrawal limit. It is a label on the quote, not a second balance. The quote is not a promise that the whole amount can be withdrawn now, and redemption is not marked guaranteed.
- Steakhouse exposure is the vault position. The plugin does not discover Morpho Blue markets and does not invent a look-through health score.

A zero share balance with a non-zero quote is `quote_mismatch`. A failed read is `unknown` and keeps the last stored position. It is not `none_observed` and it is not zero.

## Comet accounting

All Comet calls in one observation use the same block number from `eth_getBlockByNumber`. That numeric-height binding is not a canonical block-hash proof.

`userBasic.principal` is the stored principal and stays on the leg as `principal`. The countable base amount is the present value of that principal at the stored index from the same block: absolute principal times the applicable index, divided by 10^15, rounded down. Supply uses the base supply index. Debt uses the base borrow index. `totalsBasic` still supplies those indices and `lastAccrualTime`, and those fields stay on the leg. The amount is not labeled accrued to now. When `lastAccrualTime` equals the block timestamp, no time has elapsed, so this conversion is the present value at that block. If the block timestamp is more than 3600 seconds after `lastAccrualTime`, the label is `stale_stored_index` and the amount is still that stored-index present value, not interest brought forward to the block. A last-accrual time in the future of that block is `accrual_inconsistent`. The plugin does not call `accrueAccount`, `balanceOf`, or `borrowBalanceOf`.

`numAssets` and `getAssetInfo` enumerate every collateral slot up to 32. A longer list is `result_exceeds_bound` and is not reported as a shorter complete list. A zero collateral balance is still queried and is omitted from the legs. `assetsIn` is not used as a filter.

Base supply (`principal > 0`) is a receipt of the Comet proxy. Base debt (`principal < 0`) is a separate debt leg, `compound-v3:base-debt`. Collateral is an underlying leg of the collateral token. Collateral does not become debt and does not become a base deposit.

`getPrice` must return a positive value for the base feed and every collateral feed before the plugin reads health. A revert or a non-positive price is `oracle_failed`. The plugin then does not call the health views and does not store `is_liquidatable=false`. A positive oracle price does not prove freshness. The plugin does not read an update time, a heartbeat, or a round id, and it does not treat a positive price as a fresh oracle.

When prices are readable, the observation records `is_borrow_collateralized` and `is_liquidatable` by those names. There is no health factor. The two predicates answer different questions.

A failed Comet read is `unknown` and keeps the last stored legs and metrics.
