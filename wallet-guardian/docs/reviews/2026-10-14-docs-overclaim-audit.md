# Wallet Guardian 文件與宣傳文案誇大稽核（v0.4.0-rc.4）

- 稽核對象：`main` 16559ac（`v0.4.0-rc.4-1-g16559ac`），套件 `wallet-guardian-monitor 0.4.0rc4`
- 稽核日期：2026-10-14
- 方式：全程唯讀。逐檔讀過使用者指定的文件，再用中英關鍵字 grep，並對照程式碼（`src/guardian/**`）、`config/`、`examples/`、`deploy/`。`guardian init` 會產出哪些來源與路由，我是在 repo 外的獨立 venv 裡直接呼叫 `guardian.onboarding._build()`，在記憶體中組出來看的。沒有寫任何檔到 repo，也沒有呼叫任何外部 API。
- 遮蔽：本報告沒有引用私人錢包地址或 key。文中出現的合約地址都是公開協議地址，不是私人錢包。

---

## 1. 摘要

整體來說，這個 repo 的文案很保守。下面這些宣稱全部找不到：「production ready」「全鏈」「CI 通過」「72h 已完成」「已上 PyPI」「即時」「保證」「injection-proof」。`pyproject.toml` 也沒有 `Development Status :: 5 - Production/Stable` 這類 classifier。使用者列的事實大多也有正確寫進文件，包括 72h 未完成、沒有 CI 通過、沒有 PyPI、Telegram canary `NOT_RUN`、TRON 實驗性且預設關閉、解釋器清單為空、sUSDe／Morpho 未做、Mode A 送出鎖住。

真正的問題不在誇大用語，而在兩類「文件描述的東西和使用者實際拿到的不一樣」：

1. 文件描述的是 `examples/monitor.json`，但 README 快速開始用 `guardian init` 產生的 profile。init 產生的 profile 比範例窄很多。
2. 文件說「設了某把 key 就會解鎖某能力」，但 rc.4 的 `run-once` 根本不會呼叫那個 adapter。Helius、GoPlus 都是這種情況，DeBank／Zerion／Tenderly 也一樣。

另外還有一批 0.3.0 時期的舊文字，跟 rc.4 的描述放在一起。

**統計：共 27 項。高 6、中 10、低 11。**依類型分：與程式不符或不準確 11、過時或誇大成熟度 5、缺少限制說明或不完整 3、內部不一致 3、措辭／反向（文件少說）／其他 5。同一問題出現在多個檔案時，只算一項（表中標「合併計」）。

### 高嚴重度（可能讓使用者以為受保護，實際上沒有）

| # | 一句話 |
|---|---|
| H1 | README 與 guide-cli 快速開始示範的 `ethereum-rpc.publicnode.com`，在專案自己的 coverage-matrix 裡記錄為「無 address 的 `eth_getLogs` 任何範圍都回 -32701」。授權掃描（`permissions.standard`）與代幣歷史剛好就是用這種查詢。照 README 跑，第一輪就沒有授權監控。 |
| H2 | 文件把 `asset_usd`（USD ≥ 1000）列為告警類型。實際上 `run-once` 只會幫原生幣的候選事件加上 USD 報價，ERC-20／SPL／TRC-20 轉帳永遠拿不到報價，也就不會觸發。init profile 又沒有價格來源和 `native_unit_thresholds`，所以 BNB／POL／AVAX／SOL／BTC 的移動是 `threshold_unavailable`。結果是 init 使用者只會收到兩種金額相關的告警：4 條 ETH 鏈上 ≥0.5 ETH 的原生移動，以及授權變化。 |
| H3 | `api-keys.md` 與 coverage-matrix 寫的無 key 後備路徑（keyless Blockscout、Blockscout PRO、Alchemy、Tenderly Base 日誌路由），只存在於 `examples/monitor.json`。init 產生的 profile 只有「需要 key 的 per-chain Blockscout」。Base／OP 在免費 Etherscan 下，最終原因是 `plan_not_supported`：後面的 Blockscout 來源沒有 key 時直接跳過，不會覆寫這個原因。外部歷史讀不到，只剩餘額和 nonce（與使用者提供的事實一致）。完全沒有 Etherscan key 時是 `credential_missing`；連 URL 變數都沒設時是 `unsupported_source`（FAQ:93）。 |
| H4 | `api-keys.md` 說 Helius 解鎖「Solana parsed transaction events」，但 pipeline 從未匯入 Helius adapter。Solana 的 `run-once` 掃描固定帶 `fetch_transactions=False`，所以沒有交易層級的轉帳解析，也就沒有 SOL／SPL 轉帳告警，只剩餘額、token 帳戶、簽章清單與 SPL 權限變更。文件只寫了「Solana (partial)」。 |
| H5 | `api-keys.md` 說 GoPlus 解鎖「address and asset risk lookups」，`security-model.md` 也暗示設了 key 和 base 就會呼叫。實際上 `GoPlusClient` 在 `risk/` 以外沒有任何地方匯入，`_risk_gap()` 一律回 blocked。就算有 key 也同意了，結果仍是 `not_run`。 |
| H6 | 預設情況下沒有任何告警會主動送到使用者手上：`delivery` 是 `outbox_only`；沒有 `guardian outbox` 讀取指令；`install-service` 只做 dry-run；隨套件附的 systemd 與 cron 範本**一律帶 `--no-send`**；init 的輪詢間隔是 10800 秒（3 小時）。這些都分散寫在技術文件裡，README 狀態段與 notifications.md 都沒有把它們串起來講。 |

