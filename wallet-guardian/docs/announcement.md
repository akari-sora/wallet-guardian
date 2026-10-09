# Announcement

The project's GitLab Releases page: `https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.3`

## English

Wallet Guardian is a preview of a read-only wallet monitor you run on a machine you control. It does not sign, build a transaction, or broadcast, and it never asks for a seed phrase or a private key. Two ways to use it: run the guardian command yourself, or have Grok Bot operate that same command. The shareable Grok Bot template is coming. This note has no template link.

The named chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. Coverage differs by chain and source. A missing key, a refused plan, or a failed read is a gap, not an empty wallet. The coverage matrix in the repository lists each claim.

The 72h shadow validation is not complete. These preview commits were checked with a local gate, not a CI pass. Notifications are off by default; the live Telegram canary has not been run. Nothing is on PyPI. Install the release wheel and its constraints file from the project's GitLab Releases page at the top of this file.

## 繁體中文

Wallet Guardian 是預覽版唯讀監控，跑在你控制的機器上。它不簽名、不組交易、不廣播，也不索取助記詞或私鑰。可以自己下 guardian 指令，或讓 Grok Bot 操作同一套指令。Grok Bot 範本尚待公布，這裡沒有連結。

點名的鏈是 Ethereum、BNB Chain、Arbitrum One、Base、Optimism、Polygon PoS、Solana；Avalanche C-Chain 覆蓋有限；Bitcoin 只讀地址集合。缺金鑰、方案拒絕或讀取失敗是缺口，不是空錢包。

72 小時 shadow 驗證還沒有完成。這些預覽提交只做了本地閘門，不是 CI 通過。通知預設關閉，Telegram 實機測試尚未執行。沒有上架 PyPI。請從本檔上方的 GitLab Releases 頁安裝 wheel 與 constraints。
