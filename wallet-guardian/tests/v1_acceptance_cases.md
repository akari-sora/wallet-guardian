# v1 監控驗收案例（56）

這些是最低驗收案例，不是上線證明。離線測試見 pytest。

| ID | 說明 |
|---|---|
| D01 | 原生幣為零仍保留非零 ERC-20 |
| D02 | 錢包 token 為零仍保留 DeFi／提款 |
| D03 | 相同 symbol 不同合約不合併 |
| D04 | 垃圾資產無外流不計本金，有外流仍評估 |
| D05 | 分頁最後一頁不漏 |
| D06 | HTTP 200 加 partialErrors 不是 COMPLETE |
| D07 | 401／429／timeout 不把餘額改成零 |
| D08 | token 支援不等於 DeFi 支援 |
| D09 | 歷史窗口不足標 partial approvals |
| D10 | chainId、tip 與同源 RPC 要分開看 |
| D11 | HyperCore 空 master 不是子帳戶為零 |
| D12 | 首次基準的既有風險可告警 |
| A01 | 整數精度含 uint256 與 dust |
| A02 | 收據與底層不重複計價 |
| A03 | 負債與未知價格 |
| A04 | 自有地址互轉淨流入為零並另計 gas |
| A05 | 無新轉帳仍可反映利息累計 |
| A06 | ERC-4626 preview 不等於可提 |
| A07 | unified margin 不把 spot 與 perp 相加 |
| A08 | 來源陳舊即使觀測時間新仍是 STALE |
| A09 | partial 保留 last-known-good 且不標新鮮 |
| A10 | 較舊 run 不能覆蓋新 snapshot |
| R01 | Lista WAITING、CLAIMABLE、CLAIMED |
| R02 | idx 移動且相同金額不是身份 |
| R03 | 滿 7 日但 isClaimable 為 false 仍等待 |
| R04 | 請求消失但無領取證據不是 CLAIMED |
| R05 | 未驗證外部 watcher 的重複與缺口 |
| R06 | Aster 與 Lista 退出階段分開 |
| R07 | 無交易時健康因子仍可惡化 |
| R08 | oracle stale 與無債務不是無限大 |
| R09 | LST 市場價與贖回價可以不同 |
| R10 | 來源鏈確認不是目的鏈結算 |
| R11 | reorg 需要更正 |
| R12 | 短暫價格異常不是確定脫鉤 |
| E01 | 同一事件重送只告警一次 |
| E02 | 同一交易多個 log 都保留 |
| E03 | 告警升級可以更新，重複不洗版 |
| E04 | 回補窗口重疊 |
| E05 | 入庫前當機不能 ACK |
| E06 | 通知結果不明時先對帳 |
| E07 | 轉載不洗版，升級仍通知 |
| E08 | 沒變化不通知，失明要通知 |
| E09 | 重開後資料還在 |
| E10 | 沒有排程不能稱 MONITORING_ACTIVE |
| S01 | 非法地址與惡意 metadata |
| S02 | SSRF 與私網 |
| S03 | 偽造、過期、重放 webhook 與未固定證書 |
| S04 | 不可信文字不產生工具呼叫 |
| S05 | API key 遮罩 |
| S06 | Agent 拒絕簽名與廣播 |
| S07 | 鏈上 allowance 與未上鏈 permit 分開 |
| S08 | 預期中的 delegation 不是被駭 |
| S09 | 租戶隔離 |
| S10 | 分享包去掉私人資料 |
| S11 | 未審查 decoder 不能進 production |
| S12 | 未識別轉出不稱被盜 |
