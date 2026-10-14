# 02 — 告警即時性與 Base／Optimism／BSC 覆蓋率：免費資料源查證與方案

- 對象：Wallet Guardian（GitLab main `16559ac`，套件版本 `0.4.0rc4`）
- 日期：2026-10-14（台北）
- 範圍：全程唯讀。沒有改 repo、沒有 commit、沒有送交易、沒有註冊帳號。報告裡沒有私人地址或 API key。
- 實測環境：使用者 Mac（darwin），2026-10-14 約 22:40–23:00（台北）用 curl 打公開免 key 端點。每個端點不超過 10 次。測試對象只用公開合約與燒毀地址：`0x…dEaD`、WETH 預部署 `0x4200…0006`、BSC 的 WBNB／USDT／USDC 合約、Multicall3 `0xcA11…CA11`。觀測到的鏈頭：BSC 127,611,973；Base 52,599,719；OP 158,195,004。
- 每條結論都標了證據來源：
  - **[讀碼 檔案:行]**：讀 repo 程式碼得到的結論。
  - **[repo 文件 檔案]**：repo 文件寫的內容。這是作者記下的結果，我沒有重測。
  - **[網路查證 URL 2026-10-14]**：今天查的官方文件或公告。
  - **[實測 curl 2026-10-14]**：今天在這台 Mac 上實際打出的請求與回應。
  - **[推論]**：由上面幾種證據推出來的，沒有直接證據。

---

## 1. 摘要

1. **「3 小時才告警」幾乎全部來自排程，資料源延遲是次要的。** 拆開看是三件事：
   - 每 3 小時一輪。
   - 23:20 到 08:20 有 9 小時完全不跑。
   - 排程本身由 Grok Bot 執行：Grok 額度用完，那一輪就不會跑。
   
   依現行時間表（08:20 到 23:20 共 6 輪）算，假設事件在一天內任何時間發生的機率相同：平均要等 **2.6 小時**，P90 是 **6.6 小時**，最壞 **9 小時**；只要漏掉一輪，最壞會到 12 小時。[推論，計算見 §4]
   
   已知的資料源延遲都在秒到分鐘級：NodeReal 查詢的 toBlock 要比鏈頭落後約 40 個區塊，BSC 上大約 30 秒 [repo 文件 docs/legacy-monitor-map.md]。「BSC internal tx 晚約 3 小時」剛好等於一個輪詢週期，**現有證據沒辦法把 indexer 延遲和輪詢間隔分開** [repo 文件 docs/balance-deltas.md]。

