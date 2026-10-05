# AGENTS.md

Vibe Launcher：Windows 桌面程式（PySide6），用來在「本機或 SSH 主機」開啟官方 Codex / Claude CLI，管理多個帳號與專案。使用者以繁體中文溝通；UI 文字與 README 用繁體中文。

## 目標
- 一次登入、重複使用：每個帳號有獨立的 profile，不污染使用者自己的 `~/.codex`、`~/.claude`。
- 專案不綁帳號：開啟時才選帳號與服務；訂閱帳號的登入只存在它綁定的環境，API / endpoint 帳號可用於任何環境。
- SSH 專案用 tmux 保持執行，關掉終端後工作繼續，可再接回、停止、召回。
- 同一專案可有多個工作區（工作階段）；專案頁抽屜、工作區頁都要能看到、接回、重開。

## 方法與結構（`src/vibe_launcher/`）
- `ui.py` 主視窗與所有動作（工作階段頁在 `ui_sessions.py`、GitHub／Harness／額度在 `ui_integrations.py`，以 mixin 併入；共用顯示工具在 `ui_format.py`；表單與輸入列草稿在 `drafts.py`）；`github_dialog.py` 專案 GitHub 綁定（帳號→commit 身分→repository）；品牌圖標不隨程式發佈：`icons.py` 執行時讀 `assets/brand/*.svg`（不進版控），沒有就用通用圖示；`workspace_page.py` 內建終端與快速開啟；`kit.py` 元件、`theme.py` 設計 token；`terminal.py` + `conpty.py` + `assets/` 內建終端（xterm.js）。
- `store.py` SQLite 設定（帳號、環境、專案、工作階段；匯出不含機密）；`service.py` 建立 job；`runner.py` 在終端內執行 job 並回寫狀態；`env_state.py` 唯讀的狀態與「召回計畫」。
- **`runtime.py` 會整份當作單一腳本送到 SSH 主機執行。** 遠端需要的邏輯都放在這裡，不可 import 套件內其他模組，並相容較舊的 Python（例如 `tomllib` 要保護）。
- 帳號隔離靠 `CODEX_HOME` / `CLAUDE_CONFIG_DIR` 指向 `<data>/profiles/<tool>/<account_id>`；環境變數經 `isolated_env` 過濾。skills/hooks 等以「複製白名單」方式共用（不含憑證）。

## 工作流
1. 先讀相關程式與測試，再改；風格、註解密度、命名跟周圍一致。
2. 改完跑測試（見下），UI 變動用 `widget.grab()` 截圖檢查版面（離屏截圖中文會是方塊，只看版面）。
3. 新功能補測試（`tests/`），並在 README 加一小節。
4. 回報時說清楚驗證到哪裡、哪些沒驗證（尤其真實 SSH/tmux 流程）。

```
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q --basetemp=<暫存>/bt -p no:cacheprovider -o faulthandler_timeout=90
```
- 預設的 `%TEMP%\pytest-of-<使用者>` 可能沒有權限，請自訂 `--basetemp`。
- 不要加 `-p no:faulthandler`（原生崩潰會彈出 Windows 對話框，整個測試卡住）。
- 測試不可開真的 `ConfirmDialog` 等 modal：改 patch `view._confirm`、`view._notice` 等接縫（conftest 有防護）。
- pytest-qt 會先關視窗再做 fixture teardown：有終端的視窗要 `qtbot.addWidget(w, before_close_func=lambda w: w.workspace.shutdown())`。
- 用 `kit.dispose_widget`、`kit.later(owner, ms, fn)`；不要 `setParent(None)` 後 `deleteLater()`，不要 `QTimer.singleShot` 綁會碰 widget 的方法。
- Bash 的 heredoc 遇到 `'''` 會壞；寫補丁腳本用 Write 工具。

## 應注意
- 機密：不讀、不存、不顯示密碼 / API key / OAuth token；GUI 不讀已存的 SSH 密碼。路徑一律經 `assert_private_path`（拒絕 symlink、reparse point、hard link）。
- 終端輸出不可信為狀態來源；遠端結果看 supervisor 的結束碼（78 未啟動、79 結束、80 背景、81 失敗）。
- 資料遷移要保守：改 SQLite schema 要寫 migration，並先拿使用者資料庫的備份副本驗證。
- 刪除要有確認；不要讓外鍵 RESTRICT 擋住使用者（走 `store.delete` 的 release 流程）。
- 狀態不明的遠端工作不可直接當成已結束；本機工作離開終端必然結束。
- 對比：說明用的文字色維持足夠對比（muted `#8190A8`）。

## 邊界
- 只做 Windows 桌面 launcher；不重做 Codex / Claude 本身，不攔截或代理它們的登入流程，登入一律在官方終端 / 瀏覽器完成。
- 不用 pywinpty（自有 ConPTY 層，避免 CJK 亂碼）；xterm.js 為 vendored（MIT）。
- 不要截真實螢幕（`QScreen.grabWindow`），只用 `widget.grab()`。
- 不要自行 commit / push；沒被要求就不改使用者的 `~/.codex`、`~/.claude`、SSH 設定，也不要連線測試使用者的真實伺服器。
- `docs/private/` 放真實主機的驗證紀錄與資安評估，不進版控。

## 版控與協作
- Repository 是 private；`main` 只透過 PR 合併。分支命名 `feat/…`、`fix/…`、`docs/…`；commit 訊息一行說清楚改了什麼。
- 每個 PR 由 CI（`.github/workflows/ci.yml`，Windows＋離屏 Qt）跑完整測試；PR 說明照範本列出已驗證／未驗證項目。
- 不進版控（見 `.gitignore`）：`docs/private/`、`src/vibe_launcher/assets/brand/`、`.local-test/`、資料庫、金鑰與 `.env`。新增這類檔案前先確認 ignore 規則。
- 版權：只收自己寫的程式與相容授權的第三方檔案（例如 `assets/xterm/` 的 MIT，保留其 LICENSE）；他牌商標、字型、截圖不放進 repository。
- 範例、測試與文件不可出現真實姓名、帳號、email、主機別名或路徑；一律用 `user`、`user@server`、`example.com`、`192.0.2.x` 這類通用值。
- 這是唯一的 agent 指引檔；`CLAUDE.md` 只引用本檔，不另寫內容。
