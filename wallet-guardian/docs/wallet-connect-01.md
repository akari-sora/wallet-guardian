# WALLET-CONNECT-01 W-A：EIP-6963＋幣安注入連線（唯讀登入）

狀態：W-A 程式與離線／產物測試。真錢包路徑一律 **REAL_WALLET_PENDING**，由本人實測。
W-B（WalletConnect v2／Reown）是之後的獨立 MR，本切片沒有任何 WalletConnect 依賴，也不連 Relay。

## 範圍

終點是「選對錢包 → 簽固定 SIWE 讀取訊息 → 讀自己的私人任務 → 取消／登出」。不新增交易能力：
`requestUserSend()` 仍硬拒絕，`execution_authorized=false`，`VITE_GUARDIAN_MAINNET_SEND` 不開，
`AUTH_CHAIN_ID=56` 不變，ERC-1271 仍不支援，D1 ACL 不變。

## 模組

| 檔案 | 用途 |
| --- | --- |
| `action-client/src/wallets/registry.mjs` | W-A1 錢包發現、去重、名稱／圖示清理 |
| `action-client/src/wallets/injected.mjs` | W-A2/W-A3/W-A4 注入連線 adapter：只呼叫已選 provider、切換時放棄舊請求、personal_sign 轉碼 |
| `action-client/src/wallets/request-policy.mjs` | 請求政策（預設拒絕）。W-B 也要沿用同一份 |
| `action-client/src/main.ts` | 選單、`activeConnection()`、`selectWallet()`、登入／重查／能力查詢都走同一連線 |

## W-A1 發現與選單

- 先註冊 `eip6963:announceProvider`，再發 `eip6963:requestProvider`；listener 保留，晚到的公告照收。
  沒有錢包時只顯示「尚未偵測到」，不以單次 timeout 宣稱沒有錢包。
- 候選順序：EIP-6963 公告（依到達順序）→ `window.binancew3w?.ethereum`（幣安官方記載介面）→ `window.ethereum` 後備。
- 同一個 provider 物件不論從幾條路徑出現只列一次。同一 provider 重複公告保留第一次的資料。
  同 uuid、不同物件的公告被忽略（記為 conflict），不能覆寫已列出或已選的 provider。
  偽造 rdns 只是另一個獨立選項，不會併入已選 provider。不以 `isMetaMask`、名稱或 rdns 判定品牌。
- 名稱用 `textContent`，移除控制字元與雙向覆寫字元，最長 64 字。圖示只接受 ≤64 KB 的
  `data:image/(png|jpeg|gif|webp|svg+xml)`；SVG 只當 `<img src>`，不進 innerHTML。rdns 不合格式就不顯示。

## W-A2 單一已選連線

- `ethereumProvider()`、`watchWallet()`、`login()`、`reverifyCurrent()`、`queryCapability()` 都經
  `activeConnection()` 取同一個連線，不再各自重讀 `window.ethereum`。
- `activeConnection()`：明確選擇優先；否則沿用正在使用的 provider（另一錢包的新公告不算切換）；
  否則只有一個候選時用它；多個候選且未選擇時不呼叫任何錢包。
- 先選錢包才呼叫 `eth_requestAccounts`。發現與顯示選單不向任何錢包索取帳戶。只有一個候選時，
  登入鈕顯示「使用此錢包登入」，按下即以它為已選連線。
- 已選 provider 不會被之後覆寫的 `window.ethereum` 偷偷取代。

## W-A3 切換與登入狀態

- 選了不同 provider：遞增 `connectionEpoch` 與 auth 世代、放棄舊 provider 尚未返回的請求、清除私人畫面與重查結果、
  移除舊 provider 的事件 listener（舊 provider 不移除時，其事件也被忽略）。
- 有網站 session 時：呼叫 `/api/auth/logout` 撤銷；只有伺服器回 `revoked=true`（或已無 session 的 401）才標為已撤銷。
  未確認時保留 CSRF 供重試，且新登入前會先再試撤銷，未成功就不向新錢包索取帳戶。
