# 固定 HTTPS 任務頁

狀態：**VERIFIED_ORIGIN（2026-10-07）**。公開固定來源是 https://wallet-guardian-action-client.pages.dev/（Cloudflare Pages 專案 `wallet-guardian-action-client`，Direct Upload）。套件內 `workbench` 的 `fixed_https_origin` 仍是程式常數 `False`。view origin 寫在 `config/deployments/private-task-origins.json`，那不是執行授權，也不會把 `fixed_https_origin` 改成 true。主網送出預設關閉。

固定客戶端是 `action-client@0.3.0`。D1 綁定、AUTH 設定與私人任務上線程序見 `docs/d1-private-task-live.md`。它只接受 `guardian.task.v1`。私人任務頁是 `/t/<ref>`，資料來自 Pages Function `/api/tasks/<ref>`，不是公開的 `/tasks/<ref>.json`。網址不放地址、金額或金鑰。`fixed_https_origin` 仍是 false。API 與負例見 `docs/p1-private-task-api.md`。2026-10-14 的 GET-only 紀錄裡，這個 origin 的 `/t/<ref>` 回 HTML、`/api/session` 回 JSON。那次紀錄不是這次變更做的部署，也不是主網送出授權。

建置不會設定 `VITE_GUARDIAN_MAINNET_SEND`。預設主網送出是關閉的。

## 本機靜態建置

```bash
cd wallet-guardian/action-client
npm ci
npm run build
```

產出在 `action-client/dist/`。這個目錄已在 gitignore。

## 部署目標

正式靜態目標是 Cloudflare Pages 專案 `wallet-guardian-action-client`。逐檔雜湊清冊是 `config/deployments/action-client-releases.json`。這份清冊目前的 `releases` 是空的：沒有填入沒有實際雜湊過的檔案，也不把任何 origin 標成這次變更部署過的 active release。

GitLab Pages 與 Vercel 不是這個固定來源。`action-client/vercel.json` 與根目錄 `.gitlab-ci.yml` 的 `pages` job 仍在 repo 裡，因為刪除它們要等 D-11。不要把那兩個殘留設定當成部署步驟。

部署與刪除既有預覽由擁有者在 Cloudflare 執行。這份文件變更沒有執行部署，也沒有刪除任何預覽。

## 部署現況（2026-10-07，本機核對）

- 公開碼已在 GitLab `main`：`07e02cf`（含確認閘門、Pages/Vercel 設定、Etherscan SSRF 允許 WARP DNS）。
- 離線：`pytest` **180 passed**；`action-client` build／gate／canonical **通過**。
- GitLab CI／Pages：**失敗**，原因 `ci_quota_exceeded`（共享 runner 額度）。在額度恢復前，預期 Pages 網址 `https://aa22396584.gitlab.io/wallet-guardian/` **不是**已上線來源。
- Vercel MCP：讀專案列表可用；**建立專案／部署**需要 team scope `aa22396584-6131s-projects` 的寫入授權。目前被 403，需擁有者在聊天裡完成 Vercel 重新授權後再部署。
- （歷史）部署當下曾標 false；之後已驗證 Cloudflare Pages origin，見下一節。主網送出預設關閉；這不是主網交易授權。

## 已驗證固定 HTTPS（2026-10-07）

- 主機：Cloudflare Pages Direct Upload（非 Git 整合）
- 專案：`wallet-guardian-action-client`
- 正式網址：https://wallet-guardian-action-client.pages.dev/
- 歷史預覽：https://5a09b4e6.wallet-guardian-action-client.pages.dev/ 。2026-10-14 的 GET-only 紀錄仍看到它回 200，而且沒有 CSP。這份變更沒有刪除它，也不把它當成核准來源。
- `fixed_https_origin` 仍是 false。這個 HTTPS origin 不是主網執行授權。主網送出仍預設關閉。
