# 03 — Wallet Guardian Mode A 正式開放前阻擋項：工作包與驗收標準

- 對象：`wallet-guardian` main `16559ac`（action-client `0.3.0`，Python `0.4.0rc4`）
- 日期：2026-10-14
- 方法：只讀程式碼與文件，另外對公開正式網址發出 GET 請求（沒有登入、沒有簽名、沒有寫入）。**沒有執行任何測試、建置或 `npm ci`**，repo 也沒有任何修改。下文提到「某測試存在」，只表示讀到了程式或文件，**不代表那個測試已經通過**。
- 標註方式（路徑都相對於 `wallet-guardian/`）：
  - `[讀碼 path:line]`：直接讀程式碼確認。
  - `[文件 path]`：repo 內文件的敘述，我沒有獨立重現。
  - `[推論]`：根據前兩者推出的結論，還需要驗證。
  - `[實測 GET 2026-10-14]`：對公開網址的唯讀 HTTP 觀察，只代表當下狀態。
- 本報告不含任何私人地址、API key、token、D1 database id 或 Reown project id。唯一出現的鏈上地址是公開的 Lista proxy 合約。

---

## 1. 摘要

1. **阻擋項 1（任務存取授權）大致完成。** SIWE、一次性 nonce、`__Host-` cookie、CSRF 與 Origin 檢查、租戶 ACL（兩層 JOIN）、對外一律回 404、登出撤銷、來源 IP 限流都已寫好，文件也列了 D1-01..D1-09 本地 runtime 測試。還缺：撤銷不會連帶處理（publisher、membership、session 三者各自為政）；沒有「撤銷同一地址的所有 session」；session 只有 12 小時絕對時效、沒有閒置逾時；沒有正式環境的負面驗收；Mode A 需要的「本頁實際簽名的地址」也還沒有和任務綁定。→ WP-4
2. **阻擋項 2（送出前由客戶端獨立驗證）的骨架做得很好，但正式環境目前沒有任何合法路徑能通過。**
   - 部署核准清單是空的 `[讀碼 src/trusted-lista.mjs:22]`、`[讀碼 ../config/protocols/lista-bsc.json deployment_approvals.entries=[]]`，所以重讀永遠只會是「資料不足」`[讀碼 src/lista-reread.mjs:421-424]`。
   - 送出關卡的「模擬完成」取自伺服器給的 `plan.simulation` `[讀碼 src/gate.mjs:66]`，這正是阻擋項 2 要求不能信任的輸入。
   - 原地址取自伺服器的 `plan.sender`，沒有和本頁簽 SIWE 的地址比對 `[讀碼 src/main.ts:628,1005]`。
   - → WP-5
3. **阻擋項 3（版本化部署允許清單＋產物層級「送出關閉」負面測試）：目前正式 bundle 仍內含測試用的送出程式碼，而且舊部署還能公開存取。**
   - 正式 bundle 含 `eth_sendTransaction`×2、`testOnlyAllowWalletRequest`×3、`noBroadcast`×1 `[實測 GET 2026-10-14]`，來源是 `src/mode-a.mjs:476-485,797-826` `[讀碼]`。
   - 現有產物測試只驗證執行時沒有送出呼叫，沒有證明 bundle 裡送出路徑不可達。
   - 舊的 hash 部署 `5a09b4e6.…pages.dev` 還能公開存取：沒有 CSP，bundle 含舊的送出程式碼 `[實測]`。
   - Lista 實作的版本化核准 pin 是空的（見第 2 點）。
   - 本報告將這一項解讀為「Lista 部署核准 pin」加上「客戶端部署／產物清冊」兩部分 `[推論]`。
   - → WP-1、WP-3、WP-5
4. **阻擋項 4（發行路由和客戶端路由一致）：主要路徑一致，但有三個缺口。**
   - Python 舊函式 `issue_task_link` 接受任意 https origin，`build_lista_task` 還在使用它 `[讀碼 src/guardian/actions/workbench.py:76-89,476]`。
   - `data_path` 欄位沒有任何程式讀取。
   - 沒有任何一個測試斷言 wrangler `AUTH_URI`、origin policy、Python 發行、server `viewLink`、client router、`_redirects` 指向同一個值。
   - `vercel.json` 是殘留設定（沒有 rewrite、header、Functions）；GitLab `pages` job 會發布一份不含 Functions 的副本。
   - → WP-1、WP-2
5. **阻擋項 5（真錢包實測）：全部標為 REAL_WALLET_PENDING `[文件 docs/wallet-connect-01.md:88-92,211-216]`。**
   - 另外有兩個結構性缺口：
     - WalletConnect 只核准 `personal_sign`，所以 S24「Chrome → 幣安 App → Chrome」這條路徑目前無法送交易 `[讀碼 src/wallets/walletconnect-config.mjs:20; src/wallets/walletconnect.mjs:345-346]`。
     - Mode A 的進行中狀態只存在記憶體裡 `[讀碼 src/main.ts:160]`，手機切到錢包 App 再回來時，分頁若被回收，pending 就會遺失。
   - 拒簽目前被歸成 `failed`，而且同一頁內不能再試 `[讀碼 src/mode-a.mjs:108-110,373-386]`。
   - → WP-6、WP-7、WP-8

**工作包（依賴順序）**：

- WP-1 部署收斂與產物清冊
- WP-2 路由契約（依賴 WP-1）
- WP-3 送出程式隔離與產物負面測試（依賴 WP-1）
- WP-4 授權補強（可平行）
- WP-5 客戶端自有證據（依賴 WP-3、WP-4）
- WP-6 結果生命週期（依賴 WP-5）
- WP-7 送出變體（依賴 WP-3、WP-5、WP-6，WalletConnect 範圍待決定）
- WP-8 真錢包實測：Phase A 在 WP-1/2/4 完成後即可開始；Phase B/C 要等 WP-7 完成並取得使用者書面授權

**最關鍵 3 個風險**：

- (R1) 部署核准是空的，而送出關卡又依賴伺服器給的模擬結果，所以 Mode A 今天沒有合法的開啟路徑。不能用「改旗標」走捷徑，必須補上客戶端自有證據並由人審核 pin。
- (R2) 正式產物內含送出程式碼，又沒有 bundle 層級的不可達證明；舊部署仍公開可取；部署和 commit 之間沒有溯源。
- (R3) 手機 WalletConnect 不能送交易，進行中狀態也撐不過手機重新整理。使用者要測的 S24 送出路徑目前還不存在。

---

## 2. 阻擋項現況盤點

### 2.0 總表

| # | 阻擋項 | 已有 | 缺口 | 判定 |
|---|---|---|---|---|
| 1 | 任務存取授權 | SIWE＋nonce CAS、ACL JOIN、404 一致、登出撤銷、CSRF＋Origin、IP 限流、D1-01..09（文件） | 連帶撤銷、撤銷全部 session、閒置逾時、session rotation、正式環境負面驗收、登入地址↔任務綁定 | 大致完成，需補強（WP-4） |
| 2 | 客戶端送出前獨立驗證 | 信任 RPC／ABI／proxy、EIP-1898、uuid 定位、calldata 重建、TTL、兩次重讀＋settle、忽略 preflight_ok | 核准 pin 空 → 永不 pass；模擬取自伺服器；帳戶取自 plan.sender；送出前沒有即時讀身分；只有單一 RPC；EIP-1898 未在公開 RPC 實測 | 骨架完成，無法通過（WP-5） |
| 3 | 版本化部署允許清單＋產物送出關閉負面測試 | 執行時「強制點擊＝0 筆送出」測試（D1-09、WA、WC-ARTIFACT）；建置時產生 `_headers`；origin policy 檔 | 正式 bundle 含送出程式碼；沒有靜態或模組圖斷言；部署↔commit↔雜湊沒有清冊；舊部署仍公開；Lista pin 空 | 未完成（WP-1、WP-3、WP-5） |
| 4 | 發行路由↔客戶端路由一致 | `/t/{ref}` 與 `/api/tasks/{ref}` 在 server、client、`_redirects`、`_routes` 一致；Python 有讀 policy 的發行函式 | Python 舊發行函式接受任意 origin；`data_path` 是死設定；沒有跨檔契約測試；publish 工具不驗證回傳連結；Vercel／GitLab 殘留 | 部分完成（WP-1、WP-2） |
| 5 | 真錢包實測 | 離線／合成／產物測試（mock relay、mock 錢包） | 全部 REAL_WALLET_PENDING；WalletConnect 不能送交易；進行中狀態不持久；拒簽分類錯誤；沒有證據範本 | 未開始（WP-6、WP-7、WP-8） |

### 2.1 阻擋項 1：任務存取授權

**已有**

- 路由表只有 7 條；`/api/rpc` 與 `/complete` 一律 404 `[讀碼 action-client/src/server/handle.mjs:6-14,21-26]`。
- `Sec-Fetch-Site: cross-site` 回 403 `[讀碼 handle.mjs:17-19]`。所有 POST 路由的 `Origin` 必須等於 `AUTH_URI` 的 origin `[讀碼 handle.mjs:39-44]`。
- 挑戰：綁定 domain、uri、chain、地址；有三層限流（IP、IP＋地址、全域未消耗上限）；發行前會先刪除過期列 `[讀碼 src/server/auth.mjs:63-130]`。
- 驗證：attempt cookie、CSRF、訊息逐字比對、EOA 回復地址；nonce 以 `consumed=0` 條件更新，只會成功一次 `[讀碼 auth.mjs:132-185]`。不支援 ERC-1271 `[讀碼 src/server/siwe.mjs:33-41]`。
- Cookie：`__Host-`、Secure、HttpOnly、`SameSite=Lax`，不設 Domain `[讀碼 src/server/http.mjs:54-55]`。
- ACL：任務可見的條件是 `task_acl` 與 `tenant_members` 兩表 JOIN 都符合 session 地址 `[讀碼 src/server/tasks.mjs:37-50]`。不存在、無權限、格式錯誤都回同一個 404 `[讀碼 tasks.mjs:22-24,65-66]`。
- ref 是 `ref_` 加 CSPRNG 產生的 16 bytes `[讀碼 tasks.mjs:201]`。
- 發布任務時，`plan.sender` 必須等於憑證綁定的錢包 `[讀碼 tasks.mjs:217-219]`。
- 登出會在伺服器端寫入 `revoked_at` `[讀碼 auth.mjs:206-227]`。
- 正式環境觀察：`/api/session` 回 200 JSON＋no-store；未登入讀 `/api/tasks/ref_0…` 回 401；`/api/rpc` 回 404 `[實測 GET 2026-10-14]`。

