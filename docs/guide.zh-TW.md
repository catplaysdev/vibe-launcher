# Vibe Launcher 詳細使用手冊（繁體中文）

README（[English](../README.md)／[繁體中文](../README.zh-TW.md)／[简体中文](../README.zh-CN.md)）只放簡介與入門；這裡保留各功能的完整說明、安全細節與限制。

## 語言與統一設定

介面預設為 **English**，另提供繁體中文（台灣）。從側邊欄 **Settings／設定** 或 `Ctrl+,` 開啟設定，管理語言、外觀、快捷鍵、輸入列位置、預設 shell 與 CLI 擴充。語言變更在重新啟動 Launcher 後生效；快捷鍵與輸入列位置會立即套用。帳號與 SSH 的登入設定可從同一入口前往對應頁面。

| 預設快捷鍵 | 動作 |
| --- | --- |
| `Ctrl+Shift+Space` | 啟動／離開導覽模式，反白並微放大目前位置 |
| `W/A/S/D` 或方向鍵 | 在導覽模式向上／左／下／右移動；分頁模式可切換終端 |
| `Tab` | 在導覽模式切換終端與輸入列 |
| `Enter`／`Esc` | 確認新位置／取消並返回原位置 |
| `Ctrl+Alt+PageDown`／`Ctrl+Alt+PageUp` | 切換已開啟的 Launcher 視窗 |
| `Ctrl+Shift+P` | 將輸入列移到終端上方／下方 |
| `Ctrl+=`（或 `Ctrl` 加 `+`）／`Ctrl+-`／`Ctrl+0` | 工作區終端文字放大／縮小／還原預設大小（也可按住 `Ctrl` 滾動滑鼠滾輪） |

在 **Keyboard shortcuts／快捷鍵** 可改鍵或還原預設。按鍵衝突會阻止儲存；自訂設定存於資料目錄的 `ui-state.json`。導覽模式中的移動鍵不會送到 CLI，輸入列草稿也會保留。

### Terminal 與 CLI 擴充

工作區的 **Terminal／終端機** 按鈕在專案的環境開啟終端；旁邊的下拉選單可指定這次使用的 shell 或 CLI。Windows 提供 cmd、Windows PowerShell、PowerShell 7；Linux／SSH 提供 Bash、PowerShell 7。工具必須已安裝；選擇會隨工作區保存，重開時沿用。

從 **Settings → Extensions／設定 → 擴充功能** 編輯 CLI manifest，或自行編輯資料目錄的 `settings.json`：

```json
{
  "version": 1,
  "shells": {"windows": "powershell", "linux": "bash"},
  "plugins": [
    {
      "id": "example-cli",
      "label": "Example Agent CLI",
      "platforms": ["windows", "linux"],
      "command": ["example-agent", "--interactive"],
      "profile_env": "EXAMPLE_AGENT_HOME"
    }
  ]
}
```

`command` 是執行檔與參數陣列；Windows 使用原生 `.exe`，也可填完整執行檔路徑。`profile_env` 必須是該 CLI **實際支援**的專用 `*_HOME` 或 `*_CONFIG_DIR` 變數，Launcher 將它設為私有的 `profiles/plugins/<id>`。此 manifest 提供命令與 profile 擴充；多帳號登入狀態、API 設定與額度查詢仍需各 CLI 的專用整合。登入由官方 CLI 完成，manifest 不存放密碼或 token。

### 集中維護翻譯

所有介面文字放在 `src/vibe_launcher/locales/`：`en.json` 為主要語言，`zh-TW.json` 為繁體中文，`sources.json` 記錄既有文字的來源位置。英文用語採用 Terminal、Workspace、Session、Attach／Detach、Subscription 等工具常見名稱。新增文案呼叫 `i18n.tr()`，兩份 catalog 使用相同 key，並保留 `{v0}` 等命名參數；未翻譯的文字回退到英文。

```powershell
uv run python scripts/check_translations.py --sync
uv run python scripts/check_translations.py
```

`--sync` 新增英文 source key，不覆蓋既有翻譯；請補上對應繁體中文再提交。未來新增語言時，建立另一份 catalog 並在 `i18n.LANGUAGES` 登錄語系。

## 啟動

需要 Windows、Python 3.11+、uv，以及你要使用的官方 CLI 原生 `.exe`（不執行 `.cmd`／`.bat` 包裝器）。Linux 遠端需要 `/usr/bin/python3`（3.10+）、tmux 與對應 CLI；tmux 與 CLI 會在 SSH 非互動 shell 的 PATH 中尋找，之後再自動尋找你家目錄下常見的安裝位置（`~/.local/bin`、`~/.npm-global/bin`、`~/.cargo/bin`、`~/.bun/bin`、`~/.volta/bin`、nvm 的最新 Node）。tmux 讓遠端工作在斷線後繼續運行，是 Linux 遠端必要的；工具不會替你安裝任何東西。

```powershell
uv sync
uv run vibe-launcher
```

遠端 Python 需包含標準庫 `lzma`，用於解壓縮啟動腳本；腳本仍會經 SHA-256 校驗。