### 使用者懷疑但查無問題的部分

- **打包副本漂移：沒有。** `src/guardian/_resources/docs/` 只有一個檔（`mcm-01/network-tiers.plan.json`），與頂層 `docs/` 逐位元組相同。`_resources` 底下的 config／examples／schemas 也全部逐位元組相同。真正的漂移在 `config/env.example`（頂層與打包版都一樣舊）和 README 的中英兩半，見第 3 節。
- **pyproject classifiers：沒有。** 不存在誇大的 classifier；description 也是中性的「Read-only wallet monitor …」。
- **Action Client 介面字串**（`index.html`、`src/main.ts`、`src/mode-a.mjs`、`src/lista-reread.mjs`）：全都是保守語氣，例如「允許實際送出：關閉」「交易雜湊不是完成」「模式 C 不支援」，沒有誇大。
- **CLI help／doctor 輸出：沒有誇大。** `rescue` 回 `BLOCKED`；legacy doctor 對 DeBank 等會明說「key configured; this provider adapter is not implemented」。
- **internal 轉帳與新聞維度：沒有問題。**`history.native_internal` 標得很誠實：只有 BSC NodeReal、只有離線 fixture，其他鏈是 `not_queried`／`unsupported`（coverage-matrix:102、balance-deltas.md:31、:50）。新聞 feed 有列出解析結果，也聲明「a feed that parses is not a claim about incident coverage」（coverage-matrix:65–75、:84），init 也預設不抓新聞。
- **`pip install`：**文件沒有教人從 PyPI 安裝，`install.md` 還明確警告不要這樣做。

---

## 2. 逐檔發現表

嚴重度定義：**高**＝可能讓使用者以為受保護但其實沒有；**中**＝誇大成熟度、與程式不符但影響有限、或過時；**低**＝措辭或小錯。

### 2.1 `wallet-guardian/README.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| H1 | README.md:34、:40 | `export RPC_ETHEREUM_MAINNET_URL=https://ethereum-rpc.publicnode.com # any public JSON-RPC URL you choose` ／ "public endpoints rate-limit and may refuse some calls" | 誤導（示範端點已知做不到授權掃描；FAQ:93 描述了結果，但沒說示範端點就是原因） | 高 | 在 :34 之後加：「`ethereum-rpc.publicnode.com` answers balance reads but refuses address-less `eth_getLogs` (-32701, recorded in docs/coverage-matrix.md). With it, `permissions.standard` (approval scan) and `history.token` stay gaps. For approval monitoring, use an endpoint that accepts topic-only `eth_getLogs` over a 2000-block range; the matrix lists the ones that did.」也可以直接換成矩陣記錄可接受 2000 區塊的端點，但要註明那只是一次探測。 |
| H2 | README.md:7（"A rate-limited … read is shown as a gap"）、:46 "Read-only monitor … local outbox" | 沒有說明哪些移動會產生告警 | 缺少限制說明 | 高 | 在 Status 段加：「In a profile written by `init`, amount alerts fire only for native ETH ≥ 0.5 on ethereum-mainnet, arbitrum-one, base-mainnet and optimism-mainnet. Token transfers (ERC-20, SPL) are not valued in rc.4 and do not alert by amount. Native BNB, POL, AVAX, SOL and BTC movements need an operator-set `native_unit_thresholds` floor or a configured price source; `init` writes neither. Approval/permission changes alert without a threshold.」 |
| H6 | README.md:7、:49、:72 | "Notifications are off by default." ／ "You enable a schedule later, yourself." | 缺少限制說明 | 高 | 在 Status 段加：「Out of the box nothing reaches you: `run-once` is a single pass, nothing is scheduled, alerts stay in the local SQLite outbox (there is no `guardian outbox` reader), and the unit/cron templates from `install-service` run `run-once --no-send` every 3 hours. Sending to Telegram needs a manually configured delivery profile and a run without `--no-send`; that path has not been tested live.」 |
| M1 | README.md:7 | "Evidence for rc.4 is a full local gate at the release commit, not a GitLab CI pass." | 誇大成熟度 | 中 | 「Evidence for rc.4 is a manually run 12-step local gate at the release commit. The aggregate release-evidence status is `NOT_RUN` (the `public-sample-probe` step was not run). GitLab CI minutes are exhausted and there is no CI pass on record.」 |
| M3 | README.md:104、:159–167 | 「沒有 DeBank、Zerion、Alchemy、Tenderly、GoPlus key 時，不能宣稱全鏈 discovery 或保證模擬」；表格「`DEBANK_ACCESS_KEY` DeFi discovery｜沒有 key 時 BLOCKED」 | 與程式不符（暗示有 key 就解鎖） | 中 | :104 改成「DeBank、Zerion、Tenderly 在這個版本沒有實作 adapter，設了 key 也不會呼叫；doctor 會回『key configured; this provider adapter is not implemented』。GoPlus 也不會被自動呼叫。」表格加一欄「本版是否會呼叫」：DeBank／Zerion／Tenderly 填「否（未實作）」，GoPlus 填「否」，Alchemy 填「只在 examples/monitor.json 的 Base 路由；init 不會加」。 |
| M4 | README.md:91–97、:93、:157 | 「現在做得到：`guardian doctor --json`：檢查…能連上的唯讀來源」；「有 key 且 chainlist 回傳資料列時，doctor 才標 `LIVE_PASS`」 | 過時（0.3.0 舊文） | 中 | :93 加「（rc.4 起 doctor 預設離線，要 `--live` 才連線）」。:157 改「`doctor --live` 有 key 且 chainlist 回傳資料列時才標 `LIVE_PASS`」。「現在做得到／做不到」整節加小標「舊 Lista／BNB Chain 路徑（`--config`）」，並在前面放第 4 節的中文「狀態與限制」。 |
| M9 | README.md:105 | 「官方 BSC dataseed 停用 `eth_getLogs`，沒有 Etherscan key 就無法回補歷史領取。」 | 不準確 | 中 | 「官方 BSC dataseed 停用 `eth_getLogs`。本版沒有用 Etherscan 回補 BSC 歷史領取的路徑，而且 Etherscan V2 免費方案對 BSC 會回 `plan_not_supported`；有 key 也無法回補。」 |
| L5 | README.md:7 vs 其他頁 | 只有這裡寫「Solana (partial)」 | 不一致 | 低 | 見下方 announcement 那一列。 |
| L6 | README.md:7 vs :127 | 直接連到 `/-/releases/v0.4.0-rc.4`，但 :127 寫「是否已發布，see GitLab releases」 | 不一致 | 低 | 統一寫法：已發布就刪掉「是否已發布」；未確認就把連結改成「(if published)」。 |
| L4 | README.md:7 | 只列 sDAI、Steakhouse、Compound III 三種 opt-in 持倉 | 反向（文件少說） | 低 | 補一句：「Other opt-in plugins exist (`lido`, `aave-v3`, `kinza`, `uxlink-holdings`, `lista-slisbnb`, `solana-stake`); see docs/coverage-matrix.md for their cell status.」 |