**缺口**

- session 只有 12 小時絕對時效，沒有閒置逾時，也沒有 `auth_time` `[讀碼 src/server/db.mjs:43; migrations/0001_p1_init.sql:17-24]`。
- 發布任務時會自動建立 membership（`ON CONFLICT DO NOTHING`）`[讀碼 tasks.mjs:228-230]`。工具只能撤銷 credential `[讀碼 action-client/tools/bootstrap-publisher.mjs:138-145]`，membership 與 session 不會連帶撤銷 `[文件 docs/reviews/2026-10-07-p1-client-lifecycle.md:63]`。
- 沒有「撤銷某地址的全部 session」。
- 舊 verify 回應晚到時，原 session 的資料列會保留到過期 `[文件 docs/wallet-connect-01.md:52]`。
- `--revoke <id>` 沒有檢查格式就直接插進 SQL `[讀碼 bootstrap-publisher.mjs:140,151-152]`。只有操作者能觸發，屬低風險。
- 客戶端的 `sessionAddress` 取自伺服器回應 `[讀碼 src/main.ts:1160-1167]`，沒有和 `plan.sender` 比對（`sessionAddress` 只出現在 :538、:743、:772、:1167、:1347）`[讀碼]`。
- 遠端驗收只有 3 條 curl `[文件 docs/d1-private-task-live.md:81-88]`；本人登入驗收還沒做 `[文件 同檔:90-92]`。

### 2.2 阻擋項 2：客戶端送出前獨立驗證

**已有**

- 任務 JSON 不能指定 RPC、ABI 或 proxy `[讀碼 src/trusted-lista.mjs:64-75]`。
- 讀取方法有白名單 `[讀碼 trusted-lista.mjs:25-32,77-81]`。
- 狀態讀取一律用 EIP-1898 的 `{blockHash, requireCanonical:true}` `[讀碼 src/lista-reread.mjs:120-125,217-223]`。
- 以 uuid 定位提款請求（不用計畫裡的舊 idx）`[讀碼 lista-reread.mjs:136-143]`。
- 用觀察到的 idx 重建 calldata，並與計畫比對 `[讀碼 lista-reread.mjs:188-199,393-394]`。
- 有效期取 TTL、區塊時間、核准期限中最早者 `[讀碼 lista-reread.mjs:248-269]`。
- 引導流程做兩次重讀、檢查身分漂移，並以即時時鐘跑 `settleReread` `[讀碼 src/mode-a.mjs:697-785]`。
- 任務的 `preflight_ok` 被強制當成 false `[讀碼 src/gate.mjs:62]`。
- review R9 對送出關卡的疑慮，已在 reconcile 中處理：`preflightOk` 強制為 false，預設改為 fail-closed `[文件 docs/reviews/2026-10-07-reconcile-main.md:26-37; 讀碼 src/gate.mjs:40-50,62]`。R9 其餘未完成的項目（客戶端自有 preflight、精確解碼、等待錢包回傳、收據持久化）就是 WP-5／6／7 要實作的內容 `[文件 docs/reviews/2026-10-07-concurrent-main.md:33]`。

**缺口**

1. 部署核准清單是空的 `[讀碼 trusted-lista.mjs:21-22]`，測試甚至斷言它必須是空的 `[讀碼 src/lista-reread.test.mjs:183-184]`。結果一律是 `unapproved_deployment`，重讀不可能 pass `[讀碼 lista-reread.mjs:421-424]`。
2. 送出關卡的 `simulationComplete` 來自 `plan.simulation`，也就是伺服器給的值 `[讀碼 gate.mjs:66; main.ts:665,894,995]`。preview validator 又會拒絕宣稱 PASS 的計畫 `[文件 docs/reviews/2026-10-07-reconcile-main.md:35]`，所以這個關卡在結構上不可能滿足。ADR 0001 也寫明「沒有 Tenderly trace，因此不能開放錢包請求」`[文件 docs/decisions/0001-provider-selection.md:17]`。換成客戶端自有的效果檢查需要使用者決定（見 D-2）。
3. 原地址取自 `plan.sender` `[讀碼 main.ts:628,1005]`，客戶端沒有用自己簽過 SIWE 的地址核對。
4. 送出前的身分取自事件更新的頁面狀態 `[讀碼 main.ts:1007-1017]`，不是即時讀取 `eth_accounts`／`eth_chainId`。對照：登入流程在簽名前會重新讀取 `[讀碼 main.ts:1366]`。
5. 只有一個 RPC `[讀碼 trusted-lista.mjs:9]`；EIP-1898 沒有在公開 BSC RPC 上實測過 `[文件 docs/p2-lista-reread.md:5]`。
6. `clientConfirmDecision` 在 `block_reasons` 缺漏時會把 `planBlocked` 算成 false，屬於 fail-open `[讀碼 gate.mjs:67]`。目前前面還有其他條件擋住，但仍應改成 fail-closed。
7. 本機檔案與合成範例都會渲染出確認鈕 `[讀碼 main.ts:1702-1726]`。目前不能送出，但將來開放時必須把它們排除。

**伺服器被入侵時的影響上限** `[推論]`：
- 客戶端已釘死 proxy、selector、`value=0`、chain 56，並從鏈上的 uuid／idx 重建 calldata；`from` 必須等於已連線帳戶。所以惡意伺服器最多只能讓使用者「領自己另一筆可領的請求到自己地址」。
- 前提是 `claimWithdraw` 把款項付給 `msg.sender`。這點必須在核准實作的原始碼審核時確認（WP-5）。
- 這個上限只在客戶端 bundle 可信時成立（見 T-06）。

### 2.3 阻擋項 3：版本化部署允許清單＋產物層級送出關閉

**已有**

- 執行時負面測試（只有文件與程式，我沒有執行）：
  - D1-09：偽造任務＋強制點擊＝0 筆送出 `[文件 docs/d1-private-task-live.md:79]`。
  - WA 產物測試：`[讀碼 integration/wallets-artifact.test.mjs:30-33]`。
  - WC-ARTIFACT-01：`[讀碼 integration/walletconnect-artifact.test.mjs:252-253]`。
- 靜態字串掃描只用在 WalletConnect 端點 `[讀碼 walletconnect-artifact.test.mjs:233-235]`。
- `_headers` 由 `src/csp.mjs` 在建置時產生 `[讀碼 vite.config.ts:8-16; src/csp.mjs:16-49]`。
- 檢視來源 policy `[讀碼 ../config/deployments/private-task-origins.json:1-15]`。
- Lista 核准 registry 的格式已就緒（有 `registry_version`、`provenance`、`validUntil` 語意）`[讀碼 lista-reread.mjs:300-341]`。

**缺口與實測**

- 正式 bundle `index-*.js` 含 `eth_sendTransaction`×2、`wallet_sendCalls`×1、`testOnlyAllowWalletRequest`×3、`noBroadcast`×1 `[實測 GET 2026-10-14]`。對照原始碼是 `exactClaimRequest`、`assertSingleStep`、`isNoBroadcastRecorder`，以及 `provider.request(prepared)` 這個呼叫點 `[讀碼 mode-a.mjs:458-504,797-826]`。
  - 唯一擋住它的是 `main.ts` 傳入的 `testOnlyAllowWalletRequest:false` 與 `provider:null` `[讀碼 main.ts:1021-1023]`。
  - reconcile review 寫的「production bundle 不含 eth_sendTransaction」已經過時：該 review 早於 mode-a.mjs `[文件 docs/reviews/2026-10-07-reconcile-main.md:69]`。
  - 反過來看，`request-policy.mjs` 的 `REFUSED_METHODS` 字串沒有出現在正式 bundle `[實測]`，推測是被 tree-shake 掉了 `[推論]`。所以把送出模組拆出去之後，主 chunk 掃描結果可以是 0 `[推論]`。
- `reverifyCurrent(testApprovals?)` 這個測試注入參數留在正式程式碼裡 `[讀碼 main.ts:574,637]`。目前只有無參數呼叫 `[讀碼 main.ts:1050]`。
- 舊部署 `https://5a09b4e6.wallet-guardian-action-client.pages.dev/`（網址出處：`[文件 docs/deploy.md:40]`）：
  - 仍回 200，沒有 `content-security-policy` 標頭；
  - bundle 只有 10 KB，含 `eth_sendTransaction` 與 `preflight_ok`，沒有 `/api/auth`；
  - `/api/session` 回 HTML（沒有 Functions）`[實測 GET 2026-10-14]`。
  - 它是另一個 origin，所以 `__Host-` cookie 與 SIWE（受 Origin 檢查）都碰不到 `[讀碼 handle.mjs:39-44]`。風險是混淆與釣魚面，而且違反「版本化部署允許清單」的精神。