建立專案資料夾內的 Windows 捷徑：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/create-shortcut.ps1
```

如果你的 `python` 指向 LibreOffice 等內嵌版本，請使用完整 Python 安裝：`uv sync --python C:\完整Python路徑\python.exe`。本次開發環境已建立 `.venv`；不用重裝。

## 第一次使用

1. **帳號**：新增 Codex 或 Claude，選訂閱／API。API 必須填模型 ID；空白 endpoint 代表官方 API。帳號名稱只是標籤，請在官方登入頁確認實際帳號。
2. **環境**：本機已內建。新增 SSH 時填現有 SSH config alias，或 `user@host`；連接埠與 ProxyJump 由你的 SSH config 處理。
3. **帳號 → 登入**：選擇環境，在新 terminal 完成登入。API key 遮罩輸入；訂閱依官方瀏覽器流程完成密碼與 2FA。
4. **專案**：填入既有絕對路徑，例如 `C:\Projects\my-app` 或 `/home/me/my-app`；遠端也接受 `~/my-app`。選帳號／環境後啟動。
5. **工作階段**：遠端視窗關閉或斷線後，選工作接回。tmux 使用 `Ctrl+B` 再按 `D` 可離開終端但保留工作。

首次 SSH 仍會要求你確認主機指紋。密碼保存關閉時，SSH key passphrase、密碼、OTP 與手機核准沿用 terminal；啟用時由原生 SSH askpass 驗證視窗接收提示。App 不自動接受 host key、不修改 SSH config、不新增 SSH key。

在環境頁選取 SSH 主機可檢視主機信任、SSH 驗證方式、OpenSSH 與 agent 狀態。第一次連線前，請透過主機管理者或其他可信任管道核對 fingerprint；指紋變更時停止連線並重新核對。工具使用自己的 host-key 設定，不會自動信任新主機金鑰。

既有 `IdentityFile` 與 `ProxyJump` 直接沿用 `%USERPROFILE%\.ssh\config`，不需要重新建立或註冊 key。環境的 SSH target 填 `Host` 後面的 alias（允許底線），不要填 `Host ` 前綴。按「SSH 設定」可在不連線、不讀私鑰的情況下列出 IdentityFile 候選路徑與 ProxyJump。候選路徑不表示該檔案一定存在或曾成功驗證。使用既有 key 請選「金鑰」或「互動驗證」；純「密碼」模式會刻意停用公鑰驗證。

主機 fingerprint 是伺服器身分，與用來登入的 IdentityFile 不同。指紋確認視窗按「取消連線」後，新啟動的 session 會標為 `failed`，可移除本機紀錄後重試。舊版留下的 `unknown` 紀錄可按「移除本機紀錄」，確認後僅移除 App 的紀錄，不會停止遠端程序；移除後也會失去該筆 App 重連入口。若要刪除環境，還需要先修改或移除引用它的專案設定。

### 減少 SSH 密碼輸入

在「環境 → 編輯」選擇「密碼（可選快取）」及記憶期限：關閉（預設）、15 分鐘、1 小時、8 小時或持續保存。一般 SSH 密碼才可啟用；密碼加 OTP 請使用「互動驗證」，OTP、私鑰 passphrase 與跳板機驗證永遠手動輸入。期限從最近一次輸入密碼起算，不因自動填入而延長；到期後下次驗證會重新要求輸入。密碼只存入目前 Windows 使用者的 Credential Manager；SQLite、匯出 JSON 與命令列都不含密碼。

保存密碼會經 OpenSSH askpass 提示使用；若連線要求 OTP、多階段或其他無法確認的驗證，請保持互動模式並手動輸入。拒絕已存密碼時會重新詢問，不連續重試舊密碼。「密碼」模式只允許 SSH password 協定；使用 SSH key／agent 請選「金鑰」或「互動驗證」。經 ProxyJump 連線時不自動保存／填入密碼，兩端驗證均手動完成。

「憑證與安全」區塊的「立即鎖定」與環境 `⋯` 選單中的「鎖定 Credential」「忘記 SSH Credential」會撤銷該環境的快取；將模式改成關閉也會清除。匯入／匯出的設定不會啟用密碼保存。期限是本工具每次取用時檢查的政策，不代表 Windows Credential Manager 到期自動刪除。Credential Manager 保護資料免於一般磁碟明文保存，但同一 Windows 使用者執行的其他程式仍可能要求讀取；它不是應用程式間的隔離邊界。若要執行不可信專案程式碼，請使用獨立 Windows/Linux 使用者或 VM。

SSH 預設不轉送 agent 或 X11；既有 SSH config 的 ProxyJump 仍可使用。Codex localhost callback 只使用 loopback forwarding。不要開啟 agent forwarding 給不受信任的遠端主機。

此模式需要支援 `SSH_ASKPASS_REQUIRE=force` 的 Win32 OpenSSH 及 Windows .NET Framework 編譯器；工具每次連線在受限資料目錄重建原生 helper，結束後清除。若你的 SSH 版本不支援，關閉保存即可使用原 terminal。真實 SSH 密碼／OTP 的主機相容性仍需人工驗收。

Codex 遠端預設 device code；若帳號尚未啟用或無法使用，從帳號登入選「瀏覽器登入」，工具會建立僅綁定 `127.0.0.1:1455` 的 SSH callback forwarding。若連接埠已被占用，關閉該登入視窗後重試或改用 device code。Claude 依官方登入提示複製網址並完成授權；callback 不通時使用 CLI 提供的手動授權碼流程。

### 遠端顯示 `codex: MISSING`

代表伺服器上沒有程式找得到的 CLI。工具先看 **SSH 非互動 shell 的 PATH**（不會讀 `~/.bashrc` 在互動判斷之後才加入的路徑），再看上面列出的家目錄安裝位置（只採用屬於你、且其他人不可寫入的目錄，且排在系統 PATH 之後）。多數安裝方式不需要改任何 PATH。仍找不到時，在自己的終端確認：

```bash
ssh 主機 'echo $PATH; which -a codex claude tmux'
```

CLI 在其他位置時，把它安裝或建立符號連結到 `/usr/local/bin`：`sudo ln -s /實際/路徑/codex /usr/local/bin/codex`。「環境檢查」只硬性要求 tmux，Codex 與 Claude 至少找到一個即可；啟動專案時才要求該帳號所用的 CLI。啟動前檢查失敗（缺 CLI、路徑不存在等）發生在建立 tmux 工作之前，會標為「失敗」；連線中斷或執行到一半結束仍標為「狀態不明」，因為無法確認遠端是否還在運行。

## 介面導覽

左側是可收合的導覽列（收合後只剩圖示，滑過會顯示名稱；狀態會被記住）。視窗窄於 1000 px 時自動縮成圖示列，窄於 800 px 時環境詳細資訊改為由右側滑入的抽屜。

**環境頁**以卡片顯示每個環境：可用／需注意／異常／未檢查，加上 Credential、主機信任、Agent、Codex／Claude 版本等狀態標籤。顏色只代表語意（綠＝正常、黃＝警告、紅＝錯誤、灰＝未知／未檢查），每個標籤也都有圖示與文字，不只靠顏色辨識。狀態只根據已存在的非機密資料判斷：環境設定、環境檢查寫下的診斷、CLI 版本報告、已保存的主機指紋與最近的工作階段。

- 「未檢查」表示還沒有任何檢查結果；執行「環境檢查」或「檢查更新」後會更新。
- 「異常」：找不到 Windows OpenSSH Client，或該環境最近一次工作階段失敗。「需注意」：key 模式但 agent 沒有金鑰、CLI 更新失敗。找不到某個 CLI 只顯示灰色「未安裝」；有新版顯示藍色「可更新」；兩者都不影響環境狀態。
- 密碼「持續保存」是你自己的設定，不是錯誤：只在標籤上以黃色提示，不會把環境標成「需注意」；「憑證與安全」區塊提供「立即鎖定」與「調整保存期限…」。「鎖定」與「忘記」是同一個動作（清除已存密碼），選單只保留一個。
- 工作階段與環境的運行狀態顏色一致：運行中（綠）、背景執行（藍）、等待驗證（黃）、失敗（紅）、已停止（灰）；有運行中工作階段的環境卡片顯示綠色「運行中 · N」。
- Credential 標籤顯示的是**設定的保存期限**（例如 `Credential · 15m`），不是倒數計時：介面不會為了顯示剩餘時間而讀取已保存的密碼。
- 主要動作「使用此環境」：若已有使用此環境的專案，跳到專案頁並以該環境篩選；否則以此環境預填「新增專案」。環境檢查、SSH 設定、編輯是次要動作；鎖定／忘記 Credential 與刪除環境收在 `⋯` 選單，刪除需確認，內建本機環境不可刪除。
- 釘選的環境固定在最上方；其餘依最近使用排序。釘選與側邊欄狀態存於資料目錄的 `ui-state.json`（只含檢視偏好，不會匯出）。
- 檢查或更新在外部 terminal 進行；等待結果期間卡片顯示「檢查中」，結果寫入後自動更新並提示。

快捷鍵：`Ctrl+K` 指令面板、`Ctrl+1…5` 切換頁面、`Ctrl+B` 收合側邊欄、`Ctrl+F` 搜尋、`Ctrl+N` 新增、環境清單中 `↑↓` 選取、`Enter` 使用此環境、`Delete` 刪除、`Menu`／`Shift+F10` 開啟動作選單。專案、帳號、工作階段清單也支援 `Enter`（開啟／編輯／接回）與 `Delete`（刪除／移除紀錄，都會先確認）。

### 首頁、設定與訊息

- **首頁**：點左上角的 logo 開啟，也是啟動後的第一頁。上方顯示執行中、需重新登入、狀態不明的數量（點一下跳到對應頁）；下面是最近 6 個專案，執行中的按「接回」、其他按「開啟」；最下方是各環境狀態。有新版本時會出現更新卡片。
- **設定**（側邊欄齒輪或 `Ctrl+,`）：左側依類別切換：一般、外觀、終端、快捷鍵、擴充、關於與更新。外觀變更即時預覽，按取消會還原。快捷鍵依用途分組、可搜尋（名稱或按鍵），重複的按鍵會標紅且無法儲存。
- **訊息**：顯示在視窗上方中央。滑鼠停在訊息上會暫停計時；錯誤訊息不會自動消失，需按 × 關閉。拖曳任一則可移動整組訊息，位置會記住；在訊息上按右鍵可放回原位。
- **新增／編輯對話框**：輸入有問題時錯誤直接顯示在對話框內，不會關掉；搭配輸入暫存，打到一半也不會遺失。
- **焦點導覽**（`Ctrl+Shift+Space`）：每個可切換的終端與輸入列會標上編號與名稱，可用方向鍵、W/A/S/D 或數字鍵直接跳過去，Enter 確認、Esc 取消；操作提示列在畫面上方。

### 外觀與主題

「設定 → 外觀」或指令面板（輸入「主題」）可切換配色：深海（預設）、One Dark、Dracula、Nord、Catppuccin Mocha、Tokyo Night，以及淺色的 GitHub Light、Solarized Light。也可以挑強調色（8 個預設色或「自訂…」任意顏色；對比過低時會提示）、終端字級與游標閃爍。選擇會立即套用到整個視窗與所有已開啟的終端，可直接預覽；「取消」還原，「完成」保存到 `ui-state.json`（與其他檢視偏好一樣不會匯出）。每套主題的文字與狀態色都維持 WCAG 4.5:1 以上的對比。

### 縮放：終端文字與整體介面

- **終端文字**：在工作區按 `Ctrl+=`（`Ctrl` 加主鍵盤或數字鍵的 `+` 也可以）／`Ctrl+-` 放大／縮小、`Ctrl+0` 還原預設 14 px，或在終端內按住 `Ctrl` 滾動滑鼠滾輪（觸控板捏合也可）。所有已開啟的終端一起調整，右上角會短暫顯示目前字級；大小保存為 `ui-state.json` 的 `terminal_font_size`，與「設定 → 外觀」的終端字級是同一個設定。按鍵可在快捷鍵頁改。
- **整體介面**：「設定 → 一般 → 顯示 → 介面縮放」可選 80%–200%，放大或縮小整個視窗（含終端、對話框與圖示）。選單下方即時顯示目前視窗左上角在該倍率下的預覽；按「儲存」後會詢問是否立即重新啟動套用（有執行中的終端時照常先確認），按「取消」不會儲存任何變更。Qt 只在程式啟動時接受整體縮放倍率，所以實際畫面在重新啟動後才改變。設定存為 `ui-state.json` 的 `ui_scale`，啟動時以 `QT_SCALE_FACTOR` 套用；若你已自行設定這個環境變數，以你的設定為準。

## 工作區與內建終端

**「工作區」頁**把所有終端放在 App 裡：每個本機終端有自己的程序，遠端終端有**自己的一條 SSH 連線**（不是 tmux 分割窗格），所以不同專案、不同主機可以混在同一個工作區；終端用 xterm.js 渲染（支援中文輸入法、全形字、Codex／Claude 的互動介面），背後是 Windows ConPTY，輸出以原始位元組傳遞。

- **分頁／並排**：右上角切換，分頁一次一個，並排最多同時顯示全部（2×2、3×2…）；背景分頁的終端照常執行。左側「終端」清單依專案分組並顯示狀態，點選或用 `Ctrl+Tab`／`Alt+1…9` 切換。
- **快速開啟**：左下角每個專案一列，按鈕 **Codex／Claude／Terminal**。Terminal 在專案綁定的環境開啟 shell；相鄰的下拉選單可指定 shell 或 CLI 擴充。專案頁也有「終端機」按鈕，指令面板可直接輸入專案名稱開啟。
- **辨認帳號與調整位置**：終端標題、左側清單與指令面板會顯示帳號名稱（這是 Launcher 的帳號標籤，實際登入身分仍以官方 CLI 為準）。選取終端後按清單上方 **↑／↓**，可往前／後移一格；並排依左至右、上至下排列，不中斷連線。相鄰的同專案終端會分組。排列後儲存工作區，可保留順序、帳號與環境；以相同名稱儲存即可更新。
- **切換 Terminal 與 Codex／Claude**：終端標題列的「切換」按鈕，列出專案綁定環境中可開的其他 Agent 與終端機。
- **離開但保持連線／執行**：標題列「離開」（`Ctrl+Shift+D`）對使用 tmux 的 SSH 終端送出 tmux 的 detach，終端關掉、工作留在伺服器；之後可從工作階段頁或工作區重新接回。**不必按 `Ctrl+C`，也不必直接關視窗**。沒有使用 tmux 的終端離開就會結束，這時只要切換到別的分頁，連線會留著。關閉分頁（`Ctrl+Shift+W`）對 tmux 終端同樣是先離開再關閉。
- **已儲存的工作區**：開好一組終端後，「工作區」按鈕 → 「將目前的終端儲存為工作區…」，之後一鍵重新開啟整組（含排列方式）。工作區會一併匯出／匯入。
- 終端內的 `Ctrl+B`（tmux）、`Ctrl+C`、`Ctrl+K` 預設送進終端。工作區切換、導覽、設定與視窗切換等 App 動作使用設定頁列出的快捷鍵，可自訂。選取文字後按 `Ctrl+C` 或 `Ctrl+Shift+C` 複製；沒有選取時 `Ctrl+C` 仍是中斷。
- **輸入列（中文輸入）**：Codex／Claude 終端下方預設有一個原生輸入框（標題列的鍵盤按鈕或 `Ctrl+Shift+E` 開關／切換焦點）。在這裡打中文，輸入法的選字框就在輸入處旁邊，可自由移動游標修改；`Enter` 送出、`Shift+Enter` 換行，多行內容會以一次貼上送出，不會中途被 Enter 送出。`Esc`、`Ctrl+C`（沒有選取時）一律傳給程式；輸入框空白時 `↑ ↓ Tab Shift+Tab Enter` 也傳給程式，方便操作選單。`Ctrl+↑／↓` 叫回送出過的內容。SSH 終端預設不顯示，可在「外觀」調整預設。
- 登入驗證、host key 確認與 OTP 仍在終端內完成。遠端印出的任何內容都只當資料處理：網頁只能載入打包在 App 內的檔案、禁止導覽，也沒有可點擊的連結。
- 想用舊的獨立視窗：指令面板「改用外部終端視窗」。內建終端無法啟動時（例如系統不支援 ConPTY）會自動改用外部視窗。關閉 App 時若還有連線中的終端會先詢問：tmux 工作繼續，直接執行的工作會結束。

## 專案狀態、tmux 開關與工作階段

**每個專案顯示自己的狀態**：運行中（綠）、背景執行（藍）、連線中（藍）、等待驗證（黃）、已停止（灰）、失敗（紅）、狀態不明（灰）；從未啟動的專案不顯示標籤。專案有多筆工作階段時，優先顯示仍可能存活的那一筆。專案詳細資訊也會寫出「狀態」與「執行方式」。

**「關閉終端後讓工作繼續執行」開關**（新增／編輯專案，預設開啟）：

| | 開啟（預設） | 關閉 |
|---|---|---|
| SSH 專案 | 在伺服器的 tmux 中執行。可以關閉終端，agent 繼續跑；之後「接回終端」。**伺服器需要安裝 tmux** | 直接在 SSH 終端執行，**不需要 tmux**；關閉終端或斷線就結束，無法接回 |
| 本機專案 | 不適用：CLI 在原本的終端視窗執行，關閉視窗就結束 | 同左 |

tmux 只在「開啟」的 SSH 專案啟動時才是必要條件；單純的環境檢查與帳號登入不需要它。

**工作階段頁**是「每次啟動留下的一筆紀錄」：顯示它現在的狀態，並提供三個動作。
- **接回終端**：重新開啟 SSH 終端，接回仍在伺服器 tmux 中運行的工作（像重新打開被你關掉的視窗）。只有使用 tmux、且仍在運行或狀態不明的 SSH 工作階段能用。
- **停止工作階段**：結束該工作。
- **移除本機紀錄**：只刪除這台電腦上的紀錄，不會停止遠端工作。

按鈕只在有意義時才可點，選取一筆後下方會用一句話說明目前狀況。**被直接關掉的終端**會讓本機再也收不到結果，這類工作會自動變成「狀態不明」（遠端 tmux 工作可能仍在跑），按「更新工作狀態」即可向伺服器查詢。互動期間無法即時更新狀態，所以你在終端裡時會看到「連線中」，離開後才變成最終狀態。

## Skills、hooks 與多 agent 工作流

每個帳號都有自己獨立的設定資料夾（`CLAUDE_CONFIG_DIR` / `CODEX_HOME`），所以預設看不到你 `~/.claude`、`~/.codex` 裡的東西。在帳號頁明確執行「匯入設定」時，Launcher 才把白名單內的個人設定與擴充**複製**進該帳號的資料夾；日常啟動不掃描來源：

| 工具 | 會共用 | 絕不複製 |
|---|---|---|
| Claude | `skills/`、`agents/`、`commands/`、`hooks/`、`output-styles/`、最上層 `*.md`（CLAUDE.md 及其 `@` 引用檔）、`settings.json` 的 `model`/`hooks`/`permissions`/`statusLine`/`theme`/`effortLevel`/`env`（已濾掉憑證與路由）；匯入時記錄已啟用外掛路徑，啟動以 `--plugin-dir` 使用該副本清單 | `.credentials.json`、`apiKeyHelper`、`ANTHROPIC_*` / `CLAUDE_CODE_USE_*` / `*TOKEN*` / `*KEY*`；對話需另行明確匯入 |
| Codex | `skills/`、`prompts/`、`agents/`、`rules/`、`AGENTS.md`、`hooks.json`、`config.toml` 中的 `mcp_servers`/`features`/`hooks`/`plugins`/`tui` 等 | `auth.json`、`model_provider`、`model_providers`、`forced_login_method`、專案信任清單 |

- 只複製，不建立連結（資料資料夾禁止 symlink）。再次匯入只更新仍等於上次副本的項目；個人修改、個人刪除及來源已移除的內容都保留，不覆寫既有個人檔案。
- SSH 遠端是同樣邏輯：明確匯入的是**遠端主機**上的 `~/.claude` / `~/.codex`，不是本機的。
- 專案層級的 `.claude/`、`CLAUDE.md`、`.mcp.json`、`AGENTS.md` 一直都能正常使用，不受影響。
- 多 agent 的功能旗標請寫在 Claude `settings.json` 的 `env`（例如 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`）或 Codex `config.toml` 的 `[features]`，它們會跟著共用；行程環境變數中的 `CLAUDE_*` / `CODEX_*` 仍會被清除，避免把另一個帳號的路由帶進來。
- 若有 `mcp_servers` 內含金鑰，設定匯入會一併複製到帳號資料夾（僅限你本人可讀）；請在匯入前檢查來源，設定匯出不包含 profile 內容。

