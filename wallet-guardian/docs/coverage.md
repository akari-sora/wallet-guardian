# 覆蓋範圍

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
| 任務工作台模式 A | 閘門已寫入 | 確認鈕只在 `requires_user_wallet_confirmation` 且 preflight 通過時可按。`GUARDIAN_MAINNET_SEND` 預設 false，可按也不送出。真錢包未測 |
| 任務工作台模式 B | 能力判斷已寫入，真錢包未測 | 不支援、別的鏈或 `ready` 退回 A。同鏈多筆且 `supported` 才標 B。沒有對真錢包送出 `wallet_sendCalls` |
| 任務工作台模式 C | 關閉 | 程式沒有權限合約。呼叫端自述不算強制 |
| Etherscan V2 chainlist | 有 key 才 LIVE_PASS | 這次環境沒有 key，所以沒有新的 LIVE_PASS。一把 `ETHERSCAN_API_KEY` 涵蓋 V2 |
| 手機錢包內建瀏覽器 | 未測 | 沒有已驗證的固定 HTTPS 網域，沒有發行公開任務連結 |
| 固定 HTTPS 網域 | BLOCKED | 靜態建置與 GitLab Pages / Vercel / Cloudflare 設定已寫好。見 `docs/deploy.md`。沒有憑證，不能宣稱網址已上線 |
| Ethereum、其他 EVM | 未啟用 | 沒有預先核准的 RPC |
| DeBank / Zerion / Alchemy discovery | BLOCKED | 沒有 key |
| GoPlus | LIMITED_DISCOVERY | 沒有 key，不宣稱已做風險篩選 |
| OFAC | 視 doctor 結果 | SDN CSV 若沒有 0x 地址集合，就不能說已篩過 |
| Hyperliquid info | 由 run-once 實測 | 空的 master 回應不是所有子帳戶為零；未知模式不相加 spot 與 perp |
| 新聞 RSS | LIMITED_DISCOVERY | 只有過濾規則，不把錢包地址送去搜尋 |
| Webhook | BLOCKED | 種子設定禁止公開 webhook，也沒有已驗證 callback |
| 持續監控 | 未啟用 | 沒有讀回 next run，不能稱 MONITORING_ACTIVE |
| Fork / 真錢包 / 公開部署 | BLOCKED | 沒有這些證據就不是上線功能 |

操作覆蓋與監控覆蓋分開。能讀 Lista 不代表能執行 Lista 的每一個寫入方法。建置與測試不等於獲准執行任何主網資產交易。

先前 run-once 的鏈上事實仍有效：Lista implementation `0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf`，`paused=false`。一個 watch 地址當時有一筆 `isClaimable=true` 的提款，另一個陣列是空的。未簽名計畫 `wallet_request_allowed=false`。這次工作台沒有把那次讀取變成一筆已送出的交易。
