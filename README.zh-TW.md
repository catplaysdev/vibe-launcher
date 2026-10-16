# Vibe Launcher

終端可靠性修正：離開連線與停止操作等待確認結果；顯示故障時保留程式並提示輸出缺漏。受管理的 Codex 終端支援原生 `/resume` 紀錄選單。手動開啟終端會扣除相符的待恢復項目，工作區選單可丟棄不需要的待恢復終端。

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

桌面啟動器，在本機或 Linux SSH 主機開啟官方 **Codex CLI** 與 **Claude Code**。
目前支援 **Windows 10/11**，並計畫支援 **Linux 桌面版（優先 Ubuntu）**（見[待更新事項](#4-已確定待更新事項)）。
每個帳號登入一次、各自獨立 profile；專案在內建終端工作區開啟；SSH 工作以 tmux 保留，關掉視窗後繼續執行。

- 帳號不會動到你自己的 `~/.codex`／`~/.claude`，每個帳號有自己的私有 profile。
- 專案不綁帳號，開啟時才選用哪個帳號。
- 遠端工作在斷線後繼續（tmux），之後可接回、停止或一次召回。
- 登入一律在官方 CLI／瀏覽器完成；Launcher 不經手 OAuth token。

![工作區：在 SSH 主機上並排 Codex 與 Claude，下方是輸入列，左側是快速開啟](docs/images/workspace-zh-TW.png)

## 1. 建立環境

### 事先準備

**Windows（本機）**

| 需求 | 說明 |
| --- | --- |
| Windows 10 1809 以上或 Windows 11 | 內建終端使用 ConPTY。 |
| Python 3.11+ 與 [uv](https://docs.astral.sh/uv/) | 請用完整安裝的 Python，不要用其他軟體內嵌的版本。 |
| Git | 從原始碼執行與 App 內更新需要。 |
| Windows OpenSSH Client | 只有 SSH 環境需要（`ssh.exe`，Windows 選用功能）。 |
| 要使用的官方 CLI | `codex`／`claude` 原生 `.exe` 需在 `PATH` 或下列使用者安裝目錄（不執行 `.cmd`／`.bat` 包裝器）。 |
| 選用：Git + [GitHub CLI](https://cli.github.com/)（`gh`） | 只有專案綁定 GitHub 帳號時需要。 |

**每台 Linux SSH 主機**

| 需求 | 說明 |
| --- | --- |
| `/usr/bin/python3` 3.10+（含 `lzma`） | 執行 Launcher 的輔助腳本（只用標準庫）。 |
| **tmux** | 「關閉終端後繼續執行」的專案（預設）必須安裝。 |
| `codex` 和／或 `claude` | 放在 SSH 非互動 shell 的 `PATH`，或 `~/.local/bin`、`~/.npm-global/bin`、`~/.bun/bin`、nvm 等位置。 |
| 選用：`git`、`gh` | 在該主機綁定 GitHub 時需要。 |

```bash
# Debian／Ubuntu 範例；CLI 請用官方安裝方式安裝
sudo apt install tmux python3
ssh user@server 'echo $PATH; which -a python3 tmux codex claude'   # Launcher 會看到的結果
```

Launcher 不會在主機上安裝任何東西，也不修改 SSH 設定或金鑰；第一次連線的主機指紋由你在終端確認。

### 安裝與執行

**安裝程式（建議）**：到 [Releases](https://github.com/catplaysdev/vibe-launcher/releases) 下載
`Vibe-Launcher-Setup-<版本>.exe` 並執行。只安裝給目前使用者（不需系統管理員權限），Launcher 本身不需要 Python、uv 或 Git；
上面列的需求仍適用於你用它開啟的工具。可用同一個 Release 的 `SHA256SUMS.txt` 核對下載檔。之後有新版時，
「設定 › 關於與更新」會列出更新內容並可直接更新（會先備份你的資料）。檢查最多每六小時一次、只有一個小請求，也可以關閉；
其餘時間不會有任何背景程式。

**從原始碼執行**：

```powershell
git clone <repository-url> vibe-launcher
cd vibe-launcher
uv sync
uv run vibe-launcher
```

建立桌面捷徑（選用）：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/create-shortcut.ps1`。
資料存放在 `%LOCALAPPDATA%\VibeLauncher`（可用 `VIBE_LAUNCHER_HOME` 改位置）。

## 2. 開始使用

1. **帳號 → 新增**：Codex 或 Claude，選訂閱或 API（API 需填模型 ID；endpoint 空白代表官方 API）。
2. **環境**：「本機」已內建。伺服器請新增 SSH 環境，填 `~/.ssh/config` 裡的 `Host` 別名或 `user@host`，再按「環境檢查」。
3. **帳號 → 登入**：選擇環境，在終端／瀏覽器完成官方登入。訂閱登入只存在做登入的那台機器；API 帳號哪裡都能用。
4. **專案 → 新增專案**：名稱、環境與資料夾絕對路徑（SSH 可用 `~/my-app`）。按「開啟」選帳號，CLI 會在「工作區」啟動。
5. 想讓 SSH 工作繼續跑，按「離開」（`Ctrl+Shift+D`）。之後可從專案、工作區或「工作階段」接回。

常用按鍵：`Ctrl+K` 指令面板、`Ctrl+,` 設定、`Ctrl+Tab` 下一個終端、`Ctrl+=`／`Ctrl+-` 終端文字大小、
`Ctrl+Shift+Space` 鍵盤導覽。所有快捷鍵都可在「設定 → 快捷鍵」修改。

![專案：列出專案的每個工作階段、所屬工作區與狀態，並可一鍵還原](docs/images/projects-zh-TW.png)

## 3. 功能

- **帳號隔離**：每個帳號獨立的 `CODEX_HOME`／`CLAUDE_CONFIG_DIR`；啟動時自動取得執行環境使用者的 skills、plugins 與對話，專案局部資料由 CLI 正常讀取，登入憑證維持隔離。
- **本機與 SSH 環境**：狀態卡片（主機信任、驗證方式、agent、CLI 版本）、遠端資料夾瀏覽、可選的 SSH 密碼快取（Windows Credential Manager）、CLI 版本檢查與更新；Windows 在 PATH 後補查 `%USERPROFILE%/.local/bin` 與 `%LOCALAPPDATA%/Programs/OpenAI/Codex/bin`。自建 local 環境可在確認並通過相依性檢查後刪除，內建本機環境受保護。
- **專案**：標籤與篩選（含依工作區篩選）、收藏、自訂專案圖示（內建圖示、圖片或可換色的 SVG）、顯示專案所屬的工作區、「開啟到工作區…」、依工作區分組列出專案所有工作階段的抽屜、重啟後的「可恢復」標示，以及一鍵還原（回到專案所在的工作區，tmux 工作重新接回、Codex／Claude 接續上次對話）。
- **工作區**：xterm.js 終端，分頁或並排；原生輸入列方便輸入法（中文）輸入；每個專案的快速開啟；可命名的多專案桌面，每個工作區（含預設工作區）都有自己的名稱與圖示，切換工作區時保留終端與執行狀態；開關終端時自動保存工作區（強制關閉也不遺失，重啟後按「恢復 N 個終端」帶回，不會自動啟動）；每個終端有「重新載入」；重新載入與輸入列送出前可選擇是否確認；選擇 shell／CLI；文字可縮放。
- **帳號與右鍵**：每種工具的帳號依環境分組（本機在前，接著各伺服器），帳號多時該環境清單可捲動；在專案、帳號、環境或工作階段上按右鍵，可直接使用與按鈕相同的動作。伺服器上每個工作區只有一個以工作區 id 命名的 tmux 工作階段，刪除工作區時會先說明並一併結束它。
- **首頁**：執行中的工作、需要重新登入的帳號、每個工作區一張卡片（專案數、開啟與執行中的終端，點一下即切換）、最近專案（在工作區等待恢復的可直接「恢復」）與環境。
- **工作階段**：每次執行的真實狀態（依遠端監督程式的結束碼，不看終端輸出）；批次停止／清理，依「工作區 › 專案 › 終端」分層，或依時間列出。
- **整合**：每個專案的 GitHub 帳號與 commit 身分（官方 `gh`）、Codex 額度查詢、DeepSeek Harness Web（實驗性）。
- **配額方案**：分開顯示各配額類別，依環境建立帳號池，以積木組成可儲存的方案（依序接替、輪流切換、額度優先與同一帳號自動繼續四個範本；觸發、判斷、選帳號、結果四類積木各有顏色，由左到右用滑鼠或鍵盤接線，只提供允許的連線，可一鍵整理排版，儲存前檢查，可在不啟動工作下預覽）。儲存或套用方案前會檢查本機帳號：設定與核准規則可對齊到參考帳號，缺少的外掛、應用或 MCP 登入需安裝或逐項接受。自動繼續會等額度重設後接著做因額度停下的工作（Codex 重設後重新查詢確認；Claude 交給 Claude Code 內建的自動繼續，Launcher 不會中斷它）。終端預設不套用方案。Codex 終端可從工具列切換帳號：確切對話與設定在驗證過的安全時點交接，因額度中斷的工作可自動接續。已以本機真實帳號驗證；Claude Code 終端以接回確切對話的方式同樣可切換。SSH 主機尚未驗證。詳見[使用說明](docs/guide.zh-TW.md)與[問題對照表](docs/account-switching-issues.zh-TW.md)。
- **自訂**：8 套主題與強調色、介面字型與終端機字型（可分別選擇）、介面縮放（80–200%）、英文／繁體中文／簡體中文／日文／韓文／德文／西班牙文介面、`settings.json` 的 CLI 擴充 manifest。從「設定 → 一般 → 介面語言」切換語言，重新啟動後生效。
- **安全更新**：安裝版從 GitHub Releases 更新（核對 SHA-256）；git checkout 比對 tag 後 `git pull --ff-only` + `uv sync`。兩者都先備份資料庫與設定。專屬進度視窗顯示每個步驟、下載大小與更新內容：繁體中文系統顯示中文，其餘一律英文，可複製中英文全文。重複操作沿用同一更新，helper 失敗不會誤報成功，安裝接手後不再重複確認終端關閉。
- **系統匣**：可選擇關閉視窗時縮到系統匣繼續執行（設定 → 一般 → 視窗）。

<table>
  <tr>
    <td width="40%"><img src="docs/images/home-zh-TW.png" alt="首頁"><br><sub>首頁</sub></td>
    <td width="40%"><img src="docs/images/environments-zh-TW.png" alt="環境"><br><sub>環境</sub></td>
    <td width="20%"><img src="docs/images/project-icon-zh-TW.png" alt="專案圖示"><br><sub>專案圖示</sub></td>
  </tr>
</table>

<sub>截圖使用虛構的範例資料（可用 `scripts/readme_screenshots.py` 重新產生）。</sub>

完整說明、安全細節與限制：[docs/guide.zh-TW.md](docs/guide.zh-TW.md)。人工端到端驗收清單：[docs/acceptance.md](docs/acceptance.md)。

## 4. 已確定待更新事項

- **Linux 桌面版（優先 Ubuntu）**：讓 Launcher 本身也能在 Linux 上執行，而不只是連線*到* Linux 主機。目前只支援 Windows；與 Windows 綁定的部分（ConPTY 終端層、Windows OpenSSH／Credential Manager、原生標題列）需要對應的 Linux 實作（PTY 層、系統 `ssh`、Secret Service／keyring）。遠端的 tmux 流程本來就與平台無關。尚未提供；會先在 Ubuntu 上開發與測試，正式支援時再更新上方的平台需求。
- **Git graph**：在 Launcher 內檢視專案的分支與 commit 圖。
- **SSH 搭配 VPN**：SSH 環境可指定需要的 VPN，連線前先檢查（或啟動）該 VPN。

## 開發

介面文案集中在 `src/vibe_launcher/locales/`，英文 `en.json` 為主要語言；其他語系為 `zh-TW`、`zh-CN`、`ja`、`ko`、`de`、`es`。檢查工具會驗證所有已登錄語系的 key、格式參數與 HTML 標記。

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
uv run --group dev pytest -q --basetemp="$env:TEMP/vibe-launcher-tests" -p no:cacheprovider -o faulthandler_timeout=90
uv run python scripts/check_translations.py
```

協作規則、分支流程與發佈步驟見 [AGENTS.md](AGENTS.md)；變更紀錄見 [CHANGELOG.md](CHANGELOG.md)。
Codex／Claude／DeepSeek 的品牌圖示不隨程式發佈；要顯示請自行把 `codex.svg`、`claude.svg`、`deepseek.svg`
放進 `src/vibe_launcher/assets/brand/`（不進版控）。

## 授權

Vibe Launcher 是自由軟體，以 **GNU General Public License v3.0 或更新版本**（[LICENSE](LICENSE)）釋出：你可以使用、研究、修改與分享；
散布本程式或修改版的人，也必須以相同授權並提供原始碼。第三方元件與其授權見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
Codex、Claude 等名稱屬於各自的所有者，並非本專案的一部分。
