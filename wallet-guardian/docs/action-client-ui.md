# Action Client 介面（MR !10、!11）

這份說明介面層的規則。介面改版只動畫面：`index.html`、`src/style.css`、`src/icons/*.svg`、`src/typography.mjs`，以及 `src/main.ts` 裡建立 DOM 的程式。錢包、SIWE、API、送出關卡、`execution_authorized`、任務生命週期都沒改。`requestUserSend()` 一律拒絕。

## 版面

- 頂端狀態列：「允許實際送出：關閉」是固定的封蠟樣式，下面是登入狀態、錢包、鏈、任務狀態四格。這四格只顯示頁面已有的狀態（`paintStatusChips()`），出錯也不會打斷登入、錢包或任務流程。任務頁回 401 時顯示「需要登入」。
- 錢包卡片：名稱拆成主名稱與括號說明兩行（`.wallet-name-detail`，`textContent` 不變）。名稱、圖示、rdns 是錢包自述，只做顯示。
- 淺色、深色跟隨 `prefers-color-scheme`。字型只用系統字型。圖示是打包進 CSS 的 SVG mask，建置時內嵌成 `data:` URI。
- 寬度：手機單欄（360–412px）、平板 720px 單欄、960px 以上兩欄（側欄 440–480px，最寬 1200px）。

## CSP 限制

`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:`。所以不能有 inline `style` 屬性、`<style>`、`element.style`、外部字型、CDN 或遠端圖片。

## `src/typography.mjs`

在真瀏覽器裡把文字拆成文字節點加 `<span class="nw">`（不斷行：英文註解如「（insufficient）」、`/t/<ref>`、「鎖畫面不等於鎖鏈。」、`lock screen ≠ lock chain`、`personal_sign`）與 `<span class="mono">`（`window.*`、`EIP-6963`、rdns、`ref_…`、`0x…`）。

- 只切分，不增刪字元：元素的 `textContent` 前後相同。
- 只處理只有一個文字節點的元素，略過 flex 或 grid 容器。
- 沒有 `document.createTextNode` 或 `MutationObserver` 時不做事，所以單元測試的假 DOM 不受影響。

## 測試相容

- 保留所有 id、aria 標籤、`data-check` / `data-state`、測試比對的 className（`wallet-option`、`wallet-name`、`wallet-meta`、`#wc-panel` 下的 `row` 與 `wc-links`）。
- `index.html` 必須保留測試直接比對的完整字串（三個核對狀態、錢包結果、原地址提醒、`lock screen ≠ lock chain`），不能在字串中間插入標記。
- 「在我的錢包確認」按鈕的 `textContent` 必須完全相同，圖示只用 CSS。

## 版面檢查（開發工具，不是關卡）

```bash
cd action-client
npm run build            # 或用 WalletConnect 建置
node tools/ui-layout-check.mjs dist /tmp/wg-ui-check
```

在無頭 Chrome 用正式 `_headers` CSP 載入建置結果，拍 360/412/768/1440、淺色與深色的首頁、任務頁、偵測到錢包、選擇 WalletConnect、合成範例、QR（只連程式內的 MockRelay）。有 CSP 違規、console 或頁面錯誤、橫向溢出、元素溢出、狀態格不等高，或 `.nw` 被拆開就以非零結束。截圖含 QR，輸出放在 repo 外。

`test:wallets:artifact` 需要預設建置的 `dist/`（沒有 WalletConnect）；WalletConnect 建置後要先重新 `npm run build` 再跑。

尚未實機驗證：幣安 App 內建瀏覽器與 Samsung 實機的換行、真錢包登入、WalletConnect 真實配對。
