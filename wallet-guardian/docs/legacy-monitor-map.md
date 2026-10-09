# 既有監控對照（MCM-W0-01）

這份對照只描述入口、狀態欄位、資料源與目前能力。私人數值只留在操作者機器。這裡沒有真實監控地址、key、秘密檔的路徑或通知收件人。

這次盤點沒有讀取 `private/`、`data/` 或 `evidence/`，也沒有重跑私人腳本或公開的 live probe。下面私人例行的事實來自審查者提供的運維盤點。`probe` 因此只有 `documented`、`untested` 或 `blocked`，沒有 `live_pass`。未知不是安全，也不是 0。

合成範例（不是任何真實監控地址）：`0x0000000000000000000000000000000000000001`。

既有每 3 小時例行繼續跑，直到 W9。這份對照不是交接。見 [mcm-01.md](mcm-01.md)。

## 公開核心

公開核心是這個 repo 裡的 Python 監控。它目前只會在 `run-once` 路徑上讀 BNB Chain，並另外呼叫 Hyperliquid 的公開 info。Hyperliquid 不在 MCM 的 24 個網路裡，不能拿來填補任何一條未查的鏈。

### 入口

下列 `模組:名稱` 都是程式裡的真實物件。W0 的測試會匯入它們。

- `guardian.cli:build_parser`
- `guardian.cli:dispatch`
- `guardian.cli:main`
- `guardian.service:run_once`
- `guardian.service:_run_once`
- `guardian.service:doctor`
- `guardian.service:provider_probes`
- `guardian.service:etherscan_chainlist_probe`
- `guardian.service:screen_ofac`
- `guardian.service:operation_coverage`
- `guardian.service:self_check`
- `guardian.service:_hyperliquid`
- `guardian.monitoring:SnapshotCycle`
- `guardian.monitoring:reconcile_scopes`
- `guardian.providers:RpcClient`
- `guardian.providers:read_lista_state`
- `guardian.providers:safe_get_text`
- `guardian.providers:safe_post_json`
- `guardian.security.rpc_guard:assert_agent_method`
- `guardian.security.ssrf:validate_url`
- `guardian.security.ssrf:next_redirect`
- `guardian.storage:Database`
- `guardian.domain:normalize_hyperliquid`

`guardian.cli:build_parser` 註冊的指令：`doctor`、`run-once`、`snapshot`、`reconcile`、`claims`、`providers`、`wallets`、`approvals`、`explain`、`coverage`、`replay`、`alerts`、`self-check`、`diagnose`、`actions`、`action`、`rescue`、`operation-coverage`、`workbench`、`config`、`sources`、`export`、`compare-legacy`、`legacy-capture`、`watchdog`、`monitor`、`egress-doctor`、`init`、`install-service`。`legacy-capture` 只寫同機 sidecar，不開 MCM 資料庫，也不改既有指令。見 `docs/legacy-evidence.md`。

`guardian init` 寫單一操作者的唯讀設定。它不掃描可推導地址、不開啟 GoPlus 或 Chainabuse 的地址查詢，也不啟動 sender。`guardian install-service` 只印出計畫中的 unit 名稱並以非 0 結束，不安裝服務。

和監控直接相關的行為：

- `guardian doctor` 走 `guardian.service:doctor`，再走 `guardian.service:provider_probes`。它報告環境與來源等級，不印出秘密值。`monitoring_active` 是 false。
- `guardian egress-doctor` 是唯讀的主機出口診斷，只印一份 JSON。它不開帳戶檔，也不送交易。三種事實、判定與禁止事項在 `docs/host-network.md`。
- `guardian run-once` 走 `guardian.service:run_once`，再走 `guardian.service:_run_once`。在任何網路讀取之前先建立 `guardian.monitoring:SnapshotCycle`。
- `guardian coverage` 在 `guardian.cli:dispatch` 裡回傳固定的 `chains_enabled: [56]`，並附上 `guardian.service:operation_coverage`。這不是 24 條鏈的覆蓋矩陣。
- `guardian replay` 必須帶 `--no-send`。若 fixture 的 method 是禁止的發送方法，`dispatch` 會交給既有的 action policy 拒絕。W0 沒有改這條路徑。
- `guardian.providers:RpcClient.call` 在送出 HTTP 之前呼叫 `guardian.security.rpc_guard:assert_agent_method`。
- `guardian.providers:safe_get_text` 與 `guardian.providers:safe_post_json` 在連線前呼叫 `guardian.security.ssrf:validate_url`。重新導向走 `guardian.security.ssrf:next_redirect`。
- `guardian.service:_fresh_lista_plan` 會做未簽名的 Lista 計畫。這是既有的 service 接線，會用到 `guardian.actions`。新的 `guardian.network_registry:load_registry` 不匯入 `guardian.actions`，也不匯入 Action Client。

`approvals` 目前直接回傳空清單與 PARTIAL，原因是 allowance 歷史還沒有索引。那不是「沒有授權」。

### SQLite 狀態

`guardian.storage:Database` 建立下面這些表。新庫的 `PRAGMA user_version=1`。版本比 1 新就拒絕開啟，不改寫。`guardian.service:self_check` 的 `EXPECTED_TABLES` 是子集，少了 `alert_dedup` 與 `monitor_health`；以 `Database.table_names()` 的完整清單為準。

- `tenants`
- `wallets`
- `chain_capabilities`
- `adapter_versions`
- `observations`
- `snapshots`
- `snapshot_items`
- `position_legs`
- `exposure_edges`
- `events`
- `cursors`
- `pending_actions`
- `allowances`
- `incidents`
- `rules`
- `alerts`
- `outbox`
- `expected_actions`
- `deliveries`
- `runs`
- `source_registry`
- `tasks`
- `alert_dedup`
- `monitor_health`
- `mcm_schema_migrations`
- `wallet_identities`
- `wallet_label_aliases`
- `legacy_imports`
- `legacy_alert_suppressions`

W1 另外建立上面五張表。`PRAGMA user_version` 仍是 1，所以舊程式還能開這個庫。遷移檔在 `migrations/monitor/`，套用記錄在 `mcm_schema_migrations`（含 sha256）。重開不會再套用，已套用的檔案若 sha256 變了就拒絕。`run-once` 不寫這些表。`wallet_identities` 以 `wallet_id` 為身份；標籤改名會把舊標籤留在 `wallet_label_aliases`，不改 `wallet_id`。

W3 建立 `chain_events`、`capability_state` 與 `capability_cursors`。`PRAGMA user_version` 仍是 1。`run-once` 不寫這三張表。事件、能力狀態、游標與 outbox 在同一個交易裡提交。失敗的讀取不會把舊數量改成 0。

W9 建立 `delivery_leases` 與 `delivery_attempts`。`PRAGMA user_version` 仍是 1。舊的 `run-once` 不寫這兩張表。

目前 `run-once` 路徑實際會寫入的表：

- `tenants`：`tenant_id`、`label`。`guardian.storage:Database.upsert_tenant`。
- `wallets`：`wallet_id`、`tenant_id`、`label`、`address`、`ownership_basis`。`upsert_wallet`。標籤不能改綁到另一個地址。
- `snapshots`：`generation`、`tenant_id`、`created_at`。世代必須是該租戶目前最大世代加 1。
- `snapshot_items`：`generation`、`tenant_id`、`position_id`、`payload`。
- `alerts`：`tenant_id`、`dedup_key`、`payload`。
- `alert_dedup`：`tenant_id`、`dedup_key`。已存在的鍵不會再寫第二筆 outbox。
- `outbox`：`tenant_id`、`status`、`payload`。新列的 status 是 `pending`。這個模組不投遞。
- `monitor_health`：`tenant_id`、`payload`。內容有 `blind`、`episode`、`failed_scopes`、`checked_at`。
- `tasks`：`task_id`、`tenant_id`、`revision`、`state`、`payload`。未簽名 Lista 計畫寫在這裡。
- `runs`：`run_id`、`tenant_id`、`payload`。`save_run`。

