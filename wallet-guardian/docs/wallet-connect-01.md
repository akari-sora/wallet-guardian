# WALLET-CONNECT-01 W-A：EIP-6963＋幣安注入連線（唯讀登入）

狀態：W-A 程式與離線／產物測試。真錢包路徑一律 **REAL_WALLET_PENDING**，由本人實測。
W-B（WalletConnect v2／Reown）見本文件後段「W-B」；W-A 路徑本身沒有 WalletConnect 依賴，也不連 Relay。

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
- personal_sign 傳輸格式是 adapter 細節：`utf8`（原字串，D1-LIVE-01 行為）或 `hex`（`0x`＋UTF-8 hex）。注入來源都是 `utf8`；
  WalletConnect（W-B）是 `hex`（Reown RPC 文件將 personal_sign 訊息定義為 DATA）。編碼只依連線來源決定，rdns／名稱不能選擇編碼。拒簽就結束，不改用 `eth_sign` 或其他方法。
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

W-B 各列見下方「W-B 真機驗收」。

---

# WALLET-CONNECT-01 W-B：WalletConnect v2（Reown）唯讀登入

狀態：W-B 程式＋離線／合成測試＋建置產物測試。**沒有任何真錢包驗證**；真機各列皆 REAL_WALLET_PENDING。
WalletConnect 只是第二種「連線」：同一個 `guardedRequest()`／`request-policy.mjs`（預設拒絕）、同一份固定 SIWE、同一個後端驗簽。
送出仍硬關：`requestUserSend()` 無條件拒絕，`execution_authorized=false`，`VITE_GUARDIAN_MAINNET_SEND` 不設定。

## 設定（公開建置設定，fail-closed）

| 變數 | 說明 |
| --- | --- |
| `VITE_WALLETCONNECT_ENABLED` | 必須是字串 `true` |
| `VITE_REOWN_PROJECT_ID` | Reown Project ID（32 位小寫 hex，公開 client 識別碼）。只由 env 注入，原始碼不寫死 |

任一缺少或格式不符：選單不出現 WalletConnect、主 bundle 不含 SDK chunk、CSP 不放行 Relay／Verify。
不使用 Reown 付費功能；`showQrModal=false`（不載入 AppKit，`@reown/appkit/core` 在 Vite 以 stub 取代）。

## 模組

| 檔案 | 用途 |
| --- | --- |
| `src/wallets/walletconnect-config.mjs` | 設定判斷、鎖定版本、鏈／方法常數、端點常數、版本化錢包跳轉設定、pairing URI 格式檢查 |
| `src/wallets/walletconnect-session.mjs` | proposal／session 範圍計算：CAIP 帳戶、鏈、方法交集、到期 |
| `src/wallets/walletconnect-sdk.mjs` | 唯一 import SDK 的模組（lazy chunk）：`ReadOnlyLoginProvider`、`providerOptions()`、QR 繪製（canvas） |
| `src/wallets/walletconnect.mjs` | EIP-1193 facade：`eth_accounts`／`eth_chainId` 本地回答；`eth_requestAccounts` 才連線；只轉送 `personal_sign`；其他一律 4200 |
| `src/csp.mjs` | CSP 單一來源；`vite.config.ts` 依建置設定產生 `dist/_headers` |
| `src/main.ts` | 選單加 WalletConnect 選項；QR／跳轉面板；WC 專用提示；WC 永不隱含選取、永不 fallback |

鎖定版本：`@walletconnect/ethereum-provider` **2.25.0**（精確版本，lockfile 已更新）；`qrcode-generator` 2.0.4。
測試用 devDependencies：`@walletconnect/sign-client` 2.25.0（測試錢包）、`ws` 8.21.0（本機 mock relay）。

## Proposal（WB-01）與與規格的差異

送出設定：`chains=[56]`、`methods=["personal_sign"]`、`events=["chainChanged","accountsChanged"]`，
`optionalChains=[]`、`optionalMethods=[]`、`optionalEvents=[]`，`rpcMap={56: 可信 BSC RPC}`，`telemetryEnabled=false`。

**差異（需知悉）**：規格要求 required=`eip155:56`、`optionalNamespaces: {}`。SDK 2.25.0 的 sign-client 會把 required 一律搬到 optional，
實際送出的 proposal 為 `requiredNamespaces: {}`、`optionalNamespaces: {eip155: {chains:["eip155:56"], methods:["personal_sign"], events:[...]}}`
（WB-01 與 WC-ARTIFACT 皆擷取實際 proposal）。聯集範圍與契約完全相同；本頁在核准後自行強制：session 必須有 `eip155:56` 帳戶且核准 `personal_sign`，
否則立即中斷並視為失敗（WB-02）。錢包多核准的方法／鏈只顯示提示，產品可用方法永遠是交集 `["personal_sign"]`，
`wallet_switchEthereumChain` 在 provider 層是 no-op。

## 網路與 CSP（W-B5）

依鎖版 SDK 原始碼核對，dapp 端實際使用：

| 端點 | 用途 | 放行 |
| --- | --- | --- |
| `wss://relay.walletconnect.org` | Relay（irn） | `connect-src` |
| `https://verify.walletconnect.org/v3/attestation` | Verify 隱藏 iframe（proposal／request attestation） | `frame-src` |
| `pulse.walletconnect.org` | 遙測 | 不放行（`telemetryEnabled=false`） |
| `rpc.walletconnect.org` | Reown RPC | 不放行（`rpcMap` 指向可信 BSC RPC，且 facade 不轉送 RPC） |
| `echo.walletconnect.com`、`api.web3modal.org`、explorer | 推播（錢包端）／AppKit／目錄 | 不放行、不載入 |