- 沒有「origin → bundle 雜湊 → commit → 建置旗標」的清冊。Python release manifest 只涵蓋 wheel `[讀碼 tools/release_manifest.py:117-119]`。
- 文件和實況不一致：
  - README 說「已上線的 Pages 套件是合併前的建置」`[文件 README.md:195]`，但實測正式 bundle 已含 Mode A 與 WalletConnect。
  - deploy.md 對同一件事有兩種說法（「VERIFIED_ORIGIN」與「這份路由變更尚未部署」）`[文件 docs/deploy.md:3,5]`。
  - deploy.md 的部署指令用了錯的專案名 `--project-name wallet-guardian` `[文件 docs/deploy.md:23]`，wrangler 的實際專案名是 `wallet-guardian-action-client` `[讀碼 action-client/wrangler.jsonc:5]`。

### 2.4 阻擋項 4：發行路由↔客戶端路由一致

| 來源 | 任務路徑 | 資料路徑 | origin 來源 | 證據 |
|---|---|---|---|---|
| origin policy | `/t/{ref}` | `/api/tasks/{ref}`（**沒有程式讀取**） | 檔案本身 | `[讀碼 ../config/deployments/private-task-origins.json:7-9]` |
| Pages API `viewLink` | `entry.task_path` | — | `AUTH_URI` origin 對 policy | `[讀碼 src/server/origins.mjs:8-52; tasks.mjs:206]` |
| Python `issue_private_task_link` | `entry.task_path` | — | policy | `[讀碼 src/guardian/actions/workbench.py:117-151]` |
| Python `issue_task_link`（舊） | 寫死 `/t/{ref}` | — | **呼叫端任意 https 字串** | `[讀碼 workbench.py:76-89]` |
| Python `build_lista_task` | 用舊函式 | — | 呼叫端 | `[讀碼 workbench.py:458-482]` |
| publish 工具 | 直接印出 server 回傳的 `view_url`，不比對 | — | policy | `[讀碼 action-client/tools/publish-private-task.mjs:40-47,113-121]` |
| client router | `^/t/(ref_[a-f0-9]{32})/?$` | `/api/tasks/${ref}` | 同源 | `[讀碼 src/private-task.mjs:7; main.ts:1178,1652-1671]` |
| server 路由 | — | `^/api/tasks/(ref_[a-f0-9]{32})$` | — | `[讀碼 handle.mjs:11-12]` |
| Pages 靜態 | `_redirects`: `/t/*`→`/index.html 200` | `_routes.json` include `/api/*` | — | `[讀碼 public/_redirects:3; public/_routes.json:3]` |
| wrangler | — | — | `AUTH_DOMAIN`／`AUTH_URI` 寫死 | `[讀碼 wrangler.jsonc:10-16]` |
| 測試常數 | — | — | 寫死 origin | `[讀碼 integration/wallets-artifact.test.mjs:29; tests/unit/test_workbench.py:403]` |

- 舊函式的測試本身就以 `https://wallet.example` 斷言會發行連結 `[讀碼 tests/unit/test_workbench.py:60-72]`，這和 AGENTS.md:10「VIEW 連結只來自 private-task-origins.json」衝突。
- `vercel.json` 只有 framework、build、install、output 設定 `[讀碼 action-client/vercel.json:1-6]`。若部署到 Vercel：
  - `_headers`、`_redirects` 都不生效 → 沒有 CSP、`/t/<ref>` 回 404；
  - `functions/` 也不會執行 → `/api/*` 不存在 `[推論，依 Vercel 慣例]`。
  - 試了三個可能的 Vercel 主機名，都回 `DEPLOYMENT_NOT_FOUND` `[實測]`。這不能證明 Vercel 上完全沒有專案。
  - deploy.md 仍把 Vercel 列為部署選項 `[文件 docs/deploy.md:22]`。
- GitLab `pages` job 用沒有釘版的 `node:22` 建置，發布到 `public/`，而且沒有 Functions `[讀碼 ../.gitlab-ci.yml:53-68]`。目前該網址 302 導到 GitLab 登入，也就是私有存取 `[實測]`。
- Cloudflare Pages 對未知路徑一律回 `index.html` 200（例如 `/tasks/x.json`）`[實測]`。所以 `_redirects` 的 `/t/*` 目前其實是多餘的，但仍應保留，免得日後加入 404.html 時失效。

### 2.5 阻擋項 5：真錢包實測

- 所有真機項目都標 REAL_WALLET_PENDING `[文件 docs/wallet-connect-01.md:88-92,211-216; docs/action-client-ui.md:42]`。
- WalletConnect：
  - proposal 只有 `personal_sign`，其他方法回 4200 `[讀碼 walletconnect-config.mjs:20; walletconnect.mjs:343-346]`；
  - dapp metadata 沒有 `redirect`，簽完不會自動回到 Chrome `[讀碼 src/wallets/walletconnect-sdk.mjs:52-57]`；
  - 跳轉連結只用於 pairing，沒有用於後續請求 `[讀碼 walletconnect-config.mjs:101-111]`。
- 拒簽處理：
  - `userRejected` 只認 4001 或特定訊息 regex `[讀碼 mode-a.mjs:84-88]`；
  - 拒簽 → `failed` `[讀碼 mode-a.mjs:108-110]`，而 `retryModeA` 會擋下所有非 idle 狀態 `[讀碼 mode-a.mjs:373-386]`；
  - 其他錯誤一律 → `ambiguous`／`unknown` `[讀碼 mode-a.mjs:90-97]`。
- 進行中狀態只在記憶體 `[讀碼 main.ts:160]`。
- 收據核對要用 `eth_getTransactionReceipt`／`eth_getBalance` 等方法，但這些不在讀取白名單 `[讀碼 trusted-lista.mjs:25-32 對照 mode-a.mjs:255-329]`。官方 dataseed 停用 `eth_getLogs` `[文件 docs/decisions/0001-provider-selection.md:11]`。

---

## 3. 威脅情境

| ID | 情境 | 現有控制 | 殘餘風險 | WP |
|---|---|---|---|---|
| T-01 | 猜 ref／IDOR | 128-bit CSPRNG ref `[讀碼 tasks.mjs:201]`；ACL JOIN `[tasks.mjs:37-50]`；不存在、無權限、格式錯誤一律 404 `[tasks.mjs:22-24]` | 沒有測試斷言 404 回應逐位元組相同；200 回應帶 `tenant_id` `[讀碼 tasks.mjs:90]`（只回給本人，低） | WP-4 |
| T-02 | 跨租戶讀取 | 兩表 JOIN；呼叫端帶的 `tenant_id` 會被拒 `[讀碼 tasks.mjs:108-110,182-193]` | membership 不能撤銷；publisher 可重建自己錢包的 membership `[文件 p1-client-lifecycle.md:63]` | WP-4 |
| T-03a | SIWE 重放／domain 不符 | nonce 只消耗一次 `[讀碼 auth.mjs:179-185]`；訊息由伺服器保存並逐字比對；客戶端以頁面 origin 核對挑戰 `[讀碼 main.ts:1357-1358]` | 低 | 回歸 |
| T-03b | 釣魚站在伺服器端轉送 SIWE（伺服器端請求可以偽造 Origin） | 錢包端的 SIWE domain 警告（依錢包而定）`[推論]` | 受害者若照簽，攻擊者能讀和取消其任務（不涉及資金）`[推論]`。緩解：縮短 session、Mode A 送出不以 session 作為資金授權、session 能力保持最小 | WP-4、WP-5 |
| T-04 | nonce 並行重放 | `consumed=0` 條件更新；D1-02 三路並行 `[文件 d1-private-task-live.md:72]` | 低 | 回歸 |
| T-05 | 伺服器被入侵後下發惡意 calldata／任務 | client 釘死 proxy、selector、value、chain，並以鏈上 idx 重建 calldata（見 2.2） | 帳戶取自 `plan.sender`；模擬取自伺服器；最壞是領自己另一筆請求 `[推論]` | WP-5、WP-7 |
| T-06 | DNS／部署被換 bundle（Cloudflare 帳號、部署管線） | CSP 只放行 `'self'` `[實測]`；錢包畫面會顯示 to 與 data | 同源攻擊者能控制整個驗證器；沒有部署清冊與遠端雜湊比對；舊部署仍公開 `[實測]` | WP-1、WP-7（「錢包畫面應該看到」檢查清單） |
| T-07 | WalletConnect 會話劫持（symKey 外洩、XSS 讀 IndexedDB、惡意 peer 回傳假 hash） | pairing URI 不落 DOM、儲存、API、log `[文件 wallet-connect-01.md:166-167,197]`；facade 只轉送 `personal_sign` | 惡意 peer 若能送交易，可回傳假 tx hash；恢復的 session 可能屬於另一個帳戶 | WP-6（hash 一律以收據核對）、WP-7 |
| T-08 | Deep link 被攔截（`bnc://`、`wc:` 被其他 App 註冊） | 跳轉連結只取自版本化設定 `[讀碼 walletconnect-config.mjs:70-81]` | Android 的自訂 scheme 或 chooser 可能被惡意 App 接走 pairing URI → 假錢包 `[推論]`。影響是登入成攻擊者自己的地址，或送出時回傳假 hash | WP-7、WP-8（優先用驗證過的 https universal link） |
| T-09 | 使用者拒簽後狀態錯誤 | 拒簽不會自動重送 | 拒簽被當成 failed 而卡住；非 4001 的拒簽被當成 unknown；重新整理後回到 idle | WP-6 |
| T-10 | 送出前切帳號或切鏈 | 事件觸發 dropReread；身分漂移檢查 `[讀碼 main.ts:531-557; mode-a.mjs:591-599]` | 沒有事件時（例如 WalletConnect session 被另一裝置更新）只能靠快取 | WP-5（即時讀） |
| T-11 | 舊或回滾的 bundle 仍可由 hash URL 取得 | 正式 bundle 的 POST 有 Origin 檢查，hash 網域登入不了 | 舊 bundle 沒有 CSP、含送出程式碼 `[實測]`；未來 send 變體的 hash URL 會永久存在 `[推論]` | WP-1 |
| T-12 | 本機檔案或合成範例誤入送出路徑 | 目前送出路徑全部關閉 | 將來開放時沒有「只限私人 API 任務」的規則 | WP-5、WP-7 |
| T-13 | 點擊劫持、CSRF | `frame-ancestors 'none'`、XFO DENY `[實測]`；CSRF＋Origin | 低 | 回歸 |
| T-14 | 手機重新整理或分頁回收造成重複送出 | 已領取的請求會被重讀判成 `claimable_missing` | 交易還沒上鏈時 uuid 仍在 → 可能送第二次；上鏈後 idx 位移，第二筆會領到另一筆自己的請求 `[推論]` | WP-6 |

