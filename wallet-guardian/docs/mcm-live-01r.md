# MCM-LIVE-01R：免費來源與可追趕掃描

這一輪把已經寫好的 NodeReal 與 Blockscout 讀取接到 `run-once --mcm-config`，並把日誌與歷史改成每輪掃到一個固定的目標區塊。它仍然只讀。不簽名、不組交易、不廣播。公開檔只放環境變數名稱，不放 key、不放真實地址。

Blockscout PRO 的 key 由使用者自己放在私人 secret profile 的 `BLOCKSCOUT_API_KEY`，請求時只放在 `Authorization: Bearer`，不放進 URL。註冊與每一家的缺口說明見 `docs/api-keys.md`。沒有 key 時 PRO 的 HTTP 402（要 key 或 X402）是 `credential_missing`，接著改走該鏈的 keyless instance；Base 免費方案的 featured-chain 402 是 `plan_not_supported`（寫進 capability cache，下一輪不重打），然後同樣改走 keyless。Cloudflare 的 HTTP 403 HTML（`Just a moment...`）是 `blocked` / `bot_challenge`，不是空錢包。沒有任何後備時才留下缺口，不把失敗讀成 complete。

## 來源

| 網路 | 主路徑 | 限制 |
|---|---|---|
| Ethereum、Arbitrum、Polygon | Etherscan V2 free | 沒有 `ETHERSCAN_API_KEY` 時改走 Blockscout keyless instance（有設定端點才呼叫）。RPC 讀狀態、receipt 與 token logs |
| BSC | NodeReal `nr_getAssetTransfers`，類別 `external` | 沒有 key 就是 `credential_missing`，沒有 keyless 的 BSC 原生歷史。token 歷史仍走公開 RPC logs。官方只列 BSC 與 Ethereum。方法被拒是 `method_not_found` 或 `unauthorized`，不能用 `eth_call` 猜 |
| Base、Optimism | Blockscout PRO，然後 keyless instance | PRO：`BLOCKSCOUT_PRO_API_BASE`（預設 `https://api.blockscout.com`）加 `chain_id`。Optimism（10）在免費方案。Base（8453）是 `plan_not_supported`。keyless 預設 `https://base.blockscout.com` 與 `https://optimism.blockscout.com` |
| Avalanche | 原生餘額照讀 | 深度歷史維持 `expansion_pending`。沒有重大曝險時，不用它擋住原來六條鏈 |

`plan_not_supported` 記在狀態目錄旁的 `capability-failures.json`（只有來源 id、網路、原因）。下一輪改走已核准的後備，不再重送注定失敗的請求。Blockscout PRO 對 Base 的 featured-chain 402 也走這條 cache。失敗的探針不代表這條鏈沒有資產。`x-credits-remaining` 與 `x-ratelimit-limit` 有出現在回應時，記在 `budget.providers.*.rate_headers`。

沒有 key 的跑法見 `examples/monitor.nokeys.json` 與 `docs/api-keys.md`。GoPlus 沒有 key 是 `credential_missing`，隱私拒絕把地址送出去的政策不變。CoinGecko 沒有 key 走公開無 key 層級，報價讀不到是 `valuation_unknown`，不是 0 也不是 1。Helius 沒有 key 時 parsed events 是 `credential_missing`，已設定的 Solana RPC 仍用。OFAC 公開清單不需要 key；`OFAC_SLS_FEED` 沒設是 `feed_not_supplied`。Alchemy 仍不在預設路由裡。

NodeReal 的 Base 產品頁存在，但連線位址要從使用者的 Dashboard 來。不要編造 `base-...` 主機名。Alchemy Transfers 只留介面（`guardian.sources.alchemy`），免費 `eth_getLogs` 目前一次 10 個區塊，預設不接進路由。它不是大範圍日誌的免費解法。

Blockscout Free 是每天 100K credits、5 RPS、不需信用卡。多數基本方法約 20 credits。100K credits 不是 100K 次請求。範例預算把每日請求上限放在 5000，留餘裕。付費方案不在這一輪，也不是預設。

