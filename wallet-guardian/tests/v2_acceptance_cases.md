# v2 操作驗收案例（72）

這些是最低驗收案例，不是上線證明。離線測試見 pytest。

| ID | 說明 |
|---|---|
| X01 | 官方網站不通但鏈與合約正常，不推論被駭 |
| X02 | indexer 沒資料不等於資產為零 |
| X03 | RPC 故障只能切已核准備援 |
| X04 | WalletConnect relay 故障不假設換網站就好 |
| X05 | 依賴同一支 API 不算獨立備援 |
| X06 | 方法 pause 時禁止該動作 |
| X07 | 賣掉 LST 不是完成原贖回 |
| X08 | 停鏈沒有通用立即提款 |
| X09 | 缺 proof 就 BLOCKED |
| X10 | 懷疑漏洞時禁止盲簽 |
| X11 | 交易送到 proxy 不是 implementation |
| X12 | 未知 proxy 型別不猜測 |
| X13 | 實作程式碼變更使舊計畫失效 |
| X14 | verified source 不是審計或安全保證 |
| X15 | code hash 不符就阻擋 |
| X16 | selector 相同不代表可以改呼叫目標 |
| X17 | ERC-1967 可以解析 implementation |
| X18 | 實質變更產生新 revision |
| X19 | 每次規劃都重讀 idx |
| X20 | 快取 idx 與現況不同就拒絕 |
| X21 | 相同金額不能單獨當身份 |
| X22 | 上一筆之後必須重讀，不能沿用整批 idx |
| X23 | 身份不明確時停止 |
| X24 | 清單消失沒有領取證據不是完成 |
| X25 | Safe 部位不能用 owner EOA 冒充 |
| X26 | msg.sender 敏感呼叫不能包進其他合約 |
| X27 | watch-only 不是執行身份 |
| X28 | 未證明的帳戶類型保持 unproven |
| X29 | digest 不符拒絕 |
| X30 | 說明文字不能改金額 |
| X31 | 任意 hex 意圖拒絕 |
| X32 | 客戶端重建 calldata 必須相符 |
| X33 | 領取計畫不附帶 deposit 或 approve |
| X34 | claimWithdraw 的 value 為 0 |
| X35 | 餘額 state override 不是現況證據 |
| X36 | 關掉 pause 的 override 只是測試 |
| X37 | 預先授權 override 不是現況證據 |
| X38 | success=true 但沒核對效果就是不完整 |
| X39 | 本地 TTL 過期不能再提出錢包請求 |
| X40 | 本地 TTL 不會取消已送出交易 |
| X41 | 變更不會悄悄替換舊 revision |
| X42 | 沒有鏈上 deadline 必須揭露 |
| X43 | 未知費用不是免費 |
| X44 | 即將領到的資產不能當 gas |
| X45 | 授權額度用必要數量，不是無限 |
| X46 | 撤銷追不回已轉走資產 |
| X47 | Permit 簽名未開放 |
| X48 | eth_sign 與 personal_sign 拒絕 |
| X49 | 錢包拒絕不自動重試 |
| X50 | 交易雜湊只是送出證據 |
| X51 | 重新整理不另發交易 |
| X52 | 同一 revision 重按不重送 |
| X53 | 需要原子時不能悄悄改成分步 |
| X54 | capability ready 不自動升級 |
| X55 | 帳戶或鏈切換使計畫失效 |
| X56 | 手機不只能掃自己的 QR |
| X57 | 部分步驟成功是 PARTIAL |
| X58 | reorg 要發更正 |
| X59 | 完成必須核對 postconditions |
| X60 | 清單消失本身不是完成 |
| X61 | 原生幣變化要扣除 gas |
| X62 | 停寫之後讀取仍可用 |
| X63 | 代幣名稱裡的指令不執行 |
| X64 | 沒有曝險的新聞指令不告警 |
| X65 | metadata IP 請求被擋 |
| X66 | 導向 localhost 被擋 |
| X67 | 租戶看不到別人的計畫 |
| X68 | 救援包不含秘密 |
| X69 | Agent 不能提出錢包請求 |
| X70 | CLI 不能觸發錢包 |
| X71 | 未授權主網時客戶端也阻擋 |
| X72 | 沒有任意合約呼叫入口 |
