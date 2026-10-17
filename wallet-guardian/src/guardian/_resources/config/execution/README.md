# 執行登記表（S-1a）

這個目錄描述執行登記表、政策和候選產生規則。Loader 讀進來的條目一律是未核准。`approved_by_user` 會被拒絕，不代表使用者核准。`user_approved` 在檔案裡必須是 false；寫成 true 也會被拒絕。

候選產生器只印出候選，不寫入 `registry.json`。合併到主線不是核准。

## 四份鏈目錄

這四份目錄各管一件事。檔案在不在，都不打開送出。

1. `config/chains.json` 是監控程式的鏈清單。目前只有鏈 56 標成 enabled。
2. `config/chains/` 是 MCM 計劃中的網路登記。條目的 `enabled` 是 false，狀態是 planned_not_live_verified。檔案存在不代表那條鏈已啟用。
3. `src/guardian/_resources/config/` 是安裝包裡的位元組副本，由 `tools/sync_package_resources.py` 複製。它不是瀏覽器客戶端的執行副本。
4. `config/execution/chains.json` 是執行 RPC 目錄。D-4 還沒有決定端點，所以這份檔案的 `chains` 是空的。`enables_send` 是 false。這裡沒有第二個 `fixed_https_origin` 開關。

## 這一包做了什麼

- 嚴格 JSON loader（Python 與 action client）拒絕浮點數、重複 key、未知欄位，以及 v1 不認得的 proxy 種類。v1 只接受 `none` 和 `eip1967_impl`。
- 部署條目記錄鏈、位址、proxy 種類、pin 住的 implementation，以及權限型別。
- 政策條目記錄允許的動作、上限，以及收款人規則。罰金上限在 v1 必須是字串 `0`。
- 地址參數必須有角色。`receiver`、`onBehalf`、`owner` 必須是使用者自己的帳戶。`spender`、`target` 必須是同一條鏈上的登記部署。只有標籤不夠。
- 身分綁定是 `single_account_v1`。沒有「自己的地址清單」。SIWE 位址只在登入階段有值時才比對。換帳戶會清掉 evidence，保留 pending 和 unknown。
- 信任關鍵路徑列在 `trust-critical-paths.json`。改這份清單本身就是信任關鍵變更。清單不是邊界。`policy-status.json` 還沒有這個檔案，所以不在這份清單裡。

這一包沒有組交易，也沒有送出。