---

## 4. 工作包（依賴順序）

```
WP-1 部署收斂＋產物清冊 ──┬─> WP-2 路由契約
                         └─> WP-3 送出程式隔離＋產物負面測試 ─┐
WP-4 授權補強（平行）─────────────────────────────────────┴─> WP-5 客戶端自有證據 ─> WP-6 結果生命週期 ─> WP-7 送出變體
WP-8 真錢包：Phase A（WP-1/2/4 後）；Phase B/C（WP-7 後＋使用者書面授權）
```

共通「明確不做」（每包都適用，對應 AGENTS.md）：

- agent、排程、MCP 永不簽名或廣播（AGENTS.md:6、17）。
- `requestUserSend` 在 production 預設 build 中無條件拒絕（AGENTS.md:20）。
- 不加 client COMPLETE write、不加通用 `/rpc`（AGENTS.md:12）。
- 不支援 ERC-1271 登入。
- tx hash 不等於完成（AGENTS.md:23）。
- 不建立新智慧帳戶冒充原地址（AGENTS.md:22）。
- 不要求 EIP-7702 委派、無限授權、新 admin、帳戶升級（AGENTS.md:14）。
- 錢包自報的名稱、rdns、icon 不是身分（AGENTS.md:11）。
- 不開公開 port、不買方案（AGENTS.md:26）。
- 不把 `VITE_GUARDIAN_MAINNET_SEND=true` 當成授權。
- 不翻 `fixed_https_origin`。

---

### WP-1 部署目標收斂、舊部署清理與產物清冊

- **目標**：
  - 正式環境只有一個部署目標：Cloudflare Pages 專案 `wallet-guardian-action-client`。
  - 每個仍可存取的 origin 上，跑的是哪個 commit、哪份產物（逐檔雜湊）、哪些建置旗標，都能從清冊查到，並能遠端比對。
  - 殘留設定不能產生沒有 CSP 或沒有 API 的變體。
- **依賴**：無。
- **對應阻擋項**：3（部署面）、4。
- **範圍（檔案）**：
  - 刪除 `action-client/vercel.json`。
  - `../.gitlab-ci.yml:53-68` 的 `pages` job：刪除，或改成只上傳 build artifact、不發布 Pages，並把 image 釘到 `node:22.16.0`。
  - `docs/deploy.md`：移除 Vercel／GitLab 選項（:21-22），修正專案名（:23），處理 :3 與 :5 的矛盾；修正 `README.md:195`。
  - 新增 `../config/deployments/action-client-releases.json`，以及對應的 `schemas/action-client-release.schema.json`。每筆欄位：
    - `release_id`、`deployment_id`（對應 private-task-origins）、`origin`、`source_sha`、`client_build`；
    - `variant`（`send_off`／`mode_a`）、`walletconnect`、`mainnet_send:false`；
    - `files`（路徑對 sha256）、`headers_sha256`、`cf_deployment_url`；
    - `status`（`active`／`retired`／`deleted`）。
  - 新增 `action-client/tools/artifact-manifest.mjs`：讀 `dist/`，產生逐檔 sha256 與 `_headers` 雜湊。
  - 新增 `action-client/tools/verify-deployed-artifact.mjs`，只發 GET：
    - 讀 `/`，解析 index 引用的所有 assets（含 lazy chunk 清單），逐檔比對清冊；
    - 比對 CSP 標頭與 `_headers`；
    - 檢查 `/api/session` 是 JSON＋no-store、`/api/rpc` 回 404、`/t/ref_0…` 回 200 HTML；
    - 提供 `--expect-gone` 模式。
  - client 加入 origin 自我檢查：若 `location.origin` 不在建置時嵌入的核准清單，就不呼叫任何錢包方法（含 `eth_requestAccounts`），並顯示「此網址不是核准來源」。開發用的 `http://127.0.0.1:43127` 只在非 production 模式下列入允許清單；`test:runtime` 已用 host-resolver-rules 把正式 host 導到本機，不受影響。
  - 新增測試 `action-client/tests/deploy-targets.test.mjs`。
- **明確不做**：
  - agent 不執行 `wrangler pages deploy` 或刪除部署。實際部署與刪除由使用者執行，或經使用者明確授權後執行。
  - 不買 Cloudflare 方案、不改 D1、不動 send 旗標。
- **設計要點**：
  - Pages Direct Upload 的每次部署都有不可變的 `<hash>.<project>.pages.dev` URL `[推論，佐證：5a09b4e6 仍可存取 實測]`。清冊必須逐一列出這些 URL 與狀態。
  - 舊的 P1 前部署必須刪除（使用者操作）。
  - origin 自我檢查擋不住「舊 bundle」，只能保護之後的版本。所以刪除舊部署和 server 端 Origin 檢查 `[讀碼 handle.mjs:39-44]` 都要保留，三者互補。
- **驗收條件（可自動化）**：
  - AC1：`verify-deployed-artifact.mjs --origin <正式> --release <id>` 在逐檔雜湊、CSP、API 探測都符合時 exit 0。
    - 負面：本機 mock 伺服器竄改一個 asset 的 1 byte → exit≠0，並列出檔名。
    - 負面：少一個 lazy chunk → exit≠0。
  - AC2：清冊中標為 `deleted` 的 URL（含 5a09b4e6）用 `--expect-gone` 檢查時，若仍回傳 client HTML 就 exit≠0（今天預期失敗，這就是現況證據）。
  - AC3：`deploy-targets.test.mjs` 斷言以下各項，任一條件違反即失敗：
    - repo 內沒有 `vercel.json`、`netlify.toml`；
    - `.gitlab-ci.yml` 沒有發布 `public/` 的 job；
    - `docs/deploy.md` 不含 `vercel --prod`。
  - AC4：清冊 schema 測試。
    - 負面：`variant: send_off` 卻 `mainnet_send: true`，或 `origin` 不在 private-task-origins.json → 失敗。
  - AC5：headless Chrome 以非核准 origin（`--host-resolver-rules` 指向本機 dist）載入 → 錢包方法呼叫 0 次、登入鈕 disabled。
- **回歸測試**：`npm run test:api`、`test:wallets`、`test:wallets:artifact`、`test:runtime`（D1-01..09）、`uv run --frozen pytest -q`。
- **人工驗證**：
  - 使用者在 Cloudflare dashboard 列出所有 deployments，刪除舊的。
  - 確認帳號 2FA 與 API token 權限最小。
  - 確認 GitLab Pages 仍是私有存取。
  - 部署新版後跑 AC1。
- **風險與 rollback**：
  - 刪除部署不可逆，但舊部署本來就不該對外服務。
  - 清冊若寫錯，verifier 會誤報 → 先以報告模式跑一輪。
  - rollback：依清冊上一筆 `active` release 的 dist 重新部署（使用者執行），之後跑 AC1。

### WP-2 發行路由與客戶端路由一致（路由契約）

- **目標**：Python 發行、Pages `viewLink`、publish 工具、client router、`_redirects`／`_routes`、wrangler `AUTH_*`、測試常數，全部由同一份 policy 推導，並由一個契約測試保證一致。
- **依賴**：WP-1（清冊欄位）。
- **對應阻擋項**：4。
- **範圍**：
  - `src/guardian/actions/workbench.py`：
    - `issue_task_link`（:76-89）刪除，或改成只委派 `issue_private_task_link`；
    - `build_lista_task`（:476）改用 policy，不再接受任意 origin。
  - `tests/unit/test_workbench.py:60-72` 改寫成負面斷言（`:334-335` 本來就是負面案例，保留）。
  - `../config/deployments/private-task-origins.json` 的 `data_path`：二選一 —
    - 刪除；
    - 或由 server 路由與 client fetch 的契約測試讀取。
  - `action-client/tools/publish-private-task.mjs:113-121`：比對 `view_url === entry.origin + task_path.replace("{ref}", ref)`，而且 ref 必須符合格式，不符就 exit≠0。
  - `integration/*.test.mjs` 的 `ORIGIN` 常數改讀 policy。
  - 新增 `action-client/tests/route-contract.test.mjs` 與 `tests/unit/test_route_contract.py`。
- **明確不做**：
  - 不恢復 `?ref=` 或 `/tasks/<ref>.json`。
  - 任務或呼叫端不能提供 origin。
  - 不加 `/rpc`。
  - 不翻 `fixed_https_origin`（AGENTS.md:9-10）。
- **設計要點**：
  - ref 的語法只有一個來源：`^ref_[a-f0-9]{32}$`。目前分散在四處：`workbench.py:19`、`tasks.mjs:10`、`handle.mjs:11`、`private-task.mjs:3,7`。契約測試要逐一比對。
