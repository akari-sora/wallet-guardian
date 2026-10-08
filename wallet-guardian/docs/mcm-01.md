# MCM-01 多鏈監控範圍契約

這份文件是計畫與契約，不是執行結果。套件編號是 MULTICHAIN-MONITOR-01（MCM-01）。基準提交是 `d6331f02b1a1fcdb75f7c265a29d484056a42515`（短碼 `d6331f02`）。W0 的分支是 `mcm/scope-baseline`。

沒有任何鏈在本包被 live 驗證。探針沒有跑的項目維持未驗證。不能把拒絕寫入、空回應或全部拒絕的 fixture 說成功能可用。`docs/mcm-01/work-packages.plan.json` 的 `status` 是 `planning_only`。`docs/mcm-01/coverage-targets.plan.json` 的 `status` 是 `design_targets_not_wallet_results`，裡面的 `observed_coverage` 是 null，`live_verified` 是 false。那是設計目標，不是某一個錢包的結果。

既有每 3 小時的私人運維例行維持原樣，直到 W9 交接。交接先 shadow：新的 outbox 先不送出。之後才改成單一 sender。在那之前不關掉舊例行，也不讓新入口搶先送通知。W0 不切換。

既有公開核心的 `run-once`、`SnapshotCycle` 與 Lista 讀取維持不變。本包沒有改 CLI 指令的行為，也沒有把 `config/chains/*.json` 接進 `run-once`。執行時仍使用 `service.py` 裡的 BNB Chain 官方 RPC 常數。`config/chains.json` 繼續只啟用鏈 56，本包不改那個檔。

## 層級與 24 個網路

以下是工程交付順序，不是 TVL 或安全排名。T0 先做七條 EVM 與 Solana。T1 是其餘 EVM 剖面，加上 Bitcoin 與 Tron。T2 是 TON、Sui，以及低優先的 Blast 與 opBNB。T3 是 Aptos、Cosmos Hub 與 Osmosis。新鏈寫進登錄也不自動啟用。本包每一筆 `enabled` 都是 false，`status` 都是 `planned_not_live_verified`。

非 EVM 必須由使用者提供該鏈族的地址。不能把 EVM 地址換算成別的鏈。Cosmos 是鏈族，Hub 與 Osmosis 各算一條，不能一次掃完。

2026-10-08 查證到、會直接影響選型的兩件事：

- Etherscan 官方宣布 2026-10-12 起停止 opBNB Mainnet/Testnet API 支援。這是 API 支援變動，不是鏈停止運作。新的 opBNB 來源不要依賴這套 API。
- Sui Foundation 的 mainnet fullnodes 已停用 JSON-RPC。新 adapter 用 GraphQL 或 gRPC，不新建 `suix_*` 路徑作主幹。

| 網路 | family | tier | evm_chain_id | 原生資產 | 識別方法 |
|---|---|---|---|---|---|
| `ethereum-mainnet` | evm | T0 | 1 | ETH / 18 | eth_chainId |
| `bsc-mainnet` | evm | T0 | 56 | BNB / 18 | eth_chainId |
| `arbitrum-one` | evm | T0 | 42161 | ETH / 18 | eth_chainId |
| `base-mainnet` | evm | T0 | 8453 | ETH / 18 | eth_chainId |
| `optimism-mainnet` | evm | T0 | 10 | ETH / 18 | eth_chainId |
| `polygon-pos` | evm | T0 | 137 | POL / 18 | eth_chainId |
| `avalanche-c` | evm | T0 | 43114 | AVAX / 18 | eth_chainId |
| `solana-mainnet` | solana | T0 | null | SOL / 9 | getGenesisHash |
| `linea-mainnet` | evm | T1 | 59144 | ETH / 18 | eth_chainId |
| `zksync-era` | evm | T1 | 324 | ETH / 18 | eth_chainId |
| `scroll-mainnet` | evm | T1 | 534352 | ETH / 18 | eth_chainId |
| `mantle-mainnet` | evm | T1 | 5000 | MNT / 18 | eth_chainId |
| `sonic-mainnet` | evm | T1 | 146 | S / 18 | eth_chainId |
| `unichain-mainnet` | evm | T1 | 130 | ETH / 18 | eth_chainId |
| `hyperevm-mainnet` | evm | T1 | 999 | HYPE / 18 | eth_chainId |
| `bitcoin-mainnet` | bitcoin | T1 | null | BTC / 8 | genesis block hash |
| `tron-mainnet` | tron | T1 | null | TRX / 6 | genesis block hash |
| `blast-mainnet` | evm | T2 | 81457 | ETH / 18 | eth_chainId |
| `opbnb-mainnet` | evm | T2 | 204 | BNB / 18 | eth_chainId |
| `ton-mainnet` | ton | T2 | null | TON / 9 | zero_state file hash |
| `sui-mainnet` | sui | T2 | null | SUI / 9 | sui chain identifier |
| `aptos-mainnet` | aptos | T3 | null | APT / 8 | chain-id from node_info |
| `cosmos-hub` | cosmos | T3 | null | ATOM / 6 | chain-id from node_info |
| `osmosis-mainnet` | cosmos | T3 | null | OSMO / 6 | chain-id from node_info |

逐鏈的原生資產、鏈 ID 或 genesis、最終性文字與來源在 `config/chains/<network-id>.json`。載入器是 `guardian.network_registry:load_registry`。它只讀本機 JSON，不開網路，也不會因為檔案存在就把鏈打開。

## 工作包 W0 到 W17

通過條件的原文在 `docs/mcm-01/work-packages.plan.json`，不在這張表改寫。W0 到 W9 組成第一個可分享版本，不等 T1 到 T3 全部完成。W10 以後各自交付，不倒灌成第一版的驗收條件。每個 W 一個小變更；已合併的父層才重基準。

