# Wallet Guardian 檢視總覽（2026-10-14，main 16559ac / v0.4.0-rc.4）

## 這份檢視怎麼做的

- 檢視期間全程唯讀（沒改程式、沒開 issue）；這幾份報告之後以 docs-only commit 加入本目錄。
- 四條平行檢視，下文用編號引用：
  - 01 = [2026-10-14-architecture-gaps.md](2026-10-14-architecture-gaps.md)：架構與缺口
  - 02 = [2026-10-14-latency-coverage.md](2026-10-14-latency-coverage.md)：即時性與覆蓋率
  - 03 = [2026-10-14-mode-a-packages.md](2026-10-14-mode-a-packages.md)：Mode A 工作包
  - 04 = [2026-10-14-docs-overclaim-audit.md](2026-10-14-docs-overclaim-audit.md)：文案稽核
- 各份報告附 `file:line` 證據；行號對應 `16559ac`。
- 實際跑過的：`pytest`（698 passed、3 failed、4 errors，全部是環境因素，見下）；對公開端點的唯讀 curl；下載正式站 bundle 數字串。
- **範圍限制**：程式檢視集中在 MCM pipeline（`guardian run-once --mcm-config`），也就是要分享給別人用、也是 72 小時 shadow 要驗證的那套。**目前實際在發告警的舊私人例行（Grok 執行）沒有審**，那段程式不在 repo。

## 一句話結論

文案本身很保守，沒有「全鏈／production ready／CI 通過／72h 完成」這類誇大用語。真正的問題是：**照文件設定起來的 MCM，有好幾條「該叫卻不會叫」的路徑，而且監控停了也不會有人知道。** 這些要在 72 小時 shadow 之前修，否則 shadow 驗證的是一個已知會漏報的設計。

## 我親自抽查確認的關鍵事實

| 事實 | 證據 |
|---|---|
| 區塊段先寫 events＋cursor（`alerts=[]`），告警要到整輪最後才寫；中途中斷就永久漏報 | `scan/drive.py:1017-1022`、`pipeline.py:398-405` |
| 整個 src 只有一處會給候選事件 USD 報價，而且只處理原生幣 → 代幣 ≥ $1k 不會告警 | `balances/reconcile.py:19-38`（唯一 `quote_valuation="valued"`） |
| `examples/`、`config/`、`init` 都沒有 `native_unit_thresholds` → 預設下 BSC ≥1 BNB 不會叫 | grep 結果；`balances/native.py:345-382` |
| GoPlus 在 pipeline 裡一律 `not_run`，有 key 也不呼叫 | `pipeline.py:1148-1174` |
| 附的 cron／systemd 範本一律帶 `--no-send` | `deploy/systemd/guardian-unattended.cron:9`、`guardian-mcm-shadow.service:14` |
| `-32701`（publicnode「請指定 address」）被當成「範圍太大」一路對半切，切到單一區塊失敗後不換來源 | `scan/window.py:98`、`scan/drive.py:181` |
| BSC publicnode 的 `eth_getLogs` 今天實測回 JSON-RPC `-32701`（要求指定 address），不是 HTTP 403；原因是端點政策，不是額度 | 02 附錄 A 的 curl 紀錄 |
| Etherscan V2 免費版不含 Base／OP／BSC | 今天不帶 key 實測：8453／10／56 回 `Free API access is not supported for this chain` |
| 正式站 bundle 含 `eth_sendTransaction`×2、`testOnlyAllowWalletRequest`×3 | 下載 `wallet-guardian-action-client.pages.dev` 的 `index-*.js` 計數 |
| 舊預覽部署 `5a09b4e6.…pages.dev` 仍回 200、沒有 CSP、bundle 含 `eth_sendTransaction` | 今天 curl |

## 統一優先順序

排序原則：漏報 > 誤報；風險高、使用者價值大、工作量小的先做。

### 第 0 階段：先止住「靜默漏報」（72 小時 shadow 之前完成）

