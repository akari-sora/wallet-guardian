# Wallet Guardian

唯讀的錢包監控與錢包瀏覽器任務工作台，程式在 `wallet-guardian/`。

```bash
cd wallet-guardian
./scripts/bootstrap.sh
uv run guardian doctor --json
uv run guardian run-once --config private/config.json --json
```

私人地址與 API key 不放在這份說明。公開設定只存變數名稱。秘密可以放在私人 `secret-profile.json`，或放在行程環境。`guardian init` 寫出的 profile 用的是另一組名稱（例如 `RPC_<CHAIN>_URL`）。`wallet-guardian/config/env.example` 是舊 Lista／doctor 路徑的六把 key 加 `GUARDIAN_MAINNET_SEND`。`.env.local` 只載入這組舊名稱。其中 GoPlus、DeBank、Zerion，以及模擬用的 `TENDERLY_ACCESS_KEY`，在本版不會被呼叫。範例監控檔的 `tenderly_base`（`TENDERLY_BASE_URL`）是 Base 授權日誌路由會用到的 RPC，端點有值時會送出地址。邊界、降級狀態與固定 HTTPS 紀錄見 `wallet-guardian/README.md` 與 `wallet-guardian/docs/deploy.md`。

這不是投資建議。主網簽名沒有被授權。
