# MCM-LIVE-01R：免費來源與可追趕掃描

這一輪把已經寫好的 NodeReal 與 Blockscout 讀取接到 `run-once --mcm-config`，並把日誌與歷史改成每輪掃到一個固定的目標區塊。它仍然只讀。不簽名、不組交易、不廣播。公開檔只放環境變數名稱，不放 key、不放真實地址。

Blockscout PRO 的 key 由使用者自己放在私人 secret profile 的 `BLOCKSCOUT_API_KEY`，請求時只放在 `Authorization: Bearer`，不放進 URL。註冊與每一家的缺口說明見 `docs/api-keys.md`。沒有 key 時 PRO 的 HTTP 402（要 key 或 X402）是 `credential_missing`，接著改走該鏈的 keyless instance；Base 免費方案的 featured-chain 402 是 `plan_not_supported`（寫進 capability cache，下一輪不重打），然後同樣改走 keyless。Cloudflare 的 HTTP 403 HTML（`Just a moment...`）是 `blocked` / `bot_challenge`，不是空錢包。沒有任何後備時才留下缺口，不把失敗讀成 complete。

## 來源

| 網路 | 主路徑 | 限制 |
|---|---|---|
| Ethereum、Arbitrum、Polygon | Etherscan V2 free | 沒有 `ETHERSCAN_API_KEY` 時改走 Blockscout keyless instance（有設定端點才呼叫）。RPC 讀狀態、receipt 與 token logs |
| BSC | NodeReal `nr_getAssetTransfers`，類別 `external` | 沒有 key 就是 `credential_missing`，沒有 keyless 的 BSC 原生歷史。token 與授權日誌走 `bsc-logs-nodereal`（`NODEREAL_BSC_URL`，`range_limit` 2000，預算 `nodereal-key`）再 `bsc-logs-48club`（`RPC_BSC_LOGS_URL`，`range_limit` 1000）。48club 沒有預設 URL、沒有文件、沒有 SLA，營運方是 MEV builder。dataseed 與 publicnode 不是 topic-only 日誌來源。官方只列 BSC 與 Ethereum。方法被拒是 `method_not_found` 或 `unauthorized`，不能用 `eth_call` 猜 |
| Base 轉帳 | Alchemy Transfers（有 `ALCHEMY_API_KEY` 與 `ALCHEMY_TRANSFERS_URL`），否則 Blockscout PRO 然後 keyless | 沒有 key 是 `credential_missing`，接著走 Blockscout。Base（8453）PRO 免費方案仍是 `plan_not_supported`。keyless 的 Cloudflare 403 仍是 `blocked` / `bot_challenge`。不把缺口改成非必要 |
| Base 日誌 | `tenderly_base`（`range_limit` 1000），然後 `base-1rpc`（50），然後 `base-logs-drpc`（100） | `base-1rpc` 與 `base-logs-drpc` 預設關閉。只有啟用且剩餘每日預算夠追趕才接上。Alchemy 免費 `eth_getLogs` 一次 10 個區塊，不是這條路徑 |
| Optimism | 日誌走 `op-logs-public`（`RPC_OPTIMISM_LOGS_URL`，`range_limit` 2000）。原生歷史走 Blockscout PRO，然後 keyless instance | PRO：`BLOCKSCOUT_PRO_API_BASE`（預設 `https://api.blockscout.com`）加 `chain_id`。Optimism（10）在免費方案。keyless 預設 `https://explorer.optimism.io`（`optimism.blockscout.com` 已 301 轉址到它）。publicnode 不是 topic-only 日誌來源 |
| Avalanche | 原生餘額照讀 | 深度歷史維持 `expansion_pending`。沒有重大曝險時，不用它擋住原來六條鏈 |