| # | 工作包 | 為什麼最先 | 量級 | 細節 |
|---|---|---|---|---|
| 0-1 | 告警與游標同一筆 transaction（或「已存未告警」補發）＋ `run-once` 單例鎖 | 任一中斷都會永久漏報 | M | 01 P0-1 |
| 0-2 | 每條鏈隔離例外：`SecurityError`、`--news-input`／`--price-input` 讀不到都降級成 gap，不中止整輪 | 一條鏈出事 → 所有鏈都沒告警，再疊加 0-1 | S | 01 P0-2、02 WP-1 |
| 0-3 | 修正 `-32701`／`-32005` 分類並自動換下一個來源 | 任何鏈的 RPC 指到 publicnode，代幣與授權掃描游標就永遠卡住；README 快速開始示範的 `ethereum-rpc.publicnode.com` 就是這種端點，照著做 Ethereum 第一輪就沒有授權監控 | S | 02 WP-3（程式部分）、04 H1 |
| 0-4 | `init` 寫入 `native_unit_thresholds`（至少 BSC 1.0）；`threshold_unavailable` 不准默默推進 baseline | BSC ≥1 BNB 是你的明確目標，預設卻完全不叫 | S | 01 P0-4、04 H2 |
| 0-5 | 代幣估值：CoinGecko 免費合約報價；拿不到時，穩定幣清單或每代幣 raw 門檻發「未估值大額移動」 | 代幣 ≥ $1k 目前沒有任何程式路徑 | M | 01 P0-3 |
| 0-6 | Dead-man switch：watchdog 自己送出、`degraded` 用非 0 exit code、區分「長期已知缺口」和「新缺口」、`outbox pending/ack` 給 Grok 轉述用 | 監控停了沒人知道；Grok 轉述沒有確認回路 | S–M | 01 P0-5、P1-1、P1-6 |

### 第 1 階段：即時性與覆蓋率（不加付費依賴）

| # | 工作包 | 效果 | 量級 | 細節 |
|---|---|---|---|---|
| 1-0 | **營運面、零程式**：鏈上排程從 Grok 移到 launchd／cron，Grok 只負責產新聞檔；改全天跑 | 去掉夜間 9h 空窗與「Grok 額度用完整輪消失」 | 低 | 02 §5.1 方案 1 |
| 1-1 | `guardian balance-probe`：每 5 分鐘 Multicall3 讀全部餘額＋topic-OR getLogs，有變化才觸發 `run-once` | 延遲從平均 2.6h／P90 6.6h 降到 **P95 < 10 分鐘**；成本每天約 4,300 次 HTTP，幾乎不隨地址數增加 | M | 02 WP-4 |
| 1-2 | 免 key 日誌來源設定（OP 公開 RPC、Base Tenderly gateway、BSC NodeReal key／48club 備援）＋補 NodeReal `/v1/<key>` 的 log 遮罩 | Base／OP／BSC 的代幣轉帳與授權補齊 | S | 02 WP-3（設定部分） |
| 1-3 | OP internal 改走 Blockscout REST v2（status 2 當 partial） | OP 原生 internal 轉入可解釋 | M | 02 WP-5 |
| 1-4 | 新聞的時間與例外隔離 | 新聞慢或壞不再吃掉鏈上掃描 | S | 02 WP-2 |
| 1-5 | 首輪被擋、後來恢復時不要重播 30 天歷史 | 避免分享版新使用者被洗版 | S | 01 P1-3 |
| 1-6 | launchd plist 產生器（`StartCalendarInterval`，睡眠醒來會補跑一次） | 給 72 小時 shadow 用 | S | 02 WP-6 |
| 1-7 | 72 小時 shadow 加偵測延遲度量，以及「outbox 筆數 = chain_events 超門檻筆數」斷言 | shadow 能證明不漏報，不只是「有準時跑」 | S | 02 WP-7、01 P1-5 |

**免費做不到、要誠實標示的**：Base 的原生外部 tx 列表與 internal 轉入（Blockscout 被 Cloudflare 擋、Etherscan 要付費、Routescan 不支援）只能靠餘額差額，知道金額不知道原因；任何鏈的免費 trace 都沒有；公開端點都沒有服務保證。

### 第 2 階段：可分享性與文案（可和第 0 階段平行）

| # | 項目 | 細節 |
|---|---|---|
| 2-1 | README 加「狀態與限制」區塊（草稿已寫好，可直接貼） | 04 §4 |
| 2-2 | 修 6 個高嚴重度文件問題：publicnode 示範、`asset_usd` 不會觸發、無 key 後備只在範例不在 init、Helius 與 GoPlus 沒接、預設不會送出任何通知 | 04 H1–H6 |
| 2-3 | 覆蓋矩陣加「可告警」欄 | 01 P1-4 |
| 2-4 | 文件寫明最低 uv 版本，`bootstrap.sh` 檢查版本：`tools/build_release.py:61` 用 `uv build --clear --no-create-gitignore`，舊版 uv（例如 0.7.20）直接失敗 | 本次 pytest 實測 |
| 2-5 | `tools/release_manifest.py:49` 只要路徑裡有 `private`／`data`／`evidence` 任一段就拒絕；macOS TMPDIR 位於 `/private/...` 或路徑含 `/data/` 時會誤擋 | 本次 pytest 實測（低） |
| 2-6 | 移除或重新產生 0.3.0 時期的 `source_registry.json`、`environment_report.json`、`provider_benchmark.json`；更新 `coverage.md`、`deploy.md`、`env.example` | 01 P2-1、04 |

