# Wallet Guardian

- **是什麼：** `guardian`，唯讀的錢包監控 CLI。在你自己控制的電腦上監看你輸入的地址，涵蓋多條 EVM 鏈（逐鏈清單見下方「支援的鏈」）。不持有私鑰，不代你簽名，不廣播。
- **給誰用：** 想自己架監控的錢包持有人，以及替他們執行同一個 `guardian` 指令的 AI agent（例如 Grok Bot）。
- **怎麼開始：** 看下方「快速開始」：從 GitLab Releases 下載，用 `SHA256SUMS` 核對，再安裝 wheel。

> 正本和 issue 在 https://gitlab.com/aa22396584/wallet-guardian。Releases 和下載：https://gitlab.com/aa22396584/wallet-guardian/-/releases

唯讀的錢包監控與錢包瀏覽器任務工作台，程式在 `wallet-guardian/`。完整說明（英文在前、中文在後）見 [wallet-guardian/README.md](wallet-guardian/README.md)。

## 快速開始

這是預覽版 `0.4.0rc6`（標籤 `v0.4.0-rc.6`），不是正式版。沒有上 PyPI。不要用 `pip install guardian` 或 `pip install wallet-guardian-monitor`：套件索引上同名或相近的套件不是這個專案。

**1. 下載。** 打開這一版的 GitLab Releases 頁面：https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.6 （全部版本：https://gitlab.com/aa22396584/wallet-guardian/-/releases）。把 `SHA256SUMS` 和它列出的每一個檔案放進同一個目錄。rc.6 的 `SHA256SUMS` 列出 `wallet_guardian_monitor-0.4.0rc6-py3-none-any.whl`、`wallet_guardian_monitor-0.4.0rc6.tar.gz`（sdist）與 `constraints.txt`。如果 Releases 頁面有比這更新的版本，改用最新那一版，並把下面的檔名換成那一版的檔名。

**2. 核對雜湊。** 在那個目錄執行下面其中一行。第一行用在 Linux，第二行用在 macOS。缺檔或雜湊不符都會失敗；失敗就不要安裝。Windows 沒有 `sha256sum` 時，用 [wallet-guardian/docs/install.md](wallet-guardian/docs/install.md) 裡的 Python 指令。

```bash
sha256sum -c SHA256SUMS
shasum -a 256 -c SHA256SUMS
```

**3. 安裝並跑一輪（不送出通知）。** `answers.json` 列出你要監看的地址與鏈，寫法見 [wallet-guardian/README.md](wallet-guardian/README.md#quick-start) 的範例（合成地址）。

```bash
uv tool install --constraints constraints.txt ./wallet_guardian_monitor-0.4.0rc6-py3-none-any.whl
export RPC_ETHEREUM_MAINNET_URL=https://ethereum-rpc.publicnode.com
guardian init --from answers.json --yes
guardian run-once --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json" --no-send
guardian coverage --mcm-config "${XDG_CONFIG_HOME:-$HOME/.config}/guardian/monitor.json"
```

RPC 網址是你自己選的公開 JSON-RPC，每一條啟用的鏈一個變數；沒設時 `run-once` 停在 `endpoint_missing`。上面的 publicnode 只是範例，不是推薦：它會回餘額，但拒絕不帶地址的 `eth_getLogs`，所以授權掃描（`permissions.standard`）會是缺口。`uv tool install` 不會自動套用 `constraints.txt`，要自己帶 `--constraints`。安裝、升級與解除安裝的完整步驟見 [wallet-guardian/docs/install.md](wallet-guardian/docs/install.md)。

## 支援的鏈

依 [wallet-guardian/docs/coverage-matrix.md](wallet-guardian/docs/coverage-matrix.md)：Ethereum、BNB Chain、Arbitrum One、Base、Optimism、Polygon PoS；Solana 只有部分（不解析交易）；Avalanche C-Chain 有限；Bitcoin 只讀地址集合；TRON 是實驗性功能，預設關閉，沒有實測。各鏈深度不同，逐格狀態以那份矩陣為準。

## 先知道這些

- 唯讀：不簽名、不組交易、不廣播；不要求助記詞或私鑰。主網簽名沒有被授權。
- 預設不會通知你：`run-once` 只跑一輪，沒有安裝排程，告警只寫進本機 outbox。
- 讀取失敗、缺 key 或被限流是缺口，不是 0，也不是安全。
- 預覽版：rc.6 的 release evidence 是 `NOT_RUN`，72 小時 shadow 沒有做。
- 完整的狀態與限制、安全邊界與「現在做不到」見 [wallet-guardian/README.md](wallet-guardian/README.md)。

## 從原始碼 checkout 開發

```bash
cd wallet-guardian
./scripts/bootstrap.sh
uv run guardian doctor --json
uv run guardian run-once --config private/config.json --json
```

私人地址與 API key 不放在這份說明。公開設定只存變數名稱。秘密可以放在私人 `secret-profile.json`，或放在行程環境。`guardian init` 寫出的 profile 用的是另一組名稱（例如 `RPC_<CHAIN>_URL`）。`wallet-guardian/config/env.example` 是舊 Lista／doctor 路徑的六把 key 加 `GUARDIAN_MAINNET_SEND`。`.env.local` 只載入這組舊名稱。其中 GoPlus、DeBank、Zerion，以及模擬用的 `TENDERLY_ACCESS_KEY`，在本版不會被呼叫。範例監控檔的 `tenderly_base`（`TENDERLY_BASE_URL`）是 Base 授權日誌路由會用到的 RPC，端點有值時會送出地址。邊界、降級狀態與固定 HTTPS 紀錄見 `wallet-guardian/README.md` 與 `wallet-guardian/docs/deploy.md`。

這不是投資建議。主網簽名沒有被授權。
