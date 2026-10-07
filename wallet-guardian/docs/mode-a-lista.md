# Mode A：Lista 原地址單步領取

狀態：引導路徑與不廣播測試已寫入。這不是主網送出授權。

路徑是：開啟任務、獨立重讀、模式 A 引導確認、追蹤收據與結果。客戶端沿用 `docs/p2-lista-reread.md` 的重讀：信任來源、部署核准、EIP-1898、TTL、核准期限、錢包帳戶與鏈。重讀通過不會把 `allowSend` 打開，也不會把測試用旗標設成 true。`requestUserSend()` 在正式預設路徑仍然直接拒絕；`VITE_GUARDIAN_MAINNET_SEND` 即使為 true，這個函式還是丟出錯誤，不會呼叫錢包。

按下確認時，引導流程對同一個任務做兩次獨立重讀，再用第二次觀察到的 idx 重建 `claimWithdraw(uint256)` calldata。不改收款人，不預先採用一批舊索引，也不走 `wallet_sendCalls`。idx、數量、部署、calldata 或可領狀態若有實質變化，這次不能送，必須換新的 revision，舊的不能悄悄沿用。

錢包結果只記 pending、unknown、confirmed、replaced、failed。交易雜湊本身不是完成；confirmed 只在經濟結果核對之後。送出結果不明、使用者拒絕、已替代或已失敗時，不會自動再走一次模式 A。

確認這次領到自己的原地址，只代表這一筆。不是永遠信任這個地址，也不是通訊錄。

不廣播測試使用會記下 `eth_sendTransaction` 的假提供者，而且那個提供者不會廣播。只有呼叫端明確傳入 `testOnlyAllowWalletRequest === true`，而且提供者自己標成 `noBroadcast`，才會把那一筆呼叫交給假提供者。預設、正式頁面、主網旗標與重讀通過都不會走到這裡。

## 還沒有

真的主網送出仍要等錢包擁有者稍後另做一次授權。那次要寫明具體錢包、這筆提款請求、收款地址與費用。同意這份 Draft 不是把送出開關打開。`VITE_GUARDIAN_MAINNET_SEND`、重讀通過、測試旗標都不能當成那次授權。模式 B／C、TUF、Pages 部署都不在這次範圍。建置與測試不是那次授權。
