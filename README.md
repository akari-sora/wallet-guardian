# Wallet Guardian

唯讀的錢包監控與錢包瀏覽器任務工作台，程式在 `wallet-guardian/`。

```bash
cd wallet-guardian
./scripts/bootstrap.sh
uv run guardian doctor --json
uv run guardian run-once --config private/config.json --json
```

私人地址與 API key 不放在這份說明。邊界、降級狀態與 Action Client 的剩餘工作見 `wallet-guardian/README.md`。

這不是投資建議。主網簽名沒有被授權。