### 2.2 根目錄 `README.md`、`CONTRIBUTING.md`、`.gitlab/issue_templates/*`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| M4b | README.md（根）:13 | 「金鑰範例在 `wallet-guardian/config/env.example`」 | 過時（指向舊檔） | 中 | 見 env.example 那一列。或改成「init 產生的 `secret-profile.json` 會列出這份設定實際需要的變數名稱」。 |
| L5b | .gitlab/issue_templates/Feature.md:3 | "named preview chains are Ethereum … and Solana" | 措辭 | 低 | 改成 "… and Solana (partial: balances, token accounts and SPL authorities; transactions are not parsed, so no transfer alerts)"。 |
| — | CONTRIBUTING.md、Bug.md | — | OK | — | 有寫「green local run is not a CI pass」和 release evidence `NOT_RUN`，正確。 |

### 2.3 `wallet-guardian/CHANGELOG.md`、`pyproject.toml`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| M1b | CHANGELOG.md:62–63 | "evidence is the full local gate at the release SHA" ／ "while the optional public-sample-probe is not executed" | 誇大成熟度＋內部矛盾 | 中 | `docs/ci.md:93` 把 `public-sample-probe` 列為**必要**步驟，不是 optional。改成：「evidence is a manually run local gate (12 steps) at the release SHA; the required `public-sample-probe` step was not run, so release evidence is `NOT_RUN` (waived for this labelled preview).」 |
| L10 | CHANGELOG.md:6 | "this project uses Semantic Versioning" | 措辭 | 低 | "Versions follow PEP 440 (`0.4.0rc4`); git tags use the SemVer-style spelling `v0.4.0-rc.4`." |
| L8 | pyproject.toml:9 | `authors = [{ name = "Cursor Agent", email = "cursoragent@cursor.com" }]` | 不準確（套件後設資料署名） | 低 | 改成實際維護者或專案名，例如 `{ name = "Wallet Guardian maintainers" }`。 |
| — | pyproject.toml | 沒有 classifiers；description 中性 | OK | — | 若之後加 classifiers，請用 `Development Status :: 3 - Alpha` 或 `4 - Beta`，不要用 5。 |

### 2.4 `docs/api-keys.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| H3 | api-keys.md:34 | "Without the key: native history uses a Blockscout keyless instance when that chain has one (Ethereum, Arbitrum, Polygon, Base, Optimism)." | 與程式不符（只對 examples 成立） | 高 | "In `examples/monitor.json`, without the key native history falls back to a keyless Blockscout instance (Ethereum, Arbitrum, Polygon, Base, Optimism) **when you set that instance's URL variable**. A profile written by `guardian init` does not include keyless or PRO Blockscout: it adds a per-chain Blockscout source that needs both `BLOCKSCOUT_<CHAIN>_URL` and `BLOCKSCOUT_<CHAIN>_API_KEY`, so without a key that row is `credential_missing`. With only a free Etherscan key, Base and Optimism end at `plan_not_supported` (the keyed Blockscout source is skipped without its key and does not replace that reason): balance and nonce only." |
| H3b | api-keys.md:90、:96–98、:118–121 | "Order for Base and Optimism: PRO … then the keyless instance"；"Alchemy Transfers is the preferred Base source"；Tenderly／1rpc Base 日誌路由 | 與程式不符（init 沒有這些來源） | 高（與 H3 合併計） | 每段開頭加 "(applies to `examples/monitor.json`; `guardian init` does not add this source — copy the source and route from the example if you want it)"。 |
| H4 | api-keys.md:163 | "What it unlocks: Solana parsed transaction events." | 與程式不符 | 高 | "rc.4 does not call Helius from `run-once`: the adapter exists and is tested offline, but the pipeline does not import it. Setting `HELIUS_API_KEY` changes nothing in a run. Solana transactions are not parsed in this preview, so there are no SOL/SPL transfer rows and no transfer alerts; only the signature list is read." |
| H5 | api-keys.md:129 | "What it unlocks: address and asset risk lookups." | 與程式不符 | 高 | "rc.4 does not call GoPlus. With or without a key and consent, the `address_risk` row is a blocked gap (`credential_missing`, `third_party_denied`, or `not_run`). No risk screening is performed." |
| H2b | api-keys.md:146 | "What it unlocks: USD quotes." | 與程式不符（範圍誇大） | 高（與 H2 合併計） | "What it unlocks: native-coin USD quotes for native transfers and balance deltas (simple/price). Token transfers are not valued in rc.4. `init` does not add a CoinGecko source." |
| L11 | api-keys.md:80–84 | "`BLOCKSCOUT_ETH_URL` default `https://eth.blockscout.com`" | 不準確（程式沒有預設值） | 低 | "Suggested value `https://eth.blockscout.com`. There is no built-in default: if the variable is unset the source is skipped." |
| — | api-keys.md:25、:31、:65 | 免費方案 `plan_not_supported`、Base 402 | OK | — | 正確，也與 FAQ 一致。 |

