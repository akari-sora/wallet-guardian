# Wallet Guardian

跨鏈錢包的唯讀監控，以及錢包瀏覽器裡的任務工作台。使用者可見文字預設繁體中文，時區 `Asia/Taipei`。

這不是投資建議。建置與測試也不等於獲准執行任何主網資產交易。

license: pending。

多鏈監控套件 MCM-01 的範圍契約見 [docs/mcm-01.md](docs/mcm-01.md)。那是計畫，不是 live 驗證。

## 現在做得到

- `guardian doctor --json`：檢查環境、憑證是否已配置（不顯示值）、以及能連上的唯讀來源。
- `guardian run-once --config private/config.json --json`：對設定裡的 watch-only 地址讀取 BNB Chain 官方 RPC。Lista 提款會重讀 `idx`、`isClaimable` 與 BNB 數量。
- 快照、差異、告警去重、外部 watcher 未驗證時的重複／缺口標記。
- 缺 API key 時標 `LIMITED_DISCOVERY` 或 `BLOCKED`，不會把失敗讀成餘額 0。
- 固定版本任務工作台（`action-client@0.3.0`）：載入 typed 任務，核對目標、資產、接收人與費用，並把連接、登入、授權、批次、執行權限、升級與確認分開列出。模式 A 是原地址單步引導。錢包按鈕保持關閉。

## 現在做不到

- Agent、排程與這台共用電腦不能簽名或廣播。
- 主網測試交易未授權（`mainnet_test_transactions_authorized=false`）。
- 沒有 trace／fork／真錢包端到端，所以 Lista 領取的模擬是 `INCOMPLETE`，寫入覆蓋是 `DRAFT` / `BLOCKED`。
- 沒有 DeBank、Zerion、Alchemy、Tenderly、GoPlus key 時，不能宣稱全鏈 discovery 或保證模擬。
- 官方 BSC dataseed 停用 `eth_getLogs`，沒有 Etherscan key 就無法回補歷史領取。
- 沒有公開 webhook、沒有付費套餐、沒有持續監控。`monitoring_active` 是 false。
- Cloudflare Pages 已有公開來源，見 `docs/deploy.md`。程式常數 `fixed_https_origin` 仍是 false，所以這個套件不會發行公開任務連結。localhost 不是手機上的 localhost。網址回 200 不是執行授權。
- 模式 B 只在查到錢包的 EIP-5792 能力、而且是同鏈多筆操作時才考慮。這個建置沒有對真錢包送出 `wallet_sendCalls`，證據是未測。
- 模式 C 不支援。沒有已驗證、能在鏈上強制任務／鏈／原帳戶／合約／方法／資產／金額／接收人／期限／次數／費用的權限機制。Prompt、畫面或後端 if 不能代替。
- 新的智慧帳戶不能拿來假裝能操作原地址上既有的 Lista 部位。
- 手機錢包內建瀏覽器未測。模式 A 的真錢包流程也未測。

## 安全邊界

監控程式只做讀取與 typed 任務資料。禁止的 RPC 方法包含 `eth_sendTransaction`、`eth_sendRawTransaction`、`wallet_sendCalls`、`eth_sign`、`personal_sign`、`eth_signTypedData`。

Grok 只產生 `guardian.task.v1`。它不能在運行時產生並執行任意簽名 JavaScript。任務連結只帶 `ref_` 參考碼，本身不是執行授權，也不能帶其他使用者的私人資料。`fixed_https_origin` 仍是 false 時，公開連結不會發行。部署紀錄裡的 Pages 網址還沒寫進這個常數。

主錢包私鑰留在使用者的錢包。限定執行憑證是秘密，不要放進這個共用工作區、任務 JSON 或對話。

私鑰與助記詞不要貼進設定、對話或這個目錄。`private/`、`data/`、`evidence/` 已在 gitignore。公開範例是 `config/public.example.json`，裡面不是真實地址。頁面上的合成任務也不是真實餘額。

Watch-only 地址只代表「拿來觀察」，不代表這個系統可以動用它。

## 安裝

發行 wheel 的安裝、釘選的 constraints、XDG 私人目錄、升級與降版見 [docs/install.md](docs/install.md)。不要從 PyPI 執行 `pip install guardian`：這個名稱沒有被保留。

在這個 checkout 裡開發：

```bash
cd wallet-guardian
./scripts/bootstrap.sh
```