- 登入中切換：舊登入立即放棄；若舊的 verify 已送出，晚到的回應不套用，並用它自己的 CSRF 撤銷該 session，
  避免重新整理後恢復舊身分。之後才撤銷原本的 session。
- 同一錢包換成與目前 session 不同的帳戶後重新登入：先撤銷舊 session 再取 challenge。錢包端換帳戶本身只提示，不自動登出、不自動重簽。
- 鏈變更、斷線只讓重查失效（沿用 D1-LIVE-01/P2 行為），不自動重簽。Mode A 的 pending/unknown 紀錄不被清成 idle（未改動）。
- 已知限制：若舊 verify 回應晚到時，瀏覽器已用它的 cookie 覆蓋原 session cookie，原 session 的 D1 列會在到期前保留但瀏覽器已無它的 cookie。

## W-A4 固定 SIWE

- challenge 重建、來源、地址、鏈、nonce、期限與後端驗簽全部沿用。
- `request-policy.mjs` 預設拒絕；只允許 `eth_accounts`、`eth_chainId`、（已選＋使用者動作的）`eth_requestAccounts`、
  （使用者動作的）`wallet_getCapabilities`、以及 `personal_sign`——且只在訊息通過 `acceptLoginMessage` 固定讀取聲明、
  地址等於登入地址、送出 bytes 等於該訊息 UTF-8 bytes 時。交易送出／簽名、typed data、`eth_sign`、權限、委託、升級一律拒絕。
- personal_sign 傳輸格式是 adapter 細節：`utf8`（原字串，D1-LIVE-01 行為）或 `hex`（`0x`＋UTF-8 hex）。目前所有來源都是 `utf8`；
  `hex` 只在真機證明需要時才對特定來源啟用，rdns／名稱不能選擇編碼。拒簽就結束，不改用 `eth_sign` 或其他方法。
- rdns／uuid／名稱不是身分：不送到伺服器，不進 D1，不影響 ACL。

## 驗收對照（本 MR 新增，皆為離線／合成）

| 測試 | 位置 |
| --- | --- |
| WA-01 discovery-order | `tests/wallet-discovery.test.mjs` |
| WA-02 selected-provider | `tests/wallet-selection.test.mjs` |
| WA-03 binance-fallback | `tests/wallet-discovery.test.mjs` |
| WA-04 metadata-untrusted | `tests/wallet-discovery.test.mjs` |
| WA-05 switch-and-late-response | `tests/wallet-selection.test.mjs`（含以真實 handler＋記憶體 D1 驗證舊 session 列被撤銷） |
| WA-06 siwe-bytes | `tests/wallet-selection.test.mjs`（臨時測試金鑰簽名，真實 handler 驗簽；utf8 與 hex 兩種 adapter） |
| 產物 | `integration/wallets-artifact.test.mjs`：建置後的 dist 在 headless Chrome、兩個 EIP-6963 錢包＋惡意名稱／圖示、強制點確認＝0 筆交易請求、切換即撤銷 D1 session |

```bash
cd action-client
npm run test:wallets
npm run build && npm run test:wallets:artifact   # 需要 Chrome（WG_CHROME）
```

`tests/*.test.mjs` 也在 `test:api` 的 glob 內，所以 `test:api` 的總數包含 wallet 測試。

## 真機驗收（本人）

每列記錄錢包版本、Android／瀏覽器版本、連線方式、固定 HEAD 與產物 hash；未測列標 REAL_WALLET_PENDING。

| 路徑 | 狀態 |
| --- | --- |
| 桌機：幣安擴充功能＋另一個錢包同時安裝，選幣安登入 | REAL_WALLET_PENDING |
| S24 Ultra：幣安 App 內建 dApp 瀏覽器開 `/t/<ref>`，SIWE、查看、取消、登出 | REAL_WALLET_PENDING |
| 每條路徑一次拒簽／取消，且沒有交易請求 | REAL_WALLET_PENDING |

W-B 各列（WalletConnect QR、Chrome→幣安 App→Chrome、非幣安 WC 錢包）屬於 W-B，且需要本人建立的 Reown Project ID。