### 2.5 `docs/coverage-matrix.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| H3c | coverage-matrix.md:7、:198–215 | "derived from … `examples/monitor.json`"；表格 "Keyed sources and the no-key column" | 缺少範圍說明 | 高（與 H3 合併計） | 在 :7 後加："Cells describe `examples/monitor.json`. A profile from `guardian init` routes fewer sources: no keyless or PRO Blockscout, no Alchemy, no Tenderly Base log route, no CoinGecko, no OFAC source, no Helius route. For an init profile, read every `history.native_external` no-key cell except Ethereum/Arbitrum/Polygon-with-Etherscan-key as `credential_missing`." |
| H4b | coverage-matrix.md:135、:206 | "Helius parsed events when the key and URL are set and the source is enabled \| not_verified" | 與程式不符 | 高（與 H4 合併計） | keyed path 改成 "Helius adapter exists (offline tests) but is not called by run-once"；keyed status 從 `not_verified` 改成 `credential_missing`／未接線。 |
| H5b | coverage-matrix.md:146 | "GoPlus with GOPLUS_API_KEY \| not_verified \| partial" | 與程式不符 | 高（與 H5 合併計） | keyed path 改成 "not called by run-once in rc.4"；row status 改成 `blocked`。 |
| M2 | coverage-matrix.md:35、:103–104、Public eth_getLogs 表（:175 起） | BSC 的 `history.token`／`permissions.standard`："public RPC eth_getLogs when the endpoint accepts a topic filter \| not_verified" | 不完整（已知拒絕沒有記錄） | 中 | 在 publicnode 那條加 "publicnode BSC eth_getLogs returns HTTP 403"，Public eth_getLogs 表加一列 `publicnode-bsc-getLogs \| blocked`。BSC 兩個 cell 註明「with publicnode the approval scan and token history are blocked」。 |
| M7 | coverage-matrix.md:34、:121、:202、:215 vs :56；:115 vs :55 | Optimism keyless "HTTP 200 from the Linux host"；Base keyless "partial"；Arbitrum／Polygon keyless "not_verified" | 內部不一致 | 中 | 依 :55–56 的 2026-10-09 探測改 cell：Optimism keyless 是 `blocked`（301），預設 URL 改為 explorer.optimism.io 並重探；Base keyless 是 `blocked` on Linux host／host-specific；Arbitrum、Polygon keyless 是 `blocked`（403）。也要刪掉 :202、:215、:1450 的 "recorded HTTP 200"。 |
| — | coverage-matrix.md:9–24、:218–235 | 狀態定義、"What is not claimed" | OK | — | 誠實。72h、TRON、sUSDe／Morpho、PyPI、canary 都寫對了。 |

### 2.6 `docs/guide-cli.md`、`docs/faq-troubleshooting.md`、`docs/quickstart-monitor.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| H1b | guide-cli.md:16 | `export RPC_ETHEREUM_MAINNET_URL="https://ethereum-rpc.publicnode.com"` | 誤導 | 高（與 H1 合併計） | 同 README H1 的補充句。 |
| H2c | guide-cli.md:101、:111 | "Any other asset uses a USD quote." ／ "`asset_usd` \| A valued USD quote is at least `asset_usd_threshold`" | 與程式不符 | 高（與 H2 合併計） | :101 改成 "Native coins can use a USD quote when a CoinGecko source is configured (init does not add one). Token transfers are not valued by `run-once` in rc.4, so they are `valuation_unknown` and never reach `asset_usd`." 表格 `asset_usd` 列加 "(native coins only, and only with a configured price source)"。 |
| H3d | guide-cli.md:72 | "Keyed indexers stay `credential_missing` where no keyless path exists." | 與程式不符（init 沒有 keyless 路徑） | 高（與 H3 合併計） | "In an init profile no keyless history path is configured: every keyed indexer without its key is `credential_missing` (or `unsupported_source` if its URL variable is unset)." |
| H4c | guide-cli.md:76 | "If you include Solana, init enables the Helius parsed-events source. With no `HELIUS_API_KEY` that row is `credential_missing`." | 與程式不符 | 高（與 H4 合併計） | "If you include Solana, init lists a Helius source, but rc.4 `run-once` never calls it. Solana coverage is native balance, token accounts, the signature list and SPL authorities; transactions are not parsed, so there are no Solana transfer rows or transfer alerts." |
| H6b | guide-cli.md:128 | "There is no `guardian outbox` command." | 缺少後果說明 | 高（與 H6 合併計） | 接著加："So with the default `outbox_only`, the only places you see an alert are the `run-once` JSON (`outbox_written`) and the SQLite file. Nothing notifies you." |
| — | faq-troubleshooting.md:31、:81、:93 | 免費 Etherscan 的 `plan_not_supported`、"empty outbox is not all clear"、no-key 首輪 `unsupported_source` | OK | — | :93 其實符合 init profile 的真實行為。它反而跟 api-keys.md:34 矛盾，證明 H3 那句寫錯。 |
| — | quickstart-monitor.md:3、:50–57 | 「沒有 live 驗證」、交接清單 | OK（但見 H6） | — | 交接清單第 3 點「沒有帶 `--no-send`」正確。建議補一句：「`install-service` 產生的排程一律帶 `--no-send`，要送出必須自己另寫排程。」 |

