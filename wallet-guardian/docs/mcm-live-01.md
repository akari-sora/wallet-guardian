# MCM-LIVE-01 L3：私人 shadow 工具

這份文件說明私人 shadow 怎麼跑、怎麼計數、演練與回退怎麼記。L3 只交付工具、轉換與測試。它不對真實錢包跑一輪，不讀 `private/`，也不把任何真實輪次算進 24。開始計數是合併之後的操作步驟，不在這一包。

工具退出碼 0 只表示這一輪被記下來。降級、阻塞、fixture、演練都不合格。公開範例維持範例；runtime 用操作者自己的設定與環境變數名稱，不把公開範例改成 live。

## 必須在有真實 DNS 的主機上跑

審查環境會把所有主機解到 `198.18.0.1`。SSRF 釘選會拒絕那個位址，所以真實 shadow 要在有真實 DNS 的主機上跑。工具把這種失敗記成 `private_endpoint` 或 `fetch_error`，不記成 ok，也不寫成「沒有新聞」。

## 每輪雙跑

1. 私人狀態目錄 `0700`、檔案 `0600`。沿用既有來源預算。runtime 設定是啟用自己的檔，不是修改公開範例。
2. 舊 state 在穩定時點只讀備份。快照是位元組相同的複本，並記下 sha256、size、mtime 與 taken_at。複製過程中來源的位元組或 mtime 變了就拒絕，稍後再試。不寫入、不鎖定、不重新命名來源。
3. legacy 原本的工作與通知優先。MCM 不等待它，也不用逾時去拖延它。
4. 以固定的程式 SHA 執行 `run-once --mcm-config <私人設定> --no-send`。stdout、stderr、退出碼與 argv 各存一檔。增量窗口是設定裡的 `polling.interval_seconds`（範例是 10800 秒），結束在這一輪的時鐘，不是行程跑了幾秒。
5. 接著跑 `watchdog` 與 `coverage`。watchdog 讀這一輪以及持久化的 last_success，也有獨立觸發，不依賴這一輪掃描成功才跑。
6. `compare-legacy` 對那份快照做比較。完整事件身份仍然走 `matched`。舊的 `alerted` 字串加上 `latest_seen` 與 `runs` 的 `*_since_<hhmm>`，轉成交易層列：同一 wallet、network、tx 對上時記 `matched_tx_level`；落在已確認共同區間卻沒有 MCM 事件的是 `legacy_only`；MCM 有而 legacy 沒有的是 `mcm_only`。`awaiting_finality` 與 `uncomparable` 仍分開。說明文字不猜區塊、地址或 log position。
7. 原始差異先寫進證據目錄的 `diffs/raw-compare.json`，然後才做既有通知去重。legacy 已通知的記號不能把 MCM 的真漏報藏起來。
8. MCM 的重要獨有發現只有在明確加上 `--relay` 時，才寫一筆 `origin=mcm_shadow` 的私人接力紀錄，交給既有 legacy sender 路徑核對後通知一次。預設關閉。沒有新的 sender，工具與測試都不送出。同一發現的 dedup id 已存在就不再寫第二筆。技術比較留在私人檔案，不每 3 小時推給使用者。

入口是：

```sh
uv run --frozen python tools/mcm_shadow_round.py \
  --mcm-config <cfg> \
  --legacy-state <path> \
  --evidence-dir <私人目錄> \
  [--drill restart|source_failure|rollback] \
  [--relay]
```

證據目錄若在 git 工作樹裡面，或權限寬於 `0700`，工具拒絕建立。

同一輪還會留下這些既有指令的原始輸出，各為獨立檔：

```sh
uv run --frozen guardian run-once --mcm-config "$MCM_CONFIG" --no-send
uv run --frozen guardian watchdog --mcm-config "$MCM_CONFIG"
uv run --frozen guardian coverage --mcm-config "$MCM_CONFIG"
uv run --frozen guardian compare-legacy --mcm-config "$MCM_CONFIG" --legacy-state "$LEGACY_SNAPSHOT"
```

`degraded` 或 `blocked` 不會因為 CLI 退出 0 就算通過。

## 計數

第 0 輪是 baseline，不計。

合格要 24 個連續的增量窗口，而且從第一個計入窗口的起點到最後一個完成至少 72 小時。一窗的目標是 3 小時，容許上下 30 分鐘，所以長度是 2.5 小時到 3.5 小時（9000 到 12600 秒）。上一窗完成到下一窗開始若相差超過 30 分鐘，連續中斷，已累計的序列作廢，從新的合格窗再數。

一窗要同時滿足：沒有未解釋的 `legacy_only`、沒有未解釋的 `mcm_only`、沒有重複通知、沒有秘密外洩，而且每個適用的 required 能力都有新證據。下列情形都不合格，也不能寫成 ok：只有 native 餘額成功、只有 HTTP 200、沒有事件、mock 或 fixture 通過、placeholder feed。

缺口以原名記入證據：`no_allowlisted_feed`、`news_not_supplied`、`credential_missing`、`private_endpoint`、`fetch_error`、`budget_exhausted`。已宣告、已凍結、而且不是 required 的缺口可以留在合格窗裡，但 status 就是那個名稱。required 上的缺口使該窗不合格。不寫成「沒有新聞」或 ok。

