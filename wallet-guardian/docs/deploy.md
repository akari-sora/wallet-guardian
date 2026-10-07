# 固定 HTTPS 任務頁

狀態：**BLOCKED**。這個環境沒有 Vercel、Cloudflare 或 GitLab 的寫入憑證，所以沒有已驗證的公開網址。`fixed_https_origin` 仍是 false。任務連結不會發行，直到下面其中一個步驟由專案擁有者完成並回讀到 HTTPS 200。

固定客戶端是 `action-client@0.3.0`。它只接受 `guardian.task.v1`。網址只允許 `?ref=ref_` 加 32 個十六進位字，再向同一個來源讀 `/tasks/<ref>.json`。網址不放地址、金額或金鑰。

建置不會設定 `VITE_GUARDIAN_MAINNET_SEND`。預設主網送出是關閉的。

## 本機靜態建置

```bash
cd wallet-guardian/action-client
npm ci
npm run build
```

產出在 `action-client/dist/`。這個目錄已在 gitignore。

## 擁有者的下一步（擇一）

1. GitLab Pages。把 `main` 推到 `https://gitlab.com/aa22396584/wallet-guardian`。根目錄 `.gitlab-ci.yml` 的 `pages` job 會發布靜態檔。預期網址是 `https://aa22396584.gitlab.io/wallet-guardian/`。在該網址回傳 200、而且頁面橫幅仍寫主網送出關閉之前，不能把這個網址當成已上線的固定來源。
2. Vercel。在 `wallet-guardian/action-client` 執行 `npx vercel login`，然後 `npx vercel --prod`。設定已寫在 `vercel.json`。不要在專案環境變數打開 `VITE_GUARDIAN_MAINNET_SEND`，除非另外授權主網送出。
3. Cloudflare Pages。`npx wrangler login` 之後，在 `action-client` 執行 `npx wrangler pages deploy dist --project-name wallet-guardian`。

完成後把實際 HTTPS origin 寫進部署紀錄，再讓 `issue_task_link` 使用那個 origin。localhost 與未經驗證的預覽網址都不能發行公開任務連結。

## 部署現況（2026-10-07，本機核對）

- 公開碼已在 GitLab `main`：`07e02cf`（含確認閘門、Pages/Vercel 設定、Etherscan SSRF 允許 WARP DNS）。
- 離線：`pytest` **180 passed**；`action-client` build／gate／canonical **通過**。
- GitLab CI／Pages：**失敗**，原因 `ci_quota_exceeded`（共享 runner 額度）。在額度恢復前，預期 Pages 網址 `https://aa22396584.gitlab.io/wallet-guardian/` **不是**已上線來源。
- Vercel MCP：讀專案列表可用；**建立專案／部署**需要 team scope `aa22396584-6131s-projects` 的寫入授權。目前被 403，需擁有者在聊天裡完成 Vercel 重新授權後再部署。
- `fixed_https_origin` 仍是 **false**。主網送出預設關閉；這不是主網交易授權。