### 2.7 `docs/notifications.md`、`docs/scheduling.md`、`docs/box-unattended.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| M10 | notifications.md:1–7 | 「這一包只加一個頻道：Telegram Bot API。預設仍是 `outbox_only`，不會送。」 | 缺少限制說明（「未實測」寫在最後一節） | 中 | 開頭加：「狀態：Telegram 送出只有 mock 測試，從未對真實 Telegram 驗證過（canary `NOT_RUN`）。`install-service` 產生的排程一律帶 `--no-send`，所以排程本身永遠不會送 Telegram。」 |
| L7 | notifications.md:36 | 「遷移目前這兩個錢包時，同樣只有一個 `owner_id`。」 | 措辭（作者私人情境寫進公開文件） | 低 | 「從既有通知流程遷移時，同樣只有一個 `owner_id`。」 |
| — | scheduling.md:3–5、:52、:69 | 72h 未完成、host smoke `NOT_RUN`、ExecStart 帶 `--no-send` | OK | — | 技術上寫清楚了，但沒有對使用者講出「所以不會通知你」（H6）。 |

### 2.8 `docs/security-model.md`、`docs/security-privacy.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| M8 | security-model.md:29 | "GoPlus and Chainabuse user-address lookups are not called unless the operator sets the key and the base." | 不夠準確＋與程式不符 | 中 | 這句只寫了必要條件，沒寫充分條件：README:5 與 security-privacy.md:30 說還需要本機同意，而程式則是永不呼叫。改成："rc.4 does not call GoPlus or Chainabuse. A key, a base URL and `operator_approved_sources_only` together still produce a `not_run` gap; the address is not sent." |
| L3 | security-model.md:28 | "A keyed indexer (Etherscan, Blockscout, NodeReal, Helius, TronGrid) receives the address only when that source is actually called. A missing key is not sent." | 不準確（隱私描述） | 低 | 把 TronGrid 拿出來另寫："TronGrid (opt-in TRON) is called keyless too: the watch address is sent with or without `TRON_PRO_API_KEY`."（init 自己的 data-sharing 文字就是這樣寫。） |
| H2d | security-privacy.md:22 | "the request quotes a catalog asset id (`ids`) or a token contract (`contract_addresses`)" | 與程式不符（run-once 只查原生幣 id） | 高（與 H2 合併計） | "In rc.4 `run-once` only requests native-coin ids (`/simple/price`). The token-contract path exists in the client but is not used by the run." |
| — | security-privacy.md:3、:51 | "This page is not an audit"、"not a CI pass … 72h … not complete" | OK | — | — |

### 2.9 `docs/coverage.md`、`docs/deploy.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| M5 | coverage.md:1–3、:7、:23、:24 | 標題「覆蓋範圍」；「套件版本是 0.3.0」；「180 passed」；「Ethereum、其他 EVM \| 未啟用」；「Alchemy discovery \| BLOCKED」 | 過時（與 rc.4 矛盾） | 中 | 標題改成「覆蓋範圍（0.3.0 舊 Lista 路徑快照，2026-10-07）」，第一行加：「rc.4 的覆蓋以 docs/coverage-matrix.md 為準；本頁的『Ethereum 未啟用』『180 passed』只適用 0.3.0。」coverage-matrix.md:7 也要說明 coverage.md 只是舊 Lista 證據。 |
| M6 | deploy.md:3、:5、:30 | 「狀態：**VERIFIED_ORIGIN（2026-10-07）**。公開固定來源已上線」；「`pytest` **180 passed**」 | 誇大或過時 | 中 | 「狀態：Cloudflare Pages 網址在 2026-10-07 回過 HTTP 200（0.3.0 合併前建置）。之後的路由變更（`/t/<ref>`）尚未部署，`fixed_https_origin` 仍是 false，所以沒有任何任務連結上線。」刪掉 180 passed，或註明「0.3.0 當時」。 |

### 2.10 `docs/announcement.md`、`docs/guide-grok-bot.md`、`docs/release-preview.md`、`docs/ci.md`

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| L5 | announcement.md:9、:17；guide-cli.md:3；faq:3；guide-grok-bot.md:3、:18 | "The named chains are … and Solana" ／「點名的鏈是 … Solana」 | 措辭（缺「partial」） | 低 | 英文："… Polygon PoS and Solana (partial: transactions are not parsed, so no Solana transfer alerts in this preview) …"；中文：「…、Solana（部分：本預覽不解析交易，因此沒有 Solana 轉帳告警）…」。 |
| H6c | announcement.md:11、:19 | "Notifications are off by default; the live Telegram canary has not been run." | 缺少限制說明 | 高（與 H6 合併計） | 英文："Out of the box nothing notifies you: alerts stay in a local database, the scheduler is not installed, and the provided schedule runs with `--no-send` every 3 hours. Telegram delivery exists but has not been tested live."；中文同義改寫。 |
| L6b | announcement.md:3 | 直接放 release 連結 | 不一致 | 低 | 同 README L6。 |
| — | announcement.md:11、:19 | 72h 未完成、local gate、無 CI、無 PyPI | OK | — | 符合事實。建議把 "local gate" 改成 "a local 12-step gate (release evidence `NOT_RUN`)"。 |
| — | release-preview.md、ci.md | 閘門表與證據契約 | OK | — | ci.md:3 明寫 shared-runner 額度已用完，release-preview 把 24-window shadow、canary 列為 `NOT_RUN`。正確。 |