`plan_not_supported` 記在狀態目錄旁的 `capability-failures.json`（只有來源 id、網路、原因）。同一筆拒絕也涵蓋該來源的 `txlist` 與 `tokentx`：第一個拒絕之後，同一輪的下一個錢包與下一輪都不再打這家。快取帶 TTL（預設 24 小時），過期才再問一次。原因字串仍是 `plan_not_supported`。Blockscout PRO 對 Base 的 featured-chain 402 也走這條 cache。失敗的探針不代表這條鏈沒有資產。`x-credits-remaining` 與 `x-ratelimit-limit` 有出現在回應時，記在 `budget.providers.*.rate_headers`。日誌與 indexer 路由會跳過 `enabled: false` 的來源。端點登錄仍會登記它。`ofac_sls` 是例外：端點有值時 `run-once` 仍會抓名單。

沒有 key 的跑法見 `examples/monitor.nokeys.json` 與 `docs/api-keys.md`。GoPlus 沒有 key 是 `credential_missing`，隱私拒絕把地址送出去的政策不變。CoinGecko 沒有 key 走公開無 key 層級，報價讀不到是 `valuation_unknown`，不是 0 也不是 1。Helius 沒有 key 時 parsed events 是 `credential_missing`，已設定的 Solana RPC 仍用。OFAC 公開清單不需要 key；`OFAC_SLS_FEED` 沒設是 `feed_not_supplied`。Alchemy 沒有 key 時 Base 轉帳記 `credential_missing`（說明指向 `docs/api-keys.md#alchemy`），然後改走 Blockscout。不會因為缺 key 當掉，也不會把缺的能力標成 complete。

NodeReal 的 Base 產品頁存在，但連線位址要從使用者的 Dashboard 來。不要編造 `base-...` 主機名。Alchemy Transfers 讀的是轉帳，不是授權。`fromAddress` 與 `toAddress` 分開查，`pageKey` 跟到沒有為止，`fromBlock` / `toBlock` 是固定窗口。每一次 `alchemy_getAssetTransfers` 依公開價目記 120 CU，`credits_basis` 是 `estimate`。免費 `eth_getLogs` 一次 10 個區塊，不拿來掃 Approval。金鑰放在 URL 路徑裡，報告與錯誤摘錄會遮掉。Alchemy 遮 `/v2/<key>`。NodeReal 遮 `/v1/<key>`，主機前綴留著。`budget.providers` 同樣遮。

Blockscout Free 是每天 100K credits、5 RPS、不需信用卡。多數基本方法約 20 credits。100K credits 不是 100K 次請求。範例預算把每日請求上限放在 5000，留餘裕。付費方案不在這一輪，也不是預設。

第一次把地址交給新的 indexer 之前，報告的 `indexer_disclosures` 記下 provider、方法、用途，以及既有的 `third_party_wallet_queries` 同意政策。不順便查 GoPlus 或 Chainabuse。

## 日誌區間

每個 RPC 來源可以用 `log_range_max`（1 到 2000，省略時 2000）。`range_limit` 有設時，日誌寬度用它。BSC 的 `bsc-logs-nodereal` 是 2000，`bsc-logs-48club` 是 1000。OP 的 `op-logs-public` 是 2000。Base 的 `tenderly_base` 是 1000，`base-1rpc` 是 50，`base-logs-drpc` 是 100。`-32701` 加上 "specify an address"，以及 ≤1 個區塊的 `-32005` "limit exceeded"，改走下一個來源並記進 plan cache，下一輪跳過。範圍太大（"maximum 1000 blocks"、1rpc "blocks range"、dRPC "ranges over"）仍然對半切。單一區塊真的逾時仍是 `single_block_unreadable`，不跳過那個區塊。區間被拒才二分。轉入（topic 2）與轉出（topic 1）用同一組切點：只有兩邊都讀完的那個葉子才提交。一邊拒絕寬區間時，兩邊一起對半切，不會因為切點不同而留下永遠對不上的 `unread_gap`。429 依 `Retry-After` 真的等待，等待時間不超過這一輪剩下的期限；會超過期限的等待不睡，該能力以 `deadline` 結束。`budget.backoff` 分開記 `planned_wait_seconds` 與 `actual_wait_seconds`（`seconds` 是實際等待）。單一區塊仍然失敗就記 `single_block_unreadable:<block>`，游標不跨過它，也不改走下一個來源。來源整段失敗時，下一個來源只有在剩餘每日請求數夠追完剩下的區間才接上。

