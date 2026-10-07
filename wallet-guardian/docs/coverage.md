# 覆蓋範圍

更新時間：2026-10-07 13:28 Asia/Taipei。離線測試與一次唯讀 `doctor` / `run-once`。私人餘額只留在 gitignore 的 `evidence/`。

## 這次實際執行

- `uv run pytest -q`：138 passed。其中 v1 文件 56 項、v2 文件 72 項，其餘是目錄對照與 CLI 安全檢查。
- `guardian doctor --json`：exit 0。BNB Chain 官方 RPC `LIVE_PASS`（約 block 126196077）。Etherscan V2 chainlist `LIVE_PASS`（回傳 63 列，監控只啟用 chain 56）。Hyperliquid info `LIVE_PASS`。
- `guardian run-once --config private/config.json --json`：exit 0，`completed_with_limitations`，`monitoring_active=false`。Lista proxy 的 ERC-1967 implementation 讀到 `0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf`，`paused=false`。一個 watch 地址的目前提款陣列有一筆且 `isClaimable=true`；另一個陣列是空的。未簽名計畫是 `SIMULATED`，`wallet_request_allowed=false`。
- 沒有送出通知，沒有簽名，沒有廣播。

| 範圍 | 狀態 | 說明 |
|---|---|---|
| BNB Chain chain id 56，官方 RPC 讀 native、slisBNB、Lista 提款 | 由 run-once 實測 | `eth_getLogs` 在官方 dataseed 不可用 |
| Lista `claimWithdraw` 未簽名計畫 | DRAFT / BLOCKED | 會重讀 idx；不簽名 |
| Ethereum、其他 EVM | 未啟用 | 沒有預先核准的 RPC |
| DeBank / Zerion / Alchemy discovery | BLOCKED | 沒有 key |
| GoPlus | LIMITED_DISCOVERY | 沒有 key，不宣稱已做風險篩選 |
| OFAC | 視 doctor 結果 | SDN CSV 若沒有 0x 地址集合，就不能說已篩過 |
| Hyperliquid info | 由 run-once 實測 | 空的 master 回應不是所有子帳戶為零；未知模式不相加 spot 與 perp |
| 新聞 RSS | LIMITED_DISCOVERY | 只有過濾規則，不把錢包地址送去搜尋 |
| Webhook | BLOCKED | 種子設定禁止公開 webhook，也沒有已驗證 callback |
| 持續監控 | 未啟用 | 沒有讀回 next run，不能稱 MONITORING_ACTIVE |
| Fork / 真錢包 / 公開部署 | BLOCKED | 沒有這些證據就不是上線功能 |

操作覆蓋與監控覆蓋分開。能讀 Lista 不代表能執行 Lista 的每一個寫入方法。
