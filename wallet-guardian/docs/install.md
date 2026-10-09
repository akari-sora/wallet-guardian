# 安裝唯讀監控程式

授權：AGPL-3.0-only。發行套件名稱是 `wallet-guardian-monitor`；Python 匯入名稱與命令列仍是 `guardian`。這個預覽版沒有上傳到任何套件索引。不要執行 `pip install guardian` 或 `pip install wallet-guardian-monitor`：PyPI 上同名或相近的套件可能不是這個專案，名稱也沒有被保留。

只安裝你從 GitLab release 下載、而且 SHA256 對過的 wheel。需要 Python 3.12 或更新的版本，以及 [uv](https://docs.astral.sh/uv/) 或 pip。Docker 不在這份說明裡。

監控程式只讀。它不簽名、不組交易、不廣播。簽章和可選 Action Client 的差別只寫在 [docs/threat-model.md](threat-model.md#monitor-does-not-sign)。金鑰由你自己保管。設定檔只寫環境變數的名稱（`endpoint_env`、`credential_env`），不寫 key，也不寫真實監控地址。Telegram 發送器存在，但是選用；投遞預設關閉（`outbox_only`）。`--no-send` 禁止送出。標籤 `v0.4.0-rc.2` 是否已發布，see GitLab releases。

## 從 GitLab release 安裝

到這個專案的 GitLab release，下載 `SHA256SUMS`，以及 `SHA256SUMS` 裡列出的每一個檔案（wheel `wallet_guardian_monitor-0.4.0rc2-py3-none-any.whl`、sdist、`constraints.txt`、`release-manifest.json`、`release-evidence.json`），全部放在同一個目錄。

sdist（`.tar.gz`）是原始碼封裝，一般安裝用 wheel。

在那個目錄核對雜湊（缺檔或雜湊不符都會失敗）：

```bash
sha256sum -c SHA256SUMS
```

Windows 沒有 `sha256sum` 時，用 Python：

```bash
python -c "import hashlib, pathlib, sys; bad=0
for line in pathlib.Path('SHA256SUMS').read_text().splitlines():
    h, n = line.split(None, 1); n = n.strip().lstrip('*'); f = pathlib.Path(n)
    ok = f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == h
    print(n, 'OK' if ok else 'FAILED'); bad += (not ok)
sys.exit(1 if bad else 0)"
```

每一行都必須是 true。不要安裝雜湊對不上的檔案。

`constraints.txt` 是從 `uv.lock` 匯出的執行期釘選（`uv export --frozen --no-dev --no-hashes`）。`uv tool install` **不會**自動套用 `uv.lock` 或這份檔案，你必須自己帶上 `--constraints`。

用 uv：

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python --constraints constraints.txt ./wallet_guardian_monitor-0.4.0rc2-py3-none-any.whl
```

Windows 的直譯器在 `.venv\Scripts\python.exe`。

或安裝成工具（仍然要自己給 constraints；這不會讀 `uv.lock`）：

```bash
uv tool install --constraints constraints.txt ./wallet_guardian_monitor-0.4.0rc2-py3-none-any.whl
```

用 pip 時也要同一份 constraints，而且不要向 PyPI 要名為 `guardian` 的套件：

```bash
python -m venv .venv
.venv/bin/python -m pip install -c constraints.txt ./wallet_guardian_monitor-0.4.0rc2-py3-none-any.whl
```

wheel 本身仍要用 `SHA256SUMS` 先對過。這份 constraints 沒有雜湊，pip 會為了相依套件連到 PyPI，但版本必須和 constraints 裡的 `==` 一致。不要改成 `pip install guardian`。

安裝後：

```bash
guardian --help
```

## 私人目錄

預設不寫進安裝目錄，也不寫進 wheel。

| 用途 | 環境變數（優先） | 否則 |
|---|---|---|
| 私人設定 | `GUARDIAN_CONFIG_DIR`，或 `$XDG_CONFIG_HOME/guardian` | `~/.config/guardian` |
| 私人狀態（sqlite、evidence、doctor 報告） | `GUARDIAN_STATE_DIR`，或 `$XDG_STATE_HOME/guardian` | `~/.local/state/guardian` |

`XDG_CONFIG_HOME` 與 `XDG_STATE_HOME` 是目錄本身，程式會再加一層 `guardian`。`GUARDIAN_CONFIG_DIR` 與 `GUARDIAN_STATE_DIR` 則是最終目錄。

在 POSIX 上，這些目錄是 `0700`，新寫的私人檔是 `0600`。Windows 不強制這些權限位元。

已設定 `XDG_CONFIG_HOME` 或 `GUARDIAN_CONFIG_DIR` 時，`.env.local` 放在設定目錄。沒有這兩個變數、而且是從原始碼 checkout 執行時，若 checkout 裡已有 gitignore 的 `.env.local`，仍會讀那一份。

明確的 CLI 路徑維持原樣：`--db`、`--mcm-config`、`--config` 指向你給的檔案，不會被改寫到 XDG。

公開的 `--mcm-config` 只含環境變數名稱。帳戶檔、狀態目錄、secret profile 放在 git 工作樹外面，由設定裡的 `private_inputs` 環境變數指出路徑。

## 升級、備份、降版、解除安裝

資料庫遷移只往前。升級前先手動備份狀態目錄裡的 sqlite（含旁邊的 `-wal` 與 `-shm`，若有的話）：

```bash
cp -a "$XDG_STATE_HOME/guardian" "$XDG_STATE_HOME/guardian.backup-$(date -u +%Y%m%dT%H%M%SZ)"
```

沒有設定 `XDG_STATE_HOME` 時，目錄是 `~/.local/state/guardian`。沒有自動備份程式。不要在監控程式開著資料庫時複製；先停掉行程。

然後用**那一版 release 自己的** `constraints.txt` 安裝新的 wheel（同上，`uv pip install` 或 `uv tool install --constraints`）。

同一個 schema 世代再裝回上一版 wheel 時，私人狀態仍可讀。若新版已經套用了舊版沒有的遷移，舊版會拒絕開啟，訊息含 `database schema is newer than this build`，並且不刪資料。這時要回到舊版，請把備份複製回來，不要指望舊程式把 schema 降回去。

解除安裝：

```bash
uv tool uninstall wallet-guardian-monitor
```

若是 venv，刪掉那個 venv 即可。XDG 裡的設定與狀態不會被解除安裝刪掉。確定不再需要時，自行刪除 `~/.config/guardian` 與 `~/.local/state/guardian`（或你覆寫的目錄）。

## 從 checkout 開發

開發仍用 checkout 裡的 `./scripts/bootstrap.sh` 與 `uv run`。那不是發行安裝。發行物用上面的 wheel 與 constraints。
