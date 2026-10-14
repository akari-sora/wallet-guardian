# Mode A：Lista 原地址單步領取

狀態：引導路徑與不廣播測試已寫入。這不是主網送出授權。

路徑是：開啟任務、獨立重讀、模式 A 引導確認、追蹤收據與結果。客戶端沿用 `docs/p2-lista-reread.md` 的重讀：信任來源、部署核准、EIP-1898、TTL、核准期限、錢包帳戶與鏈。重讀通過不會把 `allowSend` 打開，也不會把測試用旗標設成 true。`requestUserSend()` 在正式預設路徑仍然直接拒絕；`VITE_GUARDIAN_MAINNET_SEND` 即使為 true，這個函式還是丟出錯誤，不會呼叫錢包。

按下確認時，引導流程對同一個任務做兩次獨立重讀，再用第二次觀察到的 idx 重建 `claimWithdraw(uint256)` calldata。不改收款人，不預先採用一批舊索引，也不走 `wallet_sendCalls`。idx、數量、部署、calldata 或可領狀態若有實質變化，這次不能送，必須換新的 revision，舊的不能悄悄沿用。

錢包結果只記 pending、unknown、confirmed、replaced、failed。交易雜湊本身不是完成。confirmed 必須綁到這一筆已送出的交易、成功收據、對得上的 ClaimWithdrawal、原帳戶、請求編號、實收金額，以及同一條鏈上相容的區塊。被移除的 log、收據和 log 的區塊對不上、換了一條鏈，或少了區塊欄位，都維持未核對。費用是 `gasUsed × effectiveGasPrice`。缺資料、來源對不上，或沒辦法排除其他餘額變動，就維持未核對。呼叫端自己帶的數字即使加減對得上，也不能當成完成。

同一個任務在第一次等待之前就佔住進行中的狀態，結果記在這條流程上，不是只靠呼叫端事後傳回 prior。交給記錄器或錢包之前，會用現在的時鐘對第二次觀察跑 `settleReread()`，計畫、區塊、觀察與核准到期都算。沒有目前這筆任務就不能繼續，也不會退回已經清掉的舊閉包。pending 與 unknown 跟重讀結果分開放，重讀、重新整理或回到分頁時不會被畫回 idle。結果不明時走核對，不再送一次模式 A。

確認這次領到自己的原地址，只代表這一筆。不是永遠信任這個地址，也不是通訊錄。

不廣播測試使用會記下 `eth_sendTransaction` 的假提供者，而且那個提供者不會廣播。只有呼叫端明確傳入 `testOnlyAllowWalletRequest === true`，而且提供者自己標成 `noBroadcast`，才會把那一筆呼叫交給假提供者。預設、正式頁面、主網旗標與重讀通過都不會走到這裡。

## 客戶端自有證據

送出前多了一道只看客戶端自己證據的關卡 `modeAClientGate`。輸入是重讀、dry-run 效果檢查、即時身分、部署核准，以及呼叫端傳入的 session（`auth_time`、`address`）。這道關卡不讀伺服器給的 `simulation`、`wallet_request_allowed`、`preflight_ok`。`clientConfirmDecision` 仍只用於預覽畫面。通過這道關卡也不會把送出打開；`requestUserSend()` 維持直接拒絕。

`effectsCheck` 在重讀用的同一個區塊標籤上，對 Lista proxy 做 `eth_call`（`from` 是帳戶，`data` 是觀察到的 `claimWithdraw(idx)`）。沒有 revert 記成 `dry_run_pass`；revert 是失敗；節點不支援或出錯，包含 JSON-RPC `-32602`，是資料不足（`block_identity_unsupported`）。這是 dry-run，不是 trace。它不寫回 `preflight_ok`，也不把未完成的模擬標成通過。它不取代 ADR 0001 要求的模擬。要不要用它取代那份模擬是 D-2，由使用者決定。這份實作不修訂 ADR 0001。

部署核准要三份一致：`action-client/src/trusted-lista.mjs` 的 `DEPLOYMENT_APPROVALS`、`config/protocols/lista-bsc.json`、`src/guardian/_resources/config/protocols/lista-bsc.json`。清單可以是空的。空清單仍然有效，意思是 `unapproved_deployment`，重讀不能通過。鏈上讀到的雜湊不會自動寫進清單。`action-client/tools/propose-lista-approval.mjs` 只對既有的信任 RPC 做唯讀查詢並印出候選；`provenance` 與 `validUntil` 留給人填，工具不寫設定檔。沒有加入第二個 RPC。

`auth_time` 用的是規格裡的建議預設：15 分鐘（`SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS`）。這個數是操作者擁有的建議值，不是 D-9 的決定。缺少 `auth_time`，或它早於這個門檻，關卡要求重新登入。

伺服器被入侵時的影響上限：客戶端釘死 proxy、selector、`value` 0 與 chain 56，並從鏈上的 uuid／idx 重建 calldata。登入地址、session 地址、即時 `eth_accounts[0]`、`plan.sender`、`plan.recipient`、`plan.execution_account` 必須是同一個地址。因此被入侵的伺服器最多只能請使用者把一筆自己可領的請求領到自己的地址。這個上限還要 `claimWithdraw` 把款項付給 `msg.sender`。付款對象是核准實作時的人工檢查，這裡不把它說成已經驗證。只在客戶端 bundle 本身可信時，這個上限才成立。

## 還沒有

真的主網送出仍要等錢包擁有者稍後另做一次授權。那次要寫明具體錢包、這筆提款請求、收款地址與費用。同意這份 Draft 不是把送出開關打開。`VITE_GUARDIAN_MAINNET_SEND`、重讀通過、測試旗標都不能當成那次授權。模式 B／C、TUF、Pages 部署都不在這次範圍。建置與測試不是那次授權。
