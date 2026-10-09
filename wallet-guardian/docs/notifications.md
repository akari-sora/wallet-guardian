# 通知

license: pending

這一包只加一個頻道：Telegram Bot API。預設仍是 `outbox_only`，不會送。沒有 Slack、email 或 webhook 用戶端。也沒有 Grok 用戶端；這裡沒有可呼叫的 Grok API，不能用假的回條假裝送過。

金鑰由操作者自己帶。公開設定只寫環境變數名稱。Bot token 不進 repo、不進資料庫、不進日誌與錯誤。下面的 chat id 是合成範例。

## 設定

1. 在 Telegram 找 BotFather，用 `/newbot` 建立一個 bot，拿到 token。
2. 把 token 放進環境變數，例如 `TELEGRAM_BOT_TOKEN`。不要寫進公開設定，也不要寫進 profile 檔的值裡。profile 只記變數名稱。
3. 對這個 bot 開一個聊天。chat id 是數字（群組可以是負數）。合成範例是 `10001`，不是真實收件人。
4. 私人 profile 放在 git 工作樹外面。POSIX 權限 `0600`。公開 `delivery.profile_env` 指到存放這個路徑的環境變數名稱。

profile 形狀：

```json
{
  "schema_version": "guardian.delivery-profile.v1",
  "channel": "telegram",
  "opt_in": true,
  "owner_id": "owner-a",
  "chat_id": "10001",
  "token_env": "TELEGRAM_BOT_TOKEN",
  "single_sender_handoff": true,
  "legacy_shadow": false,
  "event_watermark": null
}
```

`opt_in` 必須是布林 `true`。漏了、是 false，或 profile 宣稱接上舊的 shadow sender（`legacy_shadow` 不能為 true），都不會送。新使用者要自己填頻道與收件人，不能宣稱沿用舊 shadow。

公開 `delivery.mode` 要是 `configured_sender`。範例檔維持 `outbox_only`。

遷移目前這兩個錢包時，同樣只有一個 `owner_id`。`event_watermark` 是已經交給舊通知的最後一個 outbox id；小於等於這個 id 的列標成 `suppressed_watermark`，不會再送。進行中的列、`unknown`，以及 handoff 閘門照舊生效。

## 語意

程式只對 `https://api.telegram.org` 送 `sendMessage`。別的 host、別的路徑、重新導向，在連線前拒絕。位址檢查與唯讀 transport 相同：先解析，釘住連線位址，私人位址與 loopback 不送。這是送出用的單獨允許清單，不是鏈上讀取允許清單。

Telegram 回 `ok: true` 而且有 `message_id`，狀態是 `provider_accepted`。這表示 Telegram 收下了這則訊息。不是使用者已讀，也不是端到端的 exactly-once。

| 結果 | 狀態 | 之後 |
|---|---|---|
| `ok` 且有 `message_id` | `provider_accepted` | 不自動重送 |
| 400 chat not found，或 403 forbidden | `failed_permanent` | 錯的收件人，不重試 |
| 429 | 留在 pending | 等到 `retry_after` 才再試 |
| 寫出之後逾時，或連線被重置 | `unknown` | 不自動重送 |
| 遠端已接受，本地在記下結果前崩潰 | `unknown` | 重啟後不送第二次 |

`failed_permanent` 與 `unknown` 都不是安全，也不是零。失敗的讀取同樣不是零。

## unknown

送出前會先把這一列標成 in-flight。若程序在 Telegram 可能已經看到請求之後死掉，重啟時看到這個記號，就把列改成 `unknown`，不會再 POST。

逾時或連線重置若發生在請求已經寫出之後，同樣是 `unknown`。不能因為本地沒有 `message_id` 就再送一次。

操作者的處理是 acknowledge：`resolve_unknown(..., action="acknowledge")` 把列標成 `resolved_acknowledged`。它不送出，重啟也不會送。這一包沒有自動重試 unknown 的路徑。`action` 不是 `acknowledge` 會被拒絕。

## canary

這一包不跑 canary。操作者之後在自己的頻道上明確開啟之後，才另案做。`run-once` 與 `monitor plan` 的 canary 都是 `NOT_RUN`。不能把它讀成通過或失敗。

`--no-send` 一律贏過 `configured_sender`、opt-in、租約與注入的 sender。它不呼叫 sender，也不把 outbox 改成 sent。
