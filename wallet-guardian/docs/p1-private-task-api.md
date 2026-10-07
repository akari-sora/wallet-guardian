# P1 私人任務 API

這是讀取用的任務 API，不是主網送出，也不是模式 C。`fixed_https_origin` 仍是 false。這份說明不部署 Pages，也不打開 `VITE_GUARDIAN_MAINNET_SEND`。

SIWE 的 `AUTH_URI` 只綁定登入挑戰的 domain／URI。它不是執行來源允許清單，也不能把 `issue_task_link` 變成已發行。

## 路由

瀏覽器開 `/t/<ref>`。`ref` 是 `ref_` 加 32 個十六進位字。`?ref=` 不再向 `/tasks/<ref>.json` 讀公開檔。

| 方法 | 路徑 | 誰可以呼叫 |
| --- | --- | --- |
| POST | `/api/auth/challenge` | 瀏覽器登入。限流。挑戰綁這次 attempt、地址、domain、URI、chain、到期 |
| POST | `/api/auth/verify` | 同一瀏覽器。nonce 只會成功消耗一次 |
| GET | `/api/session` | 目前 cookie。沒有別人的資料 |
| POST | `/api/auth/logout` | 作廢伺服器上的 session，不只清 cookie |
| GET | `/api/tasks/{ref}` | session + tenant 成員 + ACL + 未過期 + 目前 revision |
| POST | `/api/tasks/{ref}/cancel` | 同上，而且 revision 相符、尚未執行。回應 `on_chain_revoked: false` |
| POST | `/api/bot/tasks` | `Authorization: Bearer wgpub_<id>.<secret>`。tenant 與錢包只採憑證資料列 |

沒有 `/api/rpc`。沒有客戶端 COMPLETE。取消或建立時帶上的交易雜湊不會被標成已驗證，也不會把 `executed` 設成 1。

## 安全負例

- 跨租戶、不存在的 ref、格式不對的 ref：已登入時都是同樣的 `404 not_found`，不含任務本文。
- 呼叫端自己帶的 `tenant_id`、`wallet_address` 或 `X-Tenant-Id` 不能取代憑證或 session。不符就拒絕，不會照單建立。
- 兩次同時送出同一個有效簽章，只會寫入一個 session。用過的 nonce 再送是 `nonce_replay`。
- 登出後舊 cookie 不能讀任務。GET 不改任務、不撤銷 session。
- 過期、已取消、`superseded`，或 `?revision=` 不是目前版本，都不回任務本文。
- ERC-1271 合約帳戶不支援。65 byte 以外的簽章，或 `account_type: contract`，回 `erc1271_unsupported`。EOA 簽章回復出的地址必須等於挑戰地址，不能拿擁有者的鑰匙冒充另一個地址。沒有 `isValidSignature`，也沒有通用 RPC。
- 變更狀態的瀏覽器路由要對得上 `Origin`，並帶 `X-CSRF-Token`。`Sec-Fetch-Site: cross-site` 會被拒絕。
- Cookie 是 `__Host-wg_session` 與短命的 `__Host-wg_attempt`：`Secure; HttpOnly; Path=/; SameSite=Lax`，不設 `Domain`。
- D1 的 session、ACL、最新 revision 都走 `withSession("first-primary")`。直接讀 replica 會失敗。
- Pages Function 的每個成功與錯誤回應自己帶 `Cache-Control: no-store`。`_headers` 管不到 Function。
- `/api/*` 由 `functions/api/[[path]].js` 回答 JSON。`_redirects` 只把 `/t/*` 指到 `index.html`，不把 `/api` 指過去。公開目錄沒有 `tasks/` 或 `api/`。合成範例仍是 `/sample-task.json`，不是使用者的任務。
- 任務裡的 `preflight_ok` 不能打開送出。獨立鏈上重讀另外顯示，通過也不會寫回 `preflight_ok` 或打開確認鈕。模式 C 不能建立成可執行任務。

## 本機

```bash
cd wallet-guardian/action-client
npm ci
npm run test:api
npm run test:gate
npm run build
```

D1 遷移在 `action-client/migrations/0001_p1_init.sql`。`wrangler.jsonc` 的 `database_id` 是佔位，尚未建立資料庫。正式環境要另外設定 `AUTH_DOMAIN`、`AUTH_URI`、`AUTH_CHAIN_ID`，以及只存在 D1 裡的 publish credential。這次沒有部署。

## 仍不做

模式 B 送出、模式 C、TUF／獨立簽章程式、R2、把 `fixed_https_origin` 改成 true、部署 https://wallet-guardian-action-client.pages.dev/ 。唯讀重讀見 `docs/p2-lista-reread.md`。
