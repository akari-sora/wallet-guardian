# 同機 legacy 證據

MCM 的比較器要看 legacy 這一輪實際掃了什麼。legacy 掃描器仍是另一套私人程式，不在這個 repo，這一包也沒有把它接上。這裡交付的是報告用的寫入庫，以及比較器怎麼只讀那些檔案。

同一台機器不代表兩份狀態可以一起寫。legacy 只寫 legacy 狀態；MCM 只寫 MCM 資料庫。這個 hook 不開啟 MCM 資料庫，也不寫 legacy 狀態檔。

## 目錄

呼叫端傳入私人的 `--runs-dir`（必須在 git 工作樹外面）。一次執行寫成：

```text
<runs-dir>/
  <run_id>/
    manifest.json
    state-before.json
    state-after.json
    coverage.json
    events.jsonl
    COMPLETE
```

`state-before.json` 與 `state-after.json` 是 legacy 狀態檔的位元組複本。複本期間來源有變就拒絕這一次，不把撕到一半的內容發表。`events.jsonl` 一行一個 `guardian.legacy-event.v1` 物件，schema 在 `schemas/legacy-event.schema.json`。`coverage.json` 的 `windows` 與 manifest 裡的查詢窗口相同。

manifest（`schemas/legacy-run.schema.json`）記載：

- `run_id`
- `legacy_tool` 的 name、version、hash
- `host_id`
- `started_at`、`finished_at`（UTC）
- `exit_code`
- scope：帳戶 label 與 address hash，以及由它們算出的 `scope_hash`。manifest 不放原始地址。
- 每個 network × capability 的查詢窗口：`from_block`、`to_block`、target hash、target timestamp、`status`、`source`
- 上述四個 payload 檔的 sha256 與 size

`status` 只會是 `complete`、`partial`、`error`、`not_scanned`。hook 不會把 partial 或 error 改成 complete。

## 原子發表

寫入先落到 `<run_id>.tmp-<token>/`。每個檔案 fsync，目錄也 fsync，再用 rename 換成 `<run_id>/`。rename 之後才寫 `COMPLETE`。`COMPLETE` 的內容是 `manifest.json` 位元組的 sha256，先寫暫存檔再 rename。

沒有 `COMPLETE` 的目錄（包含只存在 tmp 的目錄）不能參加比較。sha256 或 size 對不上也不能參加。同一個 `run_id` 再跑會拒絕，目錄是不可變的。POSIX 上目錄是 `0700`，檔案是 `0600`。

`<runs-dir>/.capture.lock` 是這個目錄的寫入鎖。同一時間只允許一個 capture writer。比較器不加這把鎖，也不建立檔案。

## 比較器怎麼讀

`tools/mcm_shadow_round.py` 加上 `--legacy-runs-dir` 之後，會把同一輪的 slot（polling interval 結束在這輪時鐘）傳給 `compare-legacy`。比較器只讀 runs 目錄：

1. 選最新一份結構完整、而且窗口與這輪 slot 重疊的 run。
2. 核對 `COMPLETE` 與每個檔案的 sha256。
3. manifest 的 `scope_hash` 必須等於 MCM 帳戶 label 與 identity hash 算出的值。
4. `finished_at` 早於 slot 結束超過 `--legacy-max-age-seconds`（預設 12600 秒）就是過期。
5. 事件列轉成既有比較用的事件形狀。`monitor_state` 那種 `alerted`、`latest_seen`、`runs`、`nonces`、`bsc_last_block` 仍走 `legacy_state.convert_legacy_document`。

`compare_valid` 為 true，表示這份證據通過上面的檢查，而且 MCM 這一輪 `required` 裡每一項都是 `complete_in_declared_scope`，並且有一份 legacy 查詢窗口蓋住同一個 network 與 capability。區塊範圍有寫下來時，legacy 的 from/to 必須落在 MCM 的範圍裡。

否則 `compare_reason` 是其中一個：

| reason | 意思 |
| --- | --- |
| `legacy_evidence_missing` | 沒有 runs 目錄，或裡面沒有任何 run |
| `legacy_incomplete_marker` | 只有 tmp，或發表目錄沒有 `COMPLETE` |
| `legacy_hash_mismatch` | `COMPLETE` 或檔案 sha256 / size 對不上 |
| `legacy_identity_mismatch` | scope hash 與 MCM 帳戶不一致 |
| `legacy_stale` | 這份 run 比允許的最大年齡更舊 |
| `legacy_window_gap` | 沒有窗口、窗口沒蓋住 slot、或 `not_scanned` |
| `legacy_partial` | 窗口是 `partial` 或 `error` |
| `mcm_window_incomplete` | MCM 的 required 窗口本身不是完整覆蓋 |

自由文字 `QUIET`、沒有變動的 nonce、核准筆數，都不會變成完整覆蓋。零筆事件可以成立，但前提是查詢窗口本身是 `complete` 而且蓋住 slot。

`compare_valid` 為 false 時，cutover 把該 reason 記進不合格原因，這一窗不能累計。欄位沒有出現在舊的輪次紀錄時，既有計數規則維持原樣。

## 以後 legacy 程式怎麼叫

現在沒有接到排程、通知去重或 sender。legacy 程式以後可以在自己的 run 裡這樣叫，這包沒有代為執行：

```sh
guardian legacy-capture begin \
  --runs-dir "$RUNS" \
  --run-id "$RUN_ID" \
  --legacy-state "$LEGACY_STATE" \
  --tool-name legacy-monitor \
  --tool-version "$VERSION" \
  --tool-hash "$TOOL_SHA256" \
  --host-id "$HOST" \
  --scope-hash "$SCOPE_HASH" \
  --account "$LABEL=$ADDRESS_HASH" \
  --started-at "$STARTED_AT"

guardian legacy-capture record \
  --runs-dir "$RUNS" \
  --run-id "$RUN_ID" \
  --window-json "$WINDOW_JSON" \
  --event-json "$EVENT_JSON"

guardian legacy-capture finish \
  --runs-dir "$RUNS" \
  --run-id "$RUN_ID" \
  --legacy-state "$LEGACY_STATE" \
  --exit-code "$EXIT" \
  --finished-at "$FINISHED_AT"
```

`--account` 是 `label=` 加上 identity key 的 sha256，不是地址。`--scope-hash` 必須等於那些 label 與 address hash 的穩定雜湊。

同一輪的 MCM 比較：

```sh
python tools/mcm_shadow_round.py \
  --mcm-config "$MCM_CONFIG" \
  --legacy-state "$LEGACY_STATE" \
  --evidence-dir "$EVIDENCE" \
  --legacy-runs-dir "$RUNS"
```

沒有加 `--legacy-runs-dir` 時，既有的 snapshot 與 `compare-legacy` 行為不變。
