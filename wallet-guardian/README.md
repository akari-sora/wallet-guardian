# Wallet Guardian

Wallet Guardian is a read-only monitor for addresses you type, on a machine you control. It stores coverage and alerts locally. It is not a hosted service: a tenant id is a label for one operator. You can run the `guardian` command yourself, or let Grok Bot operate that same command. It does not sign, build a transaction, or broadcast, and it does not ask for a seed phrase or a private key. API keys stay in your environment. Config files store environment-variable names only (`endpoint_env`, `credential_env`). This is a preview, and it is not investment advice.

It does not sign, does not build transactions, and does not broadcast. See [docs/threat-model.md](docs/threat-model.md#monitor-does-not-sign). It never asks for a seed phrase or a wallet private key. API credentials are read from your local environment or private secret profile and transmitted only to the configured provider when authentication requires them. Public configuration contains references (`endpoint_env`, `credential_env`), not credential values. What leaves the machine is described before anything is written: wallet addresses go to the RPC or indexer hosts you enable; your hostname is not added to those requests. GoPlus and Chainabuse user-address lookups require explicit local consent; configuring a key or endpoint alone is not consent. The Telegram sender exists and is opt-in. Delivery defaults to off (`outbox_only`). `--no-send` forbids sends. See [docs/security-model.md](docs/security-model.md) and [docs/threat-model.md](docs/threat-model.md).

**Status (preview rc.3).** Wallet Guardian is a self-hosted, read-only CLI preview for explicitly listed networks: Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS and Solana (partial), with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. Also included: an experimental, opt-in TRON reader, disabled by default and not live-qualified. It provides bounded reads, not complete TRON monitoring, and must not be used as the sole safety monitor. Coverage varies by chain, capability and data source; the dated cell-by-cell claim is [docs/coverage-matrix.md](docs/coverage-matrix.md). It runs without API keys, with explicitly reduced coverage. A rate-limited, key-less or failed read is shown as a gap, never as "no risk". Notifications are off by default. Opt-in or experimental extras ship disabled: Ethereum-specific registered positions (sDAI, Steakhouse USDC, Compound III USDC; stored-index amounts are not interest extrapolated to now, and vault quotes are not guaranteed redeemable amounts), and an offline experimental yield/promotion parser component that is not a yield-monitoring or promotion-alert service. Targeted injection and privacy tests exist; this is not an "injection-proof" claim, a security certification, or proof that no address leaves the host. See the coverage matrix for each limit. Continuous-operation (72-hour shadow) qualification is **not complete**. Evidence for rc.3 is a full local gate at the release commit, not a GitLab CI pass. There is no PyPI release: install the wheel from the project's GitLab Releases page, `https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.3`.

**Quickstart.** First download every file listed in that release's `SHA256SUMS` into one directory and check them (`docs/install.md`). Then create a minimal `answers.json` (the address is synthetic; replace it with an address you want to watch):

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

Then:

```bash
uv tool install --constraints constraints.txt ./wallet_guardian_monitor-0.4.0rc3-py3-none-any.whl
export RPC_ETHEREUM_MAINNET_URL=https://ethereum-rpc.publicnode.com   # any public JSON-RPC URL you choose; one variable per chain you enable
guardian init --from answers.json --yes
guardian run-once --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json" --no-send
guardian coverage --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json"
```

The RPC variable is required: with no endpoint set, `run-once` stops with `endpoint_missing` and reads nothing. The URL above is an example of a public endpoint, not an endorsement; public endpoints rate-limit and may refuse some calls, which shows up as a gap in `coverage`. On a host whose system resolver returns a benchmark or private address, `run-once` refuses with a resolver message; set `GUARDIAN_RESOLVER_MODE=doh` and supply `GUARDIAN_DOH_ENDPOINTS` and `GUARDIAN_DOH_BOOTSTRAP` (see [docs/host-network.md](docs/host-network.md)). DoH enabled is not a qualified path. `guardian doctor` stays offline unless you pass `--live`. A first no-key run is bounded by a round deadline of about three minutes and reports what it could not finish as `partial`.

`arbitrum-mainnet` is `arbitrum-one`, and `polygon-mainnet` is `polygon-pos`. Typing the registry id (`arbitrum-one`, `polygon-pos`) keeps that id. Other names are rejected with the valid list and suggestions; nothing is guessed. A nonempty secret-profile value wins over the process environment. See [docs/faq-troubleshooting.md](docs/faq-troubleshooting.md) and [docs/api-keys.md](docs/api-keys.md).

The `answers.json` above is all `init` needs. `init` writes under the XDG config directory (or `GUARDIAN_CONFIG_DIR`) and prints `private_dir`. Pass that directory's `monitor.json` if it is not the path above. `run-once --no-send` still reads the endpoints you set. It leaves alerts in the local outbox and does not send them.

- Read-only monitor: addresses you choose, local coverage, local outbox. No signing and no broadcast.
- No-key and bring-your-own-key use the same files. A missing key is `credential_missing`, with a hint, not a zero balance.
- Coverage is per chain and per source. The claim is [docs/coverage-matrix.md](docs/coverage-matrix.md). A failed read stays a gap.
- `guardian install-service --dry-run` prints a schedule plan and exits non-zero. It does not install a unit.
- Grok Bot can run the same CLI. The shareable template is coming; there is no template link yet.

Guides: [CLI only](docs/guide-cli.md), [command reference](docs/cli-reference.md), [scheduling](docs/scheduling.md), [FAQ](docs/faq-troubleshooting.md), [Grok Bot](docs/guide-grok-bot.md), [security and privacy](docs/security-privacy.md). Contributing: [CONTRIBUTING.md](../CONTRIBUTING.md).

You do not need Cloudflare, D1, a wallet login, or Node to run the monitor. The Action Client section below is optional.

The MCM-01 scope contract is [docs/mcm-01.md](docs/mcm-01.md). That document is a plan, not a live result.

## Quickstart

Same steps as the quick start at the top of this file, with the default private-directory config path (`init` prints `private_dir`). Install as in [docs/install.md](docs/install.md), set an RPC endpoint variable for each enabled chain, then:

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
- 沒有 DeBank、Zerion、Alchemy、Tenderly、GoPlus key 時，不能宣稱全鏈 discovery 或保證模擬。
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

發行 wheel 的安裝、釘選的 constraints、XDG 私人目錄、升級與降版見 [docs/install.md](docs/install.md)。發行套件名稱是 `wallet-guardian-monitor`（wheel `wallet_guardian_monitor-0.4.0rc3-py3-none-any.whl`；匯入名稱與命令仍是 `guardian`）；預覽版沒有上傳到任何套件索引，名稱也沒有被保留。標籤 `v0.4.0-rc.3` 是否已發布，see GitLab releases。

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

範例名稱在 `config/env.example`。把值寫進 gitignore 的 `.env.local`，或匯出到環境。已存在的環境變數優先。doctor 只報告 `configured` 或 `not_configured`，不會印出值。

Etherscan V2 只用一把 `ETHERSCAN_API_KEY`，涵蓋 chainlist 以及之後的帳戶、交易與日誌。沒有第二把鏈別金鑰。有 key 且 chainlist 回傳資料列時，doctor 才標 `LIVE_PASS`。沒有 key 時是 `LIMITED_DISCOVERY`，不會把未認證的呼叫當成通過。

| 變數 | 用途 | 沒有 key 時 |
|---|---|---|
| `ETHERSCAN_API_KEY` | Etherscan V2 chainlist、交易、ABI、日誌 | `LIMITED_DISCOVERY`，chainlist 不是 LIVE_PASS |
| `GOPLUS_API_KEY` | 地址／授權風險補充 | `LIMITED_DISCOVERY`，不會假裝已篩過 |
| `DEBANK_ACCESS_KEY` | DeFi discovery | `BLOCKED` |
| `ZERION_API_KEY` | 替代 discovery | `BLOCKED`；本建置也不開放 webhook |
| `ALCHEMY_API_KEY` | Portfolio token | `BLOCKED` |
| `TENDERLY_ACCESS_KEY` | 可選模擬 | `BLOCKED` |
| `GUARDIAN_MAINNET_SEND` | 使用者錢包的主網送出開關，預設 `false` | 確認鈕即使可按也不會呼叫錢包 |

`GUARDIAN_MAINNET_SEND=true` 也不會讓監控程式簽名或廣播。`mainnet_test_transactions_authorized` 在這個建置仍必須是 false。不要購買套餐，除非另外授權。

## 任務工作台

```bash
cd action-client
npm install
npm run dev
```

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

BNB Chain 官方 RPC、Lista 官方文件上的 StakeManager proxy、Hyperliquid 公開 info、Etherscan V2 chainlist、OFAC SDN CSV。文件網址與查證等級見 `docs/source-registry.json` 與 doctor 產生的 `source_registry.json`。

## License

授權：AGPL-3.0-only（見 `LICENSE`）。