- **驗收條件**：
  - AC1：對 policy 中每個正式 entry 斷言：
    - wrangler `AUTH_URI` 的 origin 等於 `entry.origin`，`AUTH_DOMAIN` 等於 host；
    - Python `issue_private_task_link(ref).url` 等於 server `viewLink(origin, ref).url`；
    - `parsePrivateTaskRef(new URL(url).pathname) === ref`；
    - `_redirects` 含 `/t/*` 規則，`_routes.json` 含 `/api/*`；
    - server 路由 regex 能匹配 `data_path`（若保留）。
  - AC2（負面）：`build_lista_task(plan, ref=REF, origin="https://evil.example")` 不發行連結（或該參數已移除）；`issue_task_link(REF, "https://wallet.example")` 不再得到 `issued=True`。
  - AC3（負面）：mock server 回 `view_url: "https://evil.example/t/ref_…"` → publish 工具 exit≠0。
  - AC4（負面）：ref 為大寫 hex、31 位、或 `/t/ref_…/x` → client 顯示格式錯誤，server 回 404 JSON。
  - AC5：除了 policy、wrangler、docs、reviews 之外，程式碼與測試中不得硬編碼正式 origin（grep 測試，附允許清單）。
- **回歸測試**：`uv run --frozen pytest -q`、`npm run test:api`、`test:runtime`。
- **人工驗證**：照 `docs/d1-private-task-live.md:41-52` 發一筆測試任務，在桌機與 S24 Chrome 打開回傳連結，確認打開的是同一個 client 並要求登入。
- **風險與 rollback**：Python API 變更可能影響外部呼叫端。repo 內只有 workbench 自己呼叫 `[讀碼 grep]`。rollback 用 revert。

### WP-3 送出程式與 production 產物隔離＋產物層級「送出關閉」負面測試

- **目標**：production 預設 bundle（`send_off`）不包含任何會建構或提出交易請求的程式碼，並在 dist 與已部署 origin 兩個層級證明這件事。
- **依賴**：WP-1。
- **對應阻擋項**：3。
- **範圍**：
  - 拆分 `src/mode-a.mjs`：
    - 唯讀部分留下：結果狀態、`verifyClaimEconomics`、`materialClaimChanges`、引導流程的重讀與 settle；
    - `exactClaimRequest`、`assertSingleStep`、`isNoBroadcastRecorder`、`provider.request(prepared)`（:458-504,797-826）移到 `src/send/mode-a-request.mjs`；
    - 引導流程只回傳純資料的 `preparedClaim`（不含 method 字串）。
  - `vite.config.ts` 加入：
    - `define: { __WG_SEND_VARIANT__: "off" }`；
    - 一個 rollup plugin：variant 為 off 時，模組圖若含 `src/send/**` 就讓 build 失敗。
  - 移除 `main.ts:574,637` 在正式程式中可達的 `testApprovals` 參數，改由測試 harness 注入。
  - `src/gate.mjs:67` 改為 fail-closed：`block_reasons` 缺漏時 `planBlocked=true`。
  - 新增 `action-client/tools/scan-send-markers.mjs` 與 `integration/sendoff-artifact.test.mjs`，以及 npm script `test:artifact:sendoff`。
  - `tests/mode-a-nobroadcast.test.mjs` 改從 `src/send/` 匯入，case 數不得減少。
- **明確不做**：
  - 不實作真送出（WP-7 才做）。
  - 不改 request-policy 的 allow 規則。
  - 不刪除既有 runtime 負面測試。
- **設計要點**（三層證明）：
  1. 原始碼層：從 `src/main.ts` 遞迴展開的 import 集合，和 `src/send/**` 的交集必須為空。
  2. 產物層：dist 中非第三方 SDK 的 JS chunk，以下字串出現 0 次：
     - `eth_sendTransaction|eth_sendRawTransaction|eth_signTransaction|wallet_sendCalls`
     - `testOnlyAllowWalletRequest|noBroadcast`
     - WalletConnect SDK 的 lazy chunk 可能本身就含 SDK 內建的方法常數 `[推論，未建置驗證]`。這個 chunk 只做 runtime 斷言：proposal 只有 `personal_sign`（WB-01），facade 預設回 4200 `[讀碼 walletconnect.mjs:345-346]`。
  3. 執行層：headless Chrome 對 dist 與已部署 origin 執行：偽造「看似可送」的任務、強制點擊，搭配一個「什麼都接受」的 EIP-1193 mock → 送出類方法呼叫 0 次。
- **驗收條件**：
  - AC1：預設 `npm run build` 後執行 `node tools/scan-send-markers.mjs dist`，應用程式 chunk 命中 0 次，exit 0。
    - 負面：在測試暫存目錄複製一份 src，讓 `main.ts` import `src/send/mode-a-request.mjs` 後建置 → build 失敗或 scan exit≠0。
  - AC2：import-graph 測試，`main.ts` 的遞迴 import 與 `src/send/**` 交集為空。
  - AC3：D1-09、WA、WC-ARTIFACT 仍是 0 筆送出。另新增情境：mock RPC 回傳與任務一致的部署和可領狀態（核准仍為空）→ 送出 0 次，畫面顯示重讀為「資料不足（unapproved_deployment）」。
  - AC4：`verify-deployed-artifact.mjs --scan-send-markers` 對正式 origin → 0 命中。今天預期失敗：`[實測]` 已見 `eth_sendTransaction`×2。
  - AC5：單元測試 `clientConfirmDecision({plan:{/*無 block_reasons*/}})` 得到 `buttonEnabled=false`。
  - AC6：`node --test` 回報的 case 總數，拆分前後相同或增加。
- **回歸測試**：`test:api`（含 `tests/mode-a-nobroadcast.test.mjs` 全部 case）、`test:gate`、`test:wallets`、`test:wallets:artifact`、`test:runtime`。
- **人工驗證**：無。新版由使用者部署後跑 AC4。
- **風險與 rollback**：
  - 拆分可能讓 no-broadcast 測試失去覆蓋 → 由 AC6 擋住。
  - rollback：重新部署清冊上一版（仍是 `send_off`，只是含 dead code）。

### WP-4 任務存取授權補強

- **目標**：補齊撤銷、session 生命週期、session rotation 與正式環境負面驗收，並提供 Mode A 需要的登入時間與登入地址訊號。
- **依賴**：無，可和 WP-1 平行。
- **對應阻擋項**：1。
- **範圍**：
  - 新增 `action-client/migrations/0002_session_lifecycle.sql`。所有新欄位都可為 null，保持向前相容：
    - `sessions` 加 `auth_time`、`last_seen_at`；
    - `tenant_members` 加 `revoked_at`。
  - `src/server/auth.mjs`：
    - `postVerify` 時若請求帶有效的舊 session cookie，在同一個 batch 內撤銷它（解決 `docs/wallet-connect-01.md:52` 的已知限制）；
    - `loadSession` 加入閒置逾時，並更新 `last_seen_at`（不要每次 GET 都寫；可節流）；
    - `getSession` 回傳 `auth_time`。
  - 新增 `POST /api/auth/logout-all`：需要 session、CSRF、Origin，撤銷同一 address 的所有 session。這是撤銷操作，不是通用 RPC。
  - `src/server/tasks.mjs`：
    - `visibleTask` JOIN 加 `m.revoked_at IS NULL`；
    - `postCreate` 遇到已撤銷的 membership 回 403，不得自動復活。
  - `tools/bootstrap-publisher.mjs`：
    - `--revoke` 先以 `^[a-f0-9]{16}$` 驗格式；
    - 新增 `--revoke-wallet <addr>`：在同一個 D1 batch 撤銷 credential、membership、sessions。
  - client `main.ts`：
    - 登入成功時記錄 `loginAddress`（本頁實際送去簽名、且 verify 回應地址相同的那個地址），只存在記憶體；
    - 跨分頁登出同步（BroadcastChannel，可選）。
- **明確不做**：
  - 不支援 ERC-1271，不接受 owner 簽章冒充其他地址。
  - 不把錢包的 name／rdns 送到 server。
  - 不改 `AUTH_CHAIN_ID=56`。
  - 不加 `/rpc` 或 COMPLETE。
- **設計要點**：
  - 可見條件：session 未撤銷、未過期、未閒置逾時，且以下各項都對得上：
    - `session.address = task_acl.wallet_address`；
    - `tenant_members(tenant, address)` 未撤銷；
    - `status=active`，未過期；
    - revision 是目前版本。
  - 對外一律 404。
  - session 時效建議 12 小時絕對＋30 分鐘閒置；Mode A 送出前要求 `auth_time` 在 15 分鐘內。這兩個數值由使用者決定（D-9）。
- **驗收條件**（在真 Functions＋本地 D1 執行，新增 D1-10 起編號）：
  - AC1：未登入 `GET /api/tasks/<ref>` → 401 JSON、`no-store`，body 不含 `task`。
  - AC2：已登入 A 去讀 B 的 ref、不存在的 ref、格式錯誤的 ref → 三者 status 都是 404，body 位元組完全相同，不是 403。
  - AC3：`--revoke-wallet` 之後：
    - 舊 session 讀任務 → 401；
    - publish → 401；
    - 即使該地址用新的 SIWE 建立了 session，讀任務仍是 404（membership 已撤銷）。
  - AC4：帶舊 cookie 完成新的 verify → 舊 `token_hash` 的 `revoked_at` 有值。
  - AC5：模擬時鐘超過閒置門檻 → 401。
  - AC6：`logout-all` 缺 CSRF → 403；跨 Origin → 403；成功後同地址所有 session → 401。
  - AC7：`--revoke "x' OR 1=1 --"` → 工具因格式錯誤退出，不執行任何 SQL。
  - AC8：回歸 nonce 重放、challenge 的 domain 不符、`Sec-Fetch-Site: cross-site`。
  - AC9：部署後只做 GET 的遠端檢查：`docs/d1-private-task-live.md:84-88` 三條；另加偽造 cookie 讀任務 → 401。