`eth_getLogs.address` 是發出事件的合約。監控中的錢包不能放進去。兩個錢包只在正確的 topic 位置做 OR，再在本地拆開。轉入看 topic 2，轉出看 topic 1，不把兩個位置 OR 在一起。`log_contract` 只有在它不是錢包時才當成合約篩選。

新來源的 token transfer 不能取代 approval。Approval 與 `ApprovalForAll` 仍走日誌。Permit 記在 `permission_notes`，`not_current_allowance` 是 true，不當成目前額度。

## 追趕

每一輪開始時凍結目標：高度、區塊 hash、時間戳，以及該鏈的 finality 文字。同一輪不再追新的 head。有游標時從游標加一、再減掉重疊 2。沒有游標時，明示的 `polling.bootstrap_origin`（含 0）優先。省略時起點是 `max(0, head - lookback_blocks)`。`lookback_blocks` 是 `ceil(bootstrap_lookback_days * 86400 / seconds_per_block)`。內建秒數：ethereum 12、bsc 0.75、arbitrum 0.25、base 2、optimism 2、polygon 2.1、avalanche 2，其餘 12。公開設定可以用可選的 `polling.seconds_per_block` 覆寫單一網路。這不是從創世區塊掃到現在。覆蓋範圍是這個 origin 到本輪 head，`complete_in_declared_scope` 只表示宣告範圍內讀完。origin 大於 0 時帶 `pre_origin_history_unknown`。

日誌仍依 `log_range_max`（預設 2000）切片。`etherscan_v2`、`blockscout`、`nodereal_transfers`、`alchemy_transfers` 在來源沒有自訂 `range_limit` 或 `log_range_max` 時，用一個 `[origin, head]` 區間。客戶端仍會在結果窗滿的時候二分。授權掃描用同一個 origin。

`polling.bootstrap_request_cap`（範例預設 2000）是每個來源、每一輪的 bootstrap 請求預算。Indexer 的分頁與二分要算，`eth_getLogs` 的每一段也要算；JSON-RPC batch 按裡面的 log 方法數計入同一個上限。數字在報告的 `budget.bootstrap_request_cap` 與 `budget.bootstrap_requests`。沒有寫 `polling.round_deadline_seconds` 時，這一輪的期限是 180 秒。有寫就用那個整數。碰到上限或期限就停，提交最後一個完整讀完的區塊前綴，該能力是 partial，原因是 `bootstrap_request_cap` 或 `deadline`，並帶 `historical_backfill_pending`。沒讀完的歷史不會報 complete。下一輪從那個游標加一、再減掉重疊 2 繼續。同一輪不會空轉。

每一輪先做有界的現況：餘額、最近一個 log 片段裡的授權（含目前 allowance），然後才做歷史回補。回補按能力各自留游標。Arbitrum 預設 30 天、0.25 秒一塊是 10,368,000 個區塊；2000 寬的切片約 5,182 段。Token 轉入加轉出是每段 2 次，授權日誌加 permit 探測是每段 2 次，一共約 20,728 次 log 讀取。只看預設上限 2000、期限沒有先把輪次截斷時，這段回補是 12 輪（12×2000 蓋過 20,728，並留每輪開頭的現況讀取）。若公開 RPC 慢到每秒 1 次、180 秒先把每一輪截斷，同一段約 116 輪（20,728 / 180）。沒有 key 的新資料庫每一輪仍會在上限與期限內返回。

每個完整成功的 chunk 在同一個資料庫交易裡寫入事件和連續前綴游標。後面的 chunk 失敗就留下前綴，報告是 partial，下一輪從那個前綴減掉重疊再繼續。不跳過缺口，也不把最後一頁的旗標當成整輪。

