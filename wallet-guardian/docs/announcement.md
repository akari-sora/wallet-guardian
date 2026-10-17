# Announcement

The project's GitLab Releases page: `https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.5`

## English

Wallet Guardian is a preview of a read-only wallet monitor you run on a machine you control. It does not sign, build a transaction, or broadcast, and it never asks for a seed phrase or a private key. Two ways to use it: run the guardian command yourself, or have Grok Bot operate that same command. The shareable Grok Bot template is coming. This note has no template link.

The named chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana (partial: transactions are not parsed, so no Solana transfer alerts in this preview), with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. Coverage differs by chain and source. A missing key, a refused plan, or a failed read is a gap, not an empty wallet. The coverage matrix in the repository lists each claim.

The 72h shadow validation is not complete. Release evidence for rc.5 is `NOT_RUN`. No gate result is recorded for the rc.5 release commit. The only recorded CI run is pipeline #2957467957 on `5a2c33f`, not on the release commit. Out of the box nothing notifies you: alerts stay in a local database, the scheduler is not installed, and the provided schedule runs `run-once --no-send` every 3 hours and `watchdog --no-send` every 15 minutes. Telegram delivery exists but has not been tested live. Nothing is on PyPI. Install the release wheel and its constraints file from the project's GitLab Releases page at the top of this file.

## 繁體中文

Wallet Guardian 是預覽版唯讀監控，跑在你控制的機器上。它不簽名、不組交易、不廣播，也不索取助記詞或私鑰。可以自己下 guardian 指令，或讓 Grok Bot 操作同一套指令。Grok Bot 範本尚待公布，這裡沒有連結。

點名的鏈是 Ethereum、BNB Chain、Arbitrum One、Base、Optimism、Polygon PoS、Solana（部分：本預覽不解析交易，因此沒有 Solana 轉帳告警）；Avalanche C-Chain 覆蓋有限；Bitcoin 只讀地址集合。缺金鑰、方案拒絕或讀取失敗是缺口，不是空錢包。

72 小時 shadow 驗證還沒有完成。這些預覽提交只做了本地閘門，不是 CI 通過。預設不會通知你：告警留在本機資料庫，沒有安裝排程，附上的排程每 3 小時跑 `run-once --no-send`、每 15 分鐘跑 `watchdog --no-send`。Telegram 送出存在，但沒有實機測試。沒有上架 PyPI。請從本檔上方的 GitLab Releases 頁安裝 wheel 與 constraints。