schema 有建立、但 `run-once` 沒有 INSERT 的表：`chain_capabilities`、`adapter_versions`、`observations`、`position_legs`、`exposure_edges`、`events`、`pending_actions`、`allowances`、`incidents`、`rules`、`expected_actions`、`deliveries`，以及 SQLite 裡的 `source_registry`。`cursors` 的欄位是 `scope`、`tenant_id`、`value`。`SnapshotCycle` 仍用快照世代，不寫這張表。表仍要留著，不能因為目前沒寫入就丟掉。W1 的 `guardian.legacy_state:import_legacy_state` 才會把舊游標寫進 `cursors`。缺的欄位不補 0，也不從 `runs` 製造快照。

`guardian.service:_run_once` 寫進 `snapshot_items.payload` 的鍵：`position_id`、`chain_id`、`raw_amount`、`status`、`symbol`、`price`、`wallet_label`、`wallet_address`、`quality`、`fresh`、`observed_at`、`block_number`、`block_hash`、`block_timestamp`、`finality`、`paused`。Lista 請求另外有 `severity`。`price` 是 null，不是 0。

`guardian.monitoring:reconcile_scopes` 在範圍失敗時把舊列留成 `quality=STALE`、`fresh=false`、`retained_last_known_good=true`，並寫 `stale_reason`。Lista 請求從清單消失時，狀態是 `MISSING_NO_CLAIM_EVIDENCE`，保留 `last_known_raw_amount`，不把消失當成已領取。

`guardian.monitoring:SnapshotCycle.finish` 產生的告警 payload 含：`kind`、`severity`、`severity_label`、`confidence`、`wallet_label`、`chain_label`、`subject`、`what`、`amount_display`、`unit`、`price`、`price_time`、`why`、`step`、`event_time`、`observed_at`、`source`、`limits`、`dedup_key`。健康告警則是較短的 `kind`、`severity`、`failed_scopes`、`observed_at`、`what`、`dedup_key`。

### 資料源

執行時實際用到的來源，以程式常數為準。`config/chains.json` 與 doctor 寫出的 `source_registry.json` 是宣告和上一次 doctor 的輸出，不是這次 W0 的新探測。

- BNB Chain 官方 RPC：`guardian.service` 的 `BSC_RPC`，主機是 `bsc-dataseed.bnbchain.org`。`config/chains.json` 的 `enabled` 只有鏈 56，並寫明官方 dataseed 停用 `eth_getLogs`。`not_enabled` 含鏈 1，原因是這個建置沒有預先核准的 Ethereum RPC。`run-once` 沒有讀 `config/chains.json`，URL 是程式常數。
- Lista：`guardian.providers:read_lista_state`。合約常數是 `LISTA_PROXY` 與 `SLISBNB`，另外對應 `config/protocols/lista-bsc.json`。這裡不貼地址。讀取包含 `eth_chainId`、`eth_blockNumber`、`eth_getBlockByNumber`、`eth_getStorageAt`、`eth_getCode`、`eth_call`、`eth_getBalance`。鏈 ID 不是 56 就失敗。`finality` 寫成 `provisional`。
- Hyperliquid 公開 info：`guardian.service:_hyperliquid` 對 `api.hyperliquid.xyz` 的 `clearinghouseState`、`spotClearinghouseState`、`subAccounts`。`guardian.domain:normalize_hyperliquid` 正規化。品質是 PARTIAL。空的 master 不是所有子帳戶為零。這不是 24 鏈登錄的一員。
- Etherscan V2 chainlist：`guardian.service:etherscan_chainlist_probe`。沒有 `ETHERSCAN_API_KEY` 就是 `LIMITED_DISCOVERY`，不會發出請求。有 key 才查 chainlist。這不是帳戶歷史掃描。公開文件不寫查詢參數的賦值形式。
- OFAC SDN CSV：`guardian.service:screen_ofac` 與 `OFAC_URL`（treasury.gov 的 sdn.csv）。程式把結果標成 `LIMITED_DISCOVERY` 或 `PARTIAL`，不是完整數位貨幣制裁清單。
- GoPlus、DeBank、Zerion、Alchemy、Tenderly：只在 `provider_probes` 裡標 `LIMITED_DISCOVERY` 或 `BLOCKED`。GoPlus 即使有 key 也不自動呼叫。沒有對應的餘額 adapter。
- 新聞：`provider_probes` 的 `news_rss` 是過濾骨架，不把錢包送去搜尋。

`guardian.providers:read_lista_state` 成功時，`_run_once` 為每個錢包寫三種 `position_id`：`{label}:bnb:native`、`{label}:bsc:slisbnb`、`{label}:lista:{uuid}`。範圍成功才把 `(wallet label, 56)` 放進 `successful_scopes`。失敗則舊的快照列留成 STALE，餘額欄位不是 0。

### 公開核心目前能力

下表每一列是一個 `(network, capability)`。沒有列出來的 Hyperliquid 見上一節，不佔這 24 個網路的任何一格。

和「未實作」預設不同的公開列：