token 歷史、原生歷史、授權各自提交。`tokentx` 失敗不會清掉已經寫入的原生歷史。預算或 `round_deadline_seconds` 到了就停，剩下的後綴留到下一輪。

增量窗口完整（`incremental_window_complete`）只在 chunk 的聯集蓋住 origin 到 target 時成立。origin 大於 0 時同時標 `pre_origin_history_unknown`。那表示 origin 之前的歷史與授權沒有被查過，不是「舊授權不存在」，也不是從創世區塊就完整。已知 pair 的目前 allowance 仍在同一個 checkpoint 讀。

DoH 或系統解析的成功答案，在同一個 `HostnameResolver` 裡快取 `min(記錄 TTL, 300)` 秒。失敗與私有 IP 不快取。命中時仍做私有位址與 pinning 檢查。這是行程內、每個 resolver 實例一份，不是全行程共用。

餘額與 nonce 只是快照。+1 與 −1 可以淨值為零。收款不會增加收款人的 nonce。兩者都不能讓 `history.native_external` 變成 complete。

## Kinza 與 ERC-4626

`examples/monitor.json` 的 plugins 加上 `kinza` 與 `uxlink-holdings`。UXLINK 只讀公開的 `config/assets/uxlink.json` 與 `balanceOf`，不需要額外的私人資料。

Kinza 插件本身不改。同一輪裡，Pool、Provider 與準備金清單每個錢包都重讀；清單或實作變了就丟掉中介資料。`getReserveData`、`POOL`、`UNDERLYING_ASSET_ADDRESS`、`decimals` 可以在同一條鏈、同一個部署上共用。`balanceOf` 與 `getUserAccountData` 不進快取。兩個錢包的讀取分開計。抵押與借款為 0 時仍要讀供給，因為供給不一定是抵押。缺準備金是 `unknown`，不是 `none_observed`。

沒有設定 vault、也沒有先前部位的 ERC-4626 格子是 `unknown` / `vaults_not_configured`，`required` 是 false。不是 `none_observed`，也不是 `not_applicable`。已經看過的收據仍留在 required。

## 預算

NodeReal 的免費額度是一把 key 在所有主機上共用的每秒計算單位，不是每條鏈各算一份。範例把 `nodereal-bsc` 放在預算組 `nodereal-key`（`max_rps` 1、`max_concurrency` 1）。同一個 `budget_group` 的來源共用一個 token bucket。Ethereum 與 Optimism 的 RPC 來源在範例裡仍用各自的 `rpc-<chain>`：公開設定只有環境變數名稱，程式不會從主機名猜測它是不是 NodeReal。若 `RPC_ETHEREUM_MAINNET_URL` 或 `RPC_OPTIMISM_MAINNET_URL` 是你從 NodeReal Dashboard 複製的主機，把那個來源的 `budget_group` 改成 `nodereal-key`。超過額度時提供者回 429，監視器依 token bucket 與 Retry-After 在這一輪期限內等待並重試 `eth_chainId`。仍然讀不到是 partial，或 `identity_unreadable` 加上 `rate_limited`，不是 0，也不是 complete。會超過這一輪期限的等待不睡。

`run-once` 的 `budget.providers` 有每個來源的邏輯方法數、HTTP 次數、credits、分頁與供應商回報的剩餘額度。Blockscout 基本方法按 20 credits 計。JSON-RPC batch 的 HTTP 是一次，方法數仍是裡面的每一筆。`budget.kinza` 有兩個錢包的 `wallets_read`、餘額讀取與共用命中，不含地址。

`tools/mcm_shadow_round.py` 的 `budget.json` 保留 `kinza_eth_calls` 等原有欄位，並把這一輪的 `budget` 放進 `run_once`。起始仍留約 30% 餘裕；要改設定先拿實測，不把付費方案當預設。