### 2.11 其他 docs

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| L1 | yield-promotions-plan.md:3 | "Status: plan only. No code, no config, no allowlist entry exists for this feature." | 過時 | 低 | "Status: plan. An offline fixture/parser package now exists (`guardian.yields`, see yield-promotions.md), disabled, with no live fetch, scheduler, alert or push. The goal below (notify on APY events) is not implemented." |
| L2 | coverage-tron.md:9 vs :25 | "Six successful keyless responses" ／ 證據只列 5 個 endpoint | 內部不一致 | 低 | 補上第 6 個 endpoint，或改成 "the keyless responses below"。 |
| L4b | inflow-explainer.md:64、:66；api-keys.md:15 | "See the Aave position reader"；"Lido stETH … are position reads on Ethereum" | 反向（README 沒提） | 低 | 不用改這兩頁，改 README（見 L4）。 |
| — | positions-more.md:5 | "sUSDe is not included … Morpho Blue markets are not included." | OK | — | 符合事實。 |
| — | yield-promotions.md:3、:51 | "`ENABLED` is false … no scheduler, no live fetch, and no send" | OK | — | 符合「APY 活動即時推播未做」。 |
| — | inflow-explainer.md:5、:9 | "shipped manifest has no approved deployment intervals"；"No live historical-path qualification" | OK | — | 與 `config/explainers/lista-stakemanager.json` 的 `implementations: []` 一致。 |
| — | mode-a-lista.md、action-client-ui.md、p1／p2／d1 | 「正式送出仍關閉」 | OK | — | 與 UI 一致。 |

### 2.12 打包副本與設定範例

| # | 檔案:行號 | 原文（短引用） | 問題類型 | 嚴重度 | 建議改寫 |
|---|---|---|---|---|---|
| M3b | config/env.example:1–10（`src/guardian/_resources/config/env.example` 內容相同） | 只列 ETHERSCAN、GOPLUS、DEBANK、ZERION、ALCHEMY、TENDERLY、`GUARDIAN_MAINNET_SEND` | 過時（不是 rc.4 profile 會用到的變數） | 中 | 檔頭加：「這份是舊 Lista／doctor 路徑的範例。`guardian init` 產生的 `secret-profile.json` 才是你這份設定需要的變數清單（`RPC_*_URL`、`ETHERSCAN_API_BASE`、`BLOCKSCOUT_*`、`NODEREAL_*` 等）。DeBank／Zerion／Tenderly 本版沒有 adapter。」 |
| L9 | 打包的 wheel | `credential_hint` 指向 `docs/api-keys.md#…`，README 相對連結指向 `docs/`，但 wheel 不含 docs | 措辭（裝 wheel 的使用者找不到） | 低 | hint 改用完整 GitLab URL（例如 `…/-/blob/v0.4.0-rc.4/wallet-guardian/docs/api-keys.md#etherscan`），或把 api-keys.md 打包進 `_resources/docs`。 |
| — | `_resources/docs/mcm-01/network-tiers.plan.json` 與其他 config／examples／schemas | — | OK（沒有漂移） | — | 用 `cmp` 逐位元組比對，全部相同。 |

---

## 3. 系統性問題

1. **文件描述的 profile 和快速開始產生的 profile 不同（H3 的根本原因）。**coverage-matrix、api-keys、FAQ 部分段落描述的是 `examples/monitor.json`。README、guide-cli、guide-grok-bot 卻要使用者跑 `guardian init`。在記憶體中實際組出 init profile，比對如下：

   | 能力 | `examples/monitor.json` | `guardian init` 產生的 profile |
   |---|---|---|
   | ETH／ARB／POL 外部歷史 | Etherscan → keyless Blockscout（`*_PUBLIC`/`BLOCKSCOUT_ETH_URL`）→ RPC | Etherscan（還要另設 `ETHERSCAN_API_BASE`）→ 需要 key 的 `BLOCKSCOUT_<CHAIN>_URL`＋`_API_KEY` |
   | Base 外部歷史 | Alchemy → PRO → keyless → keyed | Etherscan（免費方案 `plan_not_supported`）→ 需要 key 的 Blockscout |
   | OP 外部歷史 | PRO → keyless → keyed | Etherscan（免費方案 `plan_not_supported`）→ 需要 key 的 Blockscout |
   | Base 日誌／授權 | `tenderly_base` → `base-1rpc` | 使用者自己的 `RPC_BASE_MAINNET_URL` |
   | 價格 | CoinGecko | 無 |
   | OFAC、新聞 | 有來源 | 無 |
   | 輪詢間隔 | — | 10800 秒（3 小時） |

   **建議：**coverage-matrix 加一欄「init profile」，或在每個 doc 開頭標明它描述的是哪個 profile。另一個選擇是讓 init 產生與 examples 相同的後備路由，那就是程式修改，不在本稿範圍。

