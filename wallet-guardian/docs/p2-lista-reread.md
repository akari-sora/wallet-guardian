# Lista 原地址單步領取的獨立唯讀重讀

客戶端用固定政策重讀鏈上狀態，核對這筆任務是否仍是同一個帳戶、同一筆提款、同一個部署、同一段 `claimWithdraw(uint256)`。RPC、ABI、proxy 與允許的操作來自這份客戶端政策，對應 `config/chains.json` 與 `config/protocols/lista-bsc.json`。任務 JSON 不能指定這些來源。

重讀在同一個區塊上重建：原地址、以 uuid 對上的目前索引、可領、暫停、實作與程式碼、以及精確的 calldata 與 value。不預先採用計畫裡的舊索引。

結果綁在計畫 revision、digest、任務、帳戶、鏈、區塊與有效時間。登出、取消、revision 變更、錢包帳戶或鏈變更都會作廢舊結果。較慢的舊查詢不能蓋過較新的狀態。

畫面三項分開：

1. 獨立鏈上重讀：通過 / 失敗 / 資料不足
2. 交易效果模擬：通過 / 失敗 / 未做
3. 允許實際送出：關閉

重讀通過不會寫回 `preflight_ok`，也不會把未完成的模擬標成通過。

鎖畫面不等於鎖鏈。lock screen ≠ lock chain.

## 不做

主網送出、模式 B／C、TUF、Pages 部署、讓確認鈕真正送出。