需要 Python 3.12 與 [uv](https://docs.astral.sh/uv/)。

把私人設定放在 `private/config.json`（不要提交）。格式見 `config/public.example.json`。已安裝的程式預設把私人設定與狀態放在 XDG 目錄，見 `docs/install.md`。

## 指令

```bash
uv run guardian doctor --json
uv run guardian run-once --config private/config.json --json
uv run guardian self-check --json
uv run guardian operation-coverage --json
uv run guardian workbench --json
uv run guardian alerts test --dry-run
```

`run-once` 的終端機輸出是給操作者看的 JSON；失敗會有非 0 的 exit code。舊的 Lista 路徑把結果寫進狀態目錄（預設 `$XDG_STATE_HOME/guardian`，否則 `~/.local/state/guardian`），不寫進安裝套件。見 `docs/install.md`。

## API key

範例名稱在 `config/env.example`。把值寫進 gitignore 的 `.env.local`，或匯出到環境。已存在的環境變數優先。doctor 只報告 `configured` 或 `not_configured`，不會印出值。

Etherscan V2 只用一把 `ETHERSCAN_API_KEY`，涵蓋 chainlist 以及之後的帳戶、交易與日誌。沒有第二把鏈別金鑰。有 key 且 chainlist 回傳資料列時，doctor 才標 `LIVE_PASS`。沒有 key 時是 `LIMITED_DISCOVERY`，不會把未認證的呼叫當成通過。

| 變數 | 用途 | 沒有 key 時 |
|---|---|---|
| `ETHERSCAN_API_KEY` | Etherscan V2 chainlist、交易、ABI、日誌 | `LIMITED_DISCOVERY`，chainlist 不是 LIVE_PASS |
| `GOPLUS_API_KEY` | 地址／授權風險補充 | `LIMITED_DISCOVERY`，不會假裝已篩過 |
| `DEBANK_ACCESS_KEY` | DeFi discovery | `BLOCKED` |
| `ZERION_API_KEY` | 替代 discovery | `BLOCKED`；本建置也不開放 webhook |
| `ALCHEMY_API_KEY` | Portfolio token | `BLOCKED` |
| `TENDERLY_ACCESS_KEY` | 可選模擬 | `BLOCKED` |
| `GUARDIAN_MAINNET_SEND` | 使用者錢包的主網送出開關，預設 `false` | 確認鈕即使可按也不會呼叫錢包 |

`GUARDIAN_MAINNET_SEND=true` 也不會讓監控程式簽名或廣播。`mainnet_test_transactions_authorized` 在這個建置仍必須是 false。不要購買套餐，除非另外授權。

## 任務工作台

```bash
cd action-client
npm install
npm run dev
```

開發伺服器是 `http://127.0.0.1:43127`。可開啟任務檔，或載入頁面上的合成範例。合成範例不是你的餘額。私人任務走 `/t/<ref>`，登入後向同一個來源讀 `/api/tasks/<ref>`。`?ref=` 不會再讀公開靜態任務。網址不放地址、金額或金鑰。見 `docs/p1-private-task-api.md`。

頁面會重算 digest。金額、收款地址與計畫不一致就拒絕。連接錢包、登入簽名、代幣授權、批次確認、限定執行權限、帳戶升級與「確認這筆領取」分開列出。Lista 原地址單步領取只有最後一項需要確認。

`在我的錢包確認` 不會因為任務 JSON 自述 `preflight_ok` 而送出。計畫 `wallet_request_allowed=false`、未完成模擬或阻塞原因都會關掉送出。獨立鏈上重讀、效果模擬、允許送出分開顯示；重讀通過也不會打開送出。見 `docs/p2-lista-reread.md`。模式 A 單步領取的引導路徑與不廣播測試見 `docs/mode-a-lista.md`：正式送出仍關閉，交易雜湊不是完成。之後的同意不是把 `VITE_GUARDIAN_MAINNET_SEND` 打開。`VITE_GUARDIAN_MAINNET_SEND` 預設不設定。監控程式永遠不能簽名。

`查詢批次能力` 是另一個按鈕。它只在你按下時讀 `wallet_getCapabilities`。不支援、查到別的鏈、或狀態是 `ready`，都留在模式 A。同鏈而且多於一筆、能力是 `supported`，畫面才標模式 B。真錢包批次未測。這個預覽不呼叫 `wallet_sendCalls`。

模式 C 保持關閉。程式裡沒有權限合約。任務資料自己宣稱已驗證，不算鏈上強制。

介面規則（狀態列、錢包卡片、CSP 限制、`src/typography.mjs`、版面檢查工具）見 `docs/action-client-ui.md`。

Python 的狀態重讀可以讓 `preflight_ok` 為 true，但 `continue_to_wallet` 仍是 false。客戶端的獨立重讀不會寫回這個旗標。交易雜湊不算完成。拒絕、送出不明、替代與完成核對有離線狀態機測試。

固定 HTTPS 來源見 `docs/deploy.md`。套件常數 `fixed_https_origin` 仍是 false，所以不會發行任務連結，這也不是主網執行授權。

剩餘工作：把已驗證 origin 接進連結發行設定；手機錢包內建瀏覽器實測；模式 A 的真錢包拒絕與完成核對；模式 B 的真錢包批次；以及一個真的鏈上權限合約。完成前不能說領取功能已上線。已上線的 Pages 套件是合併前的建置，這次沒有重新部署。

## 測試

```bash
uv run pytest -q
```

- `tests/v1_acceptance_cases.json`：原有 56 項監控案例，離線。
- `tests/v2_acceptance_cases.json`：72 項操作案例，離線。
- `tests/workbench_acceptance_cases.json`：31 項任務工作台案例，離線。不併入前面兩份，也不把未測項目算成通過。
- fork、真錢包、手機內建瀏覽器與部署沒有在這輪執行。模式 A／B 標未測，模式 C 標不支援。
- 真網讀取與離線測試要分開看。HTTP 200 本身不算通過。建置與測試不等於獲准執行主網交易。

## 資料來源

BNB Chain 官方 RPC、Lista 官方文件上的 StakeManager proxy、Hyperliquid 公開 info、Etherscan V2 chainlist、OFAC SDN CSV。文件網址與查證等級見 `docs/source-registry.json` 與 doctor 產生的 `source_registry.json`。

## License

Wallet Guardian is licensed under the GNU Affero General Public License v3.0 only (SPDX: AGPL-3.0-only). See [LICENSE](LICENSE). If you run a modified version as a network service, AGPL section 13 requires offering its source to that service's users.