2. **「設 key 就解鎖」的描述，對應的 adapter 其實沒有接進 `run-once`（H4、H5、M3）。**Helius、GoPlus、Chainabuse、DeBank、Zerion、Tenderly 的 adapter 或 doctor 項目存在，但 `run-once` 不會呼叫。文件統一用 `not_verified` 或 `BLOCKED (no key)`，讀者自然會推論「有 key 就會動」。**建議：**新增一個狀態詞 `not_wired`（未接入執行路徑），跟 `not_verified`（有接、沒探測）分開。

3. **告警面（alert surface）沒有按鏈列出來（H2）。**目前沒有任何一頁告訴使用者「在這條鏈上，什麼事會讓你收到告警」。依程式追蹤，init profile 的結果如下：

   | 鏈 | 金額相關告警 | 權限告警 | 備註 |
   |---|---|---|---|
   | Ethereum、Arbitrum、Base、Optimism | 原生 ETH ≥0.5（轉帳、`balance_delta`） | Approval／ApprovalForAll（端點要能做 topic-only `eth_getLogs`） | ERC-20 轉帳不會依金額告警 |
   | BSC、Polygon、Avalanche | 無（`threshold_unavailable`），除非自己設 `native_unit_thresholds` 或價格來源 | 同上（BSC publicnode 403） | — |
   | Solana | 無 | SPL delegate／authority | 不解析交易，沒有轉帳列 |
   | Bitcoin | 無（沒有門檻） | 不適用 | 只有餘額與分頁歷史 |
   | TRON（opt-in） | 無（沒有門檻） | 不告警 | — |

   **建議：**guide-cli 的「Alert types」前面加上這張表。最好再請維護者寫一個 fixture 測試鎖住它，因為這是由程式推論出來的。

4. **0.3.0 舊文和 rc.4 文字混在一起（M4、M5、M6、M3b、L1）。**`coverage.md`、`deploy.md`、README 的繁中後半段、`env.example`、`yield-promotions-plan.md` 都停在 0.3.0 或規劃期，但沒有標成歷史文件。中文讀者讀 README 後半段，看到的是另一個產品的樣子：只有 BSC Lista、doctor 會連線、DeBank 有 key 就能 discovery。**建議：**給這些檔加「歷史／legacy」橫幅；README 的中文段落也要有和英文 Status 段同等的「狀態與限制」。

5. **缺少一個集中的「已知限制」區塊。**限制目前散在 Status 長段落、CHANGELOG 的 Known limitations、coverage-matrix 的 What is not claimed、FAQ 等至少 6 處，而且 Status 段是一整段長句。**建議：**README 開頭放一個短的條列區塊（見第 4 節草稿），細節再連到 CHANGELOG 和 matrix。

6. **「full local gate」這個用語。**README、CHANGELOG、announcement 都只說 local gate，沒說是幾步、哪些步沒跑。release-evidence 本身是 `NOT_RUN`。CHANGELOG 又把 ci.md 列為必要的 `public-sample-probe` 稱為 optional。**建議：**統一寫成「手動執行的本機 12 步 gate；release-evidence 彙總 `NOT_RUN`」。

7. **時效（「即時」）。**全 repo 沒有「即時／real-time」的宣稱，這部分沒問題。但 README 和 announcement 沒有寫出輪詢間隔（init 是 10800 秒；附的排程是每 3 小時，而且不送出）。balance-deltas.md:48 自己引用過「約 3 小時後才報告」的實例。**建議：**在狀態區塊寫明延遲上限。

---

## 4. 建議新增的誠實標示段落（草稿）

### 4.1 `wallet-guardian/README.md`：放在 `**Status (preview rc.4).**` 段落之前

```markdown
## Status and limits (read this first)

- **Preview, not production.** Version `0.4.0rc4` (tag `v0.4.0-rc.4`). Checked with a manually run 12-step local gate at the release commit. Release evidence is `NOT_RUN`; GitLab CI minutes are exhausted and there is no CI pass on record. The 72-hour / 24-window shadow run has not been done. Not on PyPI.
- **Nothing notifies you by default.** `run-once` is one pass. No scheduler is installed. Alerts are written to a local SQLite outbox; there is no outbox reader command. The schedule that `install-service` prints runs `run-once --no-send` every 3 hours. Telegram sending exists but has only been tested against a mock.
- **What can alert, in a profile from `guardian init`:** native ETH movements of 0.5 ETH or more on Ethereum, Arbitrum One, Base and Optimism, and approval/permission changes (only if your RPC accepts topic-only `eth_getLogs`). Token transfers are not valued and do not alert by amount. BNB, POL, AVAX, SOL and BTC movements do not alert unless you add a `native_unit_thresholds` floor or a price source.
- **Chains differ.** Ethereum, Arbitrum and Polygon get external history with a free Etherscan key. Base and Optimism get balances and nonce only unless you add a Blockscout key (Etherscan's free plan refuses them). BSC external history needs a NodeReal key, and publicnode BSC refuses `eth_getLogs` (HTTP 403). Solana: balances, token accounts and SPL authorities; transactions are not parsed, so no transfer alerts. Avalanche: limited. Bitcoin: address set only. TRON: experimental, off by default, not tested live.
- **Keys that do nothing in this preview:** Helius, GoPlus, Chainabuse, DeBank, Zerion and Tenderly. Their adapters are not called by `run-once`.
- **Not built:** sUSDe, Morpho Blue markets, live yield or APY event alerts, live historical lookups for inflow explanations (the shipped explainer list is empty), and wallet sending (Mode A send stays locked).
- A gap (`credential_missing`, `partial`, `blocked`, …) is never a zero balance or an all-clear. See docs/coverage-matrix.md; its cells describe `examples/monitor.json`, which routes more sources than `init` does.
```