### 保留 CLI 個人設定與匯入對話

- Claude 的個人設定若在帳號 profile 中調整或刪除，再次匯入也會保留；沒有改過的設定可按需更新。Codex 一旦修改 profile 的 `config.toml`，就保留整份檔案；舊 profile 無法判定是否修改過時，同樣保留。來源移除的設定不自動刪除。帳號表單明確指定的模型仍優先。
- 在帳號頁明確執行「匯入對話」。來源是該執行環境的 `~/.codex` 或 `~/.claude`，不是跨主機搬移，也不讀取 ChatGPT／Claude 網頁聊天。舊自動同步／history 偏好遷移為手動並提示一次，既有 profile 與對話保持原樣。
- 匯入只新增不存在的對話檔，不覆寫 profile 已有對話、不回寫來源、不搬移登入憑證。來源可能包含多個專案或帳號曾用過的對話；只在要接收這些內容的帳號執行。之後有新紀錄時，再明確匯入。
- **專案對話跨帳號共用**：帳號編輯裡勾選「與同工具的其他帳號共用各專案的對話紀錄」後，開啟該帳號的專案終端前，會把同一環境、同工具其他帳號 profile 裡「屬於這個專案」的對話檔複製過來（Codex 以 rollout 第一行記錄的工作目錄比對，Claude 以 `projects/` 下的專案資料夾比對）；只新增不存在的檔案，不覆寫、不碰憑證、設定與輸入歷史。已經開著的終端，其他帳號之後才產生的對話可按終端標題列的更新鈕（或再開新終端）重新同步，接著在終端內用 `/resume` 選用；寫入中或未完整的檔案本次略過，下次更新再補。
- 支援 Codex `sessions/` 的檔案型 rollout 與 Claude `projects/` 的對話 JSONL；資料庫型 Codex 對話不在此匯入範圍。終端會顯示匯入／略過結果。匯入是副本，來源後續新增的訊息不會覆蓋已匯入對話；上下鍵的輸入歷史也不會合併。
- 匯入後在原專案的官方 CLI 使用 `/resume` 選擇對話；其他專案的紀錄可能被 CLI 的專案篩選隱藏。封存、壓縮、未完整寫入或超過 64 MiB 的檔案不匯入，每次最多複製 256 MiB、掃描 10,000 個項目。[Codex 對話儲存實作](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder.rs)／[Claude 對話文件](https://code.claude.com/docs/en/sessions)。

## 帳號、專案與路徑

### 終端流量控制與同機效能量測

ConPTY 到 Qt、Chromium、xterm 的未確認原始輸出共同限制為每端 256 KiB。xterm 完成寫入才釋放額度；消費者變慢會使來源等待，不截斷 UTF-8 或控制序列。關閉終端解除等待並終止本機工作；正常輸出停止後會繼續排空。

終端預設使用 xterm.js 的 DOM 渲染器：WebGL 渲染器在 Qt WebEngine（Windows）中重繪時可能出現斜角黑塊閃爍。若要比較或改回 WebGL，啟動前設定環境變數 `VIBE_TERMINAL_WEBGL=1`。

使用 `scripts/benchmark_workspace.py` 分別量測 0／4／8 個合成終端。腳本記錄行程樹 CPU、RAM、切頁到 JavaScript 回應的延遲、各端積壓／峰值及排空時間，並用 `widget.grab()` 保存視窗截圖；不讀取帳號或專案資料。

```powershell
.venv/Scripts/python.exe scripts/benchmark_workspace.py --terminals 0 --duration 60 --output .benchmark/control-0.json
.venv/Scripts/python.exe scripts/benchmark_workspace.py --terminals 4 --duration 60 --output .benchmark/control-4.json
.venv/Scripts/python.exe scripts/benchmark_workspace.py --terminals 8 --duration 1800 --output .benchmark/stress-8.json
```

同機比較須採相同 duration、輸出速率與 renderer 模式；CPU 百分比以一核心為 100%，RAM 包含 WebEngine／ConPTY 子程序。`--offscreen --software` 可用於離屏軟體繪圖，但結果不代表正常桌面 GPU 效能。沙箱若阻止 renderer 啟動，30 秒後標記 `startup_unavailable`；未成功啟動或未排空不能算驗收通過。切頁量測是 UI 與 renderer 回應時間，並非 GPU 畫面呈現完成時間。

- **訂閱帳號綁定一個環境。** Codex／Claude 的訂閱登入是存在「做登入的那台機器」上的，Launcher 不會把登入權杖搬到別台（那等於把憑證複製出去）。所以每個訂閱帳號要選一個「登入環境」，只登入一次，之後在該環境的所有專案都直接可用；要在另一台主機用，就為那台再建一個帳號。**API／Endpoint 帳號不綁定**，哪個環境都能用。
- **專案不綁帳號。** 專案只有名稱、資料夾與環境。開啟專案時會跳出「用哪個帳號」的選單（只列出能在該環境使用的帳號，Codex 與 Claude 分開標示、顯示是否已登入）；只有一個可用帳號時直接開。勾「下次直接使用這個帳號」可略過詢問（之後可在專案設定重新打開）。「SSH」終端完全不需要帳號。
- **路徑輸入。** 新增專案時，路徑欄會建議同環境用過的路徑；本機環境有「瀏覽…」可挑資料夾，並即時提示「已存在／不存在（會建立幾層）／不是完整路徑」。
- **資料夾不存在時。** 本機專案在儲存時、SSH 專案在啟動時（直接在終端裡）會列出沿途每一層資料夾，**已存在的用灰色、將建立的用醒目色標示「將建立」**，確認後才建立；選「先不要」就不會動任何東西。

## 標籤與刪除

- **標籤**：在專案設定的「標籤」欄用逗號分隔輸入（例如「工地, 前端」），下方會列出已用過的標籤，點一下就能加入或移除。每個專案最多 12 個、每個最長 24 字，大小寫視為相同。
- **篩選**：專案頁與工作區的「快速開啟」都有標籤列，點選即篩選；可複選，**同時符合**所有選取的標籤（越選越少）。兩邊的篩選各自獨立，並且會記住上次的選擇，下次開啟直接套用。沒有任何專案有標籤時，標籤列不會出現。
- **刪除**：刪除專案時，它過去的工作階段紀錄會保留（遠端可能仍在跑，仍可接回或停止），只是不再連到這個專案。刪除帳號會清除各專案的「上次使用帳號」建議與該帳號已結束的紀錄；若該帳號還有執行中的工作階段，會明確請你先停止。刪除環境前須先處理使用它的專案，畫面會列出是哪些專案。

## 專案只在它綁定的環境開啟

專案建立時選定執行環境（本機或某台 SSH 主機），之後開啟、召回、重開都只在那個環境：

- 「工作區」頁的專案卡片不再有「在其他環境開啟」；終端的「同專案改開其他終端」只會列這個環境的 Codex／Claude／SSH 終端。
- 服務層也會拒絕「專案 + 其他環境」的啟動請求。要換主機，到「專案」頁編輯專案。
- 舊版在其他環境開過的工作階段不會被召回或重開；舊的工作區存檔裡指向其他環境的項目，會改開在專案自己的環境並重新選帳號。

## 遠端專案路徑：瀏覽主機上的資料夾

新增／編輯專案時，環境選 SSH 後「瀏覽…」也能用：

- 會連線到主機（不開終端）列出資料夾：「上一層」「家目錄」、可直接輸入路徑按「前往」、雙擊進入，預設隱藏 `.` 開頭的資料夾（可勾選顯示）。
- 路徑不存在時會顯示最近的上層，並提示；仍可選它，之後啟動時會詢問是否建立。
- 主機只回傳資料夾名稱；使用同一套受管理的 OpenSSH 設定（主機指紋、驗證方式）。需要密碼、OTP 或確認指紋時跳出小視窗輸入。環境沒有設定「記住 SSH 密碼」時，視窗只用這一次、不保存。
- 每按一次資料夾就是一次連線；用 SSH key，或在環境設定「記住 SSH 密碼」，就不會一直被問。

## 專案圖示

在「專案」頁點詳細資料左上角的圖示（或「⋯ → 變更圖示…」）可以改成：內建圖示，或自己的 SVG／PNG／JPG／WebP（最大 1 MB）。內建圖示與 SVG 可選顏色（主題色、預設色票或自訂）；照片保留原色。首頁「最近的專案」、專案清單與詳細資料都顯示這個圖示（不再用帳號的 Codex／Claude 圖示）。

- 按「儲存」才生效；「取消」不會變更任何東西。「使用預設」回到資料夾圖示。
- 選擇的檔案會檢查能否讀取，再以隨機檔名複製到資料目錄的 `project-icons/`；換圖或刪除專案時會一併刪除舊檔。SVG 由 Qt SVG 繪製，不會載入外部資源或執行腳本。
- 設定存在 `ui-state.json` 的 `project_icons`（與釘選環境一樣屬於檢視偏好，不寫入專案資料、不隨設定匯出）。

## 配色與圖示

每個區域有一個固定的色相，只用「低透明度的底色方塊＋同色圖示」呈現，不鋪大面積實色，避免長時間盯著看眼睛疲勞：

- 專案＝主題強調色、工作區＝藍、帳號＝紫、環境＝琥珀、工作階段＝綠。色相取自目前主題的語意色，切換主題會一起換。
- 左上角的貓與機器人 logo（`src/vibe_launcher/assets/logo.svg`）線條固定為黑色，放在米白色圓形底盤上，深色與淺色主題都清楚；機器人眼睛用強調色。視窗與工作列圖示相同。
- 側邊欄每個項目、每頁標題左邊都有同色的圖示方塊；目前所在頁的底色與左側色條也用該頁的色相。
- 清單列左側有圖示方塊：專案預設是資料夾，可自訂（見下方「專案圖示」）；工作階段依服務（Codex／Claude／SSH 終端）換圖示，並用狀態色（運行中綠、背景執行藍、等待驗證黃、失敗紅、已停止灰）。
- 服務各有固定顏色：Codex 藍、Claude 橘（品牌色）、終端機綠（取自主題的 ANSI 綠）、GitHub 灰；帳號頁群組標題與工作區「快速開啟」按鈕都用這組顏色。
- 專案頁的「工作區」是一般段落（標題、摘要、卡片，右側 `+` 再開一個），卡片和下方基本資料同樣是凹陷底；工作區頁左側的專案卡片按鈕會換行，沒有終端時終端清單收成一行提示。

## 專案的工作區抽屜

同一個專案可以同時開很多個工作區（Codex、Claude、SSH 終端，不同帳號各一個）。專案頁選取專案後，右側標題下方的「工作區」抽屜會列出它**每一個**工作區：

- 每列顯示服務與帳號、環境、更新時間，以及各自的狀態。
- 已在「工作區」頁開著的，按「切換」直接跳過去；SSH 上仍在背景執行的，按「接回」。
- 每列可單獨停止、移除紀錄；「再開一個」會再開啟這個專案（可選別的帳號）。
- 專案清單的狀態標籤在有多個進行中的工作區時會寫成「運行中 ×2」。
- 抽屜可以收合；已結束的排在後面。

### 召回：接回還在跑的、重開已停止的

- 已停止（結束、失敗）的工作區，抽屜那一列有「重新開啟」：用原本的服務、帳號、環境再開一次，舊的已停止紀錄會被新的取代。
- 抽屜上的「召回全部（N）」，以及專案頁的「接回工作」按鈕，一次處理這個專案的所有工作區：
  - 還在 SSH／tmux 上執行的：接回。
  - 已停止的：重新開啟。同一組（服務與帳號、模式、環境）只重開最新的一筆；那一組若已經開著或正在連線就跳過。
  - 帳號已被刪除、或登入不在該環境的，會略過。
  - 超過一個時會先列出清單讓你確認；SSH 的每個終端可能各自需要驗證。
- 在「工作區」頁：左側「快速開啟」的專案卡片在有可召回的工作區時會出現「召回 N」按鈕；終端清單的專案標題與終端的右鍵選單、卡片的「⋯」選單也有「召回全部工作區」。

## 工作階段整理

- **為什麼會有「狀態不明」**：直接關掉終端視窗時，App 無法確認遠端工作是否還在跑。**本機**的工作離開終端就必然結束，所以現在直接標為「已結束」，不再算不明；只有 SSH（tmux）的才可能不明。
- **工作階段頁**頂端會統計各狀態的數量，並提供「檢查狀態不明的」（本機的直接結案；SSH 的每個主機只開一個終端重新確認）與「清除已結束的」。
- **依專案分組**（預設，可在工具列切換「依時間」，選擇會記住）：每個專案一組，標題列出環境與「進行中／不明／已結束」數量。有進行中工作的組排最前，其次是狀態不明的，只剩已結束工作的組預設收合（篩選「已結束」時會展開）。組標題可「全部召回」（接回仍在跑的、重開已停止的）或「清除已結束」（只清這個專案的）。專案已刪除的工作放在「其他工作」組。
- **可複選**（Ctrl／Shift）：一次停止（同一個環境只開一次連線，全部處理完）或一次移除紀錄。
- **開啟時提醒**：啟動 App 時若上次留下仍在進行或狀態不明的工作，會提示數量；已結束／失敗的紀錄只保留最新 20 筆，其餘自動清掉。

## 工作區：選帳號

「快速開啟」每個專案的 Codex／Claude 都是分裂按鈕：**點名稱**用預設帳號開（上次用的或第一個可用的），**點旁邊的 ▾** 選擇要用哪個帳號（只列出能在該環境使用的）。「⋯」改為「在其他環境開啟」。

## 帳號配額

「帳號」頁選到 **Codex 訂閱**帳號時，下方會出現「用量配額」：按「查詢配額」可看到 **5 小時**與**每週**兩個額度的已用／剩餘百分比、重置時間點（含倒數），以及方案、額外點數與可用的額度重置券。

- 資料來自 Codex 官方的 `codex app-server`（與 CLI 內的 `/status` 同一來源）；Launcher 不會讀取或轉送你的登入權杖。
- 登入資料存在各環境，所以多個環境時可在面板右上角選擇要查哪個環境上的帳號。本機直接查詢；SSH 環境會在工作區開一個終端執行（需要輸入密碼時才有地方輸入），完成後自動更新。
- **API / Endpoint 帳號不顯示**（依用量計費，沒有額度視窗）。Claude 訂閱沒有給外部程式查詢的介面，請在 Claude CLI 內輸入 `/usage`。
- 結果快取在資料資料夾的 `quota/`，只含百分比與時間，不含帳號識別或憑證。

## 檢查與更新 CLI

在「環境」選擇本機或 SSH 主機，在「開發工具」區塊按「檢查更新」：顯示 Codex／Claude 目前版本、實際執行路徑、上游 npm latest 版本、更新來源與檢查時間。無法取得上游版本時，原始報告與「診斷資訊」仍如實記錄 unknown；主畫面則不顯示警告，只要沒有偵測到新版就視為已是最新。操作在外部 terminal 執行，SSH 密碼／OTP 仍由既有驗證流程處理。

已是最新版本時不顯示更新按鈕；有新版（或上次更新失敗）時該工具旁出現「更新」／「重試」，也可從「開發工具」區塊的 `⋯` 或指令面板（Ctrl+K）選「更新 Codex／Claude」，之後選擇沿用目前來源；Snap 可明確改選 latest/stable 或 latest/edge。工具辨識 Snap、與當前執行檔吻合的 npm 全域套件，或 CLI 自帶 update 指令。無法確認來源時停止並要求使用原安裝器，不下載任意安裝腳本、不改 PATH，也不自行換安裝來源。Windows 的 npm `.cmd` 包裝器目前不支援直接更新，請使用原安裝器或原生 CLI 更新指令。

Snap 更新會在 terminal 顯示發布者及頻道，輸入 yes 後才執行 `sudo snap refresh <tool>`；sudo 密碼不由 App 保存。Snap 頻道版本可能落後上游，例如 stable 沒有新套件時，refresh 成功仍會維持原版。edge 較新但可能較不穩定，須由使用者選擇。

更新後重新執行 `--version` 並重新解析 PATH。若版本未變，會明確提示檢查頻道、權限或重複安裝；更新命令退出成功不等於已更新到上游最新版本。新啟動的工作會使用重新解析的執行檔，遠端 tmux worker 使用當次 SSH 的 PATH；已執行的工作不會被重啟，接回舊工作仍是原程序。更新期間請等 terminal 完成再啟動新工作。

更新方式參考：[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)、[Claude 更新文件](https://code.claude.com/docs/en/setup#update-claude-code)。

## 更新與版本

版本號採 `重大改動.功能新增.小幅修正`，首次發布為 `v0.1.0`：第一碼增加代表必須更新的重大改動；第二碼增加代表可選擇更新的新功能；第三碼增加代表小幅改動，通常是修復 bug。增加第一或第二碼時，後面的數字歸零。這是發布政策，目前程式仍需使用者確認才套用更新，尚未強制更新。

版本號只有一個來源：`pyproject.toml` 的 `version`；每次發佈在 `main` 打 `v<版本>` tag，變更寫在根目錄 `CHANGELOG.md`。從 git checkout 執行時，launcher 會 `git fetch --tags` 比對，**只列出比目前安裝版本新的版本**與各版說明（`update_check` 可關閉自動檢查，結果快取在資料資料夾的 `app-update.json`）。

更新在內建終端執行 `python -m vibe_launcher.updates apply --repo <程式目錄> --data <資料資料夾>`：先確認程式目錄沒有未提交的修改，再把資料庫、`ui-state.json`、`settings.json` 備份到 `<資料資料夾>\backups\<版本>-<時間>\`（保留最近 10 份），接著 `git pull --ff-only` 與 `uv sync`，完成後重新啟動。`python -m vibe_launcher.updates check` 只檢查並輸出 JSON。

### 安裝版（exe）的更新

用安裝程式裝好的版本（`Vibe-Launcher-Setup-<版本>.exe`，只裝給目前使用者）沒有 git checkout，改從 GitHub Releases 更新：

- 檢查：啟動後與之後每六小時最多一次，只送出一個很小的 HTTPS 請求（`update_check` 可關閉）；結果與版本說明格式和 git 版相同。Release 要同時有 `Vibe-Launcher-Setup-<版本>.exe` 與 `SHA256SUMS.txt` 才會被列為可更新（上傳完成前不會被看到）。
- 更新：按下更新後依序是 查詢 → 備份資料 → 顯示會被移除／變更的設定 → 下載（分段寫入磁碟，不佔記憶體）→ 核對 SHA-256（不符就不執行，也不留下檔案）→ 靜默啟動安裝程式。安裝程式會關閉 Launcher、換檔、再自動開啟新版；仍在執行的本機終端會結束，以 tmux 保留的 SSH 工作不受影響。
- 資料在 `%LOCALAPPDATA%\VibeLauncher`，不在安裝資料夾，安裝、更新、解除安裝都不會動它。更新來源是公開的 GitHub Releases（`updates.RELEASE_REPO`）；目前沒有程式碼簽章，Windows SmartScreen 可能顯示警告。
- 這個版本只需要有 Windows 內建的元件；`codex` / `claude`、tmux、OpenSSH 等仍照原本的需求另外安裝。
- 資源：打包是資料夾版（不是單一 exe，避免每次啟動解壓縮），沒有常駐服務或系統匣程式；介面本身閒置時約 95 MB 記憶體（尚未開終端），開啟內建終端後 Qt WebEngine 會另外使用記憶體。

打包與發佈流程（給維護者）：`packaging/build.ps1` 產生 `dist-app\Vibe Launcher\` 並自我檢查（`vibe-launcher-cli --selftest`），裝有 Inno Setup 6 時再產生安裝程式與 `SHA256SUMS.txt`；推送 `vX.Y.Z` tag 會由 `.github/workflows/release.yml` 執行測試、打包並建立 Release。細節與注意事項寫在 `AGENTS.md` 的「Packaging, updates and licence」。

更新只會「新增」設定，不改寫既有設定：新版本寫入的欄位，舊版本讀寫時原樣保留；舊版本不認得的值也不會被改回預設。若某版本移除或改變設定，會在 `CHANGELOG.md` 的 `### 設定變更` 列出，更新前顯示清單提醒。資料庫遷移前會先備份到 `backups\db-before-v<N>-<時間>.sqlite3`；較新版本建立的資料庫在舊版本中只能讀取。

## 帳號與憑證

- 每個帳號的 Codex `CODEX_HOME`／Claude `CLAUDE_CONFIG_DIR` 分開；同一帳號在每台主機第一次各登入一次。
- 工作啟動後固定使用當時的帳號／模型／目錄；編輯收藏不會更改執行中的工作。
- 帳號的工具與驗證類型、SSH 環境的連線目標建立後不可改；請新增另一筆，避免既有工作被導向不同帳號或主機。
- Windows API key 使用 Credential Manager（目標名稱 `VibeLauncher/<tool>/<id>`）；Linux API key 位於工具專屬 profile 的 `api-key`，權限 `0600`，目錄 `0700`。
- OAuth token 由官方 CLI 保存／更新。工具不匯出憑證、不分發 OAuth token，不將 API key 放在命令列或一般設定中。
- `configured` 僅表示 API key 已設定，不代表 endpoint／額度已驗證。實際 API 錯誤由官方 CLI 顯示。
- 子程序會清除繼承的 Codex／Claude／OpenAI／Anthropic 路由變數。Claude 設定檔若另有 provider 或 auth override，會停止並提示修正，不會刪除你的設定。
- Codex 啟動前檢查 `--help`；支援時加入 `--no-daemon`，舊版不傳入不支援的選項。Snap 安裝保留 `/snap/bin/codex` 的指令名稱，不展開成 `/usr/bin/snap`。
- 自訂 Codex endpoint 必須支援 Responses API，Claude endpoint 必須支援 Anthropic 協定；本工具不轉換 API 協定。
- API endpoint 要求 HTTPS；HTTP 僅允許明確勾選的 loopback。變更 endpoint 後必須重新輸入 API key，改回舊網址也不會復用原 key。舊版未綁定 endpoint 的 key 第一次使用時也需重新輸入。

## 資料位置與狀態

本機資料：`%LOCALAPPDATA%\VibeLauncher`。可用 `VIBE_LAUNCHER_HOME` 改成其他專用資料夾。遠端資料：`~/.local/share/vibe-launcher`。

SQLite 保存帳號標籤、環境、收藏、狀態及時間；版本 2 匯出包含 AI／GitHub 帳號、環境、專案與工作區的非機密設定，仍接受版本 1 匯入。登入狀態、憑證位置、repository credential helper 與 Web 服務程序資訊不匯出；匯入後重新檢查登入及 repository 綁定。匯入依 ID 合併，驗證失敗整批取消。不要在名稱、URL 或路徑填入秘密。

SSH helper 是此專案的標準庫 Python 模組，每次連線以 SHA-256 命名部署，與已執行工作的版本分離。不使用 SCP，因此一次操作只需一次 SSH 驗證。tmux 使用獨立 socket `vibe-launcher`，不干擾其他 tmux 工作。

GUI 定時讀取本機狀態；遠端 CLI 的 terminal 輸出不作為狀態證據。正常離開（在 CLI 內結束、按 Ctrl+C 離開 CLI，或用 `Ctrl+B` 再按 `D` 離開 tmux）時，遠端監督程式會用自己的**退出碼**回報結果，狀態會直接變成「已停止」、「背景執行」或「失敗」。連線中斷、SSH 失敗、tmux 無法確認等無法判斷的情況仍顯示 `unknown`，請點「更新」透過獨立的非 PTY 查詢確認 tmux 狀態，可能再次要求 OTP。沒有常駐網路輪詢。`unknown` 不代表遠端程序已停止。主機重啟不保留執行中的程序。

本機 CLI 使用原 terminal；本機工作沒有 tmux 的斷線保留功能。終止工作會中止 CLI，請先確認已完成必要儲存。刪除設定不會刪除專案檔案或 AI 帳號憑證；刪除 SSH 環境時會先撤銷它的 SSH 密碼快取。

## 連線安全工具

- [KeePassXC SSH Agent](https://keepassxc.org/docs/)：免費開源的 SSH key 管理選項，可在 KeePassXC 解鎖期間提供 key。Windows OpenSSH agent 對 key 到期／逐次確認的支援需按實際版本測試；不要假設期限到就一定撤銷。
- [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh)：自有 Linux 主機可考慮透過私有網路連線。先確認一般 OpenSSH 連線穩定，再調整公開 SSH 入口；Tailscale 的重新驗證政策需由管理者另行設定。
- [1Password SSH Agent](https://developer.1password.com/docs/ssh/agent/security/)：付費選項，提供 key 使用授權與安全提示，非本工具必要依賴。
- [Smallstep SSH certificates](https://smallstep.com/docs/ssh/how-it-works/)：適合多主機或團隊導入短效 SSH 憑證；需規劃並維護 CA，個人版不要求安裝。

## 測試

```powershell
uv run --group dev pytest -q -o cache_dir=.local-test/pytest-cache
```

測試使用模擬 CLI／SSH 回應與隔離資料夾，不會使用你的真實訂閱或 API key。真實 SSH key／OTP、兩個訂閱帳號的瀏覽器登入，以及付費 API 請依 [人工驗收清單](acceptance.md) 驗證。

### 開發流程

1. 從 `main` 開分支（`feat/…`、`fix/…`），改完在本機跑上面的測試。
2. 推上 GitHub 開 PR；CI 會在 Windows 上以離屏 Qt 跑完整測試，PR 範本列出要勾的檢查。
3. CI 通過後合併到 `main`。Dependabot 每月提醒更新 GitHub Actions 與 Python 套件。

協作規則（不進版控的檔案、命名、範例只用通用值）寫在 [AGENTS.md](../AGENTS.md)，給人和 coding agent 共用。

**品牌圖示**：Codex／Claude／DeepSeek 的商標不隨程式發佈，預設顯示通用圖示。要顯示官方圖示，請自行下載它們的 SVG，命名為 `claude.svg`、`codex.svg`、`deepseek.svg`，放進 `src/vibe_launcher/assets/brand/`（此資料夾不進版控）。

## GitHub 登入與專案綁定

在專案的「⋯」選「GitHub 設定…」，一頁三步：

1. **GitHub 帳號**：選既有帳號，或按「登入新帳號…」用官方 `gh` 登入，帳號名稱會自動改成登入帳號。開啟頁面時會自動檢查登入並讀取 repository。
2. **Commit 身分**：名稱與 email 存在 GitHub 帳號上，登入後預設帶入 GitHub noreply email（`ID+帳號@users.noreply.github.com`，GitHub 會把 commit 歸到該帳號，又不公開真實 email）。
3. **Repository**：選 remote，或「不設定 remote（之後可補）」、「新增 origin…」。還沒 `git init` 的資料夾可以先只綁帳號，或直接按「在此資料夾執行 git init」。

按「儲存綁定」即完成。Launcher 在這個專案開啟的 Codex／Claude／SSH 終端會帶入綁定帳號：push 到 github.com 走該帳號的官方 gh helper（`GIT_CONFIG_COUNT`），commit 用上面的身分（`GIT_AUTHOR_*`／`GIT_COMMITTER_*`），不受全域 Git 設定影響，也不改全域設定。勾選「同時寫入此 repository 的 Git 設定」（預設勾選）時，會先列出要寫入 `.git/config` 的差異，再按「確認寫入並綁定」，讓你在 Launcher 以外手動 commit／push 也用同一帳號。SSH remote 會改成同一 repository 的 HTTPS 網址（列在差異中），否則 push 用的是你的 SSH key。

**登入後還是同一個帳號？** `gh auth login --web` 授權的是**瀏覽器目前登入 github.com 的帳號**。要登入另一個帳號，先在 github.com 右上角切換帳號，或把終端顯示的網址貼到無痕視窗。用「登入新帳號…」登到已存在的帳號時，新紀錄會自動移除並改選既有帳號，不會出現重複。Launcher 的 GitHub 帳號與你自己終端裡的 `gh auth status` 互不相干：每筆帳號有獨立 `GH_CONFIG_DIR`，Launcher 不接收 token、不呼叫 `gh auth switch`，也不讀取認證檔。若官方 gh 無法使用 credential store，它可能自行退回明文保存，請留意官方提示：[gh auth login](https://cli.github.com/manual/gh_auth_login)。

帳號頁也能新增、登入、檢查 GitHub 帳號並編輯 commit 身分；移除紀錄不刪除官方 profile 或憑證。需要該環境已安裝 Git 與官方 GitHub CLI，目前支援 `github.com` 與 HTTPS。預覽後設定若被外部改動須重新預覽；解除綁定只還原仍符合 Launcher 寫入值的項目，保留你之後的修改。同一帳號再次綁定（例如之後補 origin）會併入原紀錄；repository 已綁定其他 GitHub 帳號時須先解除。

GitHub 檢查、讀取等背景操作在工作區終端以帳號名稱標示；成功結束後終端自動關閉，結果回到頁面，失敗才保留輸出。重複檢查會取代上一個已結束的同類終端。

## 輸入暫存

新增／編輯專案、環境、帳號，以及 GitHub 頁的輸入會邊打邊存到 `<資料夾>/drafts`（私有檔，7 天過期）。不小心關掉對話框或儲存被拒時，重開會還原並提示，可按「捨棄並還原原值」。儲存成功後草稿刪除。輸入列（Codex／Claude 終端下方）沒送出的文字也會保留，同專案同工具重開時還原。密碼欄與名稱含 password／token／secret／api_key 的欄位一律不暫存。

## 生產環境驗證

資料庫格式與手動匯入偏好遷移已在既有資料庫備份副本驗證；保留原紀錄，通過 SQLite integrity／foreign key 檢查及重複遷移檢查。狀態資料未改變時不重繪清單；更新失敗保留上次資料並在狀態列提示，限頻診斷只記錄操作名稱與例外類型，最多保留三個 64 KiB 檔案，不收集終端全文。

同機合成量測使用 `scripts/benchmark_workspace.py`。8 終端已完成 30 分鐘持續輸出，每端待送與待確認資料合計上限 256 KiB，結束後全部排空。這是離屏軟體繪圖測試，不能代表可見桌面 GPU 效能，也沒有修改前的同條件基線，因此不宣稱 CPU 或 RAM 已降低。真實 OAuth、SSH、兩個 GitHub 帳號存取及 Harness 官方續聊仍須在指定測試環境驗收。

## DeepSeek Harness Web（實驗性）

在專案開啟 Harness，或於帳號頁選取 Harness 帳號並按「官方 Web 登入…」，選擇 SSH 環境、Harness 帳號與 GitHub 帳號，再啟動或接回官方 Web。帳號頁入口預設不綁 GitHub 身分。主機必須已安裝 `dsh`、Node 與 tmux；Launcher 不會安裝或升級。現階段限 Linux SSH 主機，使用 `/proc` 檢查 Web listener 確實屬於受管理的 tmux 服務，指定版本尚未完成真實端到端驗收。

共享範圍是「Harness 帳號＋GitHub 帳號＋SSH 環境」；同身分跨專案共用服務，不同 GitHub 身分使用不同程序及資料目錄。官方 Web 中選擇專案資料夾、登入、模型設定與既有對話皆由 Harness 管理，Launcher 不讀取憑證或轉換資料庫。未選 GitHub 時使用空的獨立 gh profile，且清除繼承的 token 與 credential helper，不沿用個人的 GitHub 認證。

遠端 Web 僅綁定 `127.0.0.1`，本機以 OpenSSH 建立固定 loopback 入口，使用外部瀏覽器。遠端服務與本機轉發分開顯示；埠衝突、無法驗證所有權或連線失敗會回報錯誤，不接入未知服務。轉發採 `BatchMode`，目前需要 SSH key／agent 與已驗證的 host key；密碼及 OTP 認證尚未支援。先完成其他 SSH 操作的登入不會讓新轉發自動免驗證。請使用沒有額外 forwarding 的 SSH alias。

關閉瀏覽器不會停止共享服務；關閉 Launcher 只停止本機轉發。停止服務會列出可能受影響的專案，官方對話保留。服務運行期間身分與連接埠鎖定，需停止後重開才能變更。新建立的 `DSH_HOME/cordis.patch.yml` 預設關閉 session-log contribution 與 OTel 上傳；之後保留使用者在官方介面的設定。

測試已涵蓋身分分離、初始上傳設定、重複啟動、所有權拒絕、狀態不明、停止與本機轉發清理。真實 SSH／OAuth、安裝版本相容性、官方 Web 續聊及多人共享停止仍須在指定測試環境驗收。參考 [官方 CLI 文件](https://github.com/deepseek-ai/deepseek-harness/tree/master/apps/cli) 與 [官方設定層說明](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/boot/app-boot)。

## 帳號分區與登入收尾

帳號頁維持單頁，依 Codex、Claude、DeepSeek Harness 與 GitHub 分區。沒有帳號的工具不顯示空 plate；共用搜尋、環境篩選與新增選單。寬視窗兩欄，窄視窗單欄並捲動；選取後在共用詳情區操作，GitHub 顯示官方回傳的實際登入帳號。

Codex／Claude／GitHub 的本次登入成功回報、runner 退出碼 0 與終端輸出確認全部排空後，工作區自動移除對應登入終端並通知成功。失敗、取消或結果不明保留終端。API 帳號只通知「設定完成」，不代表 API 已驗證。一般專案、SSH shell 與 CLI 更新不自動移除；環境檢查、SSH 設定解析、工作狀態更新、GitHub／Harness／匯入等背景操作成功後自動關閉，失敗才保留。外部登入成功直接退出，失敗仍可查看錯誤並按 Enter 關閉。更新程式後需重新啟動 Launcher。

## 參考

- [Codex 登入與無桌面環境驗證](https://learn.chatgpt.com/docs/auth)
- [Claude 多帳號驗證](https://code.claude.com/docs/en/authentication)
- [Codex 自訂 provider](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [Claude gateway](https://code.claude.com/docs/en/llm-gateway)
- 設計參考：[CAAM](https://github.com/Dicklesworthstone/coding_agent_account_manager)、[clauth](https://github.com/uwuclxdy/clauth)。本專案未複製其程式碼，也不依賴這些工具。
