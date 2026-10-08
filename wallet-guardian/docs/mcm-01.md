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