### 4.2 `wallet-guardian/README.md`：在「## 現在做得到」之前插入（繁中）

```markdown
## 狀態與限制（先讀這段）

- **預覽版，不是正式版。** 版本 `0.4.0rc4`（標籤 `v0.4.0-rc.4`）。只在發行 commit 上手動跑過本機 12 步 gate；release evidence 是 `NOT_RUN`。GitLab CI 額度已用完，沒有 CI 通過紀錄。72 小時／24 輪 shadow 驗證沒有做。沒有上 PyPI。
- **預設不會通知你。** `run-once` 只跑一輪；沒有安裝排程；告警只寫進本機 SQLite outbox，也沒有讀取指令。`install-service` 給的排程每 3 小時跑一次，而且帶 `--no-send`。Telegram 送出只有 mock 測試，沒有實測。
- **`guardian init` 產生的設定裡，會觸發告警的只有：** Ethereum、Arbitrum One、Base、Optimism 上 ≥0.5 ETH 的原生移動；以及授權／權限變更（前提是 RPC 接受 topic-only 的 `eth_getLogs`）。代幣轉帳不估值，不會依金額告警。BNB、POL、AVAX、SOL、BTC 的移動，要你自己設 `native_unit_thresholds` 或價格來源才會告警。
- **各鏈深度不同。** Ethereum、Arbitrum、Polygon 有免費 Etherscan key 就能讀外部歷史。Base、Optimism 在免費 Etherscan 下只查餘額和 nonce，要另加 Blockscout key。BSC 外部歷史要 NodeReal key；publicnode BSC 的 `eth_getLogs` 回 403。Solana 只有餘額、token 帳戶與 SPL 權限；不解析交易，所以沒有轉帳告警。Avalanche 有限。Bitcoin 只讀地址集合。TRON 是實驗性功能，預設關閉，沒有實測。
- **本版設了也沒作用的 key：** Helius、GoPlus、Chainabuse、DeBank、Zerion、Tenderly。`run-once` 不會呼叫這些 adapter。
- **沒有做：** sUSDe、Morpho Blue 市場、APY／收益活動的即時推播、入帳解釋的即時歷史查詢（內建核准清單是空的）、錢包送出（模式 A 送出鈕仍鎖住）。
- 缺口（`credential_missing`、`partial`、`blocked`…）不是 0，也不是安全。逐格狀態見 docs/coverage-matrix.md；那份描述的是 `examples/monitor.json`，比 `init` 產生的設定多接了一些來源。
```

### 4.3 `docs/notifications.md`：放在第 1 段之前

```markdown
> 狀態：Telegram 送出只有 mock 測試，從未對真實 Telegram 驗證過（canary `NOT_RUN`）。預設 `outbox_only` 不送；`install-service` 產生的 systemd／cron 排程一律帶 `--no-send`，所以排程永遠不會送出。要送出，你必須自己設定 delivery profile，並自己排一個不帶 `--no-send` 的 `run-once`。
```

### 4.4 `docs/coverage-matrix.md`：放在第 7 行之後

```markdown
**Which profile these cells describe.** Cells describe `examples/monitor.json`. A profile written by `guardian init` routes fewer sources: no keyless or PRO Blockscout, no Alchemy, no Tenderly Base log route, no CoinGecko, no OFAC source, and no Helius route. Adapters that exist but are not called by `run-once` (Helius, GoPlus, Chainabuse) are not a keyed path in rc.4, even when a key is set.
```

---

## 5. 需要維護者確認的項目（我的推論與限制）

- **H2（代幣轉帳不會依金額告警）**是讀程式得出的結論，證據如下：
  - `quote_usd` 只在 `balances/reconcile.py:19–37` 設定，而且有 `is_native` 條件。
  - `--price-input` 只傳進 `_read_positions`（UXLINK 用）。
  - 轉帳候選事件的建構（`pipeline.py:2076–2090`）沒有帶報價。
  - 測試裡唯一的 E2E `asset_usd` 是 BSC 原生幣加上 CoinGecko（`tests/integration/test_balance_deltas.py:735–750`）；ERC-20 的 `asset_usd` 只出現在直接注入 `usd` 的政策單元測試。

  建議用一個 ERC-20 fixture 跑一次 `run-once` 確認。
- **H4（Solana 不解析交易、沒有轉帳告警）**的依據：`pipeline.py` 的 `_read_solana` 呼叫 `session.scan(..., fetch_transactions=False)`；整個 `src/guardian` 除了 adapter 自己，沒有任何地方匯入 `guardian.sources.helius`。coverage-matrix:57 記錄過 `getSignaturesForAddress` 有回應，但簽章清單不等於交易解析。
- **H3** 的依據：在 repo 外的獨立 venv 呼叫 `onboarding._build()` 組出 init profile，加上 `scan/drive.py:375–386`（只有 `BLOCKSCOUT_API_KEY` 的 PRO 來源會在沒有 key 時照樣呼叫）。沒有對外連線驗證。
- **「BSC publicnode getLogs 403」與「本機 12 步 gate」**這兩項來自使用者提供的事實。repo 裡沒有這兩項的紀錄，我無法獨立驗證。
- 沒有檢查 `docs/legacy-monitor-map.md`（1809 行）、`docs/mcm-01*.md`、`docs/reviews/*` 的逐行內容。我判斷它們是內部設計／審查紀錄，不是面向使用者的文案；用關鍵字 grep 掃過，沒有發現誇大用語。