- **回歸測試**：`src/server/api.test.mjs`、`test:runtime` D1-01..09、`test:wallets`（WA-05 撤銷）。
- **人工驗證**：使用者用自己的錢包走 `docs/d1-private-task-live.md:90-92` 的流程，加上 `logout-all`，確認舊 cookie 失效。
- **風險與 rollback**：
  - migration 只新增可為 null 的欄位，所以舊版 Functions 仍能運作。
  - rollback：重新部署前一版 Functions，不刪庫 `[文件 docs/d1-private-task-live.md:92]`。

### WP-5 客戶端自有證據：Lista 部署核准 pin、自有效果檢查、身分綁定

- **目標**：送出前的每一個判斷都只依據客戶端自己取得的證據。伺服器給的 `preflight_ok`、`simulation`、`wallet_request_allowed`，在型別上就不能作為關卡的輸入。
- **依賴**：WP-3（新關卡放進分離後的結構）、WP-4（`loginAddress`、`auth_time`）。
- **對應阻擋項**：2，以及阻擋項 3 的「部署允許清單」（Lista 實作 pin）。
- **範圍**：
  - 核准 pin 機制：
    - `src/trusted-lista.mjs:21-22`、`../config/protocols/lista-bsc.json`（`deployment_approvals`）、`src/guardian/_resources/config/protocols/lista-bsc.json` 三份資料加上 parity 測試；
    - `src/lista-reread.test.mjs:183-184` 從「必須為空」改為「三方一致」；
    - **pin 條目的內容由使用者核准（D-1），Cursor 只實作機制**。
  - 新增 `action-client/tools/propose-lista-approval.mjs`（唯讀）：
    - 對信任 RPC 讀 ERC-1967 slot、proxy 與實作的程式碼 sha256、區塊號與雜湊；
    - 輸出候選條目，`provenance`、`validUntilUnix` 留給人填；
    - 不寫入 config。
  - 新增 `src/client-evidence.mjs`：
    - `effectsCheck`：在同一個 blockTag 上執行 `eth_call({from: account, to: proxy, data: claimCalldata(idx)}, blockTag)`。不 revert → `dry_run_pass`；revert → `fail`；RPC 不支援或出錯 → `insufficient`。
    - `bindIdentity`：送出前「即時」呼叫 `eth_accounts`／`eth_chainId`（比照 `main.ts:1366` 登入時的做法），並要求以下全部相同：
      - `loginAddress`
      - `/api/session` 的 address
      - `eth_accounts[0]`
      - `plan.sender`、`plan.recipient`、`plan.execution_account`
      - 另外 chain 必須是 56。
    - 任務必須來自 `/api/tasks/<ref>`（`privateRef` 非空且屬於同一個 authGeneration）。本機檔案與合成範例一律拒絕（`source_not_private_api`）。
  - 新增關卡 `modeAClientGate({reread, effects, identity, approval, session})`。函式簽名沒有任何 plan 旗標參數。舊的 `clientConfirmDecision` 只保留給 preview 畫面。
  - 可讀性修正：`sameAddress(x, x)` 被當成「是否為合法地址」使用，改成明確的 `isAddress(x)`。位置：
    - `mode-a.mjs:179,475,629`
    - `lista-reread.mjs:149,177,304`
    - `trusted-lista.mjs:128`
    - 現在功能正確，但很容易被誤讀或誤抄 `[推論]`。
  - 第二個 RPC 交叉驗證是選項（D-4）。若採用，必須同時更新 CSP `connect-src` 與 WP-1 的 CSP 測試。
- **明確不做**：
  - 不把結果寫回 `preflight_ok`（AGENTS.md:19）。
  - 不把未完成的模擬標成 pass。
  - 不因為鏈上讀到的雜湊就自動核准 `[文件 docs/p2-lista-reread.md:7]`。
  - 不呼叫 `eth_sendTransaction`。
  - 不買 Tenderly 或任何付費 trace。
  - 未經使用者同意，不新增第三方 RPC。
- **設計要點**：
  - `effectsCheck` 只是 dry-run，不是 trace 模擬。它能否取代 ADR 0001 要求的模擬（`[文件 docs/decisions/0001-provider-selection.md:17]`），由使用者決定（D-2）；若接受，要修訂 ADR。
  - 伺服器被入侵時的影響上限（見 2.2）要寫進 `docs/mode-a-lista.md`。
- **驗收條件**：
  - AC1：server 下發的 calldata 與 client 重建的不一致 → reread 為 `fail`（`calldata_mismatch` 或 `not_single_step`），錢包 mock 除了允許的讀取方法外 0 次呼叫。不一致的情況包括：
    - `calls[0].data` 的 idx 被改；
    - `to` 改成其他合約；
    - `value` 不為 0；
    - 多出一個 call。
  - AC2：mock server 回傳 `plan.sender` 不等於 `loginAddress` 的任務 → 關卡 `blocked(identity_mismatch)`，錢包請求 0 次。
  - AC3：server 回傳 `simulation:{status:"PASS",effects_checked:true,usable_as_live_evidence:true}`、`wallet_request_allowed:true`、`preflight_ok:true` → 關卡輸出不受影響。另以 property test 隨機化 plan 旗標欄位，關卡輸出只隨 client 證據改變。
  - AC4：
    - 核准清單為空 → `unapproved_deployment`；
    - `validUntil` 已過 → `approval_retired`；
    - 沒有期限且不是 permanent → `approval_unbounded`；
    - 三份 approvals 不一致 → parity 測試失敗。
  - AC5：mock 的 `eth_call` revert → effects `fail` → blocked；RPC 回 -32602 → `block_identity_unsupported`（資料不足）。
  - AC6：reread 通過後、送出前，把 mock 錢包的 `eth_accounts` 改成另一個帳號，且**不發** `accountsChanged` 事件 → blocked。這證明用的是即時讀取，而不是事件快取。
  - AC7：本機檔案或合成範例載入的任務，即使 reread mock 全部通過 → `source_not_private_api`，0 次。
  - AC8：`auth_time` 超過門檻 → 要求重新登入，0 次。
- **回歸測試**：`src/lista-reread.test.mjs`、`tests/lista-reread-lifecycle.test.mjs`、`tests/mode-a-nobroadcast.test.mjs`、`src/gate.test.mjs`、`src/server/api.test.mjs`。
- **人工驗證**：
  - 使用者審核 pin 候選：實作地址、兩個 code hash、出處（驗證過的原始碼與 Lista 官方地址表）、`validUntil`；同時確認 `claimWithdraw` 付款給 `msg.sender`。
  - 用工具對公開 BSC RPC 做一次唯讀的 EIP-1898 相容性檢查並記錄結果。
- **風險與 rollback**：
  - pin 過期或 Lista 升級實作時，重讀會 fail-closed。
  - rollback：移除 pin，`registry_version` 加 1。

### WP-6 送出結果生命週期：持久化進行中狀態、拒簽分類、收據核對

- **目標**：符合 AGENTS.md:20、23 的要求：
  - 一個任務只有一個進行中結果；
  - pending／unknown 在重新整理與手機切 App 之後不會消失；
  - hash 不是完成；
  - 不自動重送；
  - 拒簽能正確辨識，而且允許在新的使用者動作下再試。
- **依賴**：WP-5。
- **對應阻擋項**：5（拒簽後狀態），以及 AGENTS 的追蹤要求。
- **範圍**：
  - 新增 `src/outcome-store.mjs`：
    - 用 IndexedDB；無法使用時改用 sessionStorage；兩者都不可用時 fail-closed，不允許送出。
    - key 為 `origin|account|ref|revision`。
    - 值為 `{status, intent_id, requested_at, txHash?, submission}`。`submission` 只存在本機，是使用者自己的資料。
  - 寫入順序：
    1. 呼叫錢包**之前**先寫 `requested`（語意等同 unknown）；
    2. 拿到 hash 後寫 `pending`＋`txHash`；
    3. 收據核對後寫 `confirmed` 或 `failed`；
    4. 明確拒簽時寫 `rejected`。
  - `src/mode-a.mjs`：
    - 新增 `rejected` 狀態，和 `failed`（鏈上 revert）分開。`rejected` 可以在新的使用者點擊＋兩次新重讀之後再試，永不自動重試。
    - `interpretWalletReturn` 改用版本化分類表：4001、WalletConnect 5000「User rejected」→ rejected；其他 → ambiguous。分類表的證據來自 WP-8 的 A9。
  - `src/trusted-lista.mjs:25-32` 的讀取白名單加入 `eth_getTransactionReceipt`、`eth_getTransactionByHash`、`eth_getBalance`、`eth_getTransactionCount`（latest／pending），並允許 `eth_getBlockByNumber` 帶 `full=true`。全部是唯讀方法。
  - 收據核對：
    - 只在使用者按「核對結果」時，或頁面可見時做有限次數的輪詢；不建立背景排程。
    - 餘額窗口用收據區塊前後的 `eth_getBalance`，加上有上限的 full block 掃描，找出同帳戶的其他交易；超過上限就維持 `native_delta_ambiguous`。
    - 偵測替代交易：同 nonce 已被使用但原 hash 查無 → `replaced` 或 `unknown`。
- **明確不做**：
  - 不在 server 記錄 pending 或 COMPLETE。
  - 不用 `eth_getLogs` 或付費 indexer。
  - 不把「請求從列表消失」當成完成（保留 `listDisappearanceIsEnough:false`）。
  - 不自動重送。
