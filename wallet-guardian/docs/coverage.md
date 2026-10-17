# 覆蓋範圍

> 歷史紀錄。下面「2026-10-07」一節描述的是套件 `0.3.0` 當天的 Lista／BSC 路徑，以及那天記下來的離線測試數字。現況的套件版本是 `0.4.0rc4`（`pyproject.toml`）。rc.4 的覆蓋以 `docs/coverage-matrix.md` 為準。DeBank、Zerion，以及模擬用的 `TENDERLY_ACCESS_KEY`，沒有會被 `run-once` 呼叫的 adapter，設了 key 也不會呼叫。`examples/monitor.json` 的 Base 日誌路由仍會走 `tenderly_base`（`TENDERLY_BASE_URL`）。Alchemy 只出現在 `examples/monitor.json` 的 Base 路由，`guardian init` 不會加。

更新時間：2026-10-07。離線測試，加上先前一次唯讀 `doctor` / `run-once`（約 13:28 Asia/Taipei）。這次沒有重跑鏈上讀取，也沒有帶 Etherscan key，所以沒有新的 chainlist LIVE_PASS。私人餘額只留在 gitignore 的 `evidence/`。套件版本是 0.3.0。客戶端建置 `action-client@0.3.0` 已通過 `npm run build`。

## 這次實際執行

- `uv run pytest -q`：180 passed。v1 文件 56 項、v2 文件 72 項、工作台文件 31 項。其餘是目錄對照、CLI、合成任務、確認閘門與 EIP-5792 退回。這些都是離線或模擬，不能當成真錢包或手機通過。
- `node src/gate.test.mjs`：通過。確認鈕與主網送出開關的條件和 Python 一致。
- `guardian doctor --json`：exit 0。BNB Chain 官方 RPC `LIVE_PASS`（約 block 126196077）。Etherscan V2 chainlist `LIVE_PASS`（回傳 63 列，監控只啟用 chain 56）。Hyperliquid info `LIVE_PASS`。
- `guardian run-once --config private/config.json --json`：exit 0，`completed_with_limitations`，`monitoring_active=false`。Lista proxy 的 ERC-1967 implementation 讀到 `0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf`，`paused=false`。一個 watch 地址的目前提款陣列有一筆且 `isClaimable=true`；另一個陣列是空的。未簽名計畫是 `SIMULATED`，`wallet_request_allowed=false`。
- 沒有送出通知，沒有簽名，沒有廣播。

| 範圍 | 狀態 | 說明 |
|---|---|---|
| BNB Chain chain id 56，官方 RPC 讀 native、slisBNB、Lista 提款 | 由 run-once 實測 | `eth_getLogs` 在官方 dataseed 不可用 |
| Lista `claimWithdraw` 未簽名計畫 | DRAFT / BLOCKED | 會重讀 idx；不簽名 |
| 任務工作台模式 A | 引導路徑與不廣播測試已寫入 | 重讀通過不會授權送出。`requestUserSend` 在正式預設仍拒絕，主網建置旗標打開也一樣。不廣播測試只在測試旗標且提供者標記不廣播時記錄單筆 `claimWithdraw`。完成必須綁到這筆收據；同一個任務的 pending 與 unknown 不會在重讀時被清成 idle。真錢包與主網仍要擁有者稍後授權，寫明錢包、請求、收款地址與費用。同意這次實作不是打開送出開關 |
| 任務工作台模式 B | 能力判斷已寫入，真錢包未測 | 不支援、別的鏈或 `ready` 退回 A。同鏈多筆且 `supported` 才標 B。沒有對真錢包送出 `wallet_sendCalls` |
| 任務工作台模式 C | 關閉 | 程式沒有權限合約。呼叫端自述不算強制 |
| Etherscan V2 chainlist | 有 key 才 LIVE_PASS | 這次環境沒有 key，所以沒有新的 LIVE_PASS。一把 `ETHERSCAN_API_KEY` 涵蓋 V2 |
| 手機錢包內建瀏覽器 | 未測 | 沒有發行公開任務連結。手機瀏覽器未測 |
| 固定 HTTPS 網域 | 紀錄見 deploy.md | Cloudflare Pages 於這次核對回 HTTP 200。程式常數 `fixed_https_origin` 仍是 false。沒有重新部署，也沒有因此發行任務連結 |
| Ethereum、其他 EVM | （0.3.0 當時）未啟用 | 當時沒有預先核准的 RPC。現在的 `guardian init` 會為選到的 EVM 鏈寫 `evm_rpc`。這列不是現況 |
| DeBank / Zerion / Alchemy discovery | （0.3.0 當時）BLOCKED | 當時寫「沒有 key」。現況是 DeBank 與 Zerion 沒有 adapter，設了 key 也不會呼叫。Alchemy 不是全鏈 discovery，只在 `examples/monitor.json` 的 Base 路由 |
| GoPlus | （0.3.0 當時）LIMITED_DISCOVERY | 當時沒有 key。現況是 `run-once` 不論有沒有 key 都不呼叫 GoPlus |
| OFAC | 視 doctor 結果 | SDN CSV 若沒有 0x 地址集合，就不能說已篩過 |
| Hyperliquid info | 由 run-once 實測 | 空的 master 回應不是所有子帳戶為零；未知模式不相加 spot 與 perp |
| 新聞 RSS | LIMITED_DISCOVERY | 只有過濾規則，不把錢包地址送去搜尋 |
| Webhook | BLOCKED | 種子設定禁止公開 webhook，也沒有已驗證 callback |
| 持續監控 | 未啟用 | 沒有讀回 next run，不能稱 MONITORING_ACTIVE |
| Fork / 真錢包 / 公開部署 | BLOCKED | 沒有這些證據就不是上線功能 |

