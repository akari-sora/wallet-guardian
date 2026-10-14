# 監控快速開始

這一包是程式，不是上線。沒有自動切換，也沒有 live 驗證。鏈登錄的 `enabled` 仍是 false。`sources probe` 可以回報身份 `matched`，但 `probe` 欄位是 `untested`，不是 `live_pass`。不要把 fixture 通過當成節點通過。

公開的 `examples/monitor.json` 不含地址、金鑰或收件人。帳戶、端點與憑證放在 git 工作樹外面。

## 私人檔案

POSIX 上把帳戶檔與秘密 profile 設成 `0600`。狀態目錄設成 `0700`。Windows 不強制這些權限位元。三個路徑都不要放進這個 repo。

環境變數只放路徑，名稱是：

- `MONITOR_ACCOUNTS_FILE`：帳戶檔。`schema_version` 是 `guardian.accounts.v1`。
- `MONITOR_SECRET_PROFILE`：秘密 profile。鍵是環境變數名稱，值是端點 URL 或憑證。
- `MONITOR_STATE_DIR`：狀態目錄。程式使用裡面的 `monitor.sqlite`。

公開的 monitor 檔只用 `endpoint_env` 與 `credential_env` 寫這些名稱。不要把帶 key 的 URL 寫進公開檔。

## 第一次跑

不需要改程式。依序：

```text
guardian config validate --mcm-config PATH
guardian sources probe --mcm-config PATH
guardian run-once --mcm-config PATH --no-send
guardian coverage --mcm-config PATH --json
```

`guardian run-once --config` 仍是舊的 Lista 路徑。`--config` 與 `--mcm-config` 不能同時用。

覆蓋裡的 `unsupported`、`not_observable`、`blocked` 與 `credential_missing` 就是那些狀態。失敗的讀取不是 0，也不是 safe。沒有報價是 `valuation_unknown`，不是 0 美元，也不是 1 美元。原生 ETH 的 0.5 門檻只用於原生資產是 ETH 的網路，用 wei 比較，不看 symbol 是不是 `ETH`。

`guardian export --public --mcm-config PATH` 只輸出計數與來源名稱。

## 影子期

`--no-send` 不呼叫 sender，也不把 outbox 改成 sent。公開 `delivery.mode` 的預設是 `outbox_only`，同樣不送。新的 outbox 從 pending 開始。舊監控繼續送它自己的通知。

對過帳：

```text
guardian compare-legacy --mcm-config PATH --legacy-state PATH
```

這只讀舊的 `monitor_state.json`，不寫它。輸出用 wallet id、標籤與遮罩過的交易 id。舊例行從來沒查的網路會列在覆蓋差異裡。

## 交接清單

通知要送出去，下面必須同時成立：

1. 公開 `delivery.mode` 是 `configured_sender`。
2. `delivery.profile_env` 指到的私人檔在 repo 外面、POSIX 權限 `0600`，而且 JSON 裡有 `"single_sender_handoff": true`。
3. 這次執行沒有帶 `--no-send`。`install-service` 產生的排程一律帶 `--no-send`，要送出必須自己另寫排程。
4. 呼叫端注入一個 sender，或私人 profile 明確 opt-in 一個 Telegram 頻道（見 `docs/notifications.md`）。

範例設定是 `outbox_only`，所以預設是關的。少任何一項，列留在 pending，狀態是 `send_disabled` 或 `sender_not_configured`。沒有 opt-in 就不會送。`--no-send` 優先於設定、opt-in 與注入的 sender。

同一時間只有一個 owner 能拿租約。另一個 owner 會被拒絕。已經標成 sent、`provider_accepted`、`unknown` 或 `failed_permanent` 的列不會自動重送。舊例行已經通知過的交易仍是 `suppressed_legacy`。注入的 sender 若在寫出前失敗，列留在 pending，並記下嘗試次數。Telegram 在請求寫出之後逾時是 `unknown`，見 `docs/notifications.md`。

舊例行要等這份交接明確完成之後才停止送出。這包不會自己停排程，也不會在失敗時刪狀態。`execution.auto_disable_schedule` 是 false。

```text
guardian monitor plan --mcm-config PATH
guardian watchdog --mcm-config PATH
```

`monitor plan` 只印排程、來源、預算與交付模式。`started` 是 false。它不啟動 daemon，也不安裝 cron。`watchdog` 在進入失明時寫一筆健康警報，離開時寫一筆恢復。重複的失明評估不再另開一筆。
