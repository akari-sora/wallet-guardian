# Wallet Guardian

- **What it is:** `guardian`, a read-only wallet monitor CLI for addresses you type, on multiple EVM chains, with partial Solana and address-set-only Bitcoin paths (exact list: [Status and limits](#status-and-limits-read-this-first) and [docs/coverage-matrix.md](docs/coverage-matrix.md)). It does not hold private keys, does not sign on your behalf, and does not broadcast.
- **Who it is for:** wallet holders who want a self-hosted monitor on a machine they control, and AI agents such as Grok Bot that run the same command for them.
- **How to start:** [Quick start](#quick-start): download from GitLab Releases, check `SHA256SUMS`, install the wheel, run one pass with `--no-send`.

> Canonical source and issues: https://gitlab.com/aa22396584/wallet-guardian. Releases and downloads: https://gitlab.com/aa22396584/wallet-guardian/-/releases
>
> 正本和 issue 在 https://gitlab.com/aa22396584/wallet-guardian。Releases 和下載：https://gitlab.com/aa22396584/wallet-guardian/-/releases

Wallet Guardian is a read-only monitor for addresses you type, on a machine you control. It stores coverage and alerts locally. It is not a hosted service: a tenant id is a label for one operator. You can run the `guardian` command yourself, or let Grok Bot operate that same command. It does not sign, build a transaction, or broadcast, and it does not ask for a seed phrase or a private key. Public config stores environment-variable names only (`endpoint_env`, `credential_env`). Secret values can sit in the private secret profile or in the environment. A nonempty profile value is used first. This is a preview, and it is not investment advice.

It does not sign, does not build transactions, and does not broadcast. See [docs/threat-model.md](docs/threat-model.md#monitor-does-not-sign). It never asks for a seed phrase or a wallet private key. API credentials are read from your local environment or private secret profile and transmitted only to the configured provider when authentication requires them. Public configuration contains references (`endpoint_env`, `credential_env`), not credential values. What leaves the machine is described before anything is written: wallet addresses go to the RPC or indexer hosts you enable; your hostname is not added to those requests. GoPlus and Chainabuse are not called in this preview, even with a key, an endpoint, and local consent (see Status and limits). The Telegram sender exists and is opt-in. Delivery defaults to off (`outbox_only`). `--no-send` forbids sends. See [docs/security-model.md](docs/security-model.md) and [docs/threat-model.md](docs/threat-model.md).

## Quick start

Preview `0.4.0rc6` (tag `v0.4.0-rc.6`), not a production release. Read [Status and limits](#status-and-limits-read-this-first) before you rely on it. There is no PyPI release. Do not install `guardian` or `wallet-guardian-monitor` by name from any package index; a package there with one of those names is not this project.

**1. Download.** Open this tag's GitLab Releases page, https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.6 (all releases: https://gitlab.com/aa22396584/wallet-guardian/-/releases). Put `SHA256SUMS` and every file it lists in one directory. For rc.6, `SHA256SUMS` lists `wallet_guardian_monitor-0.4.0rc6-py3-none-any.whl`, `wallet_guardian_monitor-0.4.0rc6.tar.gz` (sdist), and `constraints.txt`. The release also carries `release-manifest.json` and `release-evidence.json`. If the Releases page lists a newer version, use the newest one and substitute its file names below.

**2. Verify.** Run one of these lines in that directory: the first on Linux, the second on macOS. A missing file or a mismatch fails. Do not install a file that fails. On Windows without `sha256sum`, use the Python check in [docs/install.md](docs/install.md).

```bash
sha256sum -c SHA256SUMS
shasum -a 256 -c SHA256SUMS
```

**3. Create a minimal `answers.json`.** The address is synthetic; replace it with an address you want to watch:

```bash
cat > answers.json <<'JSON'
{
  "tenant_id": "local",
  "key_mode": "none",
  "plugins": [],
  "accounts": [
    {
      "label": "demo-wallet",
      "family": "evm",
      "address": "0x0000000000000000000000000000000000000001",
      "networks": ["ethereum-mainnet"],
      "relationship": "watch_only"
    }
  ]
}
JSON
```

**4. Install and run one pass.** The run does not send notifications:

```bash
uv tool install --constraints constraints.txt ./wallet_guardian_monitor-0.4.0rc6-py3-none-any.whl
export RPC_ETHEREUM_MAINNET_URL=https://ethereum-rpc.publicnode.com   # any public JSON-RPC URL you choose; one variable per chain you enable
guardian init --from answers.json --yes
guardian run-once --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json" --no-send
guardian coverage --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json"
```

That install command uses the rc.6 wheel. The rc.4 wheel lacks `guardian outbox pending`, `guardian outbox ack` and `capabilities` (`docs/guide-agents.md`).

The RPC variable is required: with no endpoint set, `run-once` stops with `endpoint_missing` and reads nothing. The URL above is an example of a public endpoint, not an endorsement; public endpoints rate-limit and may refuse some calls, which shows up as a gap in `coverage`. `ethereum-rpc.publicnode.com` answers balance reads but refuses address-less `eth_getLogs` (-32701, recorded in [docs/coverage-matrix.md](docs/coverage-matrix.md)). With only that endpoint, `permissions.standard` (approval scan) and `history.token` stay gaps. For approval monitoring, use an endpoint that accepts topic-only `eth_getLogs` over a 2000-block range; the matrix lists the ones that did on the recorded probe. On a host whose system resolver returns a benchmark or private address, `run-once` refuses with a resolver message; set `GUARDIAN_RESOLVER_MODE=doh` and supply `GUARDIAN_DOH_ENDPOINTS` and `GUARDIAN_DOH_BOOTSTRAP` (see [docs/host-network.md](docs/host-network.md)). DoH enabled is not a qualified path. `guardian doctor` stays offline unless you pass `--live`. A first no-key run is not capped at three minutes. When `round_deadline_seconds` is omitted, the EVM log and history scan and the TRON reader each stop after 180 seconds and can report `partial`. Native balance pre-read treats a missing deadline as no limit. Solana and Bitcoin readers do not take that deadline. The sanctions feed read is not passed it. News fetches use a separate 180-second budget measured from the start of the fetch. A whole `run-once` can take longer. `docs/coverage-matrix.md` records a no-key EVM run that did not finish inside 15 minutes.

`arbitrum-mainnet` is `arbitrum-one`, and `polygon-mainnet` is `polygon-pos`. Typing the registry id (`arbitrum-one`, `polygon-pos`) keeps that id. Other names are rejected with the valid list and suggestions; nothing is guessed. A nonempty secret-profile value wins over the process environment. See [docs/faq-troubleshooting.md](docs/faq-troubleshooting.md) and [docs/api-keys.md](docs/api-keys.md).

The `answers.json` above is all `init` needs. `init` writes under the XDG config directory (or `GUARDIAN_CONFIG_DIR`) and prints `private_dir`. Pass that directory's `monitor.json` if it is not the path above. `run-once --no-send` still reads the endpoints you set. It leaves alerts in the local outbox and does not send them.

## Status and limits (read this first)

- **Preview, not production.** Version `0.4.0rc6` (tag `v0.4.0-rc.6`). This file describes the working tree. A `degraded` exit of 3 is recorded in CHANGELOG `[0.4.0rc5]`. The rc.4 wheel's `run-once --mcm-config` exits 0 on a `degraded` report. This working tree exits 3. `docs/guide-agents.md` says the `v0.4.0-rc.4` and earlier wheels do not include `capabilities`. Release evidence for rc.6 is `NOT_RUN`. No gate result is recorded for the rc.6 release commit. CHANGELOG `[0.4.0rc6]` cites no CI run. The 72-hour / 24-window shadow run has not been done. Not on PyPI.
- **Nothing notifies you by default.** `run-once` is one pass. No scheduler is installed. Alerts are written to a local SQLite outbox. `guardian outbox pending --mcm-config <monitor.json>` lists pending rows without wallet addresses; `guardian outbox ack --id <id>` records a relay receipt and does not send. Those two subcommands are not in the rc.4 wheel. On systemd and cron, the generated `run-once` and `watchdog` lines include `--no-send` and do not send. Those plans also run `--legacy-executable` (default `/usr/local/sbin/guardian-legacy-collector`) with no `--no-send`. That program is the operator's. `--schedule-mode launchd_agent` does not include that program. A `degraded` run exits 3 (`failed` is 1, misconfigured delivery 2). Telegram sending exists but has only been tested against a mock. The live canary is `NOT_RUN`.
- **What can alert, in a profile from `guardian init`:** native ETH movements of 0.5 ETH or more on Ethereum, Arbitrum One, Base and Optimism; the operator-unit floors init writes for other enabled networks that have an amount-alert path (BNB 1, POL 1000, AVAX 10, BTC 0.01); and `Approval(address,address,uint256)` plus `ApprovalForAll(address,address,bool)` logs since the bootstrap origin (init sets `bootstrap_lookback_days` to 30 and does not set `polling.bootstrap_origin`), when the RPC accepts topic-only `eth_getLogs`. Permit2 internal allowances are not tracked or alerted. A Permit2 `Permit` is recorded only as a note with `not_current_allowance`. Approvals whose logs are older than that origin are not inventoried. Set `polling.bootstrap_origin` before the first scan, or point the run at a new state directory that has no cursor and set it before that scan. The value is one block height, used for each EVM chain in the profile. After a `permissions.erc20` cursor is stored, editing the setting does not walk backward and does not reset the cursor: the next approval scan starts at that cursor plus one, minus an overlap of 2. Init does not write a Solana floor. Solana transactions are not parsed and native balance deltas are EVM-only, so a Solana amount does not alert. Token transfers are valued in USD only when a CoinGecko source is enabled (init does not add one), the contract price is fresh, and decimals are known (from Etherscan or one `decimals()` call). A missing quote is not zero. A pinned stablecoin at or above `asset_usd_threshold` units, a `token_raw_thresholds` hit, or a priced token whose decimals cannot be read raises `unvalued_large_move`, which is not a USD value. A movement with no usable floor and no fresh USD quote is one unjudged outbox row, and the baseline stays.
- **Chains differ.** Ethereum, Arbitrum and Polygon get external history with a free Etherscan key. Base and Optimism get balances and nonce only unless you add a Blockscout key (Etherscan's free plan refuses them). BSC external history needs a NodeReal key, and publicnode BSC refuses `eth_getLogs` (JSON-RPC -32701, address required). Solana: balances, token accounts and SPL authorities; transactions are not parsed, so no transfer alerts. Avalanche: limited. Bitcoin: address set only, with the 0.01 BTC floor on transfer candidates. TRON: experimental, off by default, not tested live. A balance or family read uses the first enabled endpoint that is not blocked for a missing credential. An error on that endpoint stays a gap and does not switch to another RPC. An EVM log route may try the next listed source after `filter_requires_address` or `method_disabled`, and after some other log failures. That walk stops on the scan deadline, the bootstrap request cap, or `single_block_unreadable`.
- **Keys that do nothing in this preview:** Helius, GoPlus, Chainabuse, DeBank, Zerion, and `TENDERLY_ACCESS_KEY`. `run-once` does not call those adapters. `examples/monitor.json` still enables `tenderly_base` (`adapter` `evm_rpc`, `TENDERLY_BASE_URL`) on the Base log route. When that URL is set, the watch address is sent to that host. Init does not add `tenderly_base`. Init still writes an enabled `helius-parser` source, and lists `HELIUS_API_KEY` in the secret profile, when a Solana account is included. That variable can be left empty.
- **Not built:** sUSDe, Morpho Blue markets, live yield or APY event alerts, live historical lookups for inflow explanations (the shipped explainer list is empty), and wallet sending (Mode A send stays locked).
- A gap (`credential_missing`, `partial`, `blocked`, and the rest) is never a zero balance or an all-clear. See docs/coverage-matrix.md. Its cells describe `examples/monitor.json`, which routes more sources than `init` does.
- **`guardian relations` (manual, read-only):** lists addresses that paid a watched address and addresses a chosen payer also sent funds to, with heuristic evidence only; a gap stays a gap. See [docs/relations.md](docs/relations.md).

**Status (preview rc.6).** Wallet Guardian is a self-hosted, read-only CLI preview for explicitly listed networks: Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS and Solana (partial), with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. Also included: an experimental, opt-in TRON reader, disabled by default and not live-qualified. It provides bounded reads, not complete TRON monitoring, and must not be used as the sole safety monitor. Coverage varies by chain, capability and data source; the dated cell-by-cell claim is [docs/coverage-matrix.md](docs/coverage-matrix.md). It runs without API keys, with explicitly reduced coverage. A rate-limited, key-less or failed read is shown as a gap, never as "no risk". Notifications are off by default. Opt-in or experimental extras ship disabled: Ethereum-specific registered positions (sDAI, Steakhouse USDC, Compound III USDC; stored-index amounts are not interest extrapolated to now, and vault quotes are not guaranteed redeemable amounts), and an offline experimental yield/promotion parser component that is not a yield-monitoring or promotion-alert service. Targeted injection and privacy tests exist; this is not an "injection-proof" claim, a security certification, or proof that no address leaves the host. See the coverage matrix for each limit. New in rc.4: native-movement detection (net `balance_delta` alerts between eligible checkpoints; polling-bound, no new scheduler; the first observation is a baseline; [docs/balance-deltas.md](docs/balance-deltas.md)), first-run diagnostics (network aliases, offline-by-default `doctor`, credential-source provenance, DoH shown but not treated as a qualified path), and experimental inflow attribution ([docs/inflow-explainer.md](docs/inflow-explainer.md)): automatic, bounded Lista attribution attempts are included, but the shipped manifest has no approved deployment intervals, so `MATCHES_OWN_REQUEST` is not currently available operationally and results may remain partial or unverified. An explanation is a separate revision, not a safety certification, and it does not resend the alert. None of these is live-qualified, and there is no live Telegram canary. Continuous-operation (72-hour shadow) qualification is **not complete**. Release evidence for rc.6 is `NOT_RUN`. No gate result is recorded for the rc.6 release commit. CHANGELOG `[0.4.0rc6]` cites no CI run. There is no PyPI release: install the wheel from the project's GitLab Releases page, `https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.6`.

- Read-only monitor: addresses you choose, local coverage, local outbox. No signing and no broadcast.
- In a profile from `guardian init`, amount alerts include native ETH of 0.5 or more on Ethereum, Arbitrum One, Base, and Optimism, plus the `native_unit_thresholds` floors init writes for other enabled networks that have an amount-alert path (BNB 1, POL 1000, AVAX 10, BTC 0.01). Those floors are operator units, not a USD equivalent. Init does not write a Solana floor: Solana transactions are not parsed and native balance deltas are EVM-only, so a Solana amount does not alert. Token transfers alert by amount only when a CoinGecko source is enabled and returns a fresh contract price, or when the token is a pinned stablecoin or has a `token_raw_thresholds` hit (`unvalued_large_move`, not a USD value). `init` does not add CoinGecko. A missing quote is not zero. A movement with no floor and no fresh USD quote is an unjudged outbox row; the baseline stays so it can be judged later. Approval changes that the scan does find alert without a threshold. The scan is `Approval(address,address,uint256)` and `ApprovalForAll(address,address,bool)` logs since the bootstrap origin (init: 30 days). Permit2 internal allowances are not tracked. Approvals older than that origin are not inventoried. Set `polling.bootstrap_origin` before the first scan, or use a new state directory that has no cursor. A stored `permissions.erc20` cursor is not moved backward by editing the setting.
- No-key and bring-your-own-key use the same files. A missing key is `credential_missing`, with a hint, not a zero balance.
- Coverage is per chain and per source. The claim is [docs/coverage-matrix.md](docs/coverage-matrix.md). A failed read stays a gap.
- `guardian install-service --dry-run` prints a schedule plan and exits non-zero. It does not install a unit.
- Grok Bot can run the same CLI. The shareable template is coming; there is no template link yet.

Guides: [CLI only](docs/guide-cli.md), [command reference](docs/cli-reference.md), [scheduling](docs/scheduling.md), [FAQ](docs/faq-troubleshooting.md), [Grok Bot](docs/guide-grok-bot.md), [AI agents](docs/guide-agents.md), [security and privacy](docs/security-privacy.md). Contributing: [CONTRIBUTING.md](../CONTRIBUTING.md).

You do not need Cloudflare, D1, a wallet login, or Node to run the monitor. The Action Client section below is optional.

The MCM-01 scope contract is [docs/mcm-01.md](docs/mcm-01.md). That document is a plan, not a live result.

## Use from AI agents

The skill at `skills/wallet-guardian/` tells an agent how to call the installed `guardian` command. Read [docs/guide-agents.md](docs/guide-agents.md) before copying it into an agent home. Run `guardian capabilities` and follow the class it prints. `docs/guide-agents.md` says the `v0.4.0-rc.4` and earlier wheels return `invalid choice` for that command. An agent must not run `guardian relations`. Installing the skill into Claude Code, Codex, Cursor, Grok Build, Hermes, or Grok Bot was not exercised (未實測).

## Quickstart details

Same steps as [Quick start](#quick-start), with the default private-directory config path (`init` prints `private_dir`). Install as in [docs/install.md](docs/install.md), set an RPC endpoint variable for each enabled chain, then:

```bash
guardian init --from answers.json --yes
guardian config validate --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json"
guardian run-once --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json" --no-send
guardian coverage --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json"
guardian install-service --dry-run
```

`answers.json` lists the addresses, chains, and plugins you choose; use the example above (synthetic address, `key_mode` none or byo, each account needs `label`, `family`, `address`, `networks`). Use a synthetic address in examples, never a real watch address in this repo. `init` does not scan for other addresses, does not enable third-party user-address lookups, and does not start a sender. It prints which source would receive which data. Nothing is written until you pass `--yes` (or type `yes` at the prompt). A second run refuses to overwrite. `--force` backs up the config files first and does not touch the state database.

`run-once --no-send` reads with your endpoints and writes the local outbox only. `coverage` shows each capability. A missing key is `credential_missing` with a hint, not a zero balance and not a complete history. `install-service --dry-run` prints planned unit names and exits non-zero. It does not install anything. You enable a schedule later, yourself.

No-key and bring-your-own-key are the same files: credential values are never written. No-key means you leave the credential variables unset, so keyed indexers stay `credential_missing` while public RPC still runs. Bring-your-own-key means you export your own values under the names in the secret profile. The difference is described in [docs/api-keys.md](docs/api-keys.md).

## Legacy commands

The Lista / BNB Chain path is unchanged. Use `--config` for that file and `--mcm-config` for a profile from `init`. These commands still behave as before:

```bash
guardian doctor --json
guardian run-once --config private/config.json --json
guardian self-check --json
guardian snapshot --json
```

## Backup, upgrade, uninstall

Back up the state directory before an upgrade, install the new wheel with that release's constraints, and uninstall without deleting the XDG config and state directories unless you mean to. The steps are in [docs/install.md](docs/install.md).

## 快速開始

預覽版 `0.4.0rc6`（標籤 `v0.4.0-rc.6`），不是正式版。沒有上 PyPI；不要從任何套件索引用名稱安裝 `guardian` 或 `wallet-guardian-monitor`。

**1. 下載。** 打開 https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.6 （全部版本：https://gitlab.com/aa22396584/wallet-guardian/-/releases），把 `SHA256SUMS` 和它列出的每一個檔案放進同一個目錄。rc.6 的 `SHA256SUMS` 列出 `wallet_guardian_monitor-0.4.0rc6-py3-none-any.whl`、`wallet_guardian_monitor-0.4.0rc6.tar.gz`（sdist）與 `constraints.txt`。如果 Releases 頁面有比這更新的版本，改用最新那一版，並把下面的檔名換成那一版的檔名。

**2. 核對雜湊。** 第一行用在 Linux，第二行用在 macOS。缺檔或不符都會失敗；失敗就不要安裝。Windows 用 [docs/install.md](docs/install.md) 裡的 Python 指令。

```bash
sha256sum -c SHA256SUMS
shasum -a 256 -c SHA256SUMS
```

**3. 安裝並跑一輪（不送出通知）。** `answers.json` 的範例（合成地址）、RPC 變數與公開端點的限制見上面英文的 Quick start。

```bash
uv tool install --constraints constraints.txt ./wallet_guardian_monitor-0.4.0rc6-py3-none-any.whl
export RPC_ETHEREUM_MAINNET_URL=https://ethereum-rpc.publicnode.com
guardian init --from answers.json --yes
guardian run-once --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json" --no-send
guardian coverage --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json"
```

## 狀態與限制（先讀這段）

- **預覽版，不是正式版。** 版本 `0.4.0rc6`（標籤 `v0.4.0-rc.6`）。這份說明描述的是這棵工作樹。`degraded` 結束碼 3 寫在 CHANGELOG 的 `[0.4.0rc5]`。rc.4 wheel 的 `run-once --mcm-config` 在 `degraded` 時結束碼是 0。這棵工作樹是 3。`docs/guide-agents.md` 寫 `v0.4.0-rc.4` 與更早的 wheel 沒有 `capabilities`。rc.6 的 release evidence 是 `NOT_RUN`。rc.6 發行 commit 沒有記錄任何 gate 結果。CHANGELOG 的 `[0.4.0rc6]` 沒有引用任何 CI。72 小時／24 輪 shadow 沒有做。沒有上 PyPI。
- **預設不會通知你。** `run-once` 只跑一輪。沒有安裝排程。告警只寫進本機 SQLite outbox。`guardian outbox pending --mcm-config <monitor.json>` 列出待處理列（不印錢包地址）；`guardian outbox ack --id <id>` 只記錄轉述回條，不會送出。rc.4 wheel 沒有這兩個子指令。systemd 與 cron 產生的 `run-once` 與 `watchdog` 兩行帶 `--no-send`，這兩行不會送出。這兩種排程還會執行 `--legacy-executable`（預設 `/usr/local/sbin/guardian-legacy-collector`），那一行沒有 `--no-send`，程式是操作者提供的。`launchd_agent` 沒有這支 legacy 程式。`degraded` 的結束碼是 3（`failed` 是 1，delivery 設定錯誤是 2）。Telegram 只有 mock 測試。live canary 是 `NOT_RUN`。
- **`guardian init` 會告警的金額與權限：** Ethereum、Arbitrum One、Base、Optimism 上 ≥0.5 ETH 的原生移動；init 為有金額告警路徑的其他鏈寫入的操作單位門檻（BNB 1、POL 1000、AVAX 10、BTC 0.01）；以及 bootstrap 起點之後日誌裡的 `Approval(address,address,uint256)` 與 `ApprovalForAll(address,address,bool)`（init 把 `bootstrap_lookback_days` 設成 30，不設 `polling.bootstrap_origin`；RPC 要接受 topic-only 的 `eth_getLogs`）。Permit2 內部額度不追蹤、不告警（Permit2 `Permit` 只記成 `not_current_allowance` 的備註）。起點之前的既有授權不會被盤點。要在第一次掃描之前設定 `polling.bootstrap_origin`，或改用一個還沒有游標的新狀態目錄，並在那次掃描前設定。這個值是同一個區塊高度，設定檔裡的各條 EVM 鏈共用。`permissions.erc20` 游標已經存下之後，再改起點不會回補舊授權，也不會重設游標：下一輪從該游標加一、再減掉重疊 2 開始。init 不寫 Solana 門檻。Solana 不解析交易，原生餘額 delta 只做 EVM，所以 Solana 金額不會告警。代幣轉帳只有在啟用 CoinGecko 來源（init 不會加）、合約報價新鮮、且知道 decimals（Etherscan 或一次 `decimals()` 呼叫）時才估成 USD。沒有報價不是 0。釘選穩定幣達 `asset_usd_threshold` 單位、`token_raw_thresholds` 命中、或有報價但 decimals 讀不到時，發 `unvalued_large_move`（不是 USD 估值）。沒有可用門檻、也沒有新鮮 USD 報價時，寫一筆未判定 outbox，baseline 留在原地。
- **各鏈深度不同。** Ethereum、Arbitrum、Polygon 有免費 Etherscan key 就能讀外部歷史。Base、Optimism 在免費 Etherscan 下只查餘額和 nonce，要另加 Blockscout key。BSC 外部歷史要 NodeReal key；publicnode BSC 的 `eth_getLogs` 回 JSON-RPC -32701（address required）。Solana 只有餘額、token 帳戶與 SPL 權限，不解析交易，所以沒有轉帳告警。Avalanche 有限。Bitcoin 只讀地址集合，轉帳候選用 0.01 BTC 門檻。TRON 是實驗性功能，預設關閉，沒有實測。餘額或家族讀取只用第一個已啟用、且沒有因缺憑證被擋下的端點。那個端點出錯就是缺口，不會改連下一個 RPC。EVM 日誌路由在 `filter_requires_address` 或 `method_disabled` 之後，以及其他某些日誌失敗之後，可以試清單裡的下一個來源。掃描期限、bootstrap 請求上限，或 `single_block_unreadable`，會停住這段走訪。
- **本版設了也沒作用的 key：** Helius、GoPlus、Chainabuse、DeBank、Zerion，以及 `TENDERLY_ACCESS_KEY`。`run-once` 不會呼叫這些 adapter。`examples/monitor.json` 仍啟用 `tenderly_base`（`adapter` 是 `evm_rpc`，變數是 `TENDERLY_BASE_URL`），接在 Base 的日誌路由上。那個 URL 有值時，監控地址會送到該 host。`init` 不會加 `tenderly_base`。選了 Solana 帳戶時，init 仍會寫一個啟用的 `helius-parser` 來源，並在 secret profile 列出 `HELIUS_API_KEY`。這個變數可以留空。
- **沒有做：** sUSDe、Morpho Blue 市場、APY／收益活動的即時推播、入帳解釋的即時歷史查詢（內建核准清單是空的）、錢包送出（模式 A 送出仍鎖住）。
- 缺口（`credential_missing`、`partial`、`blocked` 等）不是 0，也不是安全。逐格狀態見 docs/coverage-matrix.md。那份描述的是 `examples/monitor.json`，比 `init` 多接了一些來源。

## 現在做得到

- `guardian doctor --json`：檢查環境、憑證是否已配置（不顯示值）、以及能連上的唯讀來源。
- `guardian run-once --config private/config.json --json`：對設定裡的 watch-only 地址讀取 BNB Chain 官方 RPC。Lista 提款會重讀 `idx`、`isClaimable` 與 BNB 數量。
- 快照、差異、告警去重、外部 watcher 未驗證時的重複／缺口標記。
- 缺 API key 時標 `LIMITED_DISCOVERY` 或 `BLOCKED`，不會把失敗讀成餘額 0。
- 固定版本任務工作台（`action-client@0.3.0`）：載入 typed 任務，核對目標、資產、接收人與費用，並把連接、登入、授權、批次、執行權限、升級與確認分開列出。模式 A 是原地址單步引導。錢包按鈕保持關閉。

## 現在做不到

- Agent、排程與這台共用電腦不能簽名或廣播。
- 主網測試交易未授權（`mainnet_test_transactions_authorized=false`）。
- 沒有 trace／fork／真錢包端到端，所以 Lista 領取的模擬是 `INCOMPLETE`，寫入覆蓋是 `DRAFT` / `BLOCKED`。
- DeBank、Zerion，以及模擬用的 `TENDERLY_ACCESS_KEY`，在這個版本沒有會被呼叫的 adapter。設了這些 key 也不會呼叫它們，也不會解鎖全鏈 discovery 或模擬；doctor 會回「key configured; this provider adapter is not implemented」。`examples/monitor.json` 的 `tenderly_base` 是另一條 Base 日誌 RPC，見上面的限制。GoPlus 也不會被自動呼叫。選了 Base 時，`init` 與 `examples/monitor.json` 都會在 Base 歷史路由加上 `alchemy-base`（只需 `ALCHEMY_API_KEY`，未設 `ALCHEMY_TRANSFERS_URL` 時自動推導 Base 端點）；選了 Ethereum 或 Avalanche 時，`init` 也會加上免 key 的 `routescan` 歷史來源（未設 `ROUTESCAN_API_BASE` 時預設連 `https://api.routescan.io`）。
- 官方 BSC dataseed 停用 `eth_getLogs`，沒有 Etherscan key 就無法回補歷史領取。
- 沒有公開 webhook、沒有付費套餐、沒有持續監控。`monitoring_active` 是 false。
- Cloudflare Pages 已有公開來源，見 `docs/deploy.md`。程式常數 `fixed_https_origin` 仍是 false，所以這個套件不會發行公開任務連結。localhost 不是手機上的 localhost。網址回 200 不是執行授權。
- 模式 B 只在查到錢包的 EIP-5792 能力、而且是同鏈多筆操作時才考慮。這個建置沒有對真錢包送出 `wallet_sendCalls`，證據是未測。
- 模式 C 不支援。沒有已驗證、能在鏈上強制任務／鏈／原帳戶／合約／方法／資產／金額／接收人／期限／次數／費用的權限機制。Prompt、畫面或後端 if 不能代替。
- 新的智慧帳戶不能拿來假裝能操作原地址上既有的 Lista 部位。
- 手機錢包內建瀏覽器未測。模式 A 的真錢包流程也未測。

## 安全邊界

監控程式只做讀取與 typed 任務資料。禁止的 RPC 方法包含 `eth_sendTransaction`、`eth_sendRawTransaction`、`wallet_sendCalls`、`eth_sign`、`personal_sign`、`eth_signTypedData`。

Grok 只產生 `guardian.task.v1`。它不能在運行時產生並執行任意簽名 JavaScript。任務連結只帶 `ref_` 參考碼，本身不是執行授權，也不能帶其他使用者的私人資料。`fixed_https_origin` 仍是 false 時，公開連結不會發行。部署紀錄裡的 Pages 網址還沒寫進這個常數。

主錢包私鑰留在使用者的錢包。限定執行憑證是秘密，不要放進這個共用工作區、任務 JSON 或對話。

私鑰與助記詞不要貼進設定、對話或這個目錄。`private/`、`data/`、`evidence/` 已在 gitignore。公開範例是 `config/public.example.json`，裡面不是真實地址。頁面上的合成任務也不是真實餘額。

Watch-only 地址只代表「拿來觀察」，不代表這個系統可以動用它。

## 安裝

發行 wheel 的安裝、釘選的 constraints、XDG 私人目錄、升級與降版見 [docs/install.md](docs/install.md)。發行套件名稱是 `wallet-guardian-monitor`（wheel `wallet_guardian_monitor-0.4.0rc6-py3-none-any.whl`；匯入名稱與命令仍是 `guardian`）；預覽版沒有上傳到任何套件索引，名稱也沒有被保留。標籤 `v0.4.0-rc.6` 是否已發布，see GitLab releases。

在這個 checkout 裡開發：

```bash
cd wallet-guardian
./scripts/bootstrap.sh
```

需要 Python 3.12 與 [uv](https://docs.astral.sh/uv/)。

把私人設定放在 `private/config.json`（不要提交）。格式見 `config/public.example.json`。已安裝的程式預設把私人設定與狀態放在 XDG 目錄，見 `docs/install.md`。

## 指令

```bash
uv run guardian doctor --json
uv run guardian run-once --config private/config.json --json
uv run guardian self-check --json
uv run guardian operation-coverage --json
uv run guardian workbench --json
uv run guardian alerts test --dry-run
```

`run-once` 的終端機輸出是給操作者看的 JSON；失敗會有非 0 的 exit code。舊的 Lista 路徑把結果寫進狀態目錄（預設 `$XDG_STATE_HOME/guardian`，否則 `~/.local/state/guardian`），不寫進安裝套件。見 `docs/install.md`。

## API key

`config/env.example` 是舊 doctor／Lista 路徑的變數名稱。GoPlus、DeBank、Zerion，以及模擬用的 `TENDERLY_ACCESS_KEY`，在本版不會被呼叫。範例監控檔的 `tenderly_base`（`TENDERLY_BASE_URL`）是 Base 日誌路由會用到的 RPC。`guardian init` 寫出的 `secret-profile.json` 才列出這份監控設定實際要用的變數名稱（例如 `RPC_<CHAIN>_URL`、`NODEREAL_*`、`BLOCKSCOUT_*`、`TRONGRID_API_URL`）。那些值寫進 `secret-profile.json`，或匯出到行程環境。`.env.local` 只載入 `config/env.example` 那組名稱：`ETHERSCAN_API_KEY`、`GOPLUS_API_KEY`、`DEBANK_ACCESS_KEY`、`ZERION_API_KEY`、`ALCHEMY_API_KEY`、`TENDERLY_ACCESS_KEY`、`GUARDIAN_MAINNET_SEND`。其他名稱會被略過。把 `RPC_ETHEREUM_MAINNET_URL` 只寫進 `.env.local` 時，`run-once` 仍是 `endpoint_missing`。已存在的環境變數優先是這段舊路徑的說法。`init` 的 secret profile 裡，非空白的值優先於行程環境（見 `docs/api-keys.md`）。doctor 只報告 `configured` 或 `not_configured`，不會印出值。

Etherscan V2 只用一把 `ETHERSCAN_API_KEY`，涵蓋 chainlist 以及之後的帳戶、交易與日誌。沒有第二把鏈別金鑰。有 key 且 chainlist 回傳資料列時，doctor 才標 `LIVE_PASS`。沒有 key 時是 `LIMITED_DISCOVERY`，不會把未認證的呼叫當成通過。

| 變數 | 用途 | 沒有 key 時 | 本版是否會呼叫 |
|---|---|---|---|
| `ETHERSCAN_API_KEY` | Etherscan V2 chainlist、交易、ABI、日誌 | `LIMITED_DISCOVERY`，chainlist 不是 LIVE_PASS | 會（有 key 且路由指定時） |
| `GOPLUS_API_KEY` | 地址／授權風險補充 | `LIMITED_DISCOVERY`，不會假裝已篩過 | 否 |
| `DEBANK_ACCESS_KEY` | 舊文件寫 DeFi discovery | 沒有 adapter，不是「有 key 就解鎖」 | 否（未實作） |
| `ZERION_API_KEY` | 舊文件寫替代 discovery | 沒有 adapter；本建置也不開放 webhook | 否（未實作） |
| `ALCHEMY_API_KEY` | Base `alchemy_getAssetTransfers` 歷史讀取 | 沒有這把 key 時，Base 的 `alchemy-base` 來源維持 `credential_missing` 且不會連上 | 會（`examples/monitor.json` 與選了 Base 的 `init` 設定檔都會加 `alchemy-base`；未設 `ALCHEMY_TRANSFERS_URL` 時由這把 key 推導 Base 端點） |
| `TENDERLY_ACCESS_KEY` | 舊文件寫可選模擬 | 沒有 adapter，設了 key 也不保證模擬 | 否（未實作） |
| `GUARDIAN_MAINNET_SEND` | 使用者錢包的主網送出開關，預設 `false` | 確認鈕即使可按也不會呼叫錢包 | 監控程式不會簽名或廣播 |

`GUARDIAN_MAINNET_SEND=true` 也不會讓監控程式簽名或廣播。`mainnet_test_transactions_authorized` 在這個建置仍必須是 false。不要購買套餐，除非另外授權。

## 任務工作台

```bash
cd action-client
npm install
npm run dev
```

任務工作台裡用到 `node:sqlite` 的測試需要 Node 22.13 以上。測試腳本沒有帶 `--experimental-sqlite`。CI image 是 `node:22.16.0-bookworm-slim`。這棵樹的 `package.json` 沒有 `engines` 欄位。

開發伺服器是 `http://127.0.0.1:43127`。可開啟任務檔，或載入頁面上的合成範例。合成範例不是你的餘額。私人任務走 `/t/<ref>`，登入後向同一個來源讀 `/api/tasks/<ref>`。`?ref=` 不會再讀公開靜態任務。網址不放地址、金額或金鑰。見 `docs/p1-private-task-api.md`。

頁面會重算 digest。金額、收款地址與計畫不一致就拒絕。連接錢包、登入簽名、代幣授權、批次確認、限定執行權限、帳戶升級與「確認這筆領取」分開列出。Lista 原地址單步領取只有最後一項需要確認。

`在我的錢包確認` 不會因為任務 JSON 自述 `preflight_ok` 而送出。計畫 `wallet_request_allowed=false`、未完成模擬或阻塞原因都會關掉送出。獨立鏈上重讀、效果模擬、允許送出分開顯示；重讀通過也不會打開送出。見 `docs/p2-lista-reread.md`。模式 A 單步領取的引導路徑與不廣播測試見 `docs/mode-a-lista.md`：正式送出仍關閉，交易雜湊不是完成。之後的同意不是把 `VITE_GUARDIAN_MAINNET_SEND` 打開。`VITE_GUARDIAN_MAINNET_SEND` 預設不設定。監控程式永遠不能簽名。

`查詢批次能力` 是另一個按鈕。它只在你按下時讀 `wallet_getCapabilities`。不支援、查到別的鏈、或狀態是 `ready`，都留在模式 A。同鏈而且多於一筆、能力是 `supported`，畫面才標模式 B。真錢包批次未測。這個預覽不呼叫 `wallet_sendCalls`。

模式 C 保持關閉。程式裡沒有權限合約。任務資料自己宣稱已驗證，不算鏈上強制。

介面規則（狀態列、錢包卡片、CSP 限制、`src/typography.mjs`、版面檢查工具）見 `docs/action-client-ui.md`。

Python 的狀態重讀可以讓 `preflight_ok` 為 true，但 `continue_to_wallet` 仍是 false。客戶端的獨立重讀不會寫回這個旗標。交易雜湊不算完成。拒絕、送出不明、替代與完成核對有離線狀態機測試。

固定 HTTPS 來源見 `docs/deploy.md`。套件常數 `fixed_https_origin` 仍是 false，所以不會發行任務連結，這也不是主網執行授權。

剩餘工作：把已驗證 origin 接進連結發行設定；手機錢包內建瀏覽器實測；模式 A 的真錢包拒絕與完成核對；模式 B 的真錢包批次；以及一個真的鏈上權限合約。完成前不能說領取功能已上線。已上線的 Pages 套件是合併前的建置，這次沒有重新部署。

## 測試

```bash
uv run pytest -q
```

- `tests/v1_acceptance_cases.json`：原有 56 項監控案例，離線。
- `tests/v2_acceptance_cases.json`：72 項操作案例，離線。
- `tests/workbench_acceptance_cases.json`：31 項任務工作台案例，離線。不併入前面兩份，也不把未測項目算成通過。
- fork、真錢包、手機內建瀏覽器與部署沒有在這輪執行。模式 A／B 標未測，模式 C 標不支援。
- 真網讀取與離線測試要分開看。HTTP 200 本身不算通過。建置與測試不等於獲准執行主網交易。

## 資料來源

BNB Chain 官方 RPC、Lista 官方文件上的 StakeManager proxy、Hyperliquid 公開 info、Etherscan V2 chainlist。OFAC SDN CSV 只用在舊 doctor／Lista 路徑：下載 `sdn.csv`，用規則擷取 `0x` 字串，並自標 `LIMITED_DISCOVERY`（不是完整的數位貨幣制裁名單）。MCM `run-once` 讀的是 `OFAC_SLS_FEED` 指向的 SDN XML，而且監控檔裡要有 `adapter` 為 `ofac_sls` 的來源。`init` 不會加上那個來源。文件網址與查證等級見 `docs/source-registry.json` 與 doctor 產生的 `source_registry.json`。

## License

授權：AGPL-3.0-only（見 `LICENSE`）。
