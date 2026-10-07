# ADR 0001：第一階段資料來源

日期：2026-10-07。

## 決定

第一階段只用已經寫進設定的 BNB Chain 官方 dataseed 做直接狀態，加上 Hyperliquid 官方 info。Etherscan V2 只拿 chainlist，直到有 API key 才讀地址歷史。DeBank、Zerion、Alchemy、Tenderly、GoPlus 在沒有 key 時維持 BLOCKED 或 LIMITED_DISCOVERY。

## 原因

規格要求沒有付費 key 仍要完成 doctor、已知協議 adapter 與降級報告，但不能把缺資料說成完整 discovery，也不能從搜尋結果挑一個免費 RPC 放進可信清單。官方 BSC 文件列出 `https://bsc-dataseed.bnbchain.org`，並註明這些端點停用 `eth_getLogs`。

Lista 讀取走官方地址表上的 proxy：`0x1adB950d8bB3dA4bE104211D5AB038628e477fE6`。實作位址每次用 ERC-1967 slot 重讀，不把文件或 Sourcify 的舊值當成永久真相。

## 後果

歷史授權、內部轉帳與「請求消失是否已領取」在沒有日誌索引時是 PARTIAL。價格保持未知，不填 0。操作模擬沒有 Tenderly trace，因此不能開放錢包請求。