2. **Etherscan V2 免費方案確定不含 Base、OP、BSC。** 官方鏈表把 8453、10、56 標成 "Paid Tier Only"，只有 1 和 42161 等鏈是 "Free Tier Available" [網路查證 https://docs.etherscan.io/supported-chains 2026-10-14]。
   
   我用不帶 key 的請求實測：chainid 1、42161、137 回 `Missing/Invalid API Key`，chainid 8453、10、56 回 `Free API access is not supported for this chain`。也就是伺服器在檢查 key 之前，就先依方案把這三條鏈擋掉了 [實測 curl 2026-10-14]。用使用者自己的免費 key 再打一次可以最終確認，但我判斷結果不會不同。最便宜能用這三條鏈的是 Lite 方案，每月 $49，違反「不增加付費」的前提。

3. **BSC 的 `eth_getLogs` 有免 key 可用的端點，但不是官方的，也不是 publicnode。**
   - 官方 dataseed 系列（包括 `bsc.nodereal.io` 公開端點）連 1 個區塊都回 `-32005 limit exceeded`。官方文件也寫 "eth_getLogs is disabled" [實測 curl 2026-10-14][網路查證 https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/ 2026-10-14]。
   - publicnode 不接受只帶 topic、不指定合約的查詢，回 `-32701 Please specify an address`。改成指定 3 個代幣合約的陣列就回 HTTP 200 [實測 curl 2026-10-14]。
   - `rpc-bsc.48.club` 只帶 topic 查 1,000 個區塊，1.3 秒回 200；查 5,000 個區塊要 16 秒，也是 200 [實測 curl 2026-10-14]。但這個主機沒有任何文件、沒有服務保證，營運方是 MEV builder [網路查證 https://docs.48.club/privacy-rpc.md 2026-10-14]。
   - `1rpc.io/bnb` 一次最多 50 個區塊，而且免費版每天只有 200 次 [實測 curl][網路查證 https://www.1rpc.io/ 2026-10-14]。
   - 使用者已經有的 NodeReal key 文件上支援 getLogs，範圍可到 50,000 個區塊，每次 50 CU [網路查證 https://docs.nodereal.io/reference/eth-getlogs-bnb-chain、https://docs.nodereal.io/docs/compute-units-cus 2026-10-14]。我沒有 key 可測，要使用者驗證。

4. **Base 與 OP 的代幣轉帳和授權，可以用免 key 的公開 RPC 搭 getLogs 補齊。**
   - OP：`mainnet.optimism.io` 和 `optimism.gateway.tenderly.co` 只帶 topic 查 5,000 個區塊都 OK [實測 curl 2026-10-14]。
   - Base：`base.gateway.tenderly.co` 上限 1,000 個區塊，超過會回 "maximum 1000 blocks"；`base.drpc.org` 查 100 個區塊 OK。`mainnet.base.org` 的 getLogs 一律回 429，但查餘額和 nonce 正常 [實測 curl 2026-10-14]。

5. **原生幣的 internal 轉入：OP 有免 key 的來源，Base 和 BSC 沒有。**
   - OP：`explorer.optimism.io`（Blockscout）的 REST v2 `/internal-transactions` 回 200，回應標頭 `x-ratelimit-limit: 180`。但同一主機的 Etherscan 相容 `/api` 標頭是 `x-ratelimit-limit: 10`，reset 約 1,195,000（推測單位是毫秒，也就是大約 20 分鐘只能 10 次）。`txlistinternal` 還會回 `"Some internal transactions within this block range have not yet been processed"` [實測 curl 2026-10-14]。
   - Base：`base.blockscout.com` 從這台 Mac 打過去，會被 Cloudflare 擋下來（403 `Just a moment...`）[實測 curl 2026-10-14]。Routescan 對 8453、10、56 都回 `chain not supported` [實測 curl 2026-10-14]。
   - BSC：沒有 Blockscout 實例 [實測 研究代理 GET bsc.blockscout.com 回 404，2026-10-14]。NodeReal 的 debug 和 trace 只在付費方案 [網路查證 https://nodereal.io/api-marketplace/debug-bsc 2026-10-14]。
   
   所以 **Base 和 BSC 的 internal 轉入，在不帶 key 的情況下只能用「餘額差額」偵測：看得到金額，看不到原因。** BSC 可以用使用者已有的 NodeReal key，走 `nr_getAssetTransfers` 的 internal 類別。程式已經實作，但只有離線測資，沒有 live 驗證 [repo 文件 docs/coverage-matrix.md]。

6. **可以做到「高頻便宜探針，加上有變化才觸發完整掃描」，而且請求數幾乎不隨地址數增加。** 三條鏈都已部署 Multicall3（code 長度 3,808 bytes），我在 OP 上實測用一次 `eth_call` 就拿到 2 個地址的餘額 [實測 curl 2026-10-14]。
   
   每條鏈每輪的固定開銷是：
   - 1 次讀鏈頭；
   - 1 次 multicall 讀全部地址的餘額；
   - 1 次 nonce batch（可省略）；
   - 2 次 getLogs：一次查轉入，一次查轉出加授權，所有地址放在同一個 topic 位置做 OR。
   
   每 5 分鐘跑一輪、三條鏈合計，**每天約 4,300 次 HTTP，不管 N 是 1、5 還是 20**。算上 nonce 方法數，N=20 時是每天 20,736 個方法，細節見 §6。repo 已經有這個探針的設計文件，但還沒實作 [repo 文件 docs/balance-probe-design.md]。

7. **鏈上告警和 Grok 的隔離：**
   - **P0（拓撲）**：舊例行整輪交給 Grok Bot 執行。雲端主機沒有 systemd，也沒有可用的 cron [讀碼 environment_report.json]，所以額度一用完就沒有任何鏈上告警。
   - **P0（程式）**：如果把 Grok 產出的新聞檔用 `--news-input` 接進 `run-once`，而那個檔案不存在，`_optional_bytes` 會直接丟出 `GuardianError`，整輪在讀任何一條鏈之前就失敗 [讀碼 src/guardian/cli.py:612, 775-781]。
   - **P1（程式）**：`news.fetch` 打開時，新聞抓取發生在鏈上讀取之前，而且共用同一個 180 秒的期限。最多 8 個 GET，每個 20 秒逾時，最壞會吃掉 160 秒，鏈上掃描就被擠掉了 [讀碼 pipeline.py:198, 249-253, 265；fetch.py:27；transport.py:113；drive.py:28, 1279-1286]。網路例外則已經被包成 `TransportError` 隔離 [讀碼 transport.py:146-151、fetch.py:129-136]。

8. **現有程式碼裡兩個會讓日誌路徑卡死的問題，換端點之前必須先修。**
   - publicnode 的 `-32701`（「請指定 address」）被歸類成「範圍太大」，程式會一路對半切到單一區塊，最後記成 `single_block_unreadable`。
   - 遇到單一區塊讀不到時，程式不會改走下一個來源 [讀碼 scan/window.py:21, 98, 188；scan/drive.py:181, 341]。
   
   結果是：只要 BSC、Base 或 OP 的 RPC 環境變數指到 publicnode，`history.token` 和 `permissions.standard` 的游標就會永遠停住。

9. **夜間空窗：** MCM 的 schema 本來就規定 `around_the_clock` 必須是 true [讀碼 schemas/monitor.schema.json:78]，08:20 到 23:20 的時間窗只是舊例行的設定。如果改用 Mac 的 launchd，要注意兩種排程在睡眠時的行為不同 [網路查證 https://keith.github.io/xcode-man-pages/launchd.plist.5.html 2026-10-14]：
   - `StartInterval` 在睡眠期間「that interval will be missed」，會直接漏掉。
   - `StartCalendarInterval` 會在醒來後把錯過的次數合併成一次補跑（"coalesced into one event upon wake"）。
   
   但 Mac 睡著時兩種都不會執行。要不要讓 Mac 不睡，需要使用者決定。

**工作包清單**（細節見 §7）：

| 包 | 內容 |
|---|---|
| WP-1 | P0：鏈上告警脫離 Grok，含 news-input 缺檔不再讓整輪失敗 |
| WP-2 | P1：新聞抓取的時間與例外隔離 |
| WP-3 | P0：日誌路徑錯誤分類修正，加上 BSC／Base／OP 的免 key 日誌來源設定 |
| WP-4 | P0：實作 `guardian balance-probe`，含 Multicall3 與日誌探針，有變化才觸發 |
| WP-5 | P1：OP internal 改走 Blockscout REST v2，status 2 一律當成 partial |
| WP-6 | P1：launchd plist 產生器、睡眠政策、watchdog |
| WP-7 | P2：72 小時 shadow 期間的延遲度量 |
| WP-8 | P2：需要使用者 key 的 live 驗證（NodeReal BSC getLogs／internal、Blockscout PRO 的 OP 與 Base） |

---

## 2. 查證結果表

**欄位說明**
- 「能力」欄的縮寫：tx = 原生外部轉帳列表；tok = 代幣轉帳；int = internal 原生轉帳；logs = `eth_getLogs`；bal = 餘額與 nonce。
- 「實測」欄是我今天從 Mac 打的結果。沒測的寫「未測」。

| 提供者／端點 | 鏈 | 免 key | 能力 | 限制（文件） | 來源（2026-10-14） | 實測（2026-10-14，Mac） |
|---|---|---|---|---|---|---|
| Etherscan V2 免費 | 1、42161 可用；**8453、10、56 不可用** | 要免費 key | tx、tok、int、logs | Free 3 calls/s、10 萬/天；自 2026-07-01 起每次最多回 1,000 筆；2026-11-01 起 42161 改為共用免費池 | https://docs.etherscan.io/supported-chains 、https://docs.etherscan.io/resources/rate-limits 、https://docs.etherscan.io/changelog 、https://kb.etherscan.com/whats-changing-in-the-free-api-tier-coverage-and-why | 不帶 key 打 8453、10、56 回 `Free API access is not supported for this chain`；打 1、42161、137 回 `Missing/Invalid API Key` |
| BscScan API | 56 | — | — | 已併入 Etherscan V2；BNB Chain 部落格寫 "No free tier for BNB Chain after deprecation" | https://www.bnbchain.org/en/blog/migration-guide-bscscan-api-to-bsctrace-api-via-meganode | 見上一列（56 被擋） |
| Blockscout 鏈別實例 `explorer.optimism.io` | 10 | 是 | tx、tok、int（`/api` 與 `/api/v2`） | 文件寫預設每 IP 300 次/分；文件標示 per-instance API "WILL BE DEPRECATED JULY 1"（2026），**但今天仍可用**；v12 移除 `/internal-transactions` 的 `limit` 參數 | https://docs.blockscout.com/devs/apis/requests-and-limits 、https://www.blog.blockscout.com/news-july-1-is-pro-api-day/ 、https://docs.blockscout.com/blockscout-v12-0-0-api-updates-and-breaking-changes | `/api/v2/addresses/{WETH}/internal-transactions` 回 200，50 筆，有 next；標頭 `x-ratelimit-limit: 180`。`/api?action=txlistinternal` 回 200，但 `status 2`、"not yet been processed"；標頭 `x-ratelimit-limit: 10`、reset 約 1.19M → 1.11M（兩次相差約 82K；若單位是毫秒，和兩次呼叫的間隔同一個量級 [推論]）|
| Blockscout 鏈別實例 `base.blockscout.com` | 8453 | 是（文件） | tx、tok、int | 同上 | 同上 | **從 Mac 回 403 Cloudflare `Just a moment...`（`cf-mitigated: challenge`）**。研究代理用瀏覽器型抓取拿到 200，表示擋的是非瀏覽器客戶端。repo 記錄 Linux 主機 403、操作者 Windows PC 200 [repo 文件 docs/coverage-matrix.md] |
| Blockscout PRO（`api.blockscout.com`） | 文件寫 "All chains" | 要免費 key | tx、tok、int、logs | 免費每天 100K credits、5 RPS；預設 20 credits/次，token-transfers 30、logs 30、internal 40 | https://docs.blockscout.com/devs/pro-api 、https://docs.blockscout.com/devs/pro-api-responses-and-routes | 未測（要 key）。**有衝突**：repo 在 2026-10-09 用免費 key 記到 OP 200，但 Base 回 402 "requires one of the following plans" [repo 文件 docs/coverage-matrix.md]，和文件寫的 "All chains" 不一致 |
| Blockscout（BSC） | 56 | — | — | 沒有實例 | 研究代理實測 `bsc.blockscout.com` 404；chainscout 清單沒有 56 | 未測 |
| Routescan | 8453、10、56 | 是 | Etherscan 相容 | 不帶 key 2 req/s、1 萬/天 | https://routescan.io/docs/plans-and-limits/rate-limits | 三條鏈的 txlist、txlistinternal、tokentx 都回 `{"status":"0","message":"chain not supported"}` |
| NodeReal MegaNode Free | 56（以及同一把 key 能開的其他鏈） | 要免費 key（使用者已有） | logs（≤50K 區塊）；`nr_getAssetTransfers`（external、erc20、internal） | 定價頁寫 10M CU/月、150 CUPS，API 市集頁寫 100M CU、300 CUPS，**兩頁互相矛盾**；getLogs 50 CU、getBalance 15、eth_call 20、getTransactionCount 25、`nr_getAssetTransfers` 250；debug_trace 只在 "Growth+" | https://nodereal.io/pricing 、https://nodereal.io/api-marketplace/bsc-rpc 、https://docs.nodereal.io/reference/eth-getlogs-bnb-chain 、https://docs.nodereal.io/docs/compute-units-cus 、https://nodereal.io/api-marketplace/debug-bsc | 公開的 `bsc.nodereal.io` getLogs 回 `-32005 limit exceeded`。帶 key 的端點未測。repo 記錄免費 key 的 external 與 erc20 在 2026-10-09 live 成功，internal 只有離線測資 [repo 文件 docs/coverage-matrix.md] |
| BSC 官方 dataseed（bnbchain、defibit、ninicoin 等） | 56 | 是 | bal、eth_call；**沒有 logs** | 每 IP 10K/5 分鐘；getLogs 已停用 | https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/ | blockNumber 0.18 秒回 200；getLogs 0、5、100、1000 個區塊都回 `-32005 limit exceeded`；Multicall3 `getCode` 有 3,808 bytes |
| `bsc-rpc.publicnode.com` | 56 | 是 | bal；logs 必須帶 address | 文件沒寫 | https://bsc.publicnode.com/ | 只帶 topic → `-32701 Please specify an address…`；帶 3 個代幣合約陣列、1,000 個區塊 → 200 |
| `rpc-bsc.48.club` | 56 | 是 | logs（只帶 topic 也可以） | **沒有文件**；文件只列 `rpc.48.club`，用途是防夾子的 privacy RPC | https://docs.48.club/privacy-rpc.md | 只帶 topic：1,000 個區塊 1.28 秒回 200（2,637 筆）；5,000 個區塊 16.1 秒回 200（9,909 筆） |
| `bsc.rpc.blxrbdn.com`（bloXroute） | 56 | 是 | logs | 沒有文件 | — | 1,000 個區塊 14.2 秒回 200；5,000 個區塊 25 秒逾時 |
| `1rpc.io/bnb`、`/base`、`/op` | 56、8453、10 | 是 | logs ≤50 個區塊 | Public 200 次/天 | https://www.1rpc.io/ | 回 `eth_getLogs is limited to 0 - 50 blocks range`；49 個區塊 OK |
| Ankr 公開 `rpc.ankr.com/bsc` | 56 | 文件寫免註冊 | logs ≤1,000 | 約 1,800 req/min | https://www.ankr.com/docs/rpc-service/service-plans/ | **實測回 `Unauthorized: You must authenticate your request with an API key`**，和文件不一致 |
| BlockPI 公開 | 56 | 文件不清楚 | logs ≤1,024 | — | https://docs.blockpi.io/build/api-reference/bsc/eth_getlogs | HTTP 503 `Apikey not found` |
| dRPC 公開 | 56、8453 | 是 | logs | 免費方案 210M CU / 30 天；trace、debug、filter 停用 | https://blog.drpc.org/upcoming-changes-to-drpcs-free-plan-effective-june-1-2025/ | BSC：blockNumber 回 429 "Public endpoint rate limit"，getLogs 回 Alchemy 公開 API 的限流錯誤。Base：100 個區塊 OK；1,000 個區塊回 400 "ranges over 10000 blocks are not supported on free plan"，訊息和請求不符，只記觀察 |
| 其他 BSC 公開端點（meowrpc、nodies、blastapi、llamarpc） | 56 | — | — | — | — | meowrpc 回 "eth_getLogs is not supported"；nodies 回 403 要付費；blastapi 回 Alchemy 限流；llamarpc 連不上 |
| `mainnet.base.org` | 8453 | 是 | bal；logs 被擋 | "explicit rate limiting"，沒給數字；只支援 HTTP | https://docs.base.org/base-chain/flashblocks/faq 、https://docs.base.org/base-chain/api-reference/rpc-overview | blockNumber 200；getBalance 加 getTransactionCount 的 batch 回 200；**getLogs 查 10、1,000、5,000 個區塊都回 429 `request limit reached`** |
| `base.gateway.tenderly.co` | 8453 | 是 | bal、logs ≤1,000 | 公開 gateway 沒有文件（repo 引用的 https://docs.tenderly.co/node/rpc 今天回 404） | — | 1,000 個區塊 0.62 秒回 200；5,000 個區塊回 `Block range too large for public access: maximum 1000 blocks` |
| `base-rpc.publicnode.com` | 8453 | 是 | logs 必須帶 address | — | — | 只帶 topic → `-32701` |
| 其他 Base 公開端點 | 8453 | — | — | — | — | onfinality 回 429；blastapi 只能查 10 個區塊；meowrpc 不支援 getLogs；llamarpc 回 525 |
| `mainnet.optimism.io` | 10 | 是 | bal、logs | "rate-limited and not suitable for production use"，沒給數字 | https://docs.optimism.io/app-developers/reference/rpc-providers | 1,000、5,000 個區塊都回 200。**20,000 個區塊回 413「limited to a 10,000 range」，但 100,000 個區塊卻回 200**，前後矛盾，推測是後端負載均衡不一致；兩筆都原樣記錄 |
| `optimism.gateway.tenderly.co` | 10 | 是 | bal、logs | 沒有文件 | — | 5,000 與 20,000 個區塊都回 200 |
| `optimism-rpc.publicnode.com` | 10 | 是 | logs 必須帶 address | — | — | 只帶 topic → `-32701` |
| Alchemy Free | 1、8453、10、56 | 要免費 key | `alchemy_getAssetTransfers`；logs 只能查 **10 個區塊** | 30M CU/月；不含 trace | https://www.alchemy.com/pricing 、https://www.alchemy.com/reference/eth-getlogs | 未測 |
| QuickNode 試用 | 多鏈 | 要 key | logs 只能查 5 個區塊 | 試用 1 個月 | https://www.quicknode.com/pricing | 未測，不建議（試用期過了就沒有） |
| GoldRush（Covalent）、Moralis | — | — | — | GoldRush 只有 14 天試用；Moralis 沒有免費方案 | https://goldrush.dev/pricing/ 、https://docs.moralis.com/web3-data-api/evm/pricing-plans | 不採用 |
| Multicall3 `0xcA11…CA11` | 56、8453、10 | — | 一次 `eth_call` 讀 N 個餘額 | — | — | 三條鏈的 `getCode` 都是 3,808 bytes；在 OP 上用 `aggregate3(getEthBalance×2)` 回 200 |

**repo 之前已經測過的組合**（2026-10-09，我沒有重測）[repo 文件 docs/coverage-matrix.md:30-36, 42-56, 173-190]：
- NodeReal BSC 的 `nr_getAssetTransfers`（免費 key）：external 與 erc20 用 pageKey 分頁成功。
- Blockscout PRO 免費 key：鏈 1、42161、10 回 200；8453 與 137 回 402；56 與 43114 回 404 "Network not supported"。
- keyless 的 Base Blockscout：Linux 主機 403，Windows PC 200。
- 公開 getLogs：Tenderly Base 上限 1,000、1rpc 上限 50、`mainnet.base.org` 回 429、NodeReal ETH 與 OP 都可查 2,000 個區塊。
- `doctor` 對官方 BSC dataseed 記 `eth_getLogs: disabled_on_official_dataseed` [讀碼 provider_benchmark.json]。

**失敗原因怎麼歸類**：
- Etherscan：方案不含（`plan_not_supported`）。
- publicnode：要求指定 address（`-32701`），不是範圍問題。
- 官方 dataseed：方法被停用（`-32005`）。
- `mainnet.base.org`：限流（429）。
- `base.blockscout.com`：擋機器人（Cloudflare）。

---

## 3. 現行實作盤點

### 3.1 兩套系統要分開看

| 項目 | 舊私人例行（目前正在發告警） | MCM `guardian run-once`（shadow 中，不送出） |
|---|---|---|
| 排程 | 每 3 小時，08:20 到 23:20，由 Grok Bot 雲端執行 | `interval_seconds: 10800`，`around_the_clock` 是 schema 的 const true [讀碼 examples/monitor.json:21；schemas/monitor.schema.json:78]；systemd timer 或 cron 每 3 小時，`Persistent=false` [讀碼 deploy/systemd/guardian-mcm-shadow.timer] |
| Base／OP | publicnode，只查 nonce 和原生餘額 [repo 文件 docs/legacy-monitor-map.md:905-920] | logs 走 `tenderly_base`（1,000），然後 `base-1rpc`（50，預設關閉）；OP 的 logs 走 `RPC_OPTIMISM_MAINNET_URL`；原生歷史走 Alchemy，然後 Blockscout PRO，然後 keyless [讀碼 examples/monitor.json routing] |
| BSC | NodeReal `nr_getAssetTransfers`，只取第一頁（`maxCount 0x64`），toBlock 要落後約 40 個區塊；沒有授權掃描 [repo 文件 docs/legacy-monitor-map.md] | 原生歷史與 internal 走 NodeReal，有 pageKey 與獨立游標 [讀碼 scan/drive.py:544-561, 795-836]；token 與授權走 `rpc-bsc-mainnet` 的 getLogs [讀碼 examples/monitor.json:257-268] |
| internal | 只有 BSC（NodeReal） | **只有 BSC** [讀碼 scan/drive.py:797, 840, 865 都寫死 `!= "bsc-mainnet"`]；Blockscout adapter 雖然支援 `txlistinternal`，但 drive 沒有呼叫 [讀碼 sources/blockscout.py:105-111；scan/drive.py:562-576 只呼叫 txlist 與 tokentx] |
| 餘額差額告警 | 沒有 | 已實作 `balance_delta`，綁定 EIP-1898 區塊 hash [repo 文件 docs/balance-deltas.md] |
| 送出 | Grok Bot 的流程 | 預設 `outbox_only`；Telegram sender 是確定性程式碼，沒有 Grok [repo 文件 docs/notifications.md] |
| LLM 呼叫 | 整個流程都在 Grok 裡 | repo 裡 grep `grok|xai|openai|anthropic` 都沒有命中 [讀碼 src/guardian/**] |

### 3.2 新 provider 能不能直接塞進去

- **只換 getLogs 端點：只改設定就行。** `_log_plan` 只檢查 `enabled`、`adapter == "evm_rpc"` 與端點是否就緒，不查 capability catalog [讀碼 scan/drive.py:1358-1373]。在 `sources` 加一個 evm_rpc 來源，帶 `range_limit`，再加進 `history.token` 和 `permissions.standard` 的 routing 就生效。順序就是 fallback 順序；後面的來源只有在當天剩餘預算夠追完時才會接上 [讀碼 scan/drive.py:1376-1416]。
- **但兩個錯誤分類要先修，否則換了端點也沒用：**
  - `range_refusal` 把 `-32701` 當成「範圍太大」，因此會一路對半切 [讀碼 scan/window.py:21, 98, 188]。publicnode 的 `-32701` 其實是「請指定 address」，不管範圍多小都會被拒，最後一定掉到 `single_block_unreadable:<block>`。
  - `_token_walk` 和 `_approval_walk` 遇到 `single_block_unreadable` 就直接 `break`，**不改走下一個來源** [讀碼 scan/drive.py:181, 341]。
  
  反過來看：dataseed 回的 `-32005 limit exceeded` 不在 `_RANGE_NEEDLES` 清單裡，會被當成一般讀取錯誤，並在同一個 chunk 改走下一個來源（這個行為是對的）。1rpc 回的 "0 - 50 blocks range" 和 dRPC 回的 "ranges over 10000 blocks" 也都不在清單裡；1rpc 靠設定 `range_limit` 處理，dRPC 則被當成一般錯誤。
- **原生歷史與 internal 不能只改設定。** `history_external` 是依 adapter 分支 [讀碼 scan/drive.py:350-403、527-579]。Blockscout 的 internal 要同時改這些地方：
  - `drive.py`：新增 internal walk，並移除 `bsc-mainnet` 的寫死判斷；
  - `sources/blockscout.py`：處理 status 2；
  - `config/source-capabilities/blockscout.json`：`history.native_internal` 目前是 `unsupported`，而 router 只接受 `implemented` 或 `partial` [讀碼 sources/capabilities.py:103-106]；
  - `balances/internal.py`：正規化 Blockscout 回來的資料列。
- **keyless Blockscout 的速率**：drive 呼叫的是 `/api`（`account_history`）[讀碼 sources/blockscout.py:120-137]。OP 實測 `/api` 只有 `x-ratelimit-limit: 10`，`/api/v2` 是 180。`BlockscoutClient.walk()` 已經支援 `/api/v2/addresses/*` 的分頁 [讀碼 sources/blockscout.py:188-267]，能力 allowlist 也有這個路徑 [讀碼 config/source-capabilities/blockscout.json allowed_operations]，可以直接拿來用。

---

## 4. 延遲根因

把「事件發生到收到通知」拆成五段：

| 段 | 現況（舊例行） | 數量級 | 證據 |
|---|---|---|---|
| A. 排程間隔 | 白天每 3 小時 | 平均 1.5 小時，最壞 3 小時 | 使用者提供；[推論] |
| B. 夜間空窗 | 23:20 到 08:20，共 9 小時 | 落在夜間的事件平均要等 4.5 小時，最壞 9 小時 | 使用者提供；[推論] |
| C. 執行器失敗 | Grok 額度用完就跳過一輪 | 再多 3 小時；如果跳過的是 23:20 那一輪，最壞 12 小時 | 使用者提供；雲端主機 `systemd: offline`、`cron_daemon: not_available_as_bot_scheduler` [讀碼 environment_report.json] |
| D. 資料源能力 | Base 與 OP 只看 nonce 和餘額；BSC 沒有授權日誌；Etherscan 免費版不含這三條鏈 | 這不是延遲問題，而是**完全看不到**：沒查的就永遠不會告警 | §2 |
| E. Indexer 延遲 | NodeReal 的 toBlock 要落後約 40 個區塊（約 30 秒）；Blockscout internal 有 status 2 | 秒到分鐘級；Blockscout internal 的實際延遲不明 | [repo 文件 legacy-monitor-map.md]；[實測 curl] |

用現行時間表算（每天 6 輪，間隔是 5 段 3 小時加 1 段 9 小時），假設事件在一天內任何時間發生的機率相同：
- 平均等待 = Σ間隔² ÷（2×24）=（5×9 + 81）÷ 48 = **2.63 小時**。
- 等超過 3 小時的機率 = 6 ÷ 24 = **25%**。
- P90：解 (9 − d) ÷ 24 = 0.1，得 d = **6.6 小時**。
- 最壞 **9 小時**，這還沒算每輪本身的執行時間。

[推論：純算術]

改成全天每 3 小時（也就是 72 小時 shadow 用的節奏）：平均 1.5 小時，最壞 3 小時。這只解決了 B，沒有解決 A。

改成每 5 分鐘一次探針、有變化就觸發掃描：平均約 2.5 分鐘加上觸發掃描的時間（期限 180 秒），P95 約 4.75 加 3，**不到 8 分鐘**。[推論]

**「BSC internal 晚約 3 小時」的歸因**：repo 文件寫的是「patrol 約三小時後回報」，而且舊例行的 BSC 路徑確實查了 internal 類別 [repo 文件 docs/balance-deltas.md「What this closes」]。這和 A 段完全一致；沒有證據顯示是 NodeReal 延遲。在 5 分鐘探針之下，8.4 BNB 這類轉入會先由餘額差額在 5 分鐘內告警（原因不明），接著觸發的掃描再用 NodeReal internal 補上原因。[推論]

---

## 5. 方案

### 5.1 方案比較（由簡到繁）

| # | 方案 | 必須成立的假設 | 怎麼快速驗證 | 複雜度 | 效果 |
|---|---|---|---|---|---|
| 1 | 只改排程：舊例行改成全天跑，並交給 launchd 或 cron，不再由 Grok 觸發 | 舊的私人腳本能不靠 Grok 單獨執行並送出通知 | 在 Mac 上用 launchd 跑一輪 `--dry-run` | 低（不改 repo，操作者自己處理） | 去掉 B 和 C；A 還是 3 小時 |
| 2 | 兩層迴圈：每 5 分鐘一次探針（Multicall 讀餘額、topic OR 查 getLogs），有變化才觸發 `run-once` | 公開 RPC 穩定接受 topic-only 的 getLogs 與 `eth_call`（今天實測 OK）；Mac 保持清醒 | WP-4 驗收：24 小時量測偵測延遲 | 中 | P95 ≤ 10 分鐘 |
| 3 | 推播：WSS `eth_subscribe`（newHeads 與 logs）常駐 | 公開 WSS 接受 topic-only 訂閱；transport 要新增 WS 協定 | 未測 | 高（目前 transport 只有 HTTP [讀碼 sources/transport.py:103-157]；需要常駐程序與斷線重連） | 秒級 |
| 4 | 付費 indexer（Etherscan Lite $49/月） | — | — | 低 | 違反前提，不採用 |

建議：**先做 1（立刻見效、零程式），主力做 2，暫不做 3。**

### 5.2 兩層迴圈設計

```
launchd（Mac）每 5 分鐘：guardian balance-probe --mcm-config M --probe-state P
  對每條鏈：
    1. 讀 head（latest − k；k：BSC 3、Base／OP 3）→ 取得 number 與 hash
    2. 用 eth_call 呼叫 Multicall3.aggregate3(getEthBalance × N)，綁定該 blockHash（EIP-1898）
    3.（選用）用一個 batch 呼叫 eth_getTransactionCount × N，同樣綁定 blockHash
    4. eth_getLogs 查轉入：topics = [Transfer, null, [w1..wN]]，範圍 (cursor, head]
    5. eth_getLogs 查轉出與授權：topics = [[Transfer, Approval, ApprovalForAll, Permit2 事件], [w1..wN]]
    6. 有任何一項變化 → 寫一列觸發紀錄（鏈、區塊、原因）→ 對該鏈跑一次 run-once（受 deadline 限制）
       都沒變化 → 只推進探針游標，不寫入 outbox
  讀取失敗 → 不推進游標、不寫 0、不觸發（下一輪再試）；連續失敗 K 次 → 交給 watchdog
launchd 每 3 小時：guardian run-once（不動，72 小時 shadow 照原計畫）
launchd 每 15 分鐘：guardian watchdog
```

**設計重點：**
- 地址在 topic 位置做 OR，餘額走 Multicall，所以每輪成本和 N 幾乎無關。第 3 步的 nonce 會讓方法數隨 N 增加，但 HTTP 次數仍然是 1 次。
- ERC-721 的 Transfer topic0 和 ERC-20 相同，所以查轉入也會一併抓到 NFT。[推論]
- repo 的合約已經禁止把錢包放進 `address` 欄位，地址只能出現在 topic [讀碼 scan/window.py:110 `reject_wallet_emitter`]。
- 探針**不能推進** `history.native_external` 或 `history.native_internal` 的游標，要用自己獨立的游標 [repo 文件 docs/balance-probe-design.md]。
- 探針和 shadow 的 `run-once` 共用 state 目錄時，會撞到 `sole_writer_lock` 並得到 `shadow_round_locked` [讀碼 shadow_recorder.py:122-142]。72 小時 shadow 期間，**探針要用獨立的 state 目錄**，只做量測；shadow 結束後再整合。
- 告警語意不變：
  - 餘額差額達門檻（0.5 ETH、1 BNB）時，探針觸發的 run-once 會走既有的 `balance_delta` 加 reconcile。
  - 授權類 log 一出現就觸發。授權沒有金額門檻，是最需要快的一類。
  - 代幣約 1,000 美元的門檻需要報價：CoinGecko 免 key；拿不到報價就是 `valuation_unknown`，不是 0。

### 5.3 每條鏈：做得到與做不到

| 能力 | BSC | Base | OP |
|---|---|---|---|
| 原生餘額與差額（探針） | ✅ dataseed，免 key，有文件 | ✅ `mainnet.base.org`（餘額可用） | ✅ `mainnet.optimism.io` |
| 代幣轉入／轉出（logs） | ✅ NodeReal（使用者的 key，待驗）；免 key 用 `rpc-bsc.48.club`（無文件）；備援 publicnode 帶代幣合約清單（只涵蓋前 K 個代幣） | ✅ Tenderly gateway（≤1,000，無文件）；備援 `base.drpc.org`（100 個區塊 OK） | ✅ `mainnet.optimism.io`、Tenderly |
| 授權（Approval、ApprovalForAll、Permit2 logs） | ✅ 同 logs | ✅ 同 logs | ✅ 同 logs |
| 原生外部 tx 列表 | ✅ NodeReal external（使用者已有 key，repo 已 live） | ❌ 免 key 拿不到（Blockscout 被 CF 擋、Etherscan 要付費、Routescan 不支援）。替代：nonce 變化加餘額下降可以知道「有送出交易」，但拿不到 tx 細節；可選註冊 Alchemy 免費 key | ✅ Blockscout `explorer.optimism.io` v2（免 key）；Blockscout PRO 免費 key |
| internal 原生轉入 | ⚠️ NodeReal internal（key；只有離線測資）；否則只能靠餘額差額，原因不明 | ❌ 只能靠餘額差額，原因不明 | ✅ Blockscout v2 `/internal-transactions`（status 2 要當 partial） |
| trace／debug | ❌ 免費方案都沒有 | ❌ | ❌ |

### 5.4 隔離

| 等級 | 問題 | 證據 | 修法 |
|---|---|---|---|
| **P0** | 舊例行的鏈上掃描和送出都在 Grok 裡；Grok 額度一用完，所有告警都沒了 | 使用者描述；雲端主機沒有排程器 [讀碼 environment_report.json] | WP-1：鏈上路徑交給 launchd 或 cron 加確定性 sender；Grok 只負責產生新聞檔 |
| **P0** | 用 `--news-input <Grok 產出的檔>`，檔案不存在時整輪失敗 | `_optional_bytes` 在 `OSError` 時丟出 `GuardianError` [讀碼 cli.py:775-781]，而且在 pipeline 開始之前就呼叫 [讀碼 cli.py:612] | WP-1：檔案不存在或讀不到 → `news_not_supplied` gap，繼續跑 |
| **P1** | `news.fetch: true` 時新聞抓取在鏈上讀取之前，而且共用 180 秒期限 | pipeline.py:198 先取 `started_at`，249-253 抓新聞，265 才建立 `RoundBudget`；drive.py:1279-1286 用同一個 `started_at` 算期限；最多 8 個 GET [讀碼 fetch.py:27]，每個逾時 20 秒 [讀碼 transport.py:113] | WP-2：新聞移到鏈上 commit 之後，或給新聞自己的期限 |
| **P1** | 新聞的後處理（`_apply_fetched_coverage`、`link_exposure`）如果丟出非 GuardianError 的例外，會在 `cycle.finish` 之前中斷，這一輪的鏈上告警就沒有寫進 outbox | 執行順序：351 行新聞後處理、398 行才 `cycle.finish` [讀碼 pipeline.py:351-398] | WP-2：先提交鏈上 cycle，新聞告警再另外提交；例外 → `exposure_news` gap |
| 已隔離 | 新聞網路錯誤 | httpx 例外會被包成 `TransportError`（屬於 GuardianError），並在每個 feed 內被接住 [讀碼 transport.py:146-151、fetch.py:129-136] | — |

**預設設定下的影響**：`examples/monitor.json` 沒有 `news` 鍵，所以預設不會抓新聞 [讀碼 pipeline.py:675-678]。P1 那兩項只在私人設定開啟新聞時才會發生。

### 5.5 夜間空窗

1. 主觸發改用 Mac launchd，探針和完整掃描都用 `StartCalendarInterval`。睡眠期間錯過的次數會在醒來後合併成一次補跑；`StartInterval` 則會直接漏掉 [網路查證 launchd.plist(5) 2026-10-14]。
2. Mac 睡著時兩種都不會跑，所以夜間覆蓋取決於 Mac 是否保持清醒。這需要使用者決定，見 §8。
3. 雲端每 3 小時一輪的例行（或改成全天）保留下來當獨立的第二層：不同主機、不同資料源。同一台主機上的 watchdog 看不到「整台主機掛掉」[repo 文件 docs/scheduling.md「common_cause」]。
4. 補跑的那一輪不算準時的 slot，這個 shadow 語意不變 [repo 文件 docs/scheduling.md]。

---

## 6. 請求預算

### 6.1 探針（三條鏈合計，主要端點各查 1 段 logs）

每條鏈每輪：

| 類別 | 項目 | 數量 |
|---|---|---|
| HTTP | head + multicall + nonce batch + 2 次 logs | 5 次 |
| 方法 | head + multicall + N 個 nonce + 2 次 logs | 4 + N 個 |

每 5 分鐘新增的區塊數：BSC 約 400 個（0.75 秒一塊），Base 與 OP 各約 150 個。都在 1,000（Tenderly、48club）和 2,000（`LOG_RANGE_MAX` [讀碼 chains/evm/adapter.py:27]）以內，所以一段就夠。

| 間隔 | 輪／天 | N=1 方法／天 | N=5 | N=20 | HTTP／天（和 N 無關） |
|---|---|---|---|---|---|
| 1 分 | 1,440 | 21,600 | 38,880 | 103,680 | 21,600 |
| 2 分 | 720 | 10,800 | 19,440 | 51,840 | 10,800 |
| **5 分（建議）** | 288 | **4,320** | **7,776** | **20,736** | **4,320** |
| 15 分 | 96 | 1,632 | 2,784 | 7,104 | 1,632 |

不讀 nonce 的話，方法數就等於 HTTP 數，完全和 N 無關。[推論：計算]

### 6.2 對照各家額度（5 分鐘間隔）

- **BSC dataseed**（只讀 head、multicall、nonce）：每 5 分鐘 3 次 HTTP，額度是 10K/5 分鐘 [網路查證 docs.bnbchain.org]，用量約萬分之三。✅
- **BSC logs 走 48club**：每天 576 次（288 × 2）。沒有公開額度，屬於風險項。⚠️
- **BSC logs 走 1rpc 備援**：50 個區塊一段，每輪要 8 段 × 2 = 16 次，每天 4,608 次，**遠超過 200 次/天的上限**。❌ 只能當緊急備援。
- **BSC 全部走 NodeReal**（使用者的 key）：
  - 探針每輪 15（head）+ 20（multicall）+ 25N（nonce）+ 100（2 次 getLogs）= 135 + 25N CU。
  - 每月探針：N=1 是 1.38M CU，N=5 是 2.25M，N=20 是 5.49M。
  - 每 3 小時的完整掃描：每個錢包 6 次 `nr_getAssetTransfers`（external、erc20、internal 各分 from 與 to），× 250 CU × 8 輪 × 30 天 = 每錢包每月 0.36M CU。N=20 是 7.2M。
  - 合計：N=1 是 1.74M，N=5 是 4.05M，N=20 是 **12.7M**。如果免費額度是 10M（定價頁），**N=20 會超過**；如果是 100M（市集頁）就足夠。
  - 建議：探針走 dataseed 加 48club，NodeReal 只用在完整掃描與觸發掃描。這樣 N=20 只有約 7.2M，再加上觸發掃描。⚠️ [推論：計算；CU 單價來自網路查證]
- **Base**：
  - 餘額與 nonce 走 `mainnet.base.org`，每天 864 次（288 × 3），額度沒有公開數字。⚠️
  - logs 走 Tenderly，每天 576 次，額度沒有公開數字。⚠️
- **OP**：全部走 `mainnet.optimism.io`，每天 1,440 次（288 × 5）。文件寫 "not suitable for production"。⚠️ 備援用 Tenderly。
- **OP 的 Blockscout v2**（只在觸發掃描與每 3 小時的完整掃描用）：每個錢包每次 3 個端點，多頁另計。180/分鐘的標頭額度足夠。但不能用 `/api`（10 次 / 約 20 分鐘）跑高頻。

### 6.3 不同 N 可行的頻率（結論）

| N | 免 key 端點（只看請求數） | 建議 |
|---|---|---|
| 1 | 1 分鐘也可行 | 5 分鐘 |
| 5 | 1 分鐘也可行 | 5 分鐘 |
| 20 | 2 分鐘可行；如果 BSC 要走 NodeReal，CU 只夠 5 分鐘以上（10M 方案時） | 5 分鐘 |

建議統一用 5 分鐘，理由如下：
- 公開端點沒有服務保證，打太密容易被限流。
- Mac 的電量與網路考量。
- 延遲目標「P95 ≤ 10 分鐘」已經足夠，再縮短效益很小。

另一個限制是 topic OR 清單的長度上限。N=20 在主流節點通常可以，但沒有查到文件，要在 WP-4 實測。[推論]

---

## 7. 工作包（給 Cursor 實作）

這些工作包的共同前提：
- 遵守 AGENTS.md：唯讀、不送交易、不開公開 port、不開 webhook、不把 key 寫進 repo。
- 新增或更換來源前，要在 `indexer_disclosures` 揭露（因為錢包地址會出現在 topic 和 eth_call 參數裡）。
- 讀取失敗時，一律不寫 0、不標 complete。
- 每包完成後跑 `uv run pytest -q`，並走 dual-review。

### WP-1（P0）鏈上告警脫離 Grok

- **目標**：Grok 額度用完或新聞檔不存在時，鏈上告警照常寫進 outbox；cutover 之後照常送出。
- **範圍**：
  - `src/guardian/cli.py`：`--news-input` 的檔案不存在或讀不到時，傳 `None` 加上 `news_unreadable` 原因給 pipeline，不再丟例外。
  - `src/guardian/pipeline.py`：把上述原因記成 `exposure_news` gap。
  - `docs/guide-grok-bot.md`、`docs/scheduling.md`：Grok 改成「另一個工作負責寫新聞檔，run-once 有檔就讀、沒檔就記 gap」。
  - 操作者清單（不在 repo 裡）：舊私人腳本改由 launchd 或 cron 直接執行，並直接送 Telegram。
- **不做什麼**：不在 repo 新增 Grok 用戶端；不自動打開 sender；不改 `outbox_only` 預設；不動私人腳本。
- **驗收**：
  1. `run-once --news-input /不存在路徑 --no-send` 的結束碼不是 1，`status` 不是 `failed`，鏈上 coverage 和 outbox 與「不帶 `--news-input`」時一致（同一組 fixture 比對 diff 為空）。
  2. 新聞檔內容是垃圾資料時，記成 `news_unparseable` gap，鏈上告警數不變。
- **回歸測試**：既有 `tests/integration` 中 news-input 的正常案例照樣通過。
- **負面測試**：
  - 新聞檔權限是 000 → gap。
  - 新聞檔路徑是目錄 → gap。
  - 新聞檔超大 → 被拒，記成 gap，不讓整輪崩潰。
- **風險**：私人例行的部分 Cursor 碰不到，要操作者自己執行並驗證一輪。

### WP-2（P1）新聞抓取的時間與例外隔離

- **目標**：新聞的延遲或任何例外，都不會讓鏈上掃描少讀，也不會讓鏈上告警消失。
- **範圍**：`src/guardian/pipeline.py`。
  - 二擇一：
    - (a) `fetch_allowlisted_news` 移到鏈上 `cycle.finish` 之後，新聞告警分開提交；
    - (b) 保留原位置，但鏈上的期限從新聞結束後才開始算，新聞另外給 `news_deadline_seconds`（預設 30 秒）。
  - 整個 `fetch_allowlisted_news(...)` 呼叫也要包起來：它在進入每個 feed 的 `except GuardianError` 之前，還會跑 `_load_state`、`_restore`、`_registry`、`BudgetBook.from_document` [讀碼 news/fetch.py:80-106]。
  - 新聞後處理包在 `try/except Exception`，例外 → `exposure_news` gap，並在 `news_health` 記錄原因。
  - `src/guardian/news/fetch.py`：接受一個期限，GET 之間檢查剩餘時間。
- **不做什麼**：不改新聞來源清單；不改 dedup；不改 alert policy。
- **驗收**：用假 backend 讓 8 個新聞 GET 各睡 20 秒（注入 clock）。鏈上掃描的 `reason_codes` 不能出現 `deadline`，outbox 的鏈上告警數和關閉新聞時一致。
- **回歸測試**：既有 news 與 exposure 測試。
- **負面測試**：
  - `parse_feed` 丟出 `RuntimeError` → 鏈上告警照樣寫入，並記一筆 `exposure_news` gap。
  - `link_exposure` 丟出例外 → 同上。
- **風險**：(a) 會改變新聞告警和鏈上告警在同一輪的提交順序，要確認 dedup key 不受影響。

### WP-3（P0）日誌路徑錯誤分類修正，加上 BSC、Base、OP 免 key 日誌來源

- **目標**：BSC、Base、OP 的 `history.token` 和 `permissions.standard` 不再因為端點政策卡在 `single_block_unreadable`，而且能自動改走可用的來源。
- **範圍**：
  - `src/guardian/scan/window.py`：
    - `-32701` 加上 "specify an address" 的回應，改分類成 `filter_requires_address`（來源不可用），不要對半切。
    - `-32005 "limit exceeded"` 出現在 ≤1 個區塊的請求時，分類成 `method_disabled`。
    - 補上 1rpc 的 "blocks range" 和 dRPC 的 "ranges over" 字樣。
  - `src/guardian/scan/drive.py:181, 341`：上面兩種分類 → 改走下一個來源，並把 plan cache 記成 `plan_not_supported` 或新原因，下一輪直接跳過這個來源。
  - `examples/monitor.json`、`examples/monitor.nokeys.json`：新增 evm_rpc 來源：
    - `bsc-logs-nodereal`：`endpoint_env NODEREAL_BSC_URL`，`budget_group nodereal-key`，`range_limit 2000`；
    - `bsc-logs-48club`：`endpoint_env RPC_BSC_LOGS_URL`，`range_limit 1000`；
    - `op-logs-public`：`endpoint_env RPC_OPTIMISM_LOGS_URL`，`range_limit 2000`；
    - `base-logs-drpc`：`range_limit 100`，預設關閉；
    - 並更新 routing。
  - `config/source-capabilities/evm_rpc.json` 的 `limits` 加上今天的實測與來源 URL。
  - `docs/coverage-matrix.md` 的公開 getLogs 表、`docs/api-keys.md` 也要同步更新。
- **不做什麼**：
  - 不把 dataseed、publicnode 當 topic-only 的日誌來源。
  - 不放進任何帶 key 的 URL。
  - 不把 48club 設成預設端點：只在範例裡寫環境變數名稱，並註明「沒有文件、沒有服務保證，營運方是 MEV builder」。
- **驗收**：
  1. 離線 fixture：第一個來源回 `-32701` 時，對它的請求數 ≤ 1，並在同一個 chunk 改走第二個來源，最後 coverage 是 `complete_in_declared_scope`。
  2. 回 `-32005` 也同樣處理。
  3. live（操作者在 Mac 上，用公開地址 `0x…dEaD`）：`run-once --no-send` 時，BSC、Base、OP 的 `history.token` 都不出現 `single_block_unreadable`，`budget.providers` 裡沒有任何一次 getLogs 的範圍超過該來源的 `range_limit`。
- **回歸測試**：`tenderly_base`、`base-1rpc` 既有的 fallback 測試；真正因為範圍太大被拒（例如 "maximum 1000 blocks"）時，仍然要對半切。
- **負面測試**：
  - 所有來源都拒絕 → 這個能力是 `blocked` 並帶原因，游標不前進，不是 `empty`。
  - 單一區塊真的讀不到（例如逾時）→ 仍然記 `single_block_unreadable`，不跳過那個區塊。
- **風險**：
  - 48club 或 Tenderly 隨時可能改政策，所以要有兩個以上的來源，並由 watchdog 監看 `blocked`。
  - **key 遮罩**：`redact()` 只遮 query 參數、Bearer、Telegram token 與 Alchemy 的 `/v2/<key>`，**沒有 NodeReal 的 `/v1/<key>` 樣式** [讀碼 security/redact.py:7-16]。新增重用 `NODEREAL_BSC_URL` 的 evm_rpc 來源之前，要先補一條遮罩規則和測試（確認錯誤訊息與 `budget.providers` 裡沒有完整 URL）。

### WP-4（P0）`guardian balance-probe`：Multicall3 與日誌探針，有變化才觸發

- **目標**：從鏈上事件到 outbox 寫入，P95 ≤ 10 分鐘、最壞 ≤ 15 分鐘（不含主機睡眠或離線）。
- **範圍**：
  - 新增 `src/guardian/scan/probe.py`；
  - `src/guardian/cli.py` 新增子指令 `balance-probe --mcm-config --probe-state --no-send [--trigger run-once|none]`；
  - `src/guardian/chains/evm/` 新增 Multicall3 `aggregate3(getEthBalance)` 的編解碼，固定地址 `0xcA11…CA11`；每條鏈先用 `eth_getCode` 確認地址上有程式碼，沒有就退回逐一 `eth_getBalance`；
  - `src/guardian/balances/native.py` 重用既有的 reducer 與版本化 baseline；
  - `src/guardian/storage.py` 新增探針游標表與觸發紀錄表（附 migration）；
  - 加一把獨立的 lock，不能共用 `.shadow-round.lock`；
  - `docs/balance-probe-design.md` 從「設計」改成「實作」。
- **不做什麼**：
  - 不改 3 小時的 `run-once` 和 shadow slot 語意。
  - 不推進 `history.*` 的游標。
  - 不啟動 sender。
  - 不加 WS。
  - 探針本身不寫 alert，只負責觸發 `run-once`。
- **驗收（可量測）**：
  1. 離線：沒有變化時，每條鏈每輪的 HTTP ≤ 5、方法數 ≤ 4 + N，而且不觸發。
  2. 離線：餘額改變、出現 Transfer log、或出現 Approval log，三種情況都會觸發，而且只觸發對應的鏈。
  3. 離線：head、multicall 或 logs 任一失敗 → 不觸發、不推進游標、不寫 0。
  4. live 量測：在獨立的 state 目錄觀察公開高活躍合約（Base／OP 的 `0x4200…0006`、BSC 的 WBNB，用 `--trigger none`）24 小時。對每個被偵測到的變化，計算偵測延遲 = 探針提交時間 − 事件所在區塊的 timestamp。要求 P95 ≤ 間隔 + 60 秒、429 比例 < 1%、總請求數不超過 §6 表格 ±20%。
  5. 端到端（cutover 之後，或在 shadow 中比對）：用 `compare-legacy` 把同一事件的舊例行告警時間和 MCM outbox 時間配對，P95 要比舊例行快 ≥ 2 小時。
- **回歸測試**：`run-once` 的 balance_delta 既有測試；shadow 的 24 窗資格測試（`tests/integration/test_mcm_shadow.py:359`）。
- **負面測試**：
  - multicall 回 `allowFailure` 的 false 項目 → 把那個錢包標成讀取失敗，不能當成 0。
  - head 的 hash 改變（reorg）→ 下一輪重算，不重複觸發。
  - 探針和 run-once 同時執行 → 其中一方拿不到鎖就跳過，而且留下紀錄。
- **風險**：
  - 某些節點對 topic OR 的清單長度有上限（未查到文件）。
  - 高活躍地址會一直觸發，測試時必須用 `--trigger none`。
  - Multicall3 是外部合約，要固定地址，並在每條鏈驗證過 code hash。

### WP-5（P1）OP internal 與 keyless Blockscout 改走 REST v2

- **目標**：OP 的原生 internal 轉入（≥0.5 ETH）能被偵測，而且能標出 `transfer_origin: evm_internal`；keyless 的速率不再受 `/api` 的 10 次限制。
- **範圍**：
  - `src/guardian/sources/blockscout.py`：
    - `txlistinternal` 回 `status "2"` 或 "not yet been processed" 時 → `HistoryRead` 標成 partial，原因 `indexer_lag`，游標不推進；
    - 新增 v2 的 `/internal-transactions`、`/transactions`、`/token-transfers` 讀取，用 `next_page_params` 分頁，讀到 ≤ 游標的區塊就停（v12 已移除 `limit`，不能帶）。
  - `src/guardian/scan/drive.py`：
    - 把 `_walk_bsc_internal`、`_note_bsc_internal`、`_set_internal_status` 寫死的 `bsc-mainnet` 判斷改成依 routing 決定；
    - blockscout 分支加上 internal 的 walk。
  - `src/guardian/balances/internal.py`：正規化 Blockscout 的資料列。
  - `config/source-capabilities/blockscout.json`：OP 的 `history.native_internal` 改成 `partial`。
  - 範例 routing 新增 `optimism-mainnet / history.native_internal`。
- **不做什麼**：不對 Base 或 Arbitrum 的 CF 403 做任何繞過，例如偽裝 UA 或用 headless 瀏覽器；不寫死 `api.blockscout.com` 的 key。
- **驗收**：
  1. 離線 fixture：status 2 → partial 加 `indexer_lag`，游標不推進。
  2. 離線 fixture：一筆 0.6 ETH 的 internal 轉入 → alert 有 `transfer_origin: evm_internal`。
  3. live：用公開的 `0x4200…0006` 跑一輪，`budget.providers` 顯示呼叫的是 `/api/v2`，而且沒有收到 429。
- **回歸測試**：BSC NodeReal internal 的既有測試全部通過。
- **負面測試**：
  - `items` 欄位不存在 → error，不能當 empty。
  - `next_page_params` 是壞的 → error。
  - 收到 CF 403 → `bot_challenge`。
- **風險**：Blockscout 已宣布 per-instance API 會停用（文件寫 "JULY 1"）。實作時要讓 Blockscout PRO 免費 key 可以無縫替換（同一套 v2 路徑，只差 base URL 與 Bearer 標頭）。

### WP-6（P1）launchd plist 產生器、睡眠政策、watchdog

- **目標**：在 Mac 上用一行指令印出三個 LaunchAgent：探針每 5 分鐘、`run-once` 每 3 小時、watchdog 每 15 分鐘。
- **範圍**：
  - `src/guardian/service_install.py`：新增 `schedule_mode: launchd_agent`，`--dry-run` 只印出 plist，`--output` 只寫到指定目錄。
  - `docs/scheduling.md`：說明 `StartCalendarInterval` 的 Minute 清單（0, 5, …, 55）與睡眠時合併補跑的行為，並引用 man page。
- **不做什麼**：不執行 `launchctl load`；不修改 `pmset`；不要求 root 權限。
- **驗收**：
  1. `plutil -lint` 檢查輸出的 plist 通過。
  2. `ProgramArguments` 全是絕對路徑，而且不含 `uv sync`。
  3. dry-run 的結束碼不是 0（和 systemd 的行為一致）。
- **回歸測試**：既有 systemd 與 cron 的 render 測試。
- **負面測試**：路徑含空白或中括號時，plist 要正確跳脫；檢查到 `StartInterval` 時要警告「睡眠期間會漏跑」。
- **風險**：macOS 新版的 LaunchAgent 權限提示（背景項目），要使用者手動允許。

### WP-7（P2）72 小時 shadow 期間的延遲度量

- **目標**：shadow 報告裡要有「偵測延遲」，不只是「這一輪有沒有準時」。
- **範圍**：
  - `src/guardian/shadow_recorder.py` 和 `tools/mcm_shadow_round.py`：每個 slot 記錄觸發數量，以及偵測延遲的 P50、P95；
  - `src/guardian/comparison.py`：`compare-legacy` 加一欄延遲差。
- **不做什麼**：不改 24 窗的資格規則。
- **驗收**：shadow-slots 的 JSON 裡有 `detect_latency_p95_seconds`；值為 null 時表示當窗沒有事件，不是 0。
- **回歸測試／負面測試**：`test_mcm_shadow.py` 全部通過；缺資料時是 null，不是 0。

### WP-8（P2，需要使用者的 key）keyed 路徑的 live 驗證

- **目標**：用使用者自己的免費 key，把 §2 標「未測」的格子補上實測。
- **範圍**：只動 `docs/coverage-matrix.md` 和 `config/source-capabilities/*.json` 的 `limits` 與 notes，外加操作者自己跑的 probe 指令。
- **要驗的項目**：
  - NodeReal BSC：getLogs 範圍 2,000 與 10,000，以及 `nr_getAssetTransfers` 的 internal 類別；
  - Blockscout PRO 免費 key：OP 與 **Base**（repo 記錄 402，文件寫 "All chains"，兩者衝突）；
  - Alchemy 免費 key：Base 的 `getAssetTransfers`（external 與 erc20）。
- **不做什麼**：不升級任何方案；不把 key 寫進任何檔案。
- **驗收**：每一格都有 HTTP 狀態碼、錯誤字串、日期，而且不含 key 或私人地址。

---

## 8. 做不到，或需要使用者決定的事

**做不到**（在「免費、免付費依賴」的前提下）：
1. **Base 的原生外部 tx 列表與 internal 轉入**：免 key 拿不到，原因有四：
   - `base.blockscout.com` 對這台 Mac 和 Linux 主機都是 CF 403；
   - Etherscan 免費版不含 8453；
   - Routescan 不支援；
   - Blockscout PRO 免費 key 依 repo 記錄回 402。
   
   只能做到：餘額差額（知道金額、不知道原因）加上 nonce 變化（知道有送出交易）。這不能宣稱是完整歷史。
2. **BSC 的 internal 轉入在免 key 下的原因歸屬**：只能靠餘額差額。帶 NodeReal key 可以補上，但 internal 類別還沒 live 驗證。
3. **任何鏈的免費 trace／debug**：NodeReal、Alchemy、dRPC 的免費方案都不提供。
4. **公開端點的服務保證**：48club、Tenderly gateway、`mainnet.base.org`、`mainnet.optimism.io` 都沒有公開額度數字，或者明寫「不適合 production」。只能靠多來源 fallback 加 watchdog。
5. **Blockscout per-instance API 的壽命**：官方已宣布會停用，今天還能用，但隨時可能消失。

**需要使用者決定**：
1. **Mac 夜間要不要保持清醒**：例如接電源時不自動睡眠（`pmset -c sleep 0` 或系統設定裡對應的選項，確切選項名稱請以本機 macOS 版本為準），或改用一台常開的 Linux 機器跑 repo 已有的 systemd units。不處理的話，夜間覆蓋只剩雲端那一層。
2. **雲端例行要不要改成全天跑**：Grok 額度是否允許；或在完成 WP-1 之後完全不讓 Grok 觸發。
3. **要不要註冊這些免費 key**：
   - Blockscout PRO：OP 主路徑、驗證 Base；
   - Alchemy：Base 的轉帳；
   - Routescan：能不能解鎖這三條鏈未知。
   
   NodeReal 的 key 已經有了，但要決定 **BSC 的 logs 要不要用它**（CU 預算見 §6.2）。
4. **能不能接受把監控地址送到新的第三方端點**：48club（MEV builder）、Tenderly、dRPC、Blockscout 都會在 topic 或參數裡看到監控地址。這需要使用者對 `third_party_wallet_queries` 做明確同意，並由 `indexer_disclosures` 記錄。
5. **什麼時候 cutover 到 Telegram 直送**：從 `outbox_only` 換成 `configured_sender`，依 docs/notifications.md 的流程。在那之前，WP-4 的效果只會出現在 outbox，不會到手機。
6. **探針間隔**：建議 5 分鐘。要更短可以，但要接受打公開端點更密的風險。

---

## 附錄 A：實測紀錄（2026-10-14，Mac，curl，時間是單次往返）

- `eth_blockNumber`：
  - 200：bsc-dataseed（0.18 秒）、bsc publicnode、1rpc bnb（1.35 秒）、mainnet.base.org、base publicnode、base tenderly、mainnet.optimism.io、op publicnode、op tenderly。
  - 失敗：`bsc.drpc.org` 回 429 "Public endpoint rate limit"；`rpc.ankr.com/bsc` 回 Unauthorized，要 API key；`binance.llamarpc.com` 連線失敗。
- **BSC getLogs**（topic0 是 Transfer，topic2 是 `0x…dEaD`，不帶 address）：

  | 端點 | 範圍 | 結果 |
  |---|---|---|
  | dataseed、defibit、ninicoin、`bsc.nodereal.io` | 0、5、100、1000 | `-32005 limit exceeded` |
  | publicnode | 100、1000 | `-32701` |
  | publicnode，帶 3 個代幣合約 | 1000 | 200（0 筆） |
  | 1rpc | 100 | `limited to 0 - 50 blocks`；49 個區塊 200 |
  | 48club | 100 | 200 |
  | 48club | 1000 | 200，2,637 筆，1.28 秒 |
  | 48club | 5000 | 200，9,909 筆，16.1 秒 |
  | blxr | 1000 | 200，14.2 秒 |
  | blxr | 5000 | 逾時（25 秒） |
  | blockpi | — | 503 `Apikey not found` |
  | blastapi | — | Alchemy 限流 |
  | meowrpc | — | 不支援 getLogs |
  | nodies | — | 403，要付費 |

- **Base getLogs**：

  | 端點 | 範圍 | 結果 |
  |---|---|---|
  | `mainnet.base.org` | 10、1000、5000 | 429 `-32011 request limit reached` |
  | publicnode | — | `-32701` |
  | Tenderly | 1000 | 200（92 筆，0.62 秒） |
  | Tenderly | 5000 | "maximum 1000 blocks" |
  | 1rpc | 49 | 200 |
  | drpc | 100 | 200 |
  | drpc | 1000 | 400 "ranges over 10000…" |
  | onfinality | — | 429 |
  | blastapi | — | 只能查 10 個區塊 |
  | meowrpc | — | 不支援 getLogs |

- **OP getLogs**：

  | 端點 | 範圍 | 結果 |
  |---|---|---|
  | `mainnet.optimism.io` | 1000 | 200 |
  | `mainnet.optimism.io` | 5000 | 200 |
  | `mainnet.optimism.io` | 20000 | 413 "limited to a 10,000 range" |
  | `mainnet.optimism.io` | 100000 | 200（29 筆） |
  | Tenderly | 1000、5000、20000 | 200 |
  | publicnode | — | `-32701` |
  | 1rpc | 1000 | "0 - 50 blocks" |

- **Blockscout**：
  - `base.blockscout.com` 的 `/api` 與 `/api/v2` 都回 403 Cloudflare challenge。
  - `explorer.optimism.io`：
    - `/api?action=txlistinternal` 回 200，但 status 2，標頭 `x-ratelimit-limit: 10`；
    - `/api/v2/.../internal-transactions` 回 200，50 筆，標頭 180；
    - `/api/v2/main-page/indexing-status` 回 `indexed_internal_transactions_ratio: "1.00"`，`finished_indexing: false`。
- **Routescan**：`evm/8453`、`evm/10`、`evm/56` 都回 "chain not supported"。
- **Etherscan V2**：
  - `/v2/chainlist` 免 key 回 200，共 63 列；8453、10、56 都是 `status: 1`。這只代表 API 支援這些鏈，不代表免費方案能用。
  - 不帶 key 打 balance：8453、10、56 回 "Free API access is not supported for this chain"；1、42161、137 回 "Missing/Invalid API Key"。
- **Multicall3**：三條鏈的 `getCode` 都是 3,808 bytes。OP 上的 `aggregate3(getEthBalance×2)` 回 200，回傳 2 筆結果。

## 附錄 B：來源（查詢日期都是 2026-10-14）

| 主題 | 來源 |
|---|---|
| Etherscan | https://docs.etherscan.io/supported-chains 、https://docs.etherscan.io/resources/rate-limits 、https://docs.etherscan.io/changelog 、https://kb.etherscan.com/whats-changing-in-the-free-api-tier-coverage-and-why 、https://etherscan.io/apis 、https://info.etherscan.com/etherscan-api-v1-will-be-fully-deprecated-by-15th-august-2025/ |
| BNB Chain | https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/ 、https://www.bnbchain.org/en/blog/migration-guide-bscscan-api-to-bsctrace-api-via-meganode |
| Blockscout | https://docs.blockscout.com/devs/apis/requests-and-limits 、https://www.blog.blockscout.com/news-july-1-is-pro-api-day/ 、https://docs.blockscout.com/blockscout-v12-0-0-api-updates-and-breaking-changes 、https://docs.blockscout.com/devs/pro-api 、https://docs.blockscout.com/devs/pro-api-responses-and-routes 、https://docs.blockscout.com/devs/migrate-from-etherscan |
| Routescan | https://routescan.io/docs/plans-and-limits/rate-limits 、https://routescan.io/docs/plans-and-limits/api-keys-and-pricing |
| NodeReal | https://nodereal.io/pricing 、https://nodereal.io/api-marketplace/bsc-rpc 、https://docs.nodereal.io/reference/eth-getlogs-bnb-chain 、https://docs.nodereal.io/docs/compute-units-cus 、https://nodereal.io/api-marketplace/debug-bsc 、https://docs.nodereal.io/reference/nr_getassettransfers |
| 公開 RPC | https://www.ankr.com/docs/rpc-service/service-plans/ 、https://docs.blockpi.io/build/api-reference/bsc/eth_getlogs 、https://www.1rpc.io/ 、https://docs.48.club/privacy-rpc.md 、https://blog.drpc.org/upcoming-changes-to-drpcs-free-plan-effective-june-1-2025/ |
| Base／OP | https://docs.base.org/base-chain/flashblocks/faq 、https://docs.base.org/base-chain/api-reference/rpc-overview 、https://docs.optimism.io/app-developers/reference/rpc-providers |
| 其他供應商 | https://www.alchemy.com/pricing 、https://www.alchemy.com/reference/eth-getlogs 、https://www.quicknode.com/pricing 、https://goldrush.dev/pricing/ 、https://docs.moralis.com/web3-data-api/evm/pricing-plans |
| launchd | https://keith.github.io/xcode-man-pages/launchd.plist.5.html |
| 失效連結 | repo 引用的 https://docs.tenderly.co/node/rpc 今天回 404 |