| ID | 標題 | 分支 | 依賴 | 檔案 | 測試 ID |
|---|---|---|---|---|---|
| MCM-W0 | 範圍盤點與遷移基線 | `mcm/scope-baseline` | 無 | wallet-guardian/docs/mcm-01.md<br>wallet-guardian/docs/legacy-monitor-map.md<br>wallet-guardian/config/chains/*.json | MCM-W0-01、MCM-W0-02、MCM-W0-03 |
| MCM-W1 | 公開config、私有帳戶與穩定身份 | `mcm/config-private-state` | W0 | wallet-guardian/schemas/monitor.schema.json<br>wallet-guardian/schemas/accounts.schema.json<br>wallet-guardian/src/guardian/configuration.py<br>wallet-guardian/src/guardian/identities.py<br>wallet-guardian/src/guardian/storage.py<br>wallet-guardian/migrations/monitor/ | MCM-W1-01、MCM-W1-02、MCM-W1-03、MCM-W1-04 |
| MCM-W2 | 唯讀transport與共享預算 | `mcm/readonly-sources` | W1 | wallet-guardian/src/guardian/sources/<br>wallet-guardian/src/guardian/security/transport_policy.py<br>wallet-guardian/config/source-capabilities/<br>wallet-guardian/tests/contracts/sources/ | MCM-W2-01、MCM-W2-02、MCM-W2-03、MCM-W2-04 |
| MCM-W3 | 事件、能力coverage與原子游標 | `mcm/events-coverage` | W1、W2 | wallet-guardian/src/guardian/events.py<br>wallet-guardian/src/guardian/coverage.py<br>wallet-guardian/src/guardian/monitoring.py<br>wallet-guardian/src/guardian/storage.py<br>wallet-guardian/schemas/coverage.schema.json | MCM-W3-01、MCM-W3-02、MCM-W3-03、MCM-W3-04 |
| MCM-W4 | T0 EVM資產與歷史 | `mcm/evm-core` | W2、W3 | wallet-guardian/src/guardian/chains/evm/<br>wallet-guardian/src/guardian/sources/etherscan.py<br>wallet-guardian/src/guardian/sources/blockscout.py<br>wallet-guardian/src/guardian/sources/nodereal.py<br>wallet-guardian/config/chains/ | MCM-W4-01、MCM-W4-02、MCM-W4-03、MCM-W4-04 |
| MCM-W5 | EVM權限與已上鏈permit | `mcm/evm-permissions` | W4 | wallet-guardian/src/guardian/chains/evm/permissions.py<br>wallet-guardian/src/guardian/chains/evm/permits.py<br>wallet-guardian/config/deployments/permit2/<br>wallet-guardian/tests/fixtures/evm-permissions/ | MCM-W5-01、MCM-W5-02、MCM-W5-03、MCM-W5-04 |
| MCM-W6 | Solana資產與權限 | `mcm/solana-core` | W2、W3 | wallet-guardian/src/guardian/chains/solana/<br>wallet-guardian/src/guardian/sources/helius.py<br>wallet-guardian/tests/fixtures/solana/ | MCM-W6-01、MCM-W6-02、MCM-W6-03、MCM-W6-04 |
| MCM-W7 | 行情與風險標籤 | `mcm/risk-price` | W3 | wallet-guardian/src/guardian/risk/<br>wallet-guardian/src/guardian/pricing/<br>wallet-guardian/config/risk-sources.json<br>wallet-guardian/tests/fixtures/risk/ | MCM-W7-01、MCM-W7-02、MCM-W7-03、MCM-W7-04 |
| MCM-W8 | 協議部位與曝險新聞 | `mcm/protocol-news` | W4、W5、W6、W7 | wallet-guardian/src/guardian/protocols/<br>wallet-guardian/src/guardian/news/<br>wallet-guardian/config/protocols/<br>wallet-guardian/config/news-sources/ | MCM-W8-01、MCM-W8-02、MCM-W8-03、MCM-W8-04 |
| MCM-W9 | 第一個可分享版本與平滑接手 | `mcm/core-release` | W0、W1、W2、W3、W4、W5、W6、W7、W8 | wallet-guardian/src/guardian/cli.py<br>wallet-guardian/src/guardian/scheduler.py<br>wallet-guardian/src/guardian/delivery.py<br>wallet-guardian/examples/monitor.json<br>wallet-guardian/docs/quickstart-monitor.md<br>wallet-guardian/tests/acceptance/ | MCM-W9-01、MCM-W9-02、MCM-W9-03、MCM-W9-04 |
| MCM-W10 | Bitcoin唯讀地址集監控 | `mcm/bitcoin` | W2、W3、W7、W9 | wallet-guardian/src/guardian/chains/bitcoin/<br>wallet-guardian/src/guardian/sources/esplora.py<br>wallet-guardian/tests/fixtures/bitcoin/ | MCM-W10-01、MCM-W10-02、MCM-W10-03 |
| MCM-W11 | Tron資產與帳戶權限 | `mcm/tron` | W2、W3、W7、W9 | wallet-guardian/src/guardian/chains/tron/<br>wallet-guardian/src/guardian/sources/trongrid.py<br>wallet-guardian/tests/fixtures/tron/ | MCM-W11-01、MCM-W11-02、MCM-W11-03 |
| MCM-W12 | T1 EVM擴充 | `mcm/evm-tier1` | W4、W5、W9 | wallet-guardian/config/chains/<br>wallet-guardian/config/source-capabilities/<br>wallet-guardian/tests/contracts/evm-networks/<br>wallet-guardian/src/guardian/protocols/hypercore/ | MCM-W12-01、MCM-W12-02、MCM-W12-03 |
| MCM-W13 | TON訊息與Jetton監控 | `mcm/ton` | W2、W3、W7、W9 | wallet-guardian/src/guardian/chains/ton/<br>wallet-guardian/src/guardian/sources/toncenter.py<br>wallet-guardian/tests/fixtures/ton/ | MCM-W13-01、MCM-W13-02、MCM-W13-03 |
| MCM-W14 | Sui GraphQL/gRPC監控 | `mcm/sui` | W2、W3、W7、W9 | wallet-guardian/src/guardian/chains/sui/<br>wallet-guardian/src/guardian/sources/sui_graphql.py<br>wallet-guardian/src/guardian/sources/sui_grpc.py<br>wallet-guardian/tests/fixtures/sui/ | MCM-W14-01、MCM-W14-02、MCM-W14-03 |
| MCM-W15 | Aptos Coin與FA監控 | `mcm/aptos` | W2、W3、W7、W9 | wallet-guardian/src/guardian/chains/aptos/<br>wallet-guardian/src/guardian/sources/aptos_indexer.py<br>wallet-guardian/tests/fixtures/aptos/ | MCM-W15-01、MCM-W15-02、MCM-W15-03 |
| MCM-W16 | Cosmos Hub／Osmosis監控 | `mcm/cosmos` | W2、W3、W7、W9 | wallet-guardian/src/guardian/chains/cosmos/<br>wallet-guardian/src/guardian/protocols/ibc/<br>wallet-guardian/config/chains/cosmos/<br>wallet-guardian/tests/fixtures/cosmos/ | MCM-W16-01、MCM-W16-02、MCM-W16-03 |
| MCM-W17 | 低優先EVM與擴充版清單結案 | `mcm/evm-tier2-release` | W9、W12 | wallet-guardian/config/chains/blast.json<br>wallet-guardian/config/chains/opbnb.json<br>wallet-guardian/docs/monitor-support-matrix.md<br>wallet-guardian/tests/contracts/evm-tier2/ | MCM-W17-01、MCM-W17-02、MCM-W17-03 |

## 四軸與能力目錄

能力單位是 `(tenant_id, wallet_id, network_id, capability)`，不是整條鏈一個 success。既有 `monitoring.SnapshotCycle` 的原子提交與 outbox 保留。原生幣查到了，不能因此刪掉還沒查到的代幣或部位。

四軸的詞彙：

1. support：`implemented`、`partial`、`unsupported`、`not_applicable`、`not_observable`。
2. probe：`documented`、`live_pass`、`live_fail`、`blocked`、`untested`。
3. quality：`complete_in_declared_scope`、`partial`、`stale`、`error`、`unknown`。
4. finality：`observed`、`confirmed`、`finalized`、`orphaned`、`unknown`。

本包沒有 live probe，所以沒有任何能力可以標 `live_pass`。未知的數量和價格保持未知，不寫成 0。失敗的讀取不是 0。空的 gaps 若還沒做 gap detection，不算完整。RPC 成功不等於風險 feed 正常。最終性用各鏈自己的規則，不全鏈套 12 個確認。Bitcoin 的確認數是政策門檻，不宣稱絕對 finalized。

以後每一格要能放下：宣告範圍、歷史 from/to、`pagination_complete`、`gap_detection_complete`、gaps、cursor、`last_success_at`、`valid_until`、checkpoint 或 hash、來源、adapter 版本、來源是否獨立、evidence refs。W0 只固定詞彙與現況對照，不填使用者的私人觀測。

能力目錄：

- `balances.native`、`balances.fungible`、`balances.nft`
- `history.native_external`、`history.native_internal`、`history.fungible`、`history.nft`
- `permissions.erc20`、`permissions.erc721_token`、`permissions.erc721_operator`、`permissions.erc1155_operator`
- `permits.erc2612_observed`、`permits.dai_variant`、`permit2.allowance`、`permit2.signature_usage`
- `permissions.spl_delegate`、`permissions.spl_authority`、`permissions.tron_account`
- `permissions.ton_extensions`、`permissions.move_capability`、`permissions.cosmos_authz`、`permissions.cosmos_feegrant`
- `positions`、`withdrawals`、`bridges`、`asset_risk`、`address_risk`、`sanctions`、`exposure_news`
- `permits.offchain_unused` 固定是 `not_observable`，不能變成 complete

EVM 權限的最低語義（契約，尚未在本包實作）：Approval 的 owner 是監控對象，不只找 `from` 等於錢包的交易；permit 可由 relayer 提交。依 topic、ABI 或 ERC-165 區分 20 與 721，分不出來就標 unknown。ERC-721 的單一代幣授權，和 721 或 1155 的 `setApprovalForAll`，分開記。以事件加當前 allowance 或 operator 狀態記錄。已經花掉或自動清掉的額度，不能說成使用者新的授權。

已上鏈的 ERC-2612 會更新 nonce 與 allowance，並發出 Approval。DAI 變體另外辨識。Permit2 分成 ERC-20 到 Permit2，以及 Permit2 到 spender。AllowanceTransfer 與 SignatureTransfer 分開，追有效期、nonce 失效與已觀測到的使用。只看當前 nonceBitmap 不能列舉過去所有離線簽名，也看不到還沒提交的簽名。沒有持續 allowance 的一次性消費，不能列成永久批准。

Solana 權限的最低語義（契約，尚未在本包實作）：Approve、ApproveChecked、Revoke、owner、close authority、mint、freeze authority、Token-2022 permanent delegate，與其他已知 extension 分開。帳戶 close 可能只是租金回收，不直接是失竊。結構性的 authority 風險與惡意標籤分開。新的 delegate 換掉舊的，即使額度相同也是變更，要呈現舊到新。

現況對照在 [legacy-monitor-map.md](legacy-monitor-map.md)。那份表是目前程式與私人例行各自做得到什麼，不是這 24 條鏈的目標覆蓋。

## 公開與私人分離

三份 Draft 2020-12 schema（`monitor.schema.json`、`accounts.schema.json`、`coverage.schema.json`）是建議契約，由後續工作包落地。W0 不把 schema 抄進執行路徑。公開庫放 schema、profile、鏈與 ABI 與來源登錄、通用預設門檻、合成 fixture。私人設定放在作業系統使用者設定目錄：帳戶檔、RPC 與 API 的秘密 profile、通知收件人與 token。私人狀態放在作業系統狀態目錄：`monitor.sqlite`、cursor、permissions、outbox、evidence、provider 用量。

`MONITOR_ACCOUNTS_FILE`、`MONITOR_SECRET_PROFILE`、`MONITOR_STATE_DIR` 只指向私人檔案或目錄。各來源用 `endpoint_env` 與 `credential_env` 引用環境變數的名稱。公開設定不放帶 key 的 URL。環境變數名稱本身不會自動保密，日誌仍要遮罩，檔案權限維持 0600，備份也要保護。

Schema 以外還要檢查：`wallet_id` 唯一、network 與 family 相容、地址 checksum 或解碼、routing 與 source 與 budget 的引用都存在、不從一條鏈的錢包推另一條鏈、不執行任意 plugin、同一提供者帳戶共用預算、秘密與狀態不落在這個 repo。顯示名稱不是身份，錢包 UUID 才是身份。JSON Schema 不是完整的隱私或安全保證。

公開核心現在的指令有：`doctor`、`run-once`、`snapshot`、`reconcile`、`claims`、`providers`、`wallets`、`approvals`、`explain`、`coverage`、`replay`、`alerts`、`self-check`、`diagnose`、`actions`、`action`、`rescue`、`operation-coverage`、`workbench`。

新的登錄模組 `guardian.network_registry:load_registry` 不匯入 Action Client，也不匯入 `guardian.actions`。簽名與送出仍只存在既有的 Action Client 邊界，而且正式預設保持關閉。細節見舊監控對照與 W0 的唯讀測試。

## 測試策略

預設 CI 完全離線。不使用真實 key、真實地址或真實通知。fixture 沒有命中就失敗，不能偷偷連公網。

之後每一條鏈至少要有：一個真正非零的正常 fixture、一個已確認的失敗、一個 reorg 或 rollback 或失效、一個分頁、429、超額或缺欄位。authority 還要有新增、提高、撤銷，以及前態 unknown。

金額用 raw integer 或 Decimal。要測 18、6、9、8 位小數，以及大於 2^53 的整數。外部供應商 JSON 裡的浮點數不能當成鏈上原始金額。

錄製工具只讀。真實使用者的錄製放在私人 evidence。公開 fixture 用合成資料，或明確可分享的官方例子，並記下來源、版本與授權。刪掉地址就會破壞交易、簽名或雜湊的回應，不能說成可驗證的原始證據。

一般的發送與簽名 RPC、REST 廣播端點、GraphQL mutation、gRPC execute，都必須在到達 transport 之前拒絕。不能只做 `eth_send*` 字串黑名單。通知通道與鏈上唯讀 transport 分開。

空資料、查詢失敗、HTTP 200 但內容是部分錯誤、分頁沒有完成、價格 unknown、來源降級，各自測試，而且不覆寫 last-known-good。

同一事件的多個來源、重跑、重啟、重組、通知 timeout，要驗收去重與 outbox。不能先推進 cursor 再把事件弄丟。

live smoke 只在使用者本機、自備 key、以及允許的公開測試地址上做唯讀驗證。CI 裡的 skip 不能算 PASS。不能用硬編碼 selector 替身通過，就宣稱密碼學或 SDK 已經驗收。

結果依固定的整合 SHA 分開報告：PASS、FAIL、SKIP、BLOCKED、NOT_RUN。環境跑不起來，不是反覆改產品程式的理由。

本包的 live 來源是 NOT_RUN。文件裡的 `documented` 只表示程式或運維盤點描述過這條路徑，不是這次重新打過節點。

## 不在範圍

- 任何資產交易、approve、revoke、claim、簽名、delegate、權限升級、代廣播、搶救資金，或保證防清算。
- 改動既有 Action Client、SIWE、WalletConnect 權限、D1 schema 或公開部署。非 EVM 簽名登入不包含在監控器擴充裡。
- 未提交的 Permit、Permit2 或 Solana 離線簽名，CEX 內部帳戶，隱私或加密交易內容，以及所有鏈、所有協議、全歷史的完整保證。
- Bitcoin 的 xpub、descriptors、Lightning、Ordinals、Runes；Cosmos 的全部主權鏈；Move 的任意 capability 解碼。未列的鏈（例如 XRP、Cardano）另案。
- 第一版不強制 webhook、全節點或全 archive、付費聚合器、GraphQL 或 gRPC 訂閱，或專有威脅情報。
- 不靠 LLM 估算餘額，不自動信任新的 RPC、合約或協議公告，不把來源標籤當成法律結論。

## W1 實作狀態

W1 在這個分支落地公開設定、私人帳戶與穩定身份。上面「公開與私人分離」裡「W0 不把 schema 抄進執行路徑」是 W0 的範圍。從這一節起，schema 與載入器在這個分支。

現在有的東西：

- `schemas/monitor.schema.json`、`schemas/accounts.schema.json`、`schemas/coverage.schema.json` 是規劃契約的原文。`coverage.schema.json` 只是契約，執行覆蓋從 W3 才接。
- `examples/monitor.json` 與 `examples/accounts.empty.template.json` 沒有地址、key 或狀態。
- `guardian.jsonschema_lite` 只檢查這些 schema 用到的 Draft 2020-12 子集。不認得的關鍵字會失敗，不會略過。沒有加 `jsonschema` 套件。
- `guardian.identities` 只依宣告的鏈族解析地址。鏈族不會互相推導。`wallet_id` 是身份，標籤只是顯示。
- `guardian.configuration` 載入公開設定與私人帳戶，並檢查來源、預算與鏈族是否對得上。同一個 `credential_env` 必須共用一個 `budget_group`。缺憑證的來源是 BLOCKED（`credential_missing`），其他來源繼續。`safe_export` 只輸出 schema 版本、啟用的網路、每個鏈族與網路的錢包數量、來源名稱、是否啟用、憑證是否存在，以及預算。
- `guardian.storage` 保持 `PRAGMA user_version=1`。MCM 遷移在 `migrations/monitor/`，記錄在 `mcm_schema_migrations`。重開是 no-op。
- `guardian.legacy_state` 讀舊的私人 JSON，寫入去重、抑制與游標。不寫 `alerts` 或 `outbox`，也不從 `runs` 製造快照。同一個內容再匯入是 no-op。

- 審查補強（reviewer）：
  - DNS rebinding：每次請求只解析一次 DNS，驗證通過的同一個 IP 直接釘給 socket（`PreparedCall.connect_ip`；`HttpxBackend` 以 IP 連線，保留 `Host` header 與 TLS SNI，憑證仍對原 hostname 驗證）。HTTP client 不會再自己查一次 DNS。
  - GraphQL 變數預設值（`$x: Int = 1`）可以解析；`mutation`／`subscription` 帶預設值仍拒絕；語法不明一律 fail closed。
  - GET-only adapter（blockscout、esplora、cosmos_rest、aptos_rest、goplus、coingecko、ofac_sls、chainabuse、debank）改成每個 adapter 的 path allowlist（`/` 結尾是前綴，否則是完整路徑），未知路徑拒絕。清單是暫定的，各 adapter 在 W4+ 實作時再經審查調整。
  - 能力檔 `limits[].verification`：`numbers_checked_against_source_url_2026-10-08` 表示數字已對照 `source_url`（Etherscan、Helius、TON Center、NodeReal、Esplora）；`unverified` 表示沒有核對，且檔內沒有寫數字。

還沒接上的東西：

- `run-once` 仍走既有設定與 BNB Chain 路徑，直到 W9。這包沒有改 `service.run_once`、`SnapshotCycle` 的行為或 CLI 指令。
- 鏈登錄的 `enabled` 仍是 false，`status` 仍是 `planned_not_live_verified`。公開設定裡的 `enabled_networks` 只是操作者要監控的清單，不是 live 驗證。沒有任何鏈被標成 live_pass。
- 沒有 live 來源驗證。缺 key 是 BLOCKED，不是已驗證，也不是沒有資產。

### 私人設定放哪裡

帳戶檔、秘密 profile、狀態目錄都放在這個 git 工作樹外面。公開設定只用環境變數的名稱指向它們，名稱寫在 `examples/monitor.json`：

- `MONITOR_ACCOUNTS_FILE`：帳戶 JSON 的路徑
- `MONITOR_SECRET_PROFILE`：秘密 profile JSON 的路徑。內容是環境變數名稱對應到值，不進這個 repo。
- `MONITOR_STATE_DIR`：狀態目錄。裡面會放 `monitor.sqlite`、游標與 outbox。

POSIX 上帳戶檔與秘密檔要是擁有者才可讀寫（0600 這一類，群組與其他人不可讀）。狀態目錄建立為 0700。Windows 不檢查這組權限位元。日誌不印秘密值。這裡不寫真實路徑、地址或 key。

## W2 實作狀態

W2 在這個分支加上唯讀 transport、共享預算、有限重試、分頁上限、來源切換與端點政策。鏈 adapter 還沒有。`run-once`、`SnapshotCycle`、Lista 與 CLI 的行為沒有改。

現在有的東西：

- `guardian.security.transport_policy` 在碰到 socket 之前拒絕寫入、簽名、廣播，以及批次裡夾帶的那一筆。允許的是操作語義，不是 HTTP 方法：唯讀 POST 可以，GET 的廣播不行。JSON-RPC 方法整段比對，不折大小寫，不去空白，也不吃前綴。
- `guardian.sources` 是新套件。既有的 `providers.py` 仍是那個模組，沒有同名 package 蓋住它。
- 同一個 `budget_group` 共用一個 token bucket、併發上限、每日次數與每月 credits。低優先先被拒絕。高優先與 health 可以用到硬上限。額度用盡會 raise，不會靜默跳過，也不會把失敗寫成 0。用量寫到私人狀態目錄的 `provider-usage/`，POSIX 上是 0600。
- 429 會看 Retry-After（秒數或 HTTP-date），等待有上限。5xx、逾時、連線錯誤會有限重試，每一次都扣預算。400、401、403、404 不重試。401 是 `credential_rejected`，403 是 `forbidden`，來源視為 BLOCKED。Etherscan HTTP 200 且內容是 rate limit 會重試；`No transactions found` 是該次查詢的空結果，不是失敗變 0。JSON-RPC 的 error 物件不是空歷史。
- 分頁走到 page cap、item cap、預算或錯誤時，`pagination_complete` 是 false。
- `SourceRouter` 只對該來源在該網路上宣告了的能力做 fallback。沒有的能力是 `fallback_lacks_capability`，quality 是 unknown 或 error，不會是 complete。全部失敗回傳 error，不回傳空陣列或 0，也不覆蓋舊值。不同網域、同一個 `backend` 只算一個獨立來源；quorum 不夠會拒絕。
- 端點只來自私人 profile 或環境變數解出來的登錄。任務、metadata、回應內容不能新增或改 RPC URL。重新導向不自動跟著走；每一跳都再檢查，而且必須留在同一個已登錄的 host。每次請求都看 DNS。私網、loopback、LAN 只有私人 profile 的 `allow_private_network: true` 才開放。公開設定不能帶這個旗標。
- `config/source-capabilities/` 每個 adapter 一份。support 只有 `partial`、`unsupported`、`not_applicable`、`not_observable`。probe 只有 `documented` 或 `untested`。沒有 `live_pass`。`permits.offchain_unused` 維持 `not_observable`。Etherscan 不把 opBNB 列進來（API 支援 2026-10-12 停止）。Sui 不用 JSON-RPC。TON Center v2 與 v3 的 backend 都是 `toncenter`。

還沒接上的東西：

- 沒有鏈 adapter，沒有餘額、歷史、權限或風險的解碼。能力檔裡的 `partial` 只表示這個來源以後打算讀那一格，不是這次已經讀到。
- gRPC 只有方法名稱的 allowlist，沒有 gRPC 客戶端，也沒有新的相依套件。
- 沒有任何 live 來源驗證。fixture 通過不是節點通過。缺 key 仍是 BLOCKED，不是已驗證，也不是沒有資產。

## 本包明確沒做的事

- 沒有修改 `action-client/`、`.gitlab-ci.yml` 或部署設定。
- 沒有把新鏈接到 `guardian.cli:dispatch` 或 `guardian.service:run_once`。
- 沒有 live probe，沒有 SKIP 充當 PASS，沒有把 `planned_not_live_verified` 說成可用。
- 私人地址、key、通知收件人與狀態值只留在操作者機器。這份契約不記載它們。

## W3 實作狀態

W3 在這個分支加上事件、能力 coverage 與原子游標。鏈 adapter 還沒有。沒有任何來源被 live 驗證。`run-once`、`SnapshotCycle`、Lista 與 CLI 的行為沒有改。

現在有的東西：

- `guardian.events` 把一筆交易收成鏈事件。身份是 tenant、wallet_id、network、tx_id、sub_index、kind、asset 的 sha256，不含區塊雜湊，所以同一筆再送不會變成第二筆。失敗的交易（EVM status `0x0`，或 Solana `meta.err`）不會產生資產或授權的成功事件；實際付出的手續費可以記成 `fee`，另外有一筆 `tx_failed`。`tx_status` 是 unknown 的事件不能當成成功告警。
- 最終性跟各鏈自己的政策走，不套用固定 12 個確認。Ethereum 看 finalized tag。Solana 看 commitment；那個 finalized 只是 commitment 的名字，不是不可逆保證。Bitcoin 的確認數只到政策門檻的 confirmed，不會標成絕對 finalized。來源鏈 finalized 不是跨鏈完成；完成要目的鏈的成功事件，退款是 refunded。
- 重組不默默刪事件。舊區塊雜湊已經不是 canonical、而且這次沒有重新觀測到的事件標成 orphaned，並留下 correction。重新包含的同一身份是更新，不是第二筆。亂序和正序得到同一組結果。`rollback_to` 只把該高度以上標成 orphaned。
- `guardian.coverage.build_coverage` 對每個啟用的錢包、每條啟用的鏈、能力目錄裡的每一格各寫一列，不是每個鏈族一列。沒有觀測是 unknown，reason 是 `not_queried`。`gaps` 是空的但 `gap_detection_complete` 是 false，不算完整。`permits.offchain_unused` 維持 unknown，不能 complete。只有 `balances.native` 完整不是完整資產組合，reason 是 `native_only_not_portfolio`。價格沒有觀測就是 unknown，不是 0。錯誤讀取保留原來的 `last_success_at`。這些列用 `guardian.jsonschema_lite` 對 `schemas/coverage.schema.json` 檢查。缺的列是 unknown，不是安全，也不是零。
- `guardian.monitoring.reconcile_capabilities` 按能力替換。原生餘額成功不會刪掉失敗的代幣列；失敗的代幣列留成 STALE，舊的 raw amount 還在，不會寫成 0。empty、error、partial 三種結果分開。`SnapshotCycle` 維持原樣。
- `migrations/monitor/0002_mcm_events_coverage.sql` 加上 `chain_events`、`capability_state`、`capability_cursors`。`PRAGMA user_version` 仍是 1。`commit_capability_cycle` 在同一個 `BEGIN IMMEDIATE` 裡寫事件、能力狀態、游標和 outbox。中途失敗全部回滾，游標不會先往前。游標不能倒退，除非走明確的 rollback。告警仍走既有的 dedup。

還沒接上的東西：

- 沒有鏈 adapter，沒有餘額、歷史、權限或風險的解碼。coverage 是分母：沒列到的就是 unknown，不是安全，也不是零。
- 沒有任何 live 來源驗證。fixture 通過不是節點通過。probe 不會是 live_pass。缺 key 仍是 BLOCKED，不是已驗證，也不是沒有資產。

## W4 實作狀態

W4 在這個分支加上 T0 七條 EVM 的資產與歷史讀取。上面 W3「鏈 adapter 還沒有」是 W3 當時的範圍。從這一節起，EVM 讀取在這個分支，而且只靠合成 fixture。沒有任何來源被 live 驗證。`run-once`、`SnapshotCycle`、Lista 與 CLI 的行為沒有改。鏈登錄的 `enabled` 仍是 false，`status` 仍是 `planned_not_live_verified`。

現在有的東西：

- `guardian.sources.etherscan` 經既有的唯讀 transport 呼叫 Etherscan V2 `GET /v2/api`。module/action 只走 W2 的 allowlist。API key 由呼叫端從環境變數名稱 `ETHERSCAN_API_KEY` 解出來，放在 query 的 `apikey`，不寫進 repr；錯誤文字會遮罩。`No transactions found` 是該次查詢的空結果。`Max rate limit reached` 會重試。免費方案不支援該鏈是 BLOCKED（`plan_not_supported`），不是空錢包，也不是 0。`page * offset` 超過 10000 就把區塊範圍對半切；單一塊仍然放不下就留下 `single_block_over_limit:<block>`，`pagination_complete` 是 false。失敗或半套的讀取不推進 cursor。
- `guardian.sources.blockscout` 讀 `/api/v2/addresses/{address}/transactions`、`token-transfers`、`internal-transactions`，用 `next_page_params` 翻頁。路徑在 W2 的 `/api/v2/addresses/` 前綴裡。這是第二層來源，backend 是 `blockscout`。
- `guardian.sources.nodereal` 只對 `ethereum-mainnet` 與 `bsc-mainnet` 呼叫 `nr_getAssetTransfers`。區塊跨度超過 100000 會切開。`maxCount` 是 `0x3E8`。後續頁用既有的 `pageKey` walker。`external`、`internal`、`20`、`721`、`1155` 分開對到各自的能力，不併成一格。
- `guardian.chains.evm` 在記任何餘額或事件之前，用 `eth_chainId` 對登錄的 `evm_chain_id`。對不上是 `chain_id_mismatch`，什麼都不記。原生餘額來自 `eth_getBalance`，金額是 wei 的整數字串。`history.native_external`、`history.native_internal`、`history.fungible`、`history.nft` 各是一格。只有 RPC 時，`eth_getLogs` 可以在有限區塊範圍內讀代幣歷史；原生地址歷史沒有來源，那兩格是 `rpc_has_no_address_history`，不會 complete。只有原生餘額成功時，`portfolio_summary` 不會把資產組合標成 complete。收據的 `0x1` / `0x0` 交給 `effects_from_tx`：失敗的交易沒有資產成功事件。手續費是 `gasUsed * effectiveGasPrice`；OP Stack（OP Mainnet、Base）有 `l1Fee` 就加進去，沒有就標 `fee_incomplete`，不補一個假的 L1 費用。ERC-20/721/1155 的 log 依 topic 解。mint、burn、batch、自轉、重複 log、fee/rebase 的金額差分開處理。解不出來的 topic 是 unknown，不猜，也不寫成 0。大於 2^53 的金額維持整數。小數 18/6/8 用 `amounts` 的整數換算。

還沒接上的東西：

- 沒有 live 來源驗證。fixture 通過不是節點通過。沒有 `live_pass`。缺 key 仍是 BLOCKED，不是已驗證，也不是沒有資產。
- 真的要打來源時，操作者自己提供 `ETHERSCAN_API_KEY`、各鏈的 EVM RPC endpoint，以及選用的 NodeReal endpoint。公開設定只放環境變數名稱，不放 key，也不放帶 key 的 URL。
- 權限與 permit 是 W5，不在這包。

W4 審查補充：
- Etherscan V2 實際的空結果是 `{"status":"0","message":"No transactions found","result":[]}`（文字在 message、result 是空陣列）。W2 的分類原本只認 result 是字串，會把它當 `partial`；已修正為 `empty`（該查詢範圍內完整），非空陣列仍是 `partial`。
- 2026-10-08 審查者在 box 上的唯讀 smoke（只查公開合約，key 遮罩，結果未寫入設定）：Ethereum 的 txlist／tokentx 正常回 ok／empty；用同一把免費 key 查 BSC 回 `plan_not_supported`（BLOCKED，不是空錢包）。這不是 live_pass，能力檔的 probe 仍是 untested。

## W5 實作狀態

W5 在這個分支加上 EVM 權限與已經上鏈的 permit。只靠合成 fixture。沒有任何來源被 live 驗證。`run-once`、`SnapshotCycle`、Lista 與 CLI 的行為沒有改。鏈登錄的 `enabled` 仍是 false，`status` 仍是 `planned_not_live_verified`。`config/source-capabilities/` 沒有改，沒有 `live_pass`。

現在有的東西：

- `guardian.chains.evm.permissions` 把 Approval 分成 ERC-20、ERC-721 的單一代幣、ERC-721 operator、ERC-1155 operator。`ApprovalForAll` 的簽名兩邊共用。只有呼叫端給了 ERC-165 `supportsInterface`，或 W4 的轉帳種類證據，才歸到 721 或 1155。兩邊都說支援、或沒有證據，是 `operator_unknown_standard`，不猜。owner 取索引 topic，不取 `tx.from`。變更是 new、increased、decreased、extended、revoked、consumed、first_observed。沒有前態是 first_observed，不會說成剛新增。額度等於 2^256-1，或大於等於 2^255，標 unlimited。`eth_call` 的目前額度和事件不一致時，以目前狀態為準，reason 是 `event_state_mismatch`。目前讀取失敗時狀態是 unknown，raw amount 留空，不會說成已撤銷、零或安全，cursor 留在上一次。`transferFrom` 造成的下降是 consumed，不是使用者撤銷。失敗的收據（status `0x0`）經 `effects_from_tx` 不會產生授權成功事件。餘額是 0 但仍有效、而且 spender 沒有標籤或標成風險的額度會留下，標 `zero_balance_exposure`。沒有標籤是 unlabeled，不是 safe。
- 歷史不完整（分頁沒完、有 gap、讀取失敗、log 壞掉）時，coverage 帶 `history_incomplete_permissions_unknown`，不會 complete。沒有看到授權但歷史不完整，摘要是這個 reason，不是「沒有授權」，也不是安全。失敗的讀取不推進 cursor。
- `guardian.chains.evm.permits` 把已提交的 ERC-2612 和 DAI permit 分開。兩者都要有結果 Approval，加上 `nonces(owner)` 增加。提交者可以是 relayer，也可以包在 multicall 裡。owner 來自 permit 參數和 Approval topic，不是 `tx.from`。這不是簽章的密碼學驗收，只是已上鏈 calldata 的形狀。Permit2 分成兩層：代幣對 Permit2 合約的 ERC-20 授權（`permissions.erc20`，spender 是 Permit2），以及 Permit2 對 spender 的 AllowanceTransfer（amount、expiration、nonce，`permit2.allowance`）。過期不是有效。Lockdown 而且目前額度確認是 0，才是 revoked。SignatureTransfer 的已用 nonce（`UnorderedNonceInvalidation` 或 nonceBitmap 裡設上的 bit）只記在 `permit2.signature_usage`，`persistent_allowance` 是 false。沒設上的 bit 不會被列成未使用簽名。
- `permits.offchain_unused` 永遠是 support `not_observable`、quality `unknown`、reason `offchain_signature_not_observable`。沒有函式會對它回 safe 或 none。看到一筆已上鏈 permit 也不會讓未提交的簽名變成可觀測。
- `config/deployments/permit2/` 對 T0 七條 EVM 各一份。地址是公開的 Permit2（寫在那七份設定裡，不另抄進這份文件），status 是 `documented`，來源是 https://github.com/Uniswap/permit2 ，retrieved 是 2026-10-08。本包沒有在鏈上核對這個地址。

還沒接上的東西：

- 沒有 live 來源驗證。fixture 通過不是節點通過。probe 維持 untested。缺 key 仍是 BLOCKED，不是已驗證，也不是沒有資產。
- 沒提交的簽名看不到。缺歷史不是「沒有授權」。
- 這包不編碼 approve、revoke、permit、increaseAllowance，也不簽名、不廣播。讀取只用 allowance、isApprovedForAll、getApproved、supportsInterface、balanceOf、nonces、Permit2 allowance 與 nonceBitmap 這些 view。

W5 審查補充：
- 2026-10-08 審查者在 box 上的唯讀 smoke（公開 RPC、不用 key，只查公開的代幣合約與 Permit2，結果未寫入設定）：Ethereum 上 Permit2 地址有 bytecode；`allowance`、Permit2 `allowance`、`nonceBitmap` 這幾個 view 經 W2 transport 正常回 ok 並能解碼；`permits.offchain_unused` 仍是 not_observable；試圖編碼 `approve` 被拒。這不是 live_pass，Permit2 設定的 status 仍是 `documented`，probe 仍是 untested。

## W6 實作狀態

W6 在這個分支加上 Solana 資產與權限讀取。只靠合成 fixture。沒有任何來源被 live 驗證。`run-once`、`SnapshotCycle`、Lista 與 CLI 的行為沒有改。`config/chains/solana-mainnet.json` 的 `enabled` 仍是 false，`status` 仍是 `planned_not_live_verified`。`config/source-capabilities/` 沒有改，沒有 `live_pass`。

現在有的東西：

- `guardian.chains.solana` 經既有的唯讀 transport 呼叫 Solana JSON-RPC。方法只走 W2 的 allowlist：`getBalance`、`getTokenAccountsByOwner`（SPL Token 與 Token-2022 各一次，encoding `jsonParsed`）、`getSignaturesForAddress`、`getTransaction`（`jsonParsed`、`maxSupportedTransactionVersion` 0、明確的 commitment）、`getAccountInfo`、`getMultipleAccounts`、`getGenesisHash`、`getSlot`。commitment 是參數：預設 `confirmed`，`finalized` 允許，`processed` 只當成 observed。不會假設 finalized。記任何餘額之前用 `getGenesisHash` 對登錄的 expected value。對不上是 `genesis_mismatch`，什麼都不記。`sendTransaction`、`simulateTransaction`、`requestAirdrop` 在到達 backend 之前被拒。這包不編碼 approve、revoke、closeAccount、setAuthority，也不簽名、不廣播。
- owner 的 token account 集合同時枚舉兩個 token program。歷史是錢包自己的簽名，加上每一個持有中的 token account 的簽名。owner 不在 `accountKeys` 的 SPL 入帳仍由 token account 的簽名抓到。先前看過、現在不見的帳戶是 `token_account_closed_history_gap`。讀不到歷史是 `token_account_history_unknown`。分頁或預算碰到上限是 partial，cursor 不超過已經完整讀完的範圍。失敗或被擋的讀取保留上一次的 cursor，餘額不寫成 0。只有原生餘額成功時，`portfolio_summary` 不會把資產組合標成 complete，reason 是 `native_only_not_portfolio`。
- legacy 與 version 0 都解。v0 的 account keys 是靜態 key 加上 `meta.loadedAddresses`（writable 再 readonly），所以 ALT 載入的 key 算進去。`innerInstructions` 算進去。`meta.err` 不是 null 的交易是失敗：只有手續費，沒有代幣或授權效果。`meta` 或 transaction 是 null 是 unknown，不是成功，也不是失敗。不支援的版本是 unknown，reason 是 `unsupported_transaction_version`，不默默解析。同一筆簽名在 confirmed 與 finalized 的 finality 不同。後來找不到的交易不算 final。
- 權限是 `permissions.spl_delegate` 與 `permissions.spl_authority`。目前 `jsonParsed` 帳戶記 delegate、delegatedAmount、closeAuthority、owner、state；mint 記 mintAuthority、freezeAuthority。指令只解已經回來的 `jsonParsed`：approve、approveChecked、revoke、setAuthority（authorityType 是 accountOwner、closeAccount、mintTokens、freezeAccount）、closeAccount。前態未知是 `first_observed`，不會說成剛新增。delegate 從舊換成新，即使額度相同也是 replaced。目前狀態和指令歷史不一致時以目前狀態為準，reason 是 `event_state_mismatch`。目前讀取失敗是 unknown，不會說成已撤銷、零或安全，cursor 留在上一次。已知的 Token-2022 extension 至少包含 permanentDelegate、transferHook、mintCloseAuthority、defaultAccountState、nonTransferable、transferFeeConfig、transferFeeAmount、immutableOwner、cpiGuard、memoTransfer、metadataPointer、tokenMetadata、interestBearingConfig，以及 `confidentialTransfer` 開頭的名字。任何未知 extension 是 `token2022_unknown_extension`，coverage 不 complete，不會略過。結構旗標（外部 delegate、外部 close authority、permanent delegate、有 freeze authority、未標籤的 delegate）和呼叫端給的惡意標籤分開。沒有標籤是 unlabeled，不是 safe。帳戶關閉是 closed，可能是租金回收，不是失竊。
- lamports 用 pre/postBalances 與 pre/postTokenBalances 的整數金額，不用 `uiAmount` 浮點。小數 9、6、0 都留著。大於 2^53 維持精確整數。手續費只向 fee payer 算一次，不另外算成一筆轉帳。建立帳戶存進去的租金，和 closeAccount 退回的租金，標成 rent，不是收入或損失。wSOL wrap（SOL 轉進 wSOL 帳戶再加上 syncNative）和 unwrap（關掉 wSOL 帳戶）不會同時算成 SOL 移動和代幣移動。Token-2022 的 transfer fee 是 received = sent - fee，分開記錄。
- `guardian.sources.helius` 是可選的補充。key 由呼叫端從環境變數名稱 `HELIUS_API_KEY` 解出來，放在 query 的 `api-key`，不寫進 repr；錯誤文字會遮罩。缺 key 或空 key 不建立 client，結果是 BLOCKED，reason 是 `credential_missing`，probe 是 blocked，不是 empty、不是 safe、不是沒有資產，也不 complete。POST 只走 `/v1/parsed-events/transaction-history` 與 `/v1/parsed-events/transactions`。`processed` commitment 拒絕。401 與 403 是 blocked。429 依既有 retry 重試，之後仍是 partial。壞掉的回應是 error。Helius 的結果不會自己把 RPC coverage 標成 complete，也不覆蓋鏈上解析出來的整數金額。沒有 Helius 時，RPC 路徑仍可用：無 key 的公開 RPC，或使用者自己的 `SOLANA_RPC_URL`。缺 `SOLANA_RPC_URL` 是 blocked，不是沒有資產。公開 RPC 是低流量的後備，不是完整歷史。Helius 免費 key 由使用者自己在 Dashboard 申請。本包不自動註冊、不替使用者付款、不升級套餐。
- `permits.offchain_unused` 維持 not_observable。還沒提交的 Solana 離線簽名不可觀察，不能標成 complete。

還沒接上的東西：

- 沒有 live 來源驗證。fixture 通過不是節點通過。沒有 `live_pass`。鏈登錄的 `enabled` 仍是 false。
- 真的要打來源時，操作者自己提供 `SOLANA_RPC_URL`，以及選用的 `HELIUS_API_KEY`。公開設定只放環境變數名稱，不放 key，也不放帶 key 的 URL。
- 未知的 Token-2022 extension 不會被當成完整。離線 Solana 簽名看不到。缺歷史不是「沒有授權」，也不是安全。

W6 審查補充：
- 增量掃描原本永遠不帶 `until`（條件同時要求 cursor 是 32 bytes 地址和 64 bytes 簽名，永遠不成立），每輪重走全部歷史；已修正為只要 cursor 是簽名就當 `until`。不會漏事件，但活躍錢包原本會一直撞頁數上限而標 partial。
- W2 transport 的 POST 原本沒有送 `content-type: application/json`。EVM 公開節點接受，但 Solana RPC 回 415，所以 live 讀取全部會失敗（誠實地標 error，不是空）。已補上；GET 不受影響。
- 2026-10-09 審查者在 box 上的唯讀 smoke（公開 Solana RPC、不用 key、只查公開的 wSOL 與 USDC mint，結果未寫入設定）：getGenesisHash 與設定相符；mint 讀取能解析 decimals 與 mint/freeze authority；公開節點對部分方法回 429/403 時結果是 partial，cursor 不前進；沒有 HELIUS_API_KEY 時是 blocked／credential_missing；sendTransaction、simulateTransaction、requestAirdrop 在 transport 前被拒。這不是 live_pass，probe 仍是 untested。

## W7 實作狀態

W7 在這個分支加上行情與風險標籤的程式庫。只靠合成 fixture。沒有任何來源被 live 驗證。`run-once`、`SnapshotCycle`、Lista 與 CLI 的行為沒有改。鏈登錄的 `enabled` 仍是 false，`status` 仍是 `planned_not_live_verified`。`config/source-capabilities/` 沒有改，沒有 `live_pass`。W2 的 path allowlist 沒有改。前面各節說「還沒有風險解碼」是那個工作包當時的狀態；這一包加上 `guardian.pricing` 與 `guardian.risk`，沒有接到排程或快照。

現在有的東西：

- `guardian.pricing` 的價格鍵是 `(network_id, 資產身份)`。身份是該鏈的原生身份（例如 `solana-native:SOL`）或正規化後的合約／mint。symbol 與 name 只供顯示，不用來查價，也不能借用別的合約的價格。同一符號的另一個合約沒有價格。不把穩定幣寫成 1 USD。CoinGecko 只走既有 transport 的 GET：原生資產是 `/api/v3/simple/price`（`ids`、`vs_currencies=usd`、`include_last_updated_at=true`），合約是 `/api/v3/simple/token_price/{platform}`（`contract_addresses`，同樣的計價參數）。不傳 `symbols` 或 `names`。八條 T0 的 platform id 與 native coin id 在 `config/risk-sources.json`，每筆有來源 URL、retrieved `2026-10-09`、status `documented`。Arbitrum、Base、OP Mainnet 的 gas 資產用 coin id `ethereum`。Polygon 用 `polygon-ecosystem-token`。Avalanche 的 platform id 是 `avalanche`，coin id 是 `avalanche-2`。這些 id 是文件上的對照，不是 live 核對。
- 可選的 `COINGECKO_API_KEY` 只放在 query `x_cg_demo_api_key`。沒有 key 不是錯誤，仍可查。不用 `x_cg_pro_api_key`。repr 與錯誤文字不包含 key。提供者的 JSON 數字在邊界用 `Decimal(str(value))` 轉一次。鏈上金額維持整數。`value_usd` 是 `Decimal(raw) * price / 10**decimals`。缺價格、null、回應裡沒有這個合約、429、401、403、壞掉的內容、預算用盡，價格是 None，valuation 是 `valuation_unknown`，不會變成 0，也不會變成 1 USD。`last_updated_at` 比 `max_age_seconds`（預設 3600）更舊是 `stale`；缺時間或未來時間是 `unknown`。年齡等於上限仍是 `available`。過期價格可以帶著年齡顯示，但 `threshold_decision` 是 `valuation_unknown`，不觸發也不壓掉美元門檻。門檻是至少 1000 USD：999.99 是 `below`，1000 是 `above`。浮點輸入是 `valuation_unknown`。呼叫端或提供者標了 `low_liquidity`／`unverified` 的價格只供顯示，不觸發美元警報。
- `guardian.risk.goplus` 的支援表按函式分開：`token_security`、`address_security`、`approval_security`、`solana_token_security`。每一個都有 GoPlus 文件 URL、retrieved `2026-10-09`、status `documented, not live verified`。表上沒有的函式／鏈是 `unsupported`，不發請求。Solana token security 的路徑是 `/api/v1/solana/token_security`，和 EVM 的 address security（`/api/v1/address_security/{address}`）不是同一個函式。回應要 `code` 是整數 1 而且 result 裡有查詢的那個合約或地址。`code` 不是 1（含字串 `"1"`、布林 true、0）、缺 result、429、401、403、壞掉的內容、預算用盡，是 `unknown`，不是 safe。字串旗標只認 `"0"` 和 `"1"`。`"0"` 是這個函式沒標，不是安全。整數 0／1 不是字串旗標，狀態是 `unknown`。GoPlus 的授權要放在 header，這個 transport 沒有 header。設了 `GOPLUS_API_KEY` 時回報 `credential_transport_unsupported`，key 不送出、不進 repr；無 key 的讀取仍會進行，credential 狀態是 `keyless`。
- `guardian.risk.ofac` 只吃呼叫端給的本機 SDN XML（bytes 或路徑），不下載。內文出現 `<!DOCTYPE` 或 `<!ENTITY`（大小寫不拘）就在解析前拒絕，reason 是 `dtd_or_entity_rejected`，識別碼是空的。UTF-16 BOM 是 `unparseable`。用 `xml.etree.ElementTree` 取出 `Digital Currency Address - <TICKER>`，帶 entry uid、ticker，以及表頭的發布日期與版本（`publshInformation`／`Publish_Date`，也接受 `publishInformation`）。比對前先按鏈族正規化：EVM 用小寫 hex 比較、顯示用 checksum；Solana 與 BTC legacy 的 base58 區分大小寫；bech32 用小寫。不做前綴、後綴或模糊比對。ETH 對到 `config/chains` 裡每一條 EVM（T0 加上 linea、zksync-era、scroll、mantle、sonic、unichain、hyperevm、blast、opbnb）。SOL 只對 solana-mainnet。XBT 與 BTC 只對 bitcoin-mainnet。TRX 只對 tron-mainnet。護照等其他 idType 忽略。兩個版本的差會留下新增與刪除；刪除的狀態是 `delisted`，證據是兩個版本，不會默默丟掉。版本缺、相同，或清單沒解析成功，差集是不可比，不會假造一筆撤回。清單缺失或解析失敗，制裁狀態是 `unknown`，不是 no hit。發布日比 `max_age_days`（7）更舊：命中仍是 `hit` 並且 `stale` 為 true，避免把制裁藏掉；未命中是 `stale`，不是 `no_hit`。年齡等於上限仍算當前。沒有日期或未來日期是 `unknown`。
- `guardian.risk.chainabuse` 不呼叫 transport。有 key 是 `credential_transport_unsupported`，沒有 key 是 `credential_missing`。`reports` 是 None，`requested` 是 false，`safe` 是 false。不會回傳空的報告清單，也不會把地址說成沒有報告。社群報告標籤只來自呼叫端提供的證據。
- `guardian.risk.labels` 的種類順序是 `official_sanction`（OFAC 精確命中）大於 `official_exact`（其他有文件的官方名單）大於 `community_report` 大於 `structural`（GoPlus 字串旗標 `"1"`、授權或權限結構）大於 `heuristic`（地址投毒）。每一筆帶 source、function、名單版本／日期、retrieved_at、evidence ref、命中的識別碼。較高的種類不會清掉較低的。摘要列出最高種類和全部標籤。沒有標籤時狀態是 `no_hit_in_checked_sources`，同時列出查過的來源，以及 unknown、unsupported、blocked、stale 的來源。不會寫成 clean 或 safe。某個來源後來失敗，先前的標籤留著並標 stale，不會被清成沒有命中。投毒：發送者與已知對手的 40 hex 前 4 與後 4 相同、地址不同，而且金額是 0 或呼叫端標了 dust，才在發送者上標 heuristic `poisoning_suspect`。受害接收者與真正的對手不標惡意。自己轉給自己、只有前綴相同、或大額轉帳，不標。這筆 dust 的顯示價格即使門檻是 `above`，也不觸發美元警報（`valuation_unknown`）。這些標籤不是法律結論，也不是制裁認定。

還沒接上的東西：

- 沒有 live 來源驗證。fixture 通過不是 CoinGecko、GoPlus 或 OFAC 檔案通過。沒有 `live_pass`。鏈登錄的 `enabled` 仍是 false。`config/source-capabilities/` 沒有改，probe 仍是 untested。
- 這包沒有接到 `run-once`、`SnapshotCycle`、coverage 列或 CLI。既有的 OFAC CSV `screen_ofac` 沒有改。DeBank 沒有做。
- 真的要查價時，操作者自己提供 `COINGECKO_API_URL`，以及選用的 `COINGECKO_API_KEY`。GoPlus 用 `GOPLUS_API_URL`；`GOPLUS_API_KEY` 在這個 transport 上送不出去。OFAC 名單由操作者放在本機，本包不下載。Chainabuse 的 header 憑證同樣送不出去，所以不會發出報告查詢。公開設定只放環境變數名稱，不放 key，也不放帶 key 的 URL。
- 缺價格不是 0，也不是 1 USD。過期價格不驅動美元門檻。旗標 `"0"` 不是安全。沒查到、不支援、被擋住、名單過舊，都不是清白。

W7 審查補充：

- 本包只新增 `pricing/`、`risk/`、`config/risk-sources.json`、`tests/fixtures/risk/`、`tests/unit/test_mcm_w7.py`，並在這份文件加這一節。沒有改 transport、policy、action-client、CI 或鏈登錄。
- 測試全程離線：socket 與 `httpx.Client` 被擋下，來源是 `*.example.invalid`，SDN XML 是合成的（假 uid、`0x000…0abc` 這類地址、假名字）。沒有真實的制裁地址，也沒有真實的代幣合約。
- Polygon 的 `polygon-ecosystem-token` 與 Avalanche 的 `avalanche-2` 來自公開文件對照，retrieved 是 2026-10-09，沒有對 live API 核對。GoPlus 的鏈 id 表同樣是文件對照，不是 live 核對。

W7 審查補充：
- GoPlus `token_security` 有幾個欄位是反向語意：`is_open_source`、`is_in_dex`、`is_true_token` 是 "1" 代表正常、"0" 才是風險；`trust_list` 只表示信任，不算風險旗標。原本一律把 "1" 當旗標，會把幾乎所有正常代幣標成 flagged，閉源合約反而不會被標。已修正並加測試。
- GoPlus token 與 Chainabuse 的認證要用 header，W2 的 `Operation` 沒有 header 欄位，這包誠實回 `credential_transport_unsupported`。後續（W9）要加的是：Operation 選用 `headers`，每個 adapter 只允許白名單內的憑證 header 名稱，值只能來自憑證、在 repr／log／錯誤裡遮罩，不能來自任務或回應資料，並要有測試。W9 已加上這個欄位；當時的 `credential_transport_unsupported` 不再是有 key 時的狀態。細節見 W9 實作狀態。
- 2026-10-09 審查者在 box 上的無 key smoke（只查公開的 USDT 合約，結果未寫入設定）：CoinGecko 公開端點回 429，價格是 `valuation_unknown`，不是 0 或 1；同 symbol 的未知合約沒有借到價格；GoPlus `token_security` 能回真實結果並解析旗標；EVM 函式用在 Solana 是 unsupported，而且沒有發出請求。這不是 live_pass。

## W8 實作狀態

W8 在這個分支加上協議部位與曝險新聞的程式庫。只靠合成 fixture。沒有任何協議合約、Stake 帳戶或新聞來源被 live 驗證。`run-once`、`SnapshotCycle`、Lista 與 CLI 的行為沒有改。既有的 `guardian.providers.read_lista_state` 與 `config/protocols/lista-bsc.json` 沒有改。鏈登錄的 `enabled` 仍是 false，`status` 仍是 `planned_not_live_verified`。`config/source-capabilities/` 沒有改，沒有 `live_pass`。沒有新增 Python 依賴。沒有改 transport、policy、action-client 或 CI。W2 沒有新聞或 RSS adapter，這一包也不加。

現在有的東西：

- `guardian.protocols` 的插件是程式裡的靜態登錄（`aave_v3`、`erc4626`、`lista_slisbnb`、`solana_stake`）。manifest 只能選這些 id。未知 id、模組路徑，或文件裡的 `entrypoint`／`import`／`exec`／`module`／`class`，都會拒絕。view 方法必須和該插件程式裡的 allowlist 完全一致。`claimWithdraw`、`withdraw`、`deposit`、`repay`、`redeem` 這類寫入名稱在編碼前就拒絕，不會送出 calldata。`PluginContext` 只有 allowlisted `eth_call`、Solana 帳戶讀取、時鐘，以及呼叫端給的 prior。context 上沒有 transport，也沒有簽名或寫入路徑。
- 讀取結果是 `observed`、`none_observed` 或 `unknown`。`none_observed` 只代表每一次必要讀取都成功而且是空的或零。失敗、被擋、格式壞掉、缺來源、鏈 id 或 genesis 對不上，都是 `unknown`，cursor 留在 prior，不會變成沒有曝險，也不會把部位寫成 0。未知插件會寫進 coverage，reason 是 `unknown_plugin:<id>`，quality 是 error，不是 complete。
- Lista slisBNB 只讀 `paused`、`getUserWithdrawalRequests`、`getUserRequestStatus`、`balanceOf`、`convertSnBnbToBnb`。`isClaimable` 只來自 `getUserRequestStatus` 的 bool，不用 7 天時鐘推。請求從清單消失，沒有成功的 claim 證據，是 `disappeared_unverified`，不是 claimed。失敗的 claim 交易也不算領取。流動的 slisBNB 是 receipt；`convertSnBnbToBnb` 是這張 receipt 的 underlying quote，組合只算一次。未完成提款的 BNB 用 `lista-slisbnb:withdrawal:<request id>`，和錢包原生 BNB 分開。暫停仍回報部位，並加上 `protocol_paused`。超過 32 筆是 `result_exceeds_bound`，不把部分清單當完整。
- Aave v3 讀 Pool.`getUserAccountData`。健康因子留 1e18 整數，不做浮點。呼叫端有給 reserve 清單時，抵押是 aToken receipt、債務是 variable debt，base currency 總額只放在 metrics，不疊在代幣腿上。清單沒給而且總額不是零，才用 8 位小數的 `aave-v3:collateral-base` 與 `aave-v3:debt-base`，reason 是 `reserves_not_supplied`。清單給了但代幣餘額全是 0、base 總額卻不是 0，是 `reserves_incomplete`，不是零部位。債務是單獨的負項，不先淨額再減一次。
- ERC-4626 的公開登錄是空的。沒有呼叫端提供的 vault 是 `vaults_not_configured`，不是 `none_observed`。shares 是 receipt，`convertToAssets` 是 quote，`asset()` 給 underlying 身份。小數沒在私人設定裡給就是 None，不假設。
- Solana stake 用呼叫端給的帳戶，或對 Stake program 做 `getProgramAccounts`。memcmp 只允許文件上的 staker offset 12 與 withdrawer offset 44（4-byte Borsh tag，接著 Meta.rent_exempt_reserve u64，再來 Authorized.staker 與 withdrawer）。這兩個偏移是 StakeState 文件布局，沒有在這包 live 核對。其他 program 或其他 offset 在 RPC 前拒絕。commitment 是 `confirmed`，不是 finalized。lamports 是整數；浮點 lamports 或浮點 delegation 是 malformed，不是 0。`warmupCooldownRate` 忽略。delegation stake 只放在 detail，不另算一筆 SOL。委託大於 lamports 是 `unknown`。receipt 是 stake account，lamports quote 重複那把 pubkey，所以不和錢包原生 SOL 混在一起。沒給帳戶也沒給 authority 是 `stake_accounts_not_provided`。
- 組合把 receipt 和它的 underlying quote 只算一次。W4 裡同一張 aToken、slisBNB、vault shares 或 debt token 的餘額不再加一次。錢包原生 BNB／SOL 是另一個身份，照算。任一計入的腿沒有新鮮、可用、非 display_only 的價格，或任一插件是 unknown，美元合計是 None／`valuation_unknown`，不把缺的價格當 0。
- 跨鏈用既有的 `FlowStatus`。來源交易成功只到 `source_confirmed` 或 `source_finalized`，沒有目的地證據不是 completed。逾時是 `timed_out`，不是 failed，也不是 lost。插件沒有資料是 `unknown`，reason `plugin_without_data`，即使來源已經 finalized。
- `guardian.news` 只解析呼叫端已經拿到的 RSS 2.0、Atom 或結構化項目。不下載。X 帳號的 fetch 是 `manual_not_fetched_by_monitor`。其他 feed 是 `not_fetched_by_monitor`。沒有 API key，沒有付費新聞源。`<!DOCTYPE` 或 `<!ENTITY` 在解析前拒絕。URL 會把 host 小寫、去掉 fragment 和 `utm_*`／`ref`。同一 canonical URL、48 小時內同一協議的正規化標題，或標成 correction／update 的連結，合成一則 incident，不另開一筆 alert。更正不會把時間窗往後滑。無關主題（例如標題或聲明裡的 79thVault）不繼承來源協議，內文提到 Lista、UXLINK 或 BNB 也不關聯。新聞只在該協議有 `observed` 部位，或有對到已登錄合約的 `active` 授權時才連到錢包。`revoked` 與 `expired` 不連。部位是 `unknown` 而且沒有有效授權時，每個錢包只出一筆 `exposure_unknown`，`loss_alert` 是 false，不是損失警報。沒有部位也沒有有效授權，就不通知。Kinza 與 UXLINK 沒有插件、沒有釘死的合約，所以光有新聞不會掛到錢包上。
- 公開合約地址寫在 `config/protocols/`，程式裡的 pin 必須和 manifest 一致，否則是 `contract_mismatch`，不會去呼叫 manifest 裡換過的地址。Aave Pool 來自 https://github.com/bgd-labs/aave-address-book 。Lista proxy 與 slisBNB 來自既有的 lista 文件。Stake program 是公開的 `Stake11111111111111111111111111111111111111`。retrieved 是 2026-10-09，status 是 `documented`，這包沒有上鏈核對。使用者的 vault、reserve token 與 stake account 只來自私人設定，公開檔沒有真實監控地址。

還沒接上的東西：

- 沒有 live 來源驗證。fixture 通過不是節點通過，也不是新聞訂閱通過。沒有 `live_pass`。鏈登錄的 `enabled` 仍是 false。
- 新聞的即時抓取要等以後的 W2 news adapter。這一包只解析呼叫端給的 bytes。X 帳號維持人工，監控不會自己去抓。
- 這包沒有接到 `run-once`、`SnapshotCycle` 或 CLI。未知不是沒有曝險。地址是 documented，不是 on-chain verified。Stake memcmp 的 12 與 44 也沒有 live 核對。

W8 review fixes:
- Aave v3: when `getUserAccountData` reports collateral or debt that the supplied reserve list does not explain, the plugin returns `unknown` (`reserves_incomplete`), never a partial `observed`.
- Known gap (W9): Kinza and UXLINK are listed news sources but have no plugin and no pinned contract or token yet, so their incidents link to no wallet. Exposure stays unobserved until W9 adds pins from private config. This is a coverage gap, not "no exposure".

## W9 實作狀態

W9 把第一次可交給別人跑的唯讀路徑接上。只靠合成 fixture 與離線測試。沒有 live 驗證，沒有自動切換，沒有把任何鏈登錄的 `enabled` 改成 true，也沒有 `live_pass`。`sources probe` 的身份可以是 `matched`，`probe` 仍是 `untested`。舊的 `run-once --config`、`SnapshotCycle` 與 Lista 路徑沒有改。沒有新增 Python 依賴。沒有具體的通知用戶端。

現在有的東西：

- `guardian.rules:AlertPolicy.from_public` 用公開門檻。原生 ETH 只對 `ethereum-mainnet`、`arbitrum-one`、`base-mainnet`、`optimism-mainnet` 的原生資產，用 wei 比較 0.5 ETH。`0.499` ETH 不警報，`0.5` ETH 警報。BNB、POL、AVAX、SOL 與任何代幣（含 symbol 為 ETH 的 wrapped）走 1000 美元，而且只要已計價的報價。缺價格是 `valuation_unknown`，不是 0 或 1。授權變更不論金額都警報；前態未知是 `first_observed`，不是 `newly_added`。撤銷是資訊。同一租戶自己錢包之間的轉帳合成一筆，淨額為 0 時不當成兩筆損失。第一次看到的範圍只出一筆計數摘要，不把回補歷史逐筆警報。之後沒有新事件就不出聲。`dedup_key` 穩定，重跑不會再插一列 outbox。
- `guardian.pipeline:run_capability_pipeline` 用注入的 backend。CLI 預設是 HTTP backend，測試用 fixture。EVM 先對 chain id，再讀原生餘額、有界的 token log 與授權。Solana 先對 genesis，再讀原生餘額與 token。缺來源或憑證是 blocked，不是零。第三方地址查詢在 `privacy.third_party_wallet_queries` 不是 `operator_approved_sources_only` 時不送出，覆蓋是 `third_party_denied`。部位沒有跑就是 `unknown`。價格沒有接上就是 `valuation_unknown`。失敗的讀取不推進 cursor。
- `guardian.delivery:deliver` 的影子模式不送、不改 sent。公開模式要是 `configured_sender`，私人 profile 要有 `single_sender_handoff: true`，而且呼叫端要注入 sender，才可能送。預設 `outbox_only` 是關的。租約在 `delivery_leases`，失敗次數在 `delivery_attempts`。舊例行已經通知的交易標成 `suppressed_legacy`。沒有 Telegram、Slack、email 或 webhook。
- `guardian.scheduler:Watchdog` 在失明時一筆、恢復時一筆。重複失明不另開。它不斷言可以關掉排程，也不刪狀態。`monitor plan` 的 `started` 是 false。
- `guardian.comparison:compare_with_legacy` 只讀舊狀態檔。輸出沒有原始地址。
- CLI 新增 `config validate`、`sources probe`、`run-once --mcm-config`、`coverage --mcm-config`、`export --public`、`compare-legacy`、`watchdog`、`monitor plan`。沒有新旗標時，舊指令的行為不變。操作步驟見 `docs/quickstart-monitor.md`。
- W7 後續：`Operation.headers` 只接受 header **名稱** 的 tuple（不能帶值）；每個 adapter 的白名單在 `CREDENTIAL_HEADERS`（目前只有 `goplus`／`chainabuse` 的 `authorization`）。值只來自已登錄來源的 `credential_env`，送出前格式化成 `Bearer` 或 `Basic base64(secret:)`，並用 `MaskedSecret` 遮罩。任務／回應資料不能變成 request header。缺 credential 時不發請求（`credential_missing`）。401／403 是 `credential_rejected`，不是 safe。GoPlus 的 `credential_status` 只有來源真的帶著憑證時才是 `credential_header`；環境有 key、來源沒登錄時是 `credential_not_registered`。GoPlus `/token` 鑄造不在這包，也沒有 live 驗證。

還沒做的：

- 沒有 live 來源，也沒有把監控切到這條路徑。舊例行要等私人 profile 的交接旗標與 sender 都接上之後，才停止送出。
- 新聞 RSS 沒有在這包下載。Kinza 與 UXLINK 仍沒有插件與釘死合約，新聞不會因此掛到錢包。
- 沒有具體 sender。`configured_sender` 但沒有注入 sender 是 `sender_not_configured`，列留在 pending。

## L0 實作狀態

L0 只把已經做好的讀取接到公開 CLI `guardian run-once --mcm-config --no-send`。沒有新鏈、沒有送出、沒有改 `delivery.py` 的語意，也沒有 live 驗證。fixture 通過仍是 `live_verified: false`，`live_pass` 不會是 true。地址只使用合成的 `0x000…0001` 形式。

| 缺口 | 狀態 | 證明 |
|---|---|---|
| G1 原生／代幣轉帳進 outbox | 已接上。本輪新讀到的列變成候選，經 `AlertPolicy` 後寫進既有 outbox。缺價格是 `valuation_unknown`，不是 0 美元。 | `test_mcm_live_l0_01_second_round_material_transfer`、`test_mcm_live_l0_07_unknown_price_no_zero_and_no_double_count` |
| G2 基準輪之後的新授權，並從儲存的 cursor 續讀 | 已接上。授權不再只在基準輪產生候選。間隔大於一頁時分頁；這一輪蓋不滿就不推進 cursor。 | `test_mcm_live_l0_02_post_baseline_new_approval`、`test_mcm_live_l0_03_restart_cursor_pagination_gap` |
| G3 已設定且有憑證的 Etherscan indexer | 已接上。`history.native_external` 會呼叫 `account_history`。缺憑證仍是 `blocked`／`credential_missing`。 | `test_mcm_live_l0_04_configured_indexer_protocol_path` |
| G4 已設定插件的部位 | 已接上。適用的公開插件經 `run_plugin` 讀取。失敗是 `unknown` 加原因，不是空部位或零。 | `test_mcm_live_l0_04_configured_indexer_protocol_path` |
| G5 required 含非原生能力，watchdog 讀持久化的 run | 已接上。原生成功但 token／授權失敗是 `degraded`。消失的 required key 是失敗。`last_success_at` 只在全部 required 健康時前進。 | `test_mcm_live_l0_05_non_native_health_and_missing_required_scope` |
| G6 比較鍵是錢包加交易加位置加種類，並限在同一個窗口 | 已接上。輸出有 matched、legacy_only、mcm_only、awaiting_finality、uncomparable。沒有身份的舊列是 uncomparable。輸出遮罩交易與地址。 | `test_mcm_live_l0_06_wallet_event_window_comparison` |
| G7 送出租約比外部接受更長，本地崩潰後可能重送 | 已知缺口，留給 L4（`mcm/single-channel-delivery`，MCM-LIVE-L4-03／04／05）。本包不改 `delivery.py`。 | 文件紀錄，沒有改送出語意 |

基準輪只出摘要。同一鏈狀態重跑不新增 outbox 列。`test_mcm_live_l0_08_baseline_evidence_no_synthetic_live_pass` 鎖定基準標籤與 `live_verified: false`。

Reviewer 補充（L0 review）：

- 部位事件只在實質變化時產生：出現、消失、資產或債務腿改變、`collateral_base`／`debt_base`／腿數量相對上次參考狀態變動至少 10%，或 health factor 跨過 1.1／1.25／1.5／2 的區間。儲存的 prior 是上次參考狀態（基準或上次告警），所以利息慢慢累積到 10% 仍會告警一次。`test_mcm_live_l0_04_position_interest_drift_does_not_realert` 鎖定這條。
- watchdog 判定最新 run 過期的門檻是 2 倍輪詢間隔，跟 `last_success_at` 一致，避免獨立觸發的 watchdog 在下一輪跑完前誤報失明。
- `compare-legacy` 只比對有完整事件身份（錢包、鏈、交易、位置、種類、標的）的舊紀錄。現行舊例行的 state 只有 `alerted` 字串與 `latest_seen`，沒有 `events` 清單，所以對真實舊 state 比較時會全部落在 `uncomparable`。把舊例行輸出轉成事件列的工作屬於 L3（`ops/mcm-shadow-evidence`，legacy import／comparison 更新），正式 shadow 計數前要完成。