- `bsc-mainnet` `balances.native`：support=implemented，probe=documented，quality=unknown，finality=observed。service._run_once 與 providers.read_lista_state 讀 eth_getBalance。程式欄位 finality 是 provisional，本表記 observed，不是 finalized。W0 沒有重跑，所以不是 live_pass。價格是 null，不是 0。
- `bsc-mainnet` `balances.fungible`：support=partial，probe=documented，quality=partial，finality=observed。只讀 slisBNB 的 balanceOf，不是已發現的 ERC-20 全集。
- `bsc-mainnet` `balances.nft`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有 NFT 餘額讀取。這不是零。
- `bsc-mainnet` `history.native_external`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 txlist。config/chains.json 寫明官方 dataseed 停用 eth_getLogs。缺口不是空歷史。
- `bsc-mainnet` `history.native_internal`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有內部轉帳追蹤。這不是零。
- `bsc-mainnet` `history.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 tokentx。官方 dataseed 不能用 eth_getLogs 回補。這不是空歷史。
- `bsc-mainnet` `history.nft`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有 NFT 歷史。這不是零。
- `bsc-mainnet` `permissions.erc20`：support=unsupported，probe=documented，quality=unknown，finality=unknown。cli 的 approvals 回傳空的 PARTIAL。allowances 表沒有寫入。這不是「沒有授權」。
- `bsc-mainnet` `permissions.erc721_token`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有單一代幣授權讀取。這不是安全。
- `bsc-mainnet` `permissions.erc721_operator`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有 setApprovalForAll 讀取。這不是安全。
- `bsc-mainnet` `permissions.erc1155_operator`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有 ERC-1155 operator 讀取。這不是安全。
- `bsc-mainnet` `permits.erc2612_observed`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有掃已上鏈的 ERC-2612。這不是沒有許可。
- `bsc-mainnet` `permits.dai_variant`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有辨識 DAI 變體。這不是沒有許可。
- `bsc-mainnet` `permit2.allowance`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有掃 Permit2 allowance。這不是沒有授權。
- `bsc-mainnet` `permit2.signature_usage`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有掃 Permit2 已觀測使用。這不是沒有簽名。
- `bsc-mainnet` `positions`：support=partial，probe=documented，quality=partial，finality=observed。只有手寫 Lista：slisBNB 餘額與提款請求。不是通用協議掃描。
- `bsc-mainnet` `withdrawals`：support=partial，probe=documented，quality=partial，finality=observed。只有 Lista 提款請求。清單裡消失是 MISSING_NO_CLAIM_EVIDENCE，不是已入帳。
- `bsc-mainnet` `bridges`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有跨鏈進度讀取。這不是已完成。
- `bsc-mainnet` `asset_risk`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有代幣風險 feed。這不是安全。
- `bsc-mainnet` `address_risk`：support=unsupported，probe=blocked，quality=unknown，finality=unknown。service.provider_probes 裡 GoPlus 沒有 key 是 LIMITED_DISCOVERY；有 key 也不自動呼叫，標 BLOCKED。這不是安全。
- `bsc-mainnet` `sanctions`：support=partial，probe=documented，quality=partial，finality=unknown。service.screen_ofac 下載 SDN CSV，程式自己標 LIMITED_DISCOVERY 或 PARTIAL，不是完整數位貨幣制裁清單。W0 沒有重跑。
- `bsc-mainnet` `exposure_news`：support=unsupported，probe=untested，quality=unknown，finality=unknown。news_rss 只是過濾骨架，不把錢包送去搜尋。domain.classify_news 是純函式，不是新聞 feed。

預設列是 support=`unsupported`、probe=`untested`、quality=`unknown`、finality=`unknown`，註記是未查不是空部位。鏈族不適用的權限是 `not_applicable`。`permits.offchain_unused` 全部是 `not_observable`。

| network | capability | support | probe | quality | finality | note |
|---|---|---|---|---|---|---|
| ethereum-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ethereum-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| bsc-mainnet | balances.native | implemented | documented | unknown | observed | service._run_once 與 providers.read_lista_state 讀 eth_getBalance。程式欄位 finality 是 provisional，本表記 observed，不是 finalized。W0 沒有重跑，所以不是 live_pass。價格是 null，不是 0。 |
| bsc-mainnet | balances.fungible | partial | documented | partial | observed | 只讀 slisBNB 的 balanceOf，不是已發現的 ERC-20 全集。 |
| bsc-mainnet | balances.nft | unsupported | untested | unknown | unknown | 沒有 NFT 餘額讀取。這不是零。 |
| bsc-mainnet | history.native_external | unsupported | documented | unknown | unknown | 沒有 txlist。config/chains.json 寫明官方 dataseed 停用 eth_getLogs。缺口不是空歷史。 |
| bsc-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 沒有內部轉帳追蹤。這不是零。 |
| bsc-mainnet | history.fungible | unsupported | documented | unknown | unknown | 沒有 tokentx。官方 dataseed 不能用 eth_getLogs 回補。這不是空歷史。 |
| bsc-mainnet | history.nft | unsupported | untested | unknown | unknown | 沒有 NFT 歷史。這不是零。 |
| bsc-mainnet | permissions.erc20 | unsupported | documented | unknown | unknown | cli 的 approvals 回傳空的 PARTIAL。allowances 表沒有寫入。這不是「沒有授權」。 |
| bsc-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 沒有單一代幣授權讀取。這不是安全。 |
| bsc-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 沒有 setApprovalForAll 讀取。這不是安全。 |
| bsc-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 沒有 ERC-1155 operator 讀取。這不是安全。 |
| bsc-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 沒有掃已上鏈的 ERC-2612。這不是沒有許可。 |
| bsc-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 沒有辨識 DAI 變體。這不是沒有許可。 |
| bsc-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 沒有掃 Permit2 allowance。這不是沒有授權。 |
| bsc-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 沒有掃 Permit2 已觀測使用。這不是沒有簽名。 |
| bsc-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | positions | partial | documented | partial | observed | 只有手寫 Lista：slisBNB 餘額與提款請求。不是通用協議掃描。 |
| bsc-mainnet | withdrawals | partial | documented | partial | observed | 只有 Lista 提款請求。清單裡消失是 MISSING_NO_CLAIM_EVIDENCE，不是已入帳。 |
| bsc-mainnet | bridges | unsupported | untested | unknown | unknown | 沒有跨鏈進度讀取。這不是已完成。 |
| bsc-mainnet | asset_risk | unsupported | untested | unknown | unknown | 沒有代幣風險 feed。這不是安全。 |
| bsc-mainnet | address_risk | unsupported | blocked | unknown | unknown | service.provider_probes 裡 GoPlus 沒有 key 是 LIMITED_DISCOVERY；有 key 也不自動呼叫，標 BLOCKED。這不是安全。 |
| bsc-mainnet | sanctions | partial | documented | partial | unknown | service.screen_ofac 下載 SDN CSV，程式自己標 LIMITED_DISCOVERY 或 PARTIAL，不是完整數位貨幣制裁清單。W0 沒有重跑。 |
| bsc-mainnet | exposure_news | unsupported | untested | unknown | unknown | news_rss 只是過濾骨架，不把錢包送去搜尋。domain.classify_news 是純函式，不是新聞 feed。 |
| bsc-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| arbitrum-one | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| arbitrum-one | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| base-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| base-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| optimism-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| optimism-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| polygon-pos | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| polygon-pos | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| avalanche-c | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| solana-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.spl_delegate | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permissions.spl_authority | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| linea-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| zksync-era | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| scroll-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| mantle-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| sonic-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| unichain-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| hyperevm-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| bitcoin-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | balances.fungible | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | balances.nft | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | history.fungible | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | history.nft | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| tron-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.tron_account | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| blast-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| opbnb-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| ton-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.ton_extensions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| sui-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.move_capability | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| aptos-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.move_capability | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| cosmos-hub | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.cosmos_authz | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | permissions.cosmos_feegrant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| osmosis-mainnet | balances.native | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | balances.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | history.native_external | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | history.fungible | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | history.nft | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.cosmos_authz | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | permissions.cosmos_feegrant | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | positions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | withdrawals | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | bridges | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | asset_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | address_risk | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | sanctions | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | exposure_news | unsupported | untested | unknown | unknown | 公開核心的 run-once 不讀這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |

## 私人運維例行

私人內容只留在操作者機器。本包沒有讀那些檔，也沒有把路徑寫進來。

- 每 3 小時跑一次，掃描 2 個私人 EVM 地址。
- 入口是 gitignore 的 `private/` 目錄裡的幾個小型 Python 腳本：一支完整的 Etherscan V2 掃描、一支從上次以來的差分、一支 BSC NodeReal 轉帳、一支很小的 JSON-RPC 輔助。key 從 repo 外面的 gitignore env 檔讀取。
- Ethereum、Arbitrum、Polygon 走 Etherscan V2（免費 key）：`txlist`、`txlistinternal`、`tokentx`、`tokennfttx`、`token1155tx`、`balance`，以及 `getLogs` 的 Approval（topic0）與 ApprovalForAll，topic1 是 owner。
- BSC 走 NodeReal 的 `nr_getAssetTransfers`，區塊範圍是 fromBlock 到 toBlock 的十六進位。toBlock 必須落後鏈頭約 40 個區塊。`latest` 被拒絕。desc 順序不是時間序。maxCount 是 `0x64`，只有第一頁，所以分頁不是完成。
- Base 與 OP 走公開 RPC（publicnode）：只有 nonce 與原生餘額。
- 風險是 GoPlus 的地址風險。新聞只人工檢查 Kinza、Lista、UXLINK。

私人 JSON 狀態的欄位名稱：

- `_note`
- `last_run_taipei`
- `sources`：依鏈群組寫一段說明
- `nonces`：地址底下的 `bsc`、`base`、`op`
- `latest_seen`：鍵的形狀是錢包標籤、鏈、種類，值是日期字串
- `alerted`：去重字串清單，形狀像「鏈、錢包標籤、交易雜湊、說明」
- `ignored_spam`
- `news_checked`
- `runs`：清單元素含 `at`、`result`、`nonces_unchanged`、`bsc_in_since_<hhmm>`、`eth_arb_polygon_since_<hhmm>`、`goplus`、`gaps`、`news`
- `bsc_last_block`：整數

誠實的缺口：BSC 沒有 permit 或 Permit2 掃描。Base 與 OP 沒有代幣歷史、沒有內部轉帳、沒有授權、沒有 permit。沒有非 EVM 鏈。Permit2 在任何鏈都沒有掃。NodeReal 只有第一頁。部位只有手寫的 Lista。

### 私人例行目前能力

未出現在例行裡的鏈，每一個適用能力都是 `unsupported` / `untested` / `unknown` / `unknown`。沒有查，不代表沒有部位。

和這個預設不同的私人列：

- `ethereum-mainnet` `balances.native`：support=partial，probe=documented，quality=unknown，finality=unknown。Etherscan V2 的 balance。這份公開文件沒有餘額數字。未知不是 0。W0 沒有重跑，probe 不是 live_pass。
- `ethereum-mainnet` `balances.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。tokentx 沒有被收成當前代幣餘額全集。沒有這份餘額不是零。
- `ethereum-mainnet` `balances.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。tokennfttx 與 token1155tx 是歷史，不是當前 NFT 餘額。
- `ethereum-mainnet` `history.native_external`：support=partial，probe=documented，quality=partial，finality=unknown。txlist。分頁是否到底沒有在這份對照被證明，不能標 complete。
- `ethereum-mainnet` `history.native_internal`：support=partial，probe=documented，quality=partial，finality=unknown。txlistinternal。分頁是否到底沒有被證明，不能標 complete。
- `ethereum-mainnet` `history.fungible`：support=partial，probe=documented，quality=partial，finality=unknown。tokentx。分頁是否到底沒有被證明，不能標 complete。
- `ethereum-mainnet` `history.nft`：support=partial，probe=documented，quality=partial，finality=unknown。tokennfttx 與 token1155tx。分頁是否到底沒有被證明，不能標 complete。
- `ethereum-mainnet` `permissions.erc20`：support=partial，probe=documented，quality=partial，finality=unknown。getLogs 的 Approval，topic0 是事件，topic1 是 owner。沒有讀當前 allowance。ERC-20 與 ERC-721 沒有分開。
- `ethereum-mainnet` `permissions.erc721_token`：support=partial，probe=documented，quality=unknown，finality=unknown。同一批 Approval 日誌，沒有把單代幣授權和 ERC-20 分開。
- `ethereum-mainnet` `permissions.erc721_operator`：support=partial，probe=documented，quality=partial，finality=unknown。ApprovalForAll。沒有和 ERC-1155 operator 分開，也沒有讀當前 operator 狀態。
- `ethereum-mainnet` `permissions.erc1155_operator`：support=partial，probe=documented，quality=unknown，finality=unknown。ApprovalForAll 與 721 operator 共用這次盤點描述，沒有分開。
- `ethereum-mainnet` `permits.erc2612_observed`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `ethereum-mainnet` `permits.dai_variant`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `ethereum-mainnet` `permit2.allowance`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `ethereum-mainnet` `permit2.signature_usage`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `ethereum-mainnet` `positions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。這條鏈的私人腳本沒有協議部位。這不是沒有部位。
- `ethereum-mainnet` `withdrawals`：support=unsupported，probe=untested，quality=unknown，finality=unknown。這條鏈的私人腳本沒有提款清單。這不是沒有在途資金。
- `ethereum-mainnet` `bridges`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有跨鏈進度。這不是已完成。
- `ethereum-mainnet` `asset_risk`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有代幣風險 feed。地址風險見 address_risk，兩者不是同一件事。
- `ethereum-mainnet` `address_risk`：support=partial，probe=documented，quality=unknown，finality=unknown。例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。
- `ethereum-mainnet` `sanctions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。私人例行沒有制裁清單比對。這不是未上榜。
- `ethereum-mainnet` `exposure_news`：support=partial，probe=documented，quality=partial，finality=unknown。只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。
- `bsc-mainnet` `balances.native`：support=unsupported，probe=documented，quality=unknown，finality=unknown。盤點裡的 BSC 來源是 NodeReal 轉帳，不是原生餘額。nonces 裡的 bsc 是 nonce，不是餘額。未知不是 0。
- `bsc-mainnet` `balances.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有當前代幣餘額全集。這不是零。
- `bsc-mainnet` `balances.nft`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有當前 NFT 餘額。這不是零。
- `bsc-mainnet` `history.native_external`：support=partial，probe=documented，quality=partial，finality=unknown。nr_getAssetTransfers。maxCount 是 0x64，只有第一頁，分頁不是完成。toBlock 約落後鏈頭 40 個區塊。latest 被拒絕。desc 不是時間序。
- `bsc-mainnet` `history.native_internal`：support=unsupported，probe=documented，quality=unknown，finality=unknown。這次盤點沒有說 NodeReal 腳本包含內部追蹤。不能寫成已覆蓋。
- `bsc-mainnet` `history.fungible`：support=partial，probe=documented，quality=partial，finality=unknown。同一支 nr_getAssetTransfers，只有第一頁，分頁不是完成。不能當成完整代幣歷史。
- `bsc-mainnet` `history.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。盤點沒有證明 NFT 歷史已翻完。這不是零。
- `bsc-mainnet` `permissions.erc20`：support=unsupported，probe=documented，quality=unknown，finality=unknown。BSC 私人腳本沒有 approval 掃描。這不是沒有授權。
- `bsc-mainnet` `permissions.erc721_token`：support=unsupported，probe=documented，quality=unknown，finality=unknown。BSC 沒有單代幣授權掃描。這不是安全。
- `bsc-mainnet` `permissions.erc721_operator`：support=unsupported，probe=documented，quality=unknown，finality=unknown。BSC 沒有 operator 掃描。這不是安全。
- `bsc-mainnet` `permissions.erc1155_operator`：support=unsupported，probe=documented，quality=unknown，finality=unknown。BSC 沒有 ERC-1155 operator 掃描。這不是安全。
- `bsc-mainnet` `permits.erc2612_observed`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `bsc-mainnet` `permits.dai_variant`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `bsc-mainnet` `permit2.allowance`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `bsc-mainnet` `permit2.signature_usage`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `bsc-mainnet` `positions`：support=partial，probe=documented，quality=partial，finality=observed。只有手寫 Lista。公開核心 providers.read_lista_state 是這條讀取的程式，不是通用部位掃描。
- `bsc-mainnet` `withdrawals`：support=partial，probe=documented，quality=partial，finality=observed。只有 Lista 提款請求。沒有領取收據不能當成已領取。
- `bsc-mainnet` `bridges`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有跨鏈進度。這不是已完成。
- `bsc-mainnet` `asset_risk`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有代幣風險 feed。這不是安全。
- `bsc-mainnet` `address_risk`：support=partial，probe=documented，quality=unknown，finality=unknown。例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。
- `bsc-mainnet` `sanctions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。私人例行沒有制裁清單比對。這不是未上榜。
- `bsc-mainnet` `exposure_news`：support=partial，probe=documented，quality=partial，finality=unknown。只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。
- `arbitrum-one` `balances.native`：support=partial，probe=documented，quality=unknown，finality=unknown。Etherscan V2 的 balance。這份公開文件沒有餘額數字。未知不是 0。W0 沒有重跑，probe 不是 live_pass。
- `arbitrum-one` `balances.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。tokentx 沒有被收成當前代幣餘額全集。沒有這份餘額不是零。
- `arbitrum-one` `balances.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。tokennfttx 與 token1155tx 是歷史，不是當前 NFT 餘額。
- `arbitrum-one` `history.native_external`：support=partial，probe=documented，quality=partial，finality=unknown。txlist。分頁是否到底沒有在這份對照被證明，不能標 complete。
- `arbitrum-one` `history.native_internal`：support=partial，probe=documented，quality=partial，finality=unknown。txlistinternal。分頁是否到底沒有被證明，不能標 complete。
- `arbitrum-one` `history.fungible`：support=partial，probe=documented，quality=partial，finality=unknown。tokentx。分頁是否到底沒有被證明，不能標 complete。
- `arbitrum-one` `history.nft`：support=partial，probe=documented，quality=partial，finality=unknown。tokennfttx 與 token1155tx。分頁是否到底沒有被證明，不能標 complete。
- `arbitrum-one` `permissions.erc20`：support=partial，probe=documented，quality=partial，finality=unknown。getLogs 的 Approval，topic0 是事件，topic1 是 owner。沒有讀當前 allowance。ERC-20 與 ERC-721 沒有分開。
- `arbitrum-one` `permissions.erc721_token`：support=partial，probe=documented，quality=unknown，finality=unknown。同一批 Approval 日誌，沒有把單代幣授權和 ERC-20 分開。
- `arbitrum-one` `permissions.erc721_operator`：support=partial，probe=documented，quality=partial，finality=unknown。ApprovalForAll。沒有和 ERC-1155 operator 分開，也沒有讀當前 operator 狀態。
- `arbitrum-one` `permissions.erc1155_operator`：support=partial，probe=documented，quality=unknown，finality=unknown。ApprovalForAll 與 721 operator 共用這次盤點描述，沒有分開。
- `arbitrum-one` `permits.erc2612_observed`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `arbitrum-one` `permits.dai_variant`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `arbitrum-one` `permit2.allowance`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `arbitrum-one` `permit2.signature_usage`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `arbitrum-one` `positions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。這條鏈的私人腳本沒有協議部位。這不是沒有部位。
- `arbitrum-one` `withdrawals`：support=unsupported，probe=untested，quality=unknown，finality=unknown。這條鏈的私人腳本沒有提款清單。這不是沒有在途資金。
- `arbitrum-one` `bridges`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有跨鏈進度。這不是已完成。
- `arbitrum-one` `asset_risk`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有代幣風險 feed。地址風險見 address_risk，兩者不是同一件事。
- `arbitrum-one` `address_risk`：support=partial，probe=documented，quality=unknown，finality=unknown。例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。
- `arbitrum-one` `sanctions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。私人例行沒有制裁清單比對。這不是未上榜。
- `arbitrum-one` `exposure_news`：support=partial，probe=documented，quality=partial，finality=unknown。只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。
- `base-mainnet` `balances.native`：support=partial，probe=documented，quality=unknown，finality=unknown。公開 RPC（publicnode）只讀 nonce 與原生餘額。沒有代幣、內部轉帳、授權或 permit。餘額數字不在這份公開文件。未知不是 0。latest 不是最終性證明。
- `base-mainnet` `balances.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有代幣餘額。這不是零。
- `base-mainnet` `balances.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 NFT 餘額。這不是零。
- `base-mainnet` `history.native_external`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有外部轉帳歷史。這不是空歷史。
- `base-mainnet` `history.native_internal`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有內部轉帳。這不是空歷史。
- `base-mainnet` `history.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有代幣歷史。這不是空歷史。
- `base-mainnet` `history.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 NFT 歷史。這不是空歷史。
- `base-mainnet` `permissions.erc20`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有授權掃描。這不是沒有授權。
- `base-mainnet` `permissions.erc721_token`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有單代幣授權掃描。這不是安全。
- `base-mainnet` `permissions.erc721_operator`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 operator 掃描。這不是安全。
- `base-mainnet` `permissions.erc1155_operator`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 ERC-1155 operator 掃描。這不是安全。
- `base-mainnet` `permits.erc2612_observed`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `base-mainnet` `permits.dai_variant`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `base-mainnet` `permit2.allowance`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `base-mainnet` `permit2.signature_usage`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `base-mainnet` `positions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有協議部位。這不是沒有部位。
- `base-mainnet` `withdrawals`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有提款清單。這不是沒有在途資金。
- `base-mainnet` `bridges`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有跨鏈進度。這不是已完成。
- `base-mainnet` `asset_risk`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有代幣風險 feed。這不是安全。
- `base-mainnet` `address_risk`：support=partial，probe=documented，quality=unknown，finality=unknown。例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。
- `base-mainnet` `sanctions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。私人例行沒有制裁清單比對。這不是未上榜。
- `base-mainnet` `exposure_news`：support=partial，probe=documented，quality=partial，finality=unknown。只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。
- `optimism-mainnet` `balances.native`：support=partial，probe=documented，quality=unknown，finality=unknown。公開 RPC（publicnode）只讀 nonce 與原生餘額。沒有代幣、內部轉帳、授權或 permit。餘額數字不在這份公開文件。未知不是 0。latest 不是最終性證明。
- `optimism-mainnet` `balances.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有代幣餘額。這不是零。
- `optimism-mainnet` `balances.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 NFT 餘額。這不是零。
- `optimism-mainnet` `history.native_external`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有外部轉帳歷史。這不是空歷史。
- `optimism-mainnet` `history.native_internal`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有內部轉帳。這不是空歷史。
- `optimism-mainnet` `history.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有代幣歷史。這不是空歷史。
- `optimism-mainnet` `history.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 NFT 歷史。這不是空歷史。
- `optimism-mainnet` `permissions.erc20`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有授權掃描。這不是沒有授權。
- `optimism-mainnet` `permissions.erc721_token`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有單代幣授權掃描。這不是安全。
- `optimism-mainnet` `permissions.erc721_operator`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 operator 掃描。這不是安全。
- `optimism-mainnet` `permissions.erc1155_operator`：support=unsupported，probe=documented，quality=unknown，finality=unknown。沒有 ERC-1155 operator 掃描。這不是安全。
- `optimism-mainnet` `permits.erc2612_observed`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `optimism-mainnet` `permits.dai_variant`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `optimism-mainnet` `permit2.allowance`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `optimism-mainnet` `permit2.signature_usage`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `optimism-mainnet` `positions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有協議部位。這不是沒有部位。
- `optimism-mainnet` `withdrawals`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有提款清單。這不是沒有在途資金。
- `optimism-mainnet` `bridges`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有跨鏈進度。這不是已完成。
- `optimism-mainnet` `asset_risk`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有代幣風險 feed。這不是安全。
- `optimism-mainnet` `address_risk`：support=partial，probe=documented，quality=unknown，finality=unknown。例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。
- `optimism-mainnet` `sanctions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。私人例行沒有制裁清單比對。這不是未上榜。
- `optimism-mainnet` `exposure_news`：support=partial，probe=documented，quality=partial，finality=unknown。只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。
- `polygon-pos` `balances.native`：support=partial，probe=documented，quality=unknown，finality=unknown。Etherscan V2 的 balance。這份公開文件沒有餘額數字。未知不是 0。W0 沒有重跑，probe 不是 live_pass。
- `polygon-pos` `balances.fungible`：support=unsupported，probe=documented，quality=unknown，finality=unknown。tokentx 沒有被收成當前代幣餘額全集。沒有這份餘額不是零。
- `polygon-pos` `balances.nft`：support=unsupported，probe=documented，quality=unknown，finality=unknown。tokennfttx 與 token1155tx 是歷史，不是當前 NFT 餘額。
- `polygon-pos` `history.native_external`：support=partial，probe=documented，quality=partial，finality=unknown。txlist。分頁是否到底沒有在這份對照被證明，不能標 complete。
- `polygon-pos` `history.native_internal`：support=partial，probe=documented，quality=partial，finality=unknown。txlistinternal。分頁是否到底沒有被證明，不能標 complete。
- `polygon-pos` `history.fungible`：support=partial，probe=documented，quality=partial，finality=unknown。tokentx。分頁是否到底沒有被證明，不能標 complete。
- `polygon-pos` `history.nft`：support=partial，probe=documented，quality=partial，finality=unknown。tokennfttx 與 token1155tx。分頁是否到底沒有被證明，不能標 complete。
- `polygon-pos` `permissions.erc20`：support=partial，probe=documented，quality=partial，finality=unknown。getLogs 的 Approval，topic0 是事件，topic1 是 owner。沒有讀當前 allowance。ERC-20 與 ERC-721 沒有分開。
- `polygon-pos` `permissions.erc721_token`：support=partial，probe=documented，quality=unknown，finality=unknown。同一批 Approval 日誌，沒有把單代幣授權和 ERC-20 分開。
- `polygon-pos` `permissions.erc721_operator`：support=partial，probe=documented，quality=partial，finality=unknown。ApprovalForAll。沒有和 ERC-1155 operator 分開，也沒有讀當前 operator 狀態。
- `polygon-pos` `permissions.erc1155_operator`：support=partial，probe=documented，quality=unknown，finality=unknown。ApprovalForAll 與 721 operator 共用這次盤點描述，沒有分開。
- `polygon-pos` `permits.erc2612_observed`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `polygon-pos` `permits.dai_variant`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `polygon-pos` `permit2.allowance`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `polygon-pos` `permit2.signature_usage`：support=unsupported，probe=documented，quality=unknown，finality=unknown。私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。
- `polygon-pos` `positions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。這條鏈的私人腳本沒有協議部位。這不是沒有部位。
- `polygon-pos` `withdrawals`：support=unsupported，probe=untested，quality=unknown，finality=unknown。這條鏈的私人腳本沒有提款清單。這不是沒有在途資金。
- `polygon-pos` `bridges`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有跨鏈進度。這不是已完成。
- `polygon-pos` `asset_risk`：support=unsupported，probe=untested，quality=unknown，finality=unknown。沒有代幣風險 feed。地址風險見 address_risk，兩者不是同一件事。
- `polygon-pos` `address_risk`：support=partial，probe=documented，quality=unknown，finality=unknown。例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。
- `polygon-pos` `sanctions`：support=unsupported，probe=untested，quality=unknown，finality=unknown。私人例行沒有制裁清單比對。這不是未上榜。
- `polygon-pos` `exposure_news`：support=partial，probe=documented，quality=partial，finality=unknown。只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。

| network | capability | support | probe | quality | finality | note |
|---|---|---|---|---|---|---|
| ethereum-mainnet | balances.native | partial | documented | unknown | unknown | Etherscan V2 的 balance。這份公開文件沒有餘額數字。未知不是 0。W0 沒有重跑，probe 不是 live_pass。 |
| ethereum-mainnet | balances.fungible | unsupported | documented | unknown | unknown | tokentx 沒有被收成當前代幣餘額全集。沒有這份餘額不是零。 |
| ethereum-mainnet | balances.nft | unsupported | documented | unknown | unknown | tokennfttx 與 token1155tx 是歷史，不是當前 NFT 餘額。 |
| ethereum-mainnet | history.native_external | partial | documented | partial | unknown | txlist。分頁是否到底沒有在這份對照被證明，不能標 complete。 |
| ethereum-mainnet | history.native_internal | partial | documented | partial | unknown | txlistinternal。分頁是否到底沒有被證明，不能標 complete。 |
| ethereum-mainnet | history.fungible | partial | documented | partial | unknown | tokentx。分頁是否到底沒有被證明，不能標 complete。 |
| ethereum-mainnet | history.nft | partial | documented | partial | unknown | tokennfttx 與 token1155tx。分頁是否到底沒有被證明，不能標 complete。 |
| ethereum-mainnet | permissions.erc20 | partial | documented | partial | unknown | getLogs 的 Approval，topic0 是事件，topic1 是 owner。沒有讀當前 allowance。ERC-20 與 ERC-721 沒有分開。 |
| ethereum-mainnet | permissions.erc721_token | partial | documented | unknown | unknown | 同一批 Approval 日誌，沒有把單代幣授權和 ERC-20 分開。 |
| ethereum-mainnet | permissions.erc721_operator | partial | documented | partial | unknown | ApprovalForAll。沒有和 ERC-1155 operator 分開，也沒有讀當前 operator 狀態。 |
| ethereum-mainnet | permissions.erc1155_operator | partial | documented | unknown | unknown | ApprovalForAll 與 721 operator 共用這次盤點描述，沒有分開。 |
| ethereum-mainnet | permits.erc2612_observed | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| ethereum-mainnet | permits.dai_variant | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| ethereum-mainnet | permit2.allowance | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| ethereum-mainnet | permit2.signature_usage | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| ethereum-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ethereum-mainnet | positions | unsupported | untested | unknown | unknown | 這條鏈的私人腳本沒有協議部位。這不是沒有部位。 |
| ethereum-mainnet | withdrawals | unsupported | untested | unknown | unknown | 這條鏈的私人腳本沒有提款清單。這不是沒有在途資金。 |
| ethereum-mainnet | bridges | unsupported | untested | unknown | unknown | 沒有跨鏈進度。這不是已完成。 |
| ethereum-mainnet | asset_risk | unsupported | untested | unknown | unknown | 沒有代幣風險 feed。地址風險見 address_risk，兩者不是同一件事。 |
| ethereum-mainnet | address_risk | partial | documented | unknown | unknown | 例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。 |
| ethereum-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有制裁清單比對。這不是未上榜。 |
| ethereum-mainnet | exposure_news | partial | documented | partial | unknown | 只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。 |
| ethereum-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| bsc-mainnet | balances.native | unsupported | documented | unknown | unknown | 盤點裡的 BSC 來源是 NodeReal 轉帳，不是原生餘額。nonces 裡的 bsc 是 nonce，不是餘額。未知不是 0。 |
| bsc-mainnet | balances.fungible | unsupported | documented | unknown | unknown | 沒有當前代幣餘額全集。這不是零。 |
| bsc-mainnet | balances.nft | unsupported | untested | unknown | unknown | 沒有當前 NFT 餘額。這不是零。 |
| bsc-mainnet | history.native_external | partial | documented | partial | unknown | nr_getAssetTransfers。maxCount 是 0x64，只有第一頁，分頁不是完成。toBlock 約落後鏈頭 40 個區塊。latest 被拒絕。desc 不是時間序。 |
| bsc-mainnet | history.native_internal | unsupported | documented | unknown | unknown | 這次盤點沒有說 NodeReal 腳本包含內部追蹤。不能寫成已覆蓋。 |
| bsc-mainnet | history.fungible | partial | documented | partial | unknown | 同一支 nr_getAssetTransfers，只有第一頁，分頁不是完成。不能當成完整代幣歷史。 |
| bsc-mainnet | history.nft | unsupported | documented | unknown | unknown | 盤點沒有證明 NFT 歷史已翻完。這不是零。 |
| bsc-mainnet | permissions.erc20 | unsupported | documented | unknown | unknown | BSC 私人腳本沒有 approval 掃描。這不是沒有授權。 |
| bsc-mainnet | permissions.erc721_token | unsupported | documented | unknown | unknown | BSC 沒有單代幣授權掃描。這不是安全。 |
| bsc-mainnet | permissions.erc721_operator | unsupported | documented | unknown | unknown | BSC 沒有 operator 掃描。這不是安全。 |
| bsc-mainnet | permissions.erc1155_operator | unsupported | documented | unknown | unknown | BSC 沒有 ERC-1155 operator 掃描。這不是安全。 |
| bsc-mainnet | permits.erc2612_observed | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| bsc-mainnet | permits.dai_variant | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| bsc-mainnet | permit2.allowance | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| bsc-mainnet | permit2.signature_usage | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| bsc-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bsc-mainnet | positions | partial | documented | partial | observed | 只有手寫 Lista。公開核心 providers.read_lista_state 是這條讀取的程式，不是通用部位掃描。 |
| bsc-mainnet | withdrawals | partial | documented | partial | observed | 只有 Lista 提款請求。沒有領取收據不能當成已領取。 |
| bsc-mainnet | bridges | unsupported | untested | unknown | unknown | 沒有跨鏈進度。這不是已完成。 |
| bsc-mainnet | asset_risk | unsupported | untested | unknown | unknown | 沒有代幣風險 feed。這不是安全。 |
| bsc-mainnet | address_risk | partial | documented | unknown | unknown | 例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。 |
| bsc-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有制裁清單比對。這不是未上榜。 |
| bsc-mainnet | exposure_news | partial | documented | partial | unknown | 只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。 |
| bsc-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| arbitrum-one | balances.native | partial | documented | unknown | unknown | Etherscan V2 的 balance。這份公開文件沒有餘額數字。未知不是 0。W0 沒有重跑，probe 不是 live_pass。 |
| arbitrum-one | balances.fungible | unsupported | documented | unknown | unknown | tokentx 沒有被收成當前代幣餘額全集。沒有這份餘額不是零。 |
| arbitrum-one | balances.nft | unsupported | documented | unknown | unknown | tokennfttx 與 token1155tx 是歷史，不是當前 NFT 餘額。 |
| arbitrum-one | history.native_external | partial | documented | partial | unknown | txlist。分頁是否到底沒有在這份對照被證明，不能標 complete。 |
| arbitrum-one | history.native_internal | partial | documented | partial | unknown | txlistinternal。分頁是否到底沒有被證明，不能標 complete。 |
| arbitrum-one | history.fungible | partial | documented | partial | unknown | tokentx。分頁是否到底沒有被證明，不能標 complete。 |
| arbitrum-one | history.nft | partial | documented | partial | unknown | tokennfttx 與 token1155tx。分頁是否到底沒有被證明，不能標 complete。 |
| arbitrum-one | permissions.erc20 | partial | documented | partial | unknown | getLogs 的 Approval，topic0 是事件，topic1 是 owner。沒有讀當前 allowance。ERC-20 與 ERC-721 沒有分開。 |
| arbitrum-one | permissions.erc721_token | partial | documented | unknown | unknown | 同一批 Approval 日誌，沒有把單代幣授權和 ERC-20 分開。 |
| arbitrum-one | permissions.erc721_operator | partial | documented | partial | unknown | ApprovalForAll。沒有和 ERC-1155 operator 分開，也沒有讀當前 operator 狀態。 |
| arbitrum-one | permissions.erc1155_operator | partial | documented | unknown | unknown | ApprovalForAll 與 721 operator 共用這次盤點描述，沒有分開。 |
| arbitrum-one | permits.erc2612_observed | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| arbitrum-one | permits.dai_variant | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| arbitrum-one | permit2.allowance | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| arbitrum-one | permit2.signature_usage | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| arbitrum-one | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| arbitrum-one | positions | unsupported | untested | unknown | unknown | 這條鏈的私人腳本沒有協議部位。這不是沒有部位。 |
| arbitrum-one | withdrawals | unsupported | untested | unknown | unknown | 這條鏈的私人腳本沒有提款清單。這不是沒有在途資金。 |
| arbitrum-one | bridges | unsupported | untested | unknown | unknown | 沒有跨鏈進度。這不是已完成。 |
| arbitrum-one | asset_risk | unsupported | untested | unknown | unknown | 沒有代幣風險 feed。地址風險見 address_risk，兩者不是同一件事。 |
| arbitrum-one | address_risk | partial | documented | unknown | unknown | 例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。 |
| arbitrum-one | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有制裁清單比對。這不是未上榜。 |
| arbitrum-one | exposure_news | partial | documented | partial | unknown | 只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。 |
| arbitrum-one | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| base-mainnet | balances.native | partial | documented | unknown | unknown | 公開 RPC（publicnode）只讀 nonce 與原生餘額。沒有代幣、內部轉帳、授權或 permit。餘額數字不在這份公開文件。未知不是 0。latest 不是最終性證明。 |
| base-mainnet | balances.fungible | unsupported | documented | unknown | unknown | 沒有代幣餘額。這不是零。 |
| base-mainnet | balances.nft | unsupported | documented | unknown | unknown | 沒有 NFT 餘額。這不是零。 |
| base-mainnet | history.native_external | unsupported | documented | unknown | unknown | 沒有外部轉帳歷史。這不是空歷史。 |
| base-mainnet | history.native_internal | unsupported | documented | unknown | unknown | 沒有內部轉帳。這不是空歷史。 |
| base-mainnet | history.fungible | unsupported | documented | unknown | unknown | 沒有代幣歷史。這不是空歷史。 |
| base-mainnet | history.nft | unsupported | documented | unknown | unknown | 沒有 NFT 歷史。這不是空歷史。 |
| base-mainnet | permissions.erc20 | unsupported | documented | unknown | unknown | 沒有授權掃描。這不是沒有授權。 |
| base-mainnet | permissions.erc721_token | unsupported | documented | unknown | unknown | 沒有單代幣授權掃描。這不是安全。 |
| base-mainnet | permissions.erc721_operator | unsupported | documented | unknown | unknown | 沒有 operator 掃描。這不是安全。 |
| base-mainnet | permissions.erc1155_operator | unsupported | documented | unknown | unknown | 沒有 ERC-1155 operator 掃描。這不是安全。 |
| base-mainnet | permits.erc2612_observed | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| base-mainnet | permits.dai_variant | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| base-mainnet | permit2.allowance | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| base-mainnet | permit2.signature_usage | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| base-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| base-mainnet | positions | unsupported | untested | unknown | unknown | 沒有協議部位。這不是沒有部位。 |
| base-mainnet | withdrawals | unsupported | untested | unknown | unknown | 沒有提款清單。這不是沒有在途資金。 |
| base-mainnet | bridges | unsupported | untested | unknown | unknown | 沒有跨鏈進度。這不是已完成。 |
| base-mainnet | asset_risk | unsupported | untested | unknown | unknown | 沒有代幣風險 feed。這不是安全。 |
| base-mainnet | address_risk | partial | documented | unknown | unknown | 例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。 |
| base-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有制裁清單比對。這不是未上榜。 |
| base-mainnet | exposure_news | partial | documented | partial | unknown | 只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。 |
| base-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| optimism-mainnet | balances.native | partial | documented | unknown | unknown | 公開 RPC（publicnode）只讀 nonce 與原生餘額。沒有代幣、內部轉帳、授權或 permit。餘額數字不在這份公開文件。未知不是 0。latest 不是最終性證明。 |
| optimism-mainnet | balances.fungible | unsupported | documented | unknown | unknown | 沒有代幣餘額。這不是零。 |
| optimism-mainnet | balances.nft | unsupported | documented | unknown | unknown | 沒有 NFT 餘額。這不是零。 |
| optimism-mainnet | history.native_external | unsupported | documented | unknown | unknown | 沒有外部轉帳歷史。這不是空歷史。 |
| optimism-mainnet | history.native_internal | unsupported | documented | unknown | unknown | 沒有內部轉帳。這不是空歷史。 |
| optimism-mainnet | history.fungible | unsupported | documented | unknown | unknown | 沒有代幣歷史。這不是空歷史。 |
| optimism-mainnet | history.nft | unsupported | documented | unknown | unknown | 沒有 NFT 歷史。這不是空歷史。 |
| optimism-mainnet | permissions.erc20 | unsupported | documented | unknown | unknown | 沒有授權掃描。這不是沒有授權。 |
| optimism-mainnet | permissions.erc721_token | unsupported | documented | unknown | unknown | 沒有單代幣授權掃描。這不是安全。 |
| optimism-mainnet | permissions.erc721_operator | unsupported | documented | unknown | unknown | 沒有 operator 掃描。這不是安全。 |
| optimism-mainnet | permissions.erc1155_operator | unsupported | documented | unknown | unknown | 沒有 ERC-1155 operator 掃描。這不是安全。 |
| optimism-mainnet | permits.erc2612_observed | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| optimism-mainnet | permits.dai_variant | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| optimism-mainnet | permit2.allowance | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| optimism-mainnet | permit2.signature_usage | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| optimism-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| optimism-mainnet | positions | unsupported | untested | unknown | unknown | 沒有協議部位。這不是沒有部位。 |
| optimism-mainnet | withdrawals | unsupported | untested | unknown | unknown | 沒有提款清單。這不是沒有在途資金。 |
| optimism-mainnet | bridges | unsupported | untested | unknown | unknown | 沒有跨鏈進度。這不是已完成。 |
| optimism-mainnet | asset_risk | unsupported | untested | unknown | unknown | 沒有代幣風險 feed。這不是安全。 |
| optimism-mainnet | address_risk | partial | documented | unknown | unknown | 例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。 |
| optimism-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有制裁清單比對。這不是未上榜。 |
| optimism-mainnet | exposure_news | partial | documented | partial | unknown | 只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。 |
| optimism-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| polygon-pos | balances.native | partial | documented | unknown | unknown | Etherscan V2 的 balance。這份公開文件沒有餘額數字。未知不是 0。W0 沒有重跑，probe 不是 live_pass。 |
| polygon-pos | balances.fungible | unsupported | documented | unknown | unknown | tokentx 沒有被收成當前代幣餘額全集。沒有這份餘額不是零。 |
| polygon-pos | balances.nft | unsupported | documented | unknown | unknown | tokennfttx 與 token1155tx 是歷史，不是當前 NFT 餘額。 |
| polygon-pos | history.native_external | partial | documented | partial | unknown | txlist。分頁是否到底沒有在這份對照被證明，不能標 complete。 |
| polygon-pos | history.native_internal | partial | documented | partial | unknown | txlistinternal。分頁是否到底沒有被證明，不能標 complete。 |
| polygon-pos | history.fungible | partial | documented | partial | unknown | tokentx。分頁是否到底沒有被證明，不能標 complete。 |
| polygon-pos | history.nft | partial | documented | partial | unknown | tokennfttx 與 token1155tx。分頁是否到底沒有被證明，不能標 complete。 |
| polygon-pos | permissions.erc20 | partial | documented | partial | unknown | getLogs 的 Approval，topic0 是事件，topic1 是 owner。沒有讀當前 allowance。ERC-20 與 ERC-721 沒有分開。 |
| polygon-pos | permissions.erc721_token | partial | documented | unknown | unknown | 同一批 Approval 日誌，沒有把單代幣授權和 ERC-20 分開。 |
| polygon-pos | permissions.erc721_operator | partial | documented | partial | unknown | ApprovalForAll。沒有和 ERC-1155 operator 分開，也沒有讀當前 operator 狀態。 |
| polygon-pos | permissions.erc1155_operator | partial | documented | unknown | unknown | ApprovalForAll 與 721 operator 共用這次盤點描述，沒有分開。 |
| polygon-pos | permits.erc2612_observed | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| polygon-pos | permits.dai_variant | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| polygon-pos | permit2.allowance | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| polygon-pos | permit2.signature_usage | unsupported | documented | unknown | unknown | 私人腳本沒有掃 permit 或 Permit2。這不是沒有許可。 |
| polygon-pos | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| polygon-pos | positions | unsupported | untested | unknown | unknown | 這條鏈的私人腳本沒有協議部位。這不是沒有部位。 |
| polygon-pos | withdrawals | unsupported | untested | unknown | unknown | 這條鏈的私人腳本沒有提款清單。這不是沒有在途資金。 |
| polygon-pos | bridges | unsupported | untested | unknown | unknown | 沒有跨鏈進度。這不是已完成。 |
| polygon-pos | asset_risk | unsupported | untested | unknown | unknown | 沒有代幣風險 feed。地址風險見 address_risk，兩者不是同一件事。 |
| polygon-pos | address_risk | partial | documented | unknown | unknown | 例行有做 GoPlus 地址風險。函式與鏈的覆蓋沒有在這份對照被證明。沒有結果不是安全。W0 沒有重跑，所以不是 live_pass。 |
| polygon-pos | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有制裁清單比對。這不是未上榜。 |
| polygon-pos | exposure_news | partial | documented | partial | unknown | 只有人工檢查 Kinza、Lista、UXLINK 這三個名字，不是這條鏈的新聞全集。 |
| polygon-pos | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| avalanche-c | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| avalanche-c | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| avalanche-c | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| solana-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.spl_delegate | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permissions.spl_authority | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| solana-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| solana-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| linea-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| linea-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| linea-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| zksync-era | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| zksync-era | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| zksync-era | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| scroll-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| scroll-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| scroll-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| mantle-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| mantle-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| mantle-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| sonic-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sonic-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sonic-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| unichain-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| unichain-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| unichain-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| hyperevm-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| hyperevm-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| hyperevm-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| bitcoin-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | balances.fungible | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | balances.nft | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | history.fungible | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | history.nft | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| bitcoin-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| bitcoin-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| tron-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.tron_account | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| tron-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| tron-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| blast-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| blast-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| blast-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| opbnb-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.native_internal | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc20 | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc721_token | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc721_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.erc1155_operator | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permits.erc2612_observed | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permits.dai_variant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permit2.allowance | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permit2.signature_usage | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| opbnb-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| opbnb-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| ton-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.ton_extensions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| ton-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| ton-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| sui-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.move_capability | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| sui-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| sui-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| aptos-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.move_capability | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | permissions.cosmos_authz | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | permissions.cosmos_feegrant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| aptos-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| aptos-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| cosmos-hub | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| cosmos-hub | permissions.cosmos_authz | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | permissions.cosmos_feegrant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| cosmos-hub | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |
| osmosis-mainnet | balances.native | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | balances.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | balances.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | history.native_external | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | history.native_internal | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | history.fungible | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | history.nft | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | permissions.erc20 | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.erc721_token | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.erc721_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.erc1155_operator | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permits.erc2612_observed | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permits.dai_variant | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permit2.allowance | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permit2.signature_usage | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.spl_delegate | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.spl_authority | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.tron_account | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.ton_extensions | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.move_capability | not_applicable | untested | unknown | unknown | 此鏈族沒有這個能力。不適用不是掃描通過，也不是零。 |
| osmosis-mainnet | permissions.cosmos_authz | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | permissions.cosmos_feegrant | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | positions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | withdrawals | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | bridges | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | asset_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | address_risk | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | sanctions | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | exposure_news | unsupported | untested | unknown | unknown | 私人例行沒有查這條鏈。未查不是空部位，也不是安全。 |
| osmosis-mainnet | permits.offchain_unused | not_observable | untested | unknown | unknown | 未提交簽名是 not_observable，不能標成 complete。 |

## W1 必須遷移、而且不能重送

公開 SQLite 要原樣留住，避免同一件事再通知一次：

- `alerts` 與 `alert_dedup`
- `outbox`（含已是 `pending` 但還沒送出的列）
- `cursors`（`run-once` 仍不寫這張表；已有的列不能丟。W1 匯入器才寫舊游標）
- `snapshots` 與對應的 `snapshot_items`

私人 JSON 要留住的游標與去重：

- `alerted`
- `latest_seen`
- `nonces`
- `bsc_last_block`

這些值繼續只放在操作者機器。遷移不能把它們抄進這個 repo，也不能因為新監控啟動就把舊的 `alerted` 再送一次。W1 的匯入器把 `alerted` 寫進 `alert_dedup` 與 `legacy_alert_suppressions`，不寫 `alerts` 或 `outbox`。`latest_seen`、`nonces`、`bsc_last_block` 寫進 `cursors`，並標上 `legacy_import`。對不上的鏈或錢包標籤留在報告的 `unmapped`，不猜。同一個內容再匯入是 no-op。W9 之前舊例行照常跑；新的 outbox 先不送出。