- **驗收條件**：
  - AC1：mock 錢包在 `request` 回呼內讀 store，此時應已有 `requested` 記錄。
  - AC2：store 已有 `pending`，重新載入同一個 ref → 確認鈕 `blocked(verify_outcome)`，錢包請求 0 次。即使 reread 仍 pass（uuid 還在）也一樣，以防 T-14 的二次領取。
  - AC3：mock 回 4001 → `rejected`。在新的點擊＋兩次新重讀之後，記錄器收到第 2 筆請求；自動重試 0 次。
  - AC4：-32603、逾時、斷線 → `unknown`，不能再送，只能核對。
  - AC5：mock 錢包回傳一個鏈上不存在的 hash → 維持 `pending`／`unknown`，永遠不會變成 `confirmed`。
  - AC6：收據 `0x0` → `failed`；沒有 ClaimWithdrawal log、鏈不同、區塊對不上 → 未核對（回歸既有 `mode-a` case）。
  - AC7：IndexedDB 拋錯且 sessionStorage 不可用 → `blocked(storage_unavailable)`。
  - AC8：同一個 ref 換帳號後，不會覆蓋或清掉前一個帳號的 pending。
- **回歸測試**：`tests/mode-a-nobroadcast.test.mjs`（全部）、`tests/lista-reread-lifecycle.test.mjs`、`tests/client-lifecycle.test.mjs`。
- **人工驗證**：放在 WP-8，包括拒簽、切 App，以及在 Android 開「不保留活動」後回到頁面。
- **風險與 rollback**：
  - 本機 store 損毀時 fail-closed，顯示「需要核對」。
  - 登出時是否清除本機 pending 由使用者決定（D-10）。建議 pending 期間不清除，否則違反「一個任務一個進行中結果」。

### WP-7 Mode A 真送出程式路徑（`mode_a` 送出變體；預設不建置、不部署）

- **目標**：
  - 把唯一允許的交易請求實作在獨立的送出變體中：原地址、Lista proxy、`claimWithdraw(idx)`、value 0、chain 56；
  - 請求必須經過 `request-policy.mjs` 的一條窄規則；
  - production 預設的 `send_off` 變體完全不包含這段程式。
  - 這一包**不打開**正式環境的送出。
- **依賴**：WP-3、WP-5、WP-6；WalletConnect 是否納入依 D-3 決定。
- **範圍**：
  - `src/send/mode-a-request.mjs`（WP-3 建立）：`requestModeAClaim(connection, preparedClaim, capability)`。
  - `src/wallets/request-policy.mjs`：只有送出變體會 import 一個分支模組。規則如下：
    - `eth_sendTransaction` 只在 `context.modeAClaim` 是由 `client-evidence` 產生、無法偽造的 capability 時才允許（例如模組私有的 WeakSet 註冊）；
    - params 必須**精確等於** `[{from, to: TRUSTED_LISTA.proxy, data: claimCalldata(idx), value: "0x0"}]`，不得有其他鍵，gas 交給錢包估算；
    - `wallet_sendCalls` 永遠拒絕；
    - `wallet_switchEthereumChain` 仍然拒絕（鏈不對就請使用者自己切）；
    - 預設拒絕的原則不變。
  - `main.ts:661-675` 的 `requestUserSend`：`send_off` 變體維持無條件 throw。送出變體走新函式，`VITE_GUARDIAN_MAINNET_SEND` 不再是唯一的開關。
  - build：`npm run build:send-variant` 同時需要以下三項，缺任何一項就 build 失敗：
    - `WG_SEND_VARIANT=mode_a`；
    - `WG_RELEASE_AUTH_ID`（對應使用者書面授權的編號）；
    - 清冊中有 `variant: mode_a, status: approved_by_user` 的條目。
  - 送出前的順序：
    1. 使用者點擊；
    2. `bindIdentity`（即時讀取）；
    3. 兩次重讀；
    4. `effectsCheck`；
    5. 用即時時鐘跑 `settleReread`；
    6. store 寫入 `requested`；
    7. `guardedRequest(eth_sendTransaction)`；
    8. 解讀結果並寫入 store。
  - UI 顯示「錢包畫面應該看到」清單，請使用者逐項核對，不一致就拒絕：
    - to = Lista proxy `0x1adB950d8bB3dA4bE104211D5AB038628e477fE6`（完整地址）；
    - value 0；
    - 函式 `claimWithdraw`；
    - BNB Smart Chain。
  - WalletConnect：預設不支援送出。若 D-3 決定納入，另開 W-C 子包：
    - proposal 的 methods 改為 `["personal_sign","eth_sendTransaction"]`，同步更新 WB-01／WB-02；
    - 加上送出請求後的錢包跳轉連結；
    - 加上 `metadata.redirect`（目前缺）。
- **明確不做**：
  - 不用 Mode B 或 `wallet_sendCalls`。
  - 不要求 7702、無限授權、新 admin、帳戶升級；不建立新智慧帳戶。
  - 不自動重送。
  - production 預設 build 不開；不部署送出變體（部署是 go/no-go 之後由使用者授權的動作）。
  - agent、排程、MCP 永不觸發這段程式。
- **驗收條件**（對送出變體 dist，用 headless Chrome 搭配 mock 錢包、mock RPC、真 Functions＋本地 D1）：
  - AC1（正向，只用不廣播的記錄器）：所有綁定都通過時，恰好 1 次 `eth_sendTransaction`，params 與預期物件深度相等，沒有多餘的鍵。
  - AC2（負面，每項都必須是 0 次錢包送出請求）：
    - server calldata 被改；
    - 他人的 sender；
    - 鏈不對（且不能呼叫 `wallet_switchEthereumChain`）；
    - reread 之後帳號被換掉（沒有事件）；
    - 核准在第二次重讀與送出之間到期；
    - plan TTL 已過；
    - 已有 pending；
    - storage 不可用；
    - 本機檔案載入的任務；
    - 非核准 origin；
    - `auth_time` 過舊；
    - mock 錢包回傳 4001 之後沒有新的點擊。
  - AC3：request-policy 單元測試：
    - 有 capability 但 data 不同、多帶 gas、`value: "0x1"` → 拒絕；
    - 沒有 capability → 拒絕；
    - `send_off` 變體的 bundle 不含這個分支（沿用 WP-3 的 scan）。
  - AC4：CI 同時跑兩個變體；`send_off` 變體的 WP-3 全部 AC 仍通過。
  - AC5：缺少 `WG_RELEASE_AUTH_ID` 或清冊條目 → `build:send-variant` exit≠0。
- **回歸測試**：client 全部測試以雙變體跑；`pytest`。
- **人工驗證**：只在 WP-8 的 Phase B／C 進行。
- **風險與 rollback**：
  - 最大的風險是誤部署送出變體。部署後立刻用 WP-1 的 verifier 比對 `variant`。
  - rollback：重新部署清冊上一個 `send_off` release（使用者執行，目標 10 分鐘內）。
  - 已上鏈的交易無法回滾，只能核對。

### WP-8 真錢包實測協議與證據

- **目標**：用可稽核的證據關閉所有 REAL_WALLET_PENDING 項目，分三個階段進行；Phase B／C 需要使用者書面授權。
- **依賴**：
  - Phase A：WP-1、WP-2、WP-4 完成，正式環境跑的是 `send_off`；
  - Phase B：WP-6、WP-7，並取得 D-5 決定；
  - Phase C：go/no-go 全部打勾，並取得授權 H-7。
- **範圍**：
  - 證據範本的公開格式寫在 `docs/real-wallet-evidence.md`；實際證據放在 gitignore 的 `wallet-guardian/evidence/real-wallet/`（AGENTS.md:25）。
  - 新增 `src/wallet-audit-log.mjs`：頁面內只記錄方法名、時間與 policy 決定，**不記錄參數或地址**，可以從畫面匯出。S24 上另外用 `chrome://inspect` 擷取 console 與網路紀錄。
- **測試矩陣**：
  - 桌機 Chrome：MetaMask 擴充、Rabby 擴充、Binance Web3 Wallet 擴充，各自單獨安裝一次，再全部同時安裝一次。
  - S24（記錄 Android、Chrome、App 版本）：
    - (a) 幣安 App 內建 dApp 瀏覽器開 `/t/<ref>`；
    - (b) Chrome → WalletConnect → 幣安 App → 回到 Chrome；
    - (c) WalletConnect 連一個非幣安的 v2 錢包（例如 MetaMask Mobile）。
- **Phase A**（只登入，正式環境 `send_off`）：
  - A1：SIWE 成功，錢包顯示的 domain 是正式 host。
  - A2：拒簽 SIWE → 沒有 session，也不會退回改用 `eth_sign`。
  - A3：切換帳號 → 舊 session 被撤銷，必須重新登入。
  - A4：切到 BSC 以外的鏈 → 重讀失效，頁面沒有呼叫 `wallet_switchEthereumChain`。
  - A5：S24 跳轉後回到原任務（WB-05）。
  - A6：用「不保留活動」模擬分頁被回收後回來 → D1 session 由 cookie 恢復；WalletConnect session 要按登入才恢復。
  - A7：App 沒安裝時，QR 與重試仍在。
  - A8：audit log 中只出現允許的方法（`eth_requestAccounts`、`eth_accounts`、`eth_chainId`、`personal_sign`、`wallet_getCapabilities`）。
  - A9：記錄每個錢包拒簽 SIWE 時的錯誤 code 與 message，供 WP-6 的分類表使用。
  - A10：記錄錢包顯示的 WalletConnect Verify 狀態。
- **Phase B**（二選一，D-5）：
  - B1 測試網預演：
    - 在本機建置一個預演用的送出變體：chain 97，交易是 0 值、自己轉給自己；
    - 只在 `127.0.0.1` 上跑，S24 經 `adb reverse` 連線，不開公開 port；
    - 測試拒簽、錢包畫面欄位、切 App 回來後 pending 是否持久、拒簽後能否再試。
    - 這個變體永遠不部署。
  - B2 不做預演，改在 Phase C 先拒簽一次。