範圍是凍結的：required 能力、規則、外掛、錢包集合、網路與設定雜湊。拿掉錢包、網路或 required 能力會縮小分母，那是 `scope_changed`，序列重算，不是通過的方法。改設定雜湊、`content_hash` 或外掛集合也重置。`content_hash` 是 `src/guardian/` 與 `config/` 下所有檔案內容的雜湊（略過 `__pycache__`），不是 commit SHA；docs、tests、tools 不在裡面，所以純文件或文案的 commit 不重置。先前輪次留在 history，不刪。

證據缺漏時一律判不合格（fail-closed）：`duplicates`、`secret_leaks`、`unexplained_legacy_only`、`unexplained_mcm_only` 必須是明確的非負整數，`no_send` 必須明確是 true。缺少或格式不對就是 `evidence_missing:<欄位>`，不能當成 0 或「沒送」。scope 缺 `config_hash` 或 `content_hash` 時是 `scope_missing`。反向 shadow 的輪次也要有明確的 `no_send: true`。

常規舊六鏈不能倒退。Avalanche 若已嘗試但尚未完整，維持 `expansion_pending`，不能隱藏，也不能聲稱七鏈都已覆蓋。若發現重大曝險，把該能力升為 required。

## 演練

演練另記，不佔那 24 格，也不混進自然序列。

- `restart`：一輪中途被停。恢復使用已提交的高度，不採用更高的 partial，不跳過游標。同一 event id 只留一筆。
- `source_failure`：注入一個失敗來源。該輪不合格，記成 `fetch_error`。
- `rollback`：先停新的 sender，確認 in-flight，再讓舊 sender 接同一個送達水位。兩個 sender 不會同時送。

## 反向 shadow

24 窗且跨度夠了之後才進入 `cutover`。接著是 8 輪、跨度至少 24 小時的 no-send 反向 shadow，legacy 當 shadow。有送出的反向輪不計。8 輪且跨度夠了，phase 才是 `done`。

階段是 `baseline`、`qualifying`、`qualified`、`cutover`、`reverse_shadow`、`done`，另有 `rolled_back`。回退之後不再把新的自然窗算進舊序列。

切換記的是通知與監控所有權，不是資產交易權限。預先留下切換時的程式 SHA、設定雜湊、通道指紋、事件水位，以及舊 sender 已停、新 owner 尚未同時開工的紀錄。shadow 積壓不一次送出。

## 私人證據放哪裡

操作者傳入的證據目錄必須在 repo 外面。目錄 `0700`，檔案 `0600`。公開 repo 與公開 outbox 不放原始差異、地址、金鑰或收件人。一輪通常有：

- `legacy-<sha256>.snapshot` 與同名的 `.snapshot.json`
- `commands/` 下 `run-once`、`watchdog`、`coverage`、`compare-legacy` 的 stdout、stderr、exit、argv
- `diffs/raw-compare.json`
- `budget.json`、`leak-scan.json`、`poison-review.json`、`cursor-report.json`
- `restart-proof.json`（只有 restart 演練）
- `rounds.jsonl`、`cutover.json`、`round.json`
- `relay/<dedup>.json`（只有 `--relay`，且同一 dedup 只會有一筆）

外洩掃描只看證據目錄以外的路徑。紀錄是種類、路徑與秘密的短雜湊，不寫出秘密本身。

## 預算欄位

`budget.json` 來自分輪的傳輸計數，不是把 CLI 退出 0 當成用量：

- `rpc_calls_by_network`：各網路的 RPC 次數
- `etherscan_calls`：Etherscan 次數
- `news_gets`：新聞 GET 次數
- `kinza_eth_calls`：打到已釘選 Kinza Pool 或 Provider 的 `eth_call`，另外計，不從 RPC 次數裡扣掉

Kinza 的 Pool 與 Provider 身份、以及準備金清單，每一輪、每一個錢包都重讀。清單或實作變了就丟掉這一輪的中介資料。aToken 與借款的 `balanceOf`、`getUserAccountData` 不進快取，兩個錢包分開計。BSC 約 25 個準備金、Ethereum 約 15 個，每個準備金約 9 個 `eth_call`。shadow 的 `budget.json` 要含 `kinza_eth_calls`，也要併入 run-once 的 `budget`。Pool 與 Provider 的公開地址見 `docs/mcm-01.md`；文件上的地址不是 safe list，也不是送出白名單。細節見 `docs/mcm-live-01r.md`。

## 投毒備註與游標

兩筆投毒文字是私人 heuristic 標籤，帶來源與信心。地址不是完整的 `0x` 加 40 個十六進位就存 null，不從前綴猜測。抑制只用於同一 sender、同一條鏈、同一種已識別垃圾類型的重複微額。該 sender 的非微額轉帳與核准仍然警示。標籤是 `address_poisoning`，不是 safe。

散文、短 hash、或只有高度沒有 hash 的游標進 raw archive，狀態 `unmapped`，不推導區塊。已有的 BSC 游標要鏈、整數高度與完整 hash 都對上 `eth_getBlockByNumber` 才用；hash 不符就拒絕。重建從已核對高度減去 overlap 開始，用 event id 去重。重疊區間裡被重組換掉的交易不重複計。