第一次把地址交給新的 indexer 之前，報告的 `indexer_disclosures` 記下 provider、方法、用途，以及既有的 `third_party_wallet_queries` 同意政策。不順便查 GoPlus 或 Chainabuse。

## 日誌區間

每個 RPC 來源可以用 `log_range_max`（1 到 2000，省略時 2000）。區間被拒才二分。429 依 `Retry-After` 退避，秒數記在 `budget.backoff`。單一區塊仍然失敗就記 `single_block_unreadable:<block>`，游標不跨過它。

`eth_getLogs.address` 是發出事件的合約。監控中的錢包不能放進去。兩個錢包只在正確的 topic 位置做 OR，再在本地拆開。轉入看 topic 2，轉出看 topic 1，不把兩個位置 OR 在一起。`log_contract` 只有在它不是錢包時才當成合約篩選。

新來源的 token transfer 不能取代 approval。Approval 與 `ApprovalForAll` 仍走日誌。Permit 記在 `permission_notes`，`not_current_allowance` 是 true，不當成目前額度。

## 追趕

每一輪開始時凍結目標：高度、區塊 hash、時間戳，以及該鏈的 finality 文字。同一輪不再追新的 head。沒有游標時從 `polling.bootstrap_origin` 開始；省略則從 0，不是「最近 2000」。有游標時從游標加一、再減掉重疊 2。

每個完整成功的 chunk 在同一個資料庫交易裡寫入事件和連續前綴游標。後面的 chunk 失敗就留下前綴，報告是 partial，下一輪從那個前綴減掉重疊再繼續。不跳過缺口，也不把最後一頁的旗標當成整輪。

token 歷史、原生歷史、授權各自提交。`tokentx` 失敗不會清掉已經寫入的原生歷史。預算或 `round_deadline_seconds` 到了就停，剩下的後綴留到下一輪。

增量窗口完整（`incremental_window_complete`）只在 chunk 的聯集蓋住 origin 到 target 時成立。origin 大於 0 時同時標 `pre_origin_history_unknown`。那表示 origin 之前的授權沒有被查過，不是「舊授權不存在」。已知 pair 的目前 allowance 仍在同一個 checkpoint 讀。

餘額與 nonce 只是快照。+1 與 −1 可以淨值為零。收款不會增加收款人的 nonce。兩者都不能讓 `history.native_external` 變成 complete。

## Kinza 與 ERC-4626

`examples/monitor.json` 的 plugins 加上 `kinza` 與 `uxlink-holdings`。UXLINK 只讀公開的 `config/assets/uxlink.json` 與 `balanceOf`，不需要額外的私人資料。

Kinza 插件本身不改。同一輪裡，Pool、Provider 與準備金清單每個錢包都重讀；清單或實作變了就丟掉中介資料。`getReserveData`、`POOL`、`UNDERLYING_ASSET_ADDRESS`、`decimals` 可以在同一條鏈、同一個部署上共用。`balanceOf` 與 `getUserAccountData` 不進快取。兩個錢包的讀取分開計。抵押與借款為 0 時仍要讀供給，因為供給不一定是抵押。缺準備金是 `unknown`，不是 `none_observed`。

沒有設定 vault、也沒有先前部位的 ERC-4626 格子是 `unknown` / `vaults_not_configured`，`required` 是 false。不是 `none_observed`，也不是 `not_applicable`。已經看過的收據仍留在 required。

## 預算

`run-once` 的 `budget.providers` 有每個來源的邏輯方法數、HTTP 次數、credits、分頁與供應商回報的剩餘額度。Blockscout 基本方法按 20 credits 計。JSON-RPC batch 的 HTTP 是一次，方法數仍是裡面的每一筆。`budget.kinza` 有兩個錢包的 `wallets_read`、餘額讀取與共用命中，不含地址。

`tools/mcm_shadow_round.py` 的 `budget.json` 保留 `kinza_eth_calls` 等原有欄位，並把這一輪的 `budget` 放進 `run_once`。起始仍留約 30% 餘裕；要改設定先拿實測，不把付費方案當預設。