- **Phase C**（主網小額，需要 H-7）：
  - 只針對授權書指名的那一筆提款請求做一次 `claimWithdraw`。
  - 第一次請求故意拒簽 → 確認狀態是 `rejected`，鏈上沒有交易。
  - 第二次核准 → pending → 收據 → `confirmed`；若經濟結果無法完整核對，就誠實保持「未核對」。
  - 記錄費用 = `gasUsed × effectiveGasPrice`。
  - 中止條件：
    - 錢包畫面的 to、data、value 與清單不符；
    - 費用超過授權上限；
    - 超過授權的時間窗；
    - 出現任何 ambiguous 結果 → 停止並改走核對。
- **每列證據欄位**：錢包名稱與版本、裝置／OS／瀏覽器版本、連線方式、HEAD sha、release_id 與 dist 雜湊（取自 WP-1 清冊）、時間戳、audit log 匯出、結果（PASS／FAIL）。
- **驗收條件**：
  - 矩陣每一列都有完整的證據記錄。
  - 任何一列是 FAIL 或 PENDING → no-go。
  - 負面：Phase A 的 audit log 一旦出現送出類方法，立即判 FAIL，並啟動 WP-1 的 rollback。
- **風險與 rollback**：
  - Phase A 使用的是 `send_off` 產物，物理上不含送出程式（WP-3），所以不會誤送。
  - Phase C 限定單筆，有費用上限與時間窗；鏈上交易不可回滾。

---

## 5. Mode A 開放準入清單（go/no-go）

全部打勾之前，送出保持關閉。每一項都必須附上證據連結（檔案、commit、release_id），口頭確認不算數。

- [ ] G1　WP-1..WP-7 已合併到 main，每個 MR 都有依使用者既有規則完成的獨立 review 紀錄；self-approve 不算。
- [ ] G2　在同一個 commit 上有完整測試證據：`pytest`、client 雙變體全套、`test:runtime`、`test:wallets:artifact`、`test:artifact:sendoff`。
  - 若 CI 是 `ci_quota_exceeded`，不能算通過 `[文件 docs/reviews/2026-10-07-p1-client-lifecycle.md:39]`。
  - 可以改用 box runner 並附上 `gate-run/CI_COMMIT_SHA` `[讀碼 ../.gitlab-ci.yml:89-99]`。
- [ ] G3　正式 origin 的 `verify-deployed-artifact.mjs` 結果為 PASS（逐檔雜湊、CSP、API 探測），而且 `variant=send_off`、送出標記 0 命中。
- [ ] G4　舊的 hash 部署（含 5a09b4e6）已刪除，`--expect-gone` 通過；repo 內沒有 Vercel／GitLab Pages 發布設定。
- [ ] G5　路由契約測試通過；Python 舊的 `issue_task_link` 已移除或改為委派。
- [ ] G6　授權負面測試（D1-01..09 加上新的 D1-10 起）在真 runtime 上通過；遠端 GET 檢查通過。
- [ ] G7　Lista 部署核准 pin 已由使用者核准（H-1），三份 parity 通過；`validUntil` 涵蓋 Phase C 的時間窗。
- [ ] G8　D-2 已有決定（ADR 0001 已修訂，或改採其他方式），並已實作在 `modeAClientGate`。
- [ ] G9　WP-6 全部 AC 通過；拒簽分類表已填入 Phase A 的 A9 證據。
- [ ] G10　送出變體的 WP-7 AC1–AC5 通過；`send_off` 變體的 WP-3 AC 仍通過。
- [ ] G11　WP-8 Phase A 矩陣全部 PASS；若選 B1，Phase B 也全部 PASS。
- [ ] G12　使用者已簽兩份**分開的**書面授權：H-7（主網小額測試）與 H-8（把送出變體部署到正式 origin）。
- [ ] G13　rollback 已演練：在 10 分鐘內把正式 origin 換回 `send_off`，並用 verifier 確認。
- [ ] G14　文件已同步：
  - AGENTS.md:7、11、20 的措辭要隨開放範圍修訂，**由使用者核准**；
  - `docs/mode-a-lista.md`、`docs/deploy.md`、README 已更新。
- [ ] G15　Phase C 完成，單筆的經濟結果核對為 confirmed，或誠實記為未核對。之後是否對更多任務開放，另做決定（D-12）。

---

## 6. 待使用者決定事項與人工授權步驟

| ID | 決定或授權 | 說明 | 影響 |
|---|---|---|---|
| D-1／H-1 | 核准 Lista 實作 pin | 實作地址、proxy 與實作的 code sha256、出處（驗證過的原始碼與官方地址表）、`validUntilUnix`；同時確認 `claimWithdraw` 付款給 `msg.sender` | 沒有它，重讀永遠不會 pass |
| D-2 | `eth_call` dry-run 能否視為「交易效果模擬完成」 | 與 ADR 0001:17 衝突，必須修訂 ADR；替代方案（付費 trace）違反「不買方案」 | 決定 WP-5 關卡的最後一個條件 |
| D-3 | Mode A v1 是否支援 WalletConnect 送出 | 不支援 = 只走 injected（擴充或幣安 App 內建瀏覽器）；支援 = 另開 W-C 子包，S24「Chrome→App」才能送交易 | 改變 WP-7 範圍與 WP-8 矩陣 |
| D-4 | 是否加第二個 RPC 交叉驗證 | ADR 0001 不允許從搜尋結果挑 RPC；同組織的第二個端點獨立性有限 | CSP 與 WP-5 |
| D-5 | Phase B 選 B1（測試網本機預演）或 B2 | B1 需要一個永不部署的預演變體 | WP-8 時程 |
| H-4 | Cloudflare dashboard 操作 | 刪除舊部署、確認 2FA 與 token 權限；agent 不代做 | G4 |
| H-6 | 是否允許對正式 D1 做合成金鑰的遠端負面測試 | 會在正式 D1 寫入 challenge 與 session 列（合成地址，沒有 ACL） | WP-4 AC9 的範圍 |
| H-7 | **主網小額測試書面授權** | 必須寫明：錢包（名稱與地址，地址只寫在授權書，不進 repo）、提款請求（uuid／idx）、收款地址 = 原地址、費用上限（BNB）、時間窗、使用的 origin 與 release_id；書面授權之外的同意都不算（AGENTS.md:20） | G12、Phase C |
| H-8 | **把送出變體部署到正式 origin 的書面授權** | 和 H-7 分開；寫明 release_id、dist 雜湊、rollback 負責人 | G12 |
| D-9 | session 參數 | 絕對時效、閒置逾時、Mode A 要求的 `auth_time` 視窗（建議 12h／30m／15m） | WP-4 |
| D-10 | 登出時是否清除本機 pending | 建議 pending 期間不清除 | WP-6 |
| D-11 | 刪除 `vercel.json` 與 GitLab `pages` job | 屬於 repo 變更，需要同意 | WP-1 |
| D-12 | 第一筆成功之後的範圍 | 是否擴大到其他任務或錢包；建議每次擴大都重新走 go/no-go | 後續 |

---

## 附錄 A：實測紀錄（GET-only，2026-10-14）

| URL | 觀察 |
|---|---|
| `https://wallet-guardian-action-client.pages.dev/` | 200，有 CSP（含 WalletConnect relay 與 verify），有 XFO DENY、`Referrer-Policy: no-referrer` |
| 同上 `/t/ref_000…` | 200 HTML，CSP 相同 |
| 同上 `/api/session` | 200 JSON、`no-store` |
| 同上 `/api/tasks/ref_000…` | 401 JSON、`no-store` |
| 同上 `/api/rpc` | 404 JSON |
| 同上 `/tasks/x.json` | 200 HTML（SPA fallback） |
| 正式 bundle `assets/index-*.js` | `eth_sendTransaction`×2、`wallet_sendCalls`×1、`testOnlyAllowWalletRequest`×3、`noBroadcast`×1；`action-client@0.3.0`；不含 `REFUSED_METHODS` 字串 |
| `https://5a09b4e6.wallet-guardian-action-client.pages.dev/` | 200，**沒有 CSP**；舊 bundle（約 10 KB）含 `eth_sendTransaction`、`wallet_sendCalls`、`preflight_ok`，沒有 `/api/auth`；`/api/session` 回 HTML |
| `https://<namespace>.gitlab.io/wallet-guardian/`（`docs/deploy.md:21` 所列網址） | 302 → GitLab Pages 存取驗證（私有） |
| 三個猜測的 `*.vercel.app` 主機名 | 404 `DEPLOYMENT_NOT_FOUND`（不能證明不存在其他專案） |

暫存的探測檔案已刪除，repo 沒有任何寫入。

## 附錄 B：文件與實況不一致（交給 WP-1／WP-2 修正）

- `docs/reviews/2026-10-07-reconcile-main.md:69` 說「production bundle 不含 eth_sendTransaction」→ 對照目前正式 bundle 已不成立 `[實測]`。
- `README.md:195` 說「已上線的 Pages 套件是合併前的建置」→ 正式環境已含 Mode A 與 WalletConnect `[實測]`。
- `docs/deploy.md:3` 與 `:5` 對部署狀態的描述互相矛盾；`:23` 的 project name 錯誤；`:21-22` 仍推薦 GitLab／Vercel。
- `README.md:107,193` 說「不會發行公開任務連結」，但 `issue_private_task_link` 與 server `viewLink` 實際會發行 view 連結 `[讀碼 workbench.py:117-151; origins.mjs:28-52]`。這裡要分清楚「view 連結」和 `fixed_https_origin` 是兩回事。