### 第 3 階段：Mode A（03 報告，8 包）

依賴順序：WP-1 部署收斂與產物清冊 → WP-2 路由契約、WP-3 送出程式隔離與產物負面測試；WP-4 授權補強可平行 → WP-5 客戶端自有證據 → WP-6 送出結果生命週期 → WP-7 獨立送出版本（預設不建置不部署）；WP-8 真錢包實測分 Phase A（只測登入，WP-1／2／4 後即可）與 B／C（WP-7 後＋你的書面授權）。

**WP-1 和 WP-3 建議現在就做**，不用等第 0 階段：改的是 action-client，和監控程式碼互不干擾；而且舊預覽部署和正式 bundle 裡的送出程式碼是今天就存在的暴露面。

Mode A 目前**沒有合法的開啟路徑**，只改旗標不可能開：
- Lista 部署核准清單是空的，客戶端重讀永遠過不了；
- 送出關卡的「模擬完成」取自伺服器給的值；
- 原地址取自伺服器 `plan.sender`，沒和本頁 SIWE 簽名地址比對；
- WalletConnect 只核准 `personal_sign`，S24「Chrome → 幣安 App」這條路送不了交易；
- 進行中狀態只在記憶體，手機切 App 回來可能遺失；拒簽被歸成 `failed` 而且同頁不能再試。

go/no-go 清單 G1–G15 見 03 §5。

## 需要你決定的事

| ID | 決定 | 影響 |
|---|---|---|
| 1 | Mac 夜間要不要保持清醒，或改用常開的 Linux 機器 | 夜間覆蓋 |
| 2 | 雲端例行要不要改全天跑，或在 0-2 完成後完全不讓 Grok 觸發鏈上掃描 | 1-0 |
| 3 | 要不要註冊免費 key：Blockscout PRO（OP、驗證 Base）、Alchemy（Base 轉帳） | 1-2、1-3 |
| 4 | 能不能把監控地址送到新的第三方端點（48club 是 MEV builder、Tenderly、dRPC） | 1-2 |
| 5 | BSC 的 logs 要不要用你的 NodeReal key（N=20 時每月約 12.7M CU，可能超過免費額度；定價頁和市集頁寫的額度互相矛盾） | 1-2 |
| 6 | 什麼時候從 `outbox_only` 切到 Telegram 直送 | 1-1 的效果才會到手機 |
| D-1 | 核准 Lista 實作 pin（同時確認 `claimWithdraw` 付款給 `msg.sender`） | Mode A WP-5 |
| D-2 | 客戶端 `eth_call` 試跑能不能算「模擬完成」（要修訂 ADR 0001） | Mode A WP-5 |
| D-3 | Mode A 第一版是否支援 WalletConnect 送出 | WP-7、WP-8 範圍 |
| H-7／H-8 | 兩份**分開的**書面授權：主網小額測試；把送出版本部署到正式網址 | Mode A Phase C |

## 測試實跑結果

`uv run pytest -q`：698 passed、3 failed、4 errors（63 秒）。7 個非通過我逐一看過，**都是環境因素，不是邏輯錯誤**：6 個是本機 uv 0.7.20 不支援 `uv build --clear`（見 2-4）；1 個是檢視用的 clone 位於 macOS 的 `/private/...` 底下，被 `release_manifest` 的路徑檢查擋掉（見 2-5）。這只是一個資料點，**不等於 CI 通過**。

## 詳細報告

- 01 架構與缺口優先排序：資料流圖、12 項結構性風險、P0–P2 表、缺的測試
- 02 即時性與覆蓋率：provider × 鏈查證表（附 URL 與日期）、curl 實測紀錄、延遲拆解、請求預算、8 個工作包
- 03 Mode A：阻擋項盤點、14 個威脅情境、8 個工作包（含驗收條件、負面測試、rollback）、go/no-go 清單
- 04 文案稽核：27 項（高 6／中 10／低 11）逐檔逐行，附改寫文字與 README 狀態區塊草稿
