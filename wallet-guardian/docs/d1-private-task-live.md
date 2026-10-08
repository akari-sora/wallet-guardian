# D1-LIVE-01：真實 D1＋私人任務 API

這份是公開說明，不含地址、tenant、token 或任務內容。執行證據放在 gitignore 的 `wallet-guardian/evidence/d1-live/`。

這不是主網送出，也不是模式 B／C。`VITE_GUARDIAN_MAINNET_SEND` 不設定，`requestUserSend` 仍無條件拒絕，`execution_authorized=false`，`fixed_https_origin` 仍是 false。

## 三件事分開

| 項目 | 狀態來源 | 意義 |
| --- | --- | --- |
| 查看來源已核准 | `config/deployments/private-task-origins.json` | `/t/<ref>` 可以從這個 origin 發給本人查看。不是執行授權 |
| 私人 API 已驗證 | 本地 runtime 測試＋部署後遠端檢查 | SIWE、ACL、取消、登出在真 Functions＋D1 上運作 |
| 交易執行已核准 | 無 | 仍然是 false。本包不改 |

## 部署接線

`action-client/wrangler.jsonc`：

- `d1_databases`：binding `DB`、`database_name` `wallet-guardian-tasks`、`database_id` 是 `wrangler d1 create` 回傳並由 `wrangler d1 info` 回讀的 UUID（`6cbe9ad2-7a9b-4a4b-b1e5-f9d6f6a87ce3`）、`migrations_dir` `migrations`。
- `vars`：`AUTH_DOMAIN`、`AUTH_URI`、`AUTH_CHAIN_ID=56`、`AUTH_CHALLENGE_LIMIT=10`、`AUTH_CHALLENGE_GLOBAL_LIMIT=200`。這些是登入挑戰的 audience，不是秘密，也不是執行授權。
- `env.preview`：`vars` 與 `d1_databases` 皆為空。預覽部署不繼承正式 D1，私人 API 回 `503 private_api_unavailable`。
- 沒有 `SESSION_SECRET`、`JWT_SECRET`、R2 或其他付費功能。

## 查看連結

- 使用者入口：`/t/<ref>`。私有資料：`/api/tasks/<ref>`。舊 `?ref=` 不讀公開靜態任務。
- API 建立任務時，`task.link` 只從版本化的 origin 政策產生；伺服器以 `AUTH_URI` 的 origin 選擇已核准項目，任務或呼叫端不能提供新 origin。對不上就是 `issued=false`、`reason=view_origin_not_approved`。
- Python 端 `guardian.actions.workbench.issue_private_task_link(ref, deployment_id)` 讀同一份檔案。

## 來源 IP 預算

除了既有的「IP＋地址」挑戰 bucket 與全域未消耗挑戰上限，另加：

- `challenge-ip:<ip>`：每 600 秒每個來源 IP 最多 30 次挑戰（跨地址）。
- `verify-ip:<ip>`：每 600 秒每個來源 IP 最多 30 次驗證。

可用 `AUTH_CHALLENGE_IP_LIMIT`、`AUTH_VERIFY_IP_LIMIT` 調整，正式設定不另外覆寫。

## 發行憑證

```bash
cd wallet-guardian/action-client
# 每個允許的錢包一組；首次驗收 24 小時到期
node tools/bootstrap-publisher.mjs --target remote --confirm-remote \
  --wallets-file <私人錢包設定> --store-dir <受限秘密目錄>
# 發一筆不可執行的測試任務
node tools/publish-private-task.mjs --credential-file <秘密目錄>/credentials/WG_PUBLISH_TOKEN.<id>.json
```

- token 格式 `wgpub_<16 hex id>.<64 hex secret>`，以 CSPRNG 產生。D1 只存 `sha256(secret)`。明文只寫進 0600 的秘密檔（秘密名稱 `WG_PUBLISH_TOKEN`），不印出、不進 D1、不進 Pages bundle。
- 可重跑：已核對的憑證沿用；存在但無法核對的憑證會停止並回報 `unverifiable_credential`，不覆寫也不重發。`--rotate` 明確撤銷（`revoked_at`）後重發；`--revoke <id>` 只撤銷。
- 發布工具只送到政策裡的 origin 或本機測試伺服器。預設任務是明確標示的測試任務：沒有呼叫、金額 0、模擬 INCOMPLETE，不代表真實 Lista 部位。

## 驗證

本地（真 Pages Functions＋本地 D1）：

```bash
cd wallet-guardian/action-client
npm ci
npm run build
npm run test:api && npm run test:validation && npm run test:canonical && npm run test:gate
npx --no-install wrangler pages functions build --outfile /tmp/functions-worker.js
npm run test:runtime   # D1-01..D1-09，需要 curl 與 Chrome（WG_CHROME）
```

`test:runtime` 啟動 `wrangler pages dev dist --local-protocol https --persist-to <暫存>`，用 `curl --connect-to` 與 Chrome `--host-resolver-rules` 把正式 host 導到本機，因此正式 AUTH 設定不放寬。`--insecure` 只與這個明確的本機轉向一起用。

| 編號 | 內容 |
| --- | --- |
| D1-01 | 空 DB 遷移；第二次 apply 無待執行，資料保留 |
| D1-02 | 合成 SIWE 建 session；nonce 重播與三路並行驗證只成功一次 |
| D1-03 | A 讀不到 B；未知與無權限 ref 同樣 404、不含任務本文 |
| D1-04 | 登出後舊 cookie 讀任務 401 |
| D1-05 | 取消後 410；過期 410；錯 revision 409；缺 CSRF 403 |
| D1-06 | 初始化／發布工具；tenant、wallet 由憑證決定；偽造、錯 secret、到期、撤銷都拒絕；無法核對的憑證不覆寫 |
| D1-07 | `/api/*` 成功與錯誤都是 Function 自己帶的 JSON＋no-store |
| D1-08 | 跨地址的來源 IP 挑戰／驗證限流 |
| D1-09 | 用建置產物：偽造的「看似可送」任務可建立，強制點確認後，送出類錢包呼叫為 0；只簽一則登入訊息 |

遠端（部署後）：

```bash
ORIGIN=https://wallet-guardian-action-client.pages.dev
curl -sS -D - "$ORIGIN/api/session"                                   # 200 JSON authenticated=false
curl -sS -D - "$ORIGIN/api/tasks/ref_00000000000000000000000000000000" # 401 JSON
curl -sS -D - "$ORIGIN/api/rpc"                                       # 404 JSON
```

## 本人登入驗收（不能代簽）

用自己的錢包開 `/t/<ref>` → 登入（固定讀取訊息，不是交易）→ 看到測試任務、確認鈕仍關閉 → 取消 → 重載被拒 → 登出 → 舊 session 再讀失敗。失敗時停發新任務，必要時回復上一個 Pages 部署；D1 保留，不刪庫。
