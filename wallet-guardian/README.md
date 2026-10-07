# Wallet Guardian

跨鏈錢包的唯讀監控，以及 Lista `claimWithdraw` 的未簽名計畫。使用者可見文字預設繁體中文，時區 `Asia/Taipei`。

這不是投資建議，也不是「已經可以在主網一鍵領取」的證明。

## 現在做得到

- `guardian doctor --json`：檢查環境、憑證是否已配置（不顯示值）、以及能連上的唯讀來源。
- `guardian run-once --config private/config.json --json`：對設定裡的 watch-only 地址讀取 BNB Chain 官方 RPC。Lista 提款會重讀 `idx`、`isClaimable` 與 BNB 數量。
- 快照、差異、告警去重、外部 watcher 未驗證時的重複／缺口標記。
- 缺 API key 時標 `LIMITED_DISCOVERY` 或 `BLOCKED`，不會把失敗讀成餘額 0。
- 固定版本 Action Client：檢查未簽名計畫的摘要與金額。錢包按鈕保持關閉。

## 現在做不到

- Agent、排程與這台共用電腦不能簽名或廣播。
- 主網測試交易未授權（`mainnet_test_transactions_authorized=false`）。
- 沒有 trace／fork／真錢包端到端，所以 Lista 領取的模擬是 `INCOMPLETE`，寫入覆蓋是 `DRAFT` / `BLOCKED`。
- 沒有 DeBank、Zerion、Alchemy、Tenderly、GoPlus key 時，不能宣稱全鏈 discovery 或保證模擬。
- 官方 BSC dataseed 停用 `eth_getLogs`，沒有 Etherscan key 就無法回補歷史領取。
- 沒有公開 webhook、沒有付費套餐、沒有持續監控。`monitoring_active` 是 false。
- Grok 的 localhost 不是你手機上的 localhost。這個頁面沒有對外網址。

## 安全邊界

監控程式只做讀取與未簽名計畫。禁止的 RPC 方法包含 `eth_sendTransaction`、`eth_sendRawTransaction`、`wallet_sendCalls`、`eth_sign`、`personal_sign`、`eth_signTypedData`。

私鑰與助記詞不要貼進設定、對話或這個目錄。`private/`、`data/`、`evidence/` 已在 gitignore。公開範例是 `config/public.example.json`，裡面不是真實地址。

Watch-only 地址只代表「拿來觀察」，不代表這個系統可以動用它。

## 安裝

```bash
cd wallet-guardian
./scripts/bootstrap.sh
```

需要 Python 3.12 與 [uv](https://docs.astral.sh/uv/)。

把私人設定放在 `private/config.json`（不要提交）。格式見 `config/public.example.json`。

## 指令

```bash
uv run guardian doctor --json
uv run guardian run-once --config private/config.json --json
uv run guardian self-check --json
uv run guardian operation-coverage --json
uv run guardian alerts test --dry-run
```

`run-once` 會把完整結果寫進 `evidence/`。終端機輸出是給操作者看的 JSON；失敗會有非 0 的 exit code。

## API key

只從環境變數讀取，不要寫進檔案或 README：

| 變數 | 用途 | 沒有 key 時 |
|---|---|---|
| `ETHERSCAN_API_KEY` | Etherscan V2 交易、ABI、日誌 | chainlist 仍會試；地址歷史為 LIMITED |
| `GOPLUS_API_KEY` | 地址／授權風險補充 | `LIMITED_DISCOVERY`，不會假裝已篩過 |
| `DEBANK_ACCESS_KEY` | DeFi discovery | `BLOCKED` |
| `ZERION_API_KEY` | 替代 discovery／webhook | `BLOCKED`；本建置也不開放 webhook |
| `ALCHEMY_API_KEY` | Portfolio token | `BLOCKED` |
| `TENDERLY_ACCESS_KEY` | 可選模擬 | `BLOCKED`；沒有它就不能把模擬當成可簽名證據 |

設定後重新執行 doctor。日誌會把 key 遮成 `***`。不要購買套餐，除非另外授權。

## Action Client

```bash
cd action-client
npm install
npm run dev
```

開發伺服器是 `http://127.0.0.1:43127`。可載入 `run-once` 產出的計畫，或頁面上的合成範例。合成範例不是你的餘額。

頁面會重算 digest。金額、收款地址與計畫不一致就拒絕。`在我的錢包確認` 目前禁用，因為：

1. 主網簽名未授權。
2. `eth_call` 成功看不到 BNB 餘額與 `ClaimWithdrawal` 事件，模擬不完整。
3. 設定中的地址仍是 watch-only。

剩餘工作：固定區塊的 fork 測試、有 trace 的模擬、使用者裝置上的真錢包拒絕／恢復測試、獨立發布與完整性檢查。完成前不能說領取功能已上線。

## 測試

```bash
uv run pytest -q
```

- `tests/v1_acceptance_cases.json`：原有 56 項監控案例，離線。
- `tests/v2_acceptance_cases.json`：新增 72 項操作案例，離線。
- fork、真錢包、手機與部署沒有在這輪執行，報告裡維持 `BLOCKED`。
- 真網讀取與離線測試要分開看。HTTP 200 本身不算通過。

## 資料來源

BNB Chain 官方 RPC、Lista 官方文件上的 StakeManager proxy、Hyperliquid 公開 info、Etherscan V2 chainlist、OFAC SDN CSV。文件網址與查證等級見 `docs/source-registry.json` 與 doctor 產生的 `source_registry.json`。