WalletConnect 開啟時的 CSP：

```
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
connect-src 'self' https://bsc-dataseed.bnbchain.org wss://relay.walletconnect.org;
frame-src https://verify.walletconnect.org; manifest-src 'self'; base-uri 'none'; form-action 'none';
frame-ancestors 'none'; object-src 'none'
```

關閉時去掉 Relay，`frame-src 'none'`。另有 `Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`。
沒有萬用字元、沒有 `unsafe-eval`／`unsafe-inline`。`_headers` 只套用靜態頁，不套用 Pages Function 回應。

## 流程與狀態

- 選 WalletConnect 不會連線；按登入才 lazy 載入 SDK 並建立 pairing。只選注入錢包時 SDK 從不載入、Relay 從不連線。
- Pairing URI 只存在於 QR canvas 與跳轉連結中，連線、取消、過期、失敗即清除；不進 banner、task、D1、Bot、log、遙測（SDK logger=silent）。
  SDK 自身的 IndexedDB 保存 session／keychain，屬於「SDK 必要儲存」。
- 期限：初始化 15 秒、取得 URI 20 秒、pairing 5 分鐘。重複按登入共用同一 pairing；取消／過期會清除未完成 pairing，晚到的 session 會被中斷，不會變成已連線。
- 錢包端換帳戶、session 更新／刪除／到期：只更新錢包身分並讓重查失效，不自動登出、不自動重簽。
- 重新整理：D1 session 由 cookie 恢復；WalletConnect session 只有在使用者再次按登入時才恢復（不重新 pairing、不自動簽名）。恢復 WC session ≠ 恢復 D1 登入。
- 從 WalletConnect 切到注入錢包：中斷 WC session，並依 W-A3 撤銷網站 session。登出保留 WC session。
- Relay／SDK 失敗：明確顯示 W-B 失敗，不改用注入錢包；W-A 照常可用。

## 手機跳轉（W-B4）

版本化設定 `WC_DEEP_LINKS.version = "2026-10-08.1"`，來源：WalletConnect Explorer 列表
`8a0ee50d1f22f6651afcae7eb4253e52a3310b90af5daef78a8c4929a9bb99d4`（Binance Wallet；`mobile.universal` https://app.binance.com/cedefi，
`mobile.native` bnc://app.binance.com/cedefi/）。連結只由此設定與 SDK 產生的 URI 組成，不取自 task、query 或 peer metadata：

1. `https://app.binance.com/cedefi/wc?uri=<encoded>`（新分頁）
2. `bnc://app.binance.com/cedefi/wc?uri=<encoded>`（備用）
3. `wc:` 原始 URI（交給系統已安裝的任一 WC 錢包）

跳轉失敗時 QR／重試仍在，不會假裝已連線。不跨瀏覽器搬 cookie／token。

## 驗收對照（皆為離線／合成；沒有真錢包）

| 測試 | 位置 |
| --- | --- |
| WB-01 actual-proposal | `tests/walletconnect-sdk.test.mjs`（真 SDK 2.25.0 ↔ mock relay ↔ sign-client 測試錢包）；`integration/walletconnect-artifact.test.mjs` 亦擷取 |
| WB-02 session-scope | `tests/walletconnect-sdk.test.mjs` |
| WB-03 pairing-lifecycle | `tests/walletconnect-sdk.test.mjs`、`tests/walletconnect-page.test.mjs` |
| WB-04 update-and-restore | `tests/walletconnect-sdk.test.mjs`（含重新整理恢復、hex bytes＝UTF-8 訊息、後端還原地址）、`tests/walletconnect-page.test.mjs` |
| WB-05 mobile-return | `tests/walletconnect-page.test.mjs`（跳轉連結、切背景返回、原任務、拒絕／未安裝後重試） |
| WB-06 relay-isolation | `tests/walletconnect-sdk.test.mjs`（relay 斷線）、`tests/walletconnect-page.test.mjs`（SDK 失敗不 fallback、W-A 仍可登入） |
| WC-ARTIFACT-01 no-send | `integration/walletconnect-artifact.test.mjs`：關／開兩種建置、注入與 WC 兩種連線、偽造任務＋強制點確認＝0 筆交易請求，固定 SIWE 由真實 handler 驗簽 |
| WC-ARTIFACT-02 network-policy | 同上：實際回應 CSP、0 個 CSP 違規、未選 WC 不抓 SDK chunk 不連 Relay、只接觸 Relay＋Verify、URI 不留在 DOM／頁面儲存／API／console、project ID 只由 env 注入 |

Relay 是程序內 mock（Playwright `routeWebSocket`），Verify iframe 以空白頁回應；不是 Reown 真服務，也不是真錢包 App。

```bash
cd action-client
npm run test:wallets
npm run build && npm run test:wallets:artifact   # 需要 Chrome（WG_CHROME）；W-B 產物測試自行建置兩種變體
```

## W-B 真機驗收（本人）

Reown 後台 allowlist 已加入 `https://wallet-guardian-action-client.pages.dev`，生效最多可能需 6 小時；生效前真 Relay 可能回來源錯誤，這不代表程式錯誤。

| 路徑 | 狀態 |
| --- | --- |
| 桌機：WalletConnect QR → 幣安 App，連線＋SIWE，桌機取得正確 session | REAL_WALLET_PENDING |
| S24 Ultra：Chrome → 幣安 App → Chrome，同機跳轉返回原任務 | REAL_WALLET_PENDING |
| WalletConnect → 一個非幣安錢包（證明通用 WC 路徑） | REAL_WALLET_PENDING |
| 以上每條路徑一次拒簽／取消，且沒有交易請求 | REAL_WALLET_PENDING |