操作覆蓋與監控覆蓋分開。能讀 Lista 不代表能執行 Lista 的每一個寫入方法。建置與測試不等於獲准執行任何主網資產交易。

先前 run-once 的鏈上事實仍有效：Lista implementation `0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf`，`paused=false`。一個 watch 地址當時有一筆 `isClaimable=true` 的提款，另一個陣列是空的。未簽名計畫 `wallet_request_allowed=false`。這次工作台沒有把那次讀取變成一筆已送出的交易。

## 2026-10-09 MCM-SHARE-01 S1

離線驗收，不是 live pass。Base 仍在必要範圍裡。失敗的讀取保持缺口（`credential_missing`、`plan_not_supported`、`blocked` / `bot_challenge`），不記成 0、不記成 complete。

- Base 外部與代幣轉帳：有 `ALCHEMY_API_KEY` 與 `ALCHEMY_TRANSFERS_URL` 時走 Alchemy Transfers（`from` / `to` 分開、`pageKey` 跟完、固定區塊窗口）。每次呼叫的 120 CU 是估計。沒有 key 就留下說明並改走 Blockscout。
- Base 的 Approval、ApprovalForAll、Transfer 日誌：`tenderly_base` 的 `range_limit` 是 1000。`base-1rpc`（50）預設關閉，只有啟用且剩餘預算夠追趕才用。Alchemy 免費 `eth_getLogs`（10 個區塊）不是這條路徑。
- 轉帳資料不會把授權能力標成 complete。

## 2026-10-09 Bitcoin Esplora

bitcoin-mainnet 在範例監控裡加入唯讀 Esplora。鏈登記的 `enabled` 仍是 false。讀的是呼叫端給的地址集合，不是 xpub，也不是整份 HD 錢包。

- `balances.native`：confirmed 是 `chain_stats` 的 `funded_txo_sum - spent_txo_sum`，整數 satoshi。`mempool_stats` 分開記，不併進 confirmed。
- `history.native_external`：`txs/chain` 每頁 25 筆，有硬頁數上限。沒翻完是 partial，不是 complete。vin/vout 的進出用 satoshi；fee 不算這個地址的事件。
- 6 個確認才是告警用的 confirmed。較淺與 mempool 是 pending，可能被取代，不會標成 finalized。
- token、Ordinals、Runes、Lightning、xpub 維持 unsupported，理由碼分別是 `no_tokens`、`no_ordinals`、`no_runes`、`no_lightning`、`no_xpub`。核准類能力是 not_applicable。
- 讀取失敗（429、5xx、缺端點）是 error / blocked，不是 0。

offline fixtures; live probe: see docs/coverage-matrix.md
