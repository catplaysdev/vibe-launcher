# Changelog

版本號唯一來源是 `pyproject.toml` 的 `version`；發佈時在 `main` 打 `v<版本>` tag（例如 `v0.2.0`）。
每個版本一節：`## [x.y.z] - YYYY-MM-DD`，先條列中文重點，再以 `### English` 小節條列相同內容的英文；更新資訊依系統顯示語言選擇，繁體中文系統顯示中文，其餘一律英文，並可複製兩種語言的全文。
目前仍在開發階段，不保留舊版相容，設定或資料庫欄位的移除直接寫在條列中即可。

## [Unreleased]

- 修正：Claude hook 與狀態列事件改以跨程序鎖保護的序號發布，不再因 Windows 時鐘刻度相同而覆蓋事件、遺失子代理狀態或使 CI 間歇失敗。
- 新增：Codex 配額分類顯示、依環境區隔的帳號池與視覺管理流程，提供指定優先序、輪流分配及依可用額度策略。
- 新增：Codex 終端改由 Launcher 管理的官方 app-server 執行，工具列可安全切換帳號（未設流程也可用）：只在驗證過的安全時點交接確切對話，保留模型、reasoning、權限、指示檔、skills 與 MCP 設定，未知工具結果不重播；額度失敗的工作在新帳號或可信重設後的同帳號自動接續，只通知模式不自動開始。
- 新增：配額監測只在啟用流程的終端存續期間執行，同機同帳號共用快取與租約，失敗退避；重新載入、還原工作區或重開工作階段時沿用確切對話、最後帳號與流程。
- 改善：SSH helper 依動作只傳送需要的程式；較大的受管理 Codex helper 改用緊湊編碼，仍在 Windows 命令列長度內。
- 新增：切換前分兩層比對。環境層（指示檔、設定擴充、實際載入的 skills）必須一致；核准規則只做唯讀比對，不同時列出差異，由使用者取消或明確選擇「以目前規則對齊目標帳號」（先備份，目標帳號在其他終端使用中時阻擋）；帳號層（外掛、連結的應用、MCP 登入）缺少時列出清單，手動切換由使用者選擇照常切換或取消，流程積木可預先設定。
- 新增：Claude Code 終端同樣可安全切換帳號與使用配額流程：以 Launcher 指定的對話 ID 啟動，透過只對該次啟動有效的 hooks 判斷安全時點，切換時結束 CLI、複製對話紀錄並以新帳號接回同一對話；額度由狀態列取得。
- 新增：Claude 使用者自訂的狀態列由 Launcher 包一層後照常顯示，同時記錄額度；開發用 `scripts/simulate_quota.py` 可模擬額度測試切換與接續。
- 修正：額度重設瞬間的舊觀察不再抹掉可信重設時間（原本會使重設後不自動接續）；交接後改以新帳號回報的設定為比對基準（原本第二次切換會被誤判為設定變更）。
- 修正：SSH 上 tmux 改以 UTF-8 連線與執行，Claude／Codex 的符號與圖示不再變成 `_`；終端內的 CLI 自行結束（例如 Ctrl+C）後，「重新載入」會以同一段對話重新開啟，而不是嘗試連回已不存在的 tmux 工作階段。
- 修正：SSH 終端內的 Codex／Claude 一直以 80×24 繪製（tmux 狀態列重複疊在畫面中間）；開啟與重新接回時 ssh 直接使用終端，伺服器取得實際大小並跟著調整視窗。
- 修正：SSH 上的 tmux 工作階段選項（連線標題、Ctrl+B 離開鍵、Harness 服務的擁有者標記）因目標寫法錯誤從未生效，導致「離開並在背景繼續」一直顯示「尚未連上伺服器的 tmux 工作階段」、Harness 啟動時誤報擁有者標記失敗。
- 伺服器上的 tmux 改以工作區為單位：每個工作區一個 tmux 工作階段（以工作區 id 命名），工作區的終端是其中的視窗，不再每開一個終端就多一個工作階段；分頁只在開著時使用一個自動消失的檢視工作階段。
- 修正：伺服器使用 tmux 3.4 等舊版時，每次接上都會把終端對裝置屬性查詢的回覆（`61;6;7;…c`）當成輸入打進 shell；接上時先停在暫時視窗約 1 秒吸收它（tmux 3.6 以上不需要）。tmux 視窗不再印出內部狀態行，並開啟 focus-events（只限 Launcher 自己的 tmux）。
- 刪除工作區時，若它在伺服器上還有終端，會說明對應的 tmux 工作階段，刪除後結束該工作階段（每台伺服器連線一次）。
- 帳號頁每種工具的卡片內依環境分成小卡片（本機在前），環境內帳號超過 4 個時可捲動。
- 專案、帳號、環境與工作階段清單新增右鍵選單，提供與按鈕相同的動作。
- 修正：Claude 切換帳號被帳號自己同步的 skills（`skills/synced`）與 CLI 內部資料夾（`skills/.trash`）擋下；這些不再複製或比對。
- 修正：Claude 交接到從未在該處開啟過的帳號時，會停在首次使用引導與一次性提示並吞掉接續內容；交接時帶上已完成的引導、已看過的提示與該資料夾的信任（不含任何憑證）。尚未說過話的對話交接時改為在新帳號重新開始，不再因找不到對話而讓終端結束。
- 內建終端的瀏覽器引擎（Qt WebEngine，約 200 MB 程式碼）改為開第一個終端時才載入：閒置時載入的模組由 150 個降為 118 個，工作集約 102 MB；防毒掃描等情況下工作集的上限由約 420 MB 降為約 210 MB。
- 相容：不支援 `--remote` 的舊版 Codex CLI 照常以一般模式啟動並提示。
- 修正：未知、非有限數值及布林格式的配額資料不再被當成可用額度；多配額類別不混用舊版摘要。
- 新增：可儲存的配額方案與積木編輯器：三個範本（依序接替、輪流切換、額度優先，預設低額度 10%、每 120 秒查詢、無帳號時等待並驗證恢復），可加入條件、選帳號、等待、通知與維持積木並接線（滑鼠拖曳、右鍵「連接至」或鍵盤），儲存前檢查斷線、缺少分支、迴圈、不可達與未經低額度條件的切換，錯誤標示在積木旁；可新增、另存、重新命名、刪除與不啟動工作的預覽。終端預設「無」方案，套用時複製方案，修改方案後需「重新套用」。
- 修正：切換前不再雙向合併兩個帳號的核准規則（切換遭拒或取消時原帳號仍被寫入目標帳號的 allow 規則）。
- 修正：Claude 安全點改以同步 hooks 與交接鎖判定（`onFailure: "block"`，需 Claude Code 2.1.295 以上，否則以一般模式啟動並提示）：送出 prompt 與執行工具前先記錄，交接中被擋下；hook 失敗或逾時不再放行。原本以兩秒安靜期判斷，hook 延遲時可能誤判安全。
- 修正：Claude 目標帳號回報與預期相同的對話 ID 與模型後才成為使用中帳號（模型 ID 必須與來源回報的完全相同，缺少或不符即回退；來源模型尚未回報時等待並提示，不改用帳號預設模型；準備期間設定變更時放開交接鎖重新檢查，不停止來源，目標啟動、驗證與回退使用同一份確認後的設定）；交接時須讀完整個事件佇列，積壓過多時延後交接；目標啟動失敗、逾時或模型不符時重新啟動原帳號，回報帳號與實際程序一致（原本回退後仍回報目標帳號）。Codex 交接失敗時同樣以原帳號接回同一對話。
- 修正：停用、更換或移除方案會取消舊方案尚未執行的自動切換與自動續接；已開始的交接完成安全收尾後停止。
- 修正：SSH 終端關閉或連線中斷時，伺服器端依 tmux 實際 client 狀態暫停額度查詢與自動切換，重新連上後恢復；工作仍留在 tmux。
- 改善：額度查詢失敗的冷卻時間依帳號與環境跨終端共用；輪流順序依終端保存，成功切換才前進，精確重開後沿用；新工作依方案先選帳號，恢復的對話維持原帳號。
- 修正：Windows 封裝會從 PATH 收進其他程式的 ICU（例如 LibreOffice 的 ICU 78），使 QtCore 載入失敗；封裝改用系統 ICU。
- 驗證範圍：官方 native TUI 經 Launcher 交接的完整流程已以 Codex 0.162.1 離線驗證；本機兩個真實訂閱帳號的手動切換已驗證；真實額度恢復與 SSH／tmux 主機尚未驗證。

### English

- Fix: Claude hook and status-line events use a process-locked sequence instead of timestamps, preventing event loss, missing subagent state and intermittent CI failures on Windows with equal clock ticks.
- New: separate Codex quota buckets, environment-specific account pools and a visual management flow with priority, round-robin and available-quota strategies.
- New: Codex terminals run through a launcher-managed official app-server; the terminal toolbar can switch accounts safely (with or without a flow): the exact conversation is handed over only at a verified safe point, keeping model, reasoning, permissions, instructions, skills and MCP settings, and unknown tool outcomes are never replayed. A task that failed on quota continues on the new account, or on the same account after a trusted reset; notify-only never starts work.
- New: quota monitoring runs only while a terminal with an enabled flow is open, shares one cache and lease per host and account, and backs off on failure; reloading a terminal, restoring a workspace or restarting a session keeps the exact conversation, last account and flow.
- Improved: SSH helpers carry only the code their action needs; the larger managed Codex helper uses a compact encoding that still fits the Windows command line.
- New: switching compares two layers. Environment-level state (instructions, settings extensions, loaded skills) must match; approval rules are only compared, and when they differ the user cancels or explicitly aligns the target account with the current rules (backed up first, refused while the target is open in another terminal); account-level capabilities (plugins, connected apps, MCP sign-ins) the target lacks are listed, and the user chooses to switch without them or cancel, or sets this in the flow.
- New: Claude Code terminals can switch accounts safely and use quota flows too: they start with a launcher-chosen conversation id, hooks passed for that launch only find safe points, and a switch ends the CLI, copies the transcript and resumes the same conversation on the other account; quota comes from the status line.
- New: a Claude status line the user configured keeps showing, wrapped by the launcher to record quota; the developer tool `scripts/simulate_quota.py` simulates quota to test switching and continuation.
- Fix: the stale observation right at a quota reset no longer erases the trusted reset time (the task did not continue after the reset); after a handoff the new account's own settings are the comparison baseline (a second switch was refused as "settings changed").
- Fix: tmux on SSH hosts attaches and runs with UTF-8, so Claude/Codex symbols and icons no longer turn into `_`; after the CLI in a terminal ended by itself (for example Ctrl+C), Reload starts the same conversation again instead of attaching to a tmux session that no longer exists.
- Fix: Codex/Claude in SSH terminals were always drawn for 80x24 (tmux status bars stacked mid-screen); launching and reattaching now give ssh the terminal itself, so the server gets the real size and follows resizes.
- Fix: tmux session options on SSH hosts (the attached title, the Ctrl+B leave key, the Harness service owner mark) never took effect because of a wrong target form, so "Leave and keep running" always said the terminal was not yet connected to the server's tmux session and Harness start reported an ownership-marking failure.
- tmux on servers is now per workspace: one tmux session per workspace (named by the workspace id) with each terminal as one of its windows, instead of a new session for every terminal; an open tab uses a short-lived view session that disappears when it leaves.
- Fix: with older tmux on the server (3.4 seen), every attach typed the terminal's answer to tmux's device-attributes query (`61;6;7;...c`) into the shell; attaching now rests on a placeholder window for about a second to absorb it (not needed from tmux 3.6). tmux windows no longer print internal status lines, and focus events are on (the launcher's own tmux only).
- Deleting a workspace that still has terminals on a server names its tmux session and ends it after deletion (one
  connection per server).
- The Accounts page groups each tool's accounts into environment plates (this PC first); an environment with more
  than four accounts scrolls.
- Right-click menus on the project, account, environment and session lists offer the same actions as the buttons.
- Fix: Claude account switching was blocked by account-synced skills (`skills/synced`) and the CLI's own folders
  (`skills/.trash`); these are no longer copied or compared.
- Fix: a Claude handoff to an account never opened there stopped on the first-run wizard and one-time notices and
  swallowed the continuation; the handoff now carries the finished wizard, seen notices and this folder's trust (no
  credentials). A conversation with nothing said yet starts fresh on the new account instead of ending the terminal.
- The built-in terminal's browser engine (Qt WebEngine, about 200 MB of code) now loads with the first terminal: an
  idle launcher maps 118 instead of 150 modules, its working set is about 102 MB, and when an antivirus scan touches
  every loaded image the working set peaks near 210 MB instead of about 420 MB.
- Compatibility: older Codex CLIs without `--remote` start normally and say so.
- Fix: unknown, non-finite and boolean quota values no longer imply available capacity; separate quota buckets do not mix with the legacy summary.
- New: saved quota plans and a block editor: three templates (take over in order, rotate accounts, most quota first; low quota 10%, a check every 120 s, wait and verify recovery when none is left), condition, account-selection, wait, notify and keep blocks wired by dragging, a right-click "Connect to" menu or the keyboard; saving checks open branches, loops, unreachable blocks and switching that does not follow a low-quota condition, marking each problem beside its block; new, save as, rename, delete and a preview that starts nothing. Terminals start with no plan; applying copies the plan, and an edited plan reaches a terminal only when applied again.
- Fix: switching no longer merges both accounts' approval rules in both directions (a refused or cancelled switch still wrote the target's allow rules into the source account).
- Fix: Claude safe points use synchronous hooks and a handoff lock (`onFailure: "block"`, Claude Code 2.1.295 or later; older versions start unmanaged and say so): prompts and tools are recorded before they run and refused during a handoff, and a hook that fails or times out no longer lets work through. The previous two-second quiet period could call a busy session safe when hooks were late.
- Fix: a Claude target becomes the active account only after it reported exactly the expected conversation id and model (the model id must equal the one the source reported, missing or different means rollback; until the source reported its model the switch waits and says so, never using an account default; settings changed during preparation release the lock and are checked again without stopping the source, and the target start, verification and rollback use one confirmed snapshot); a handoff must read the whole event queue and waits when the backlog is too long; if it fails to start, times out or runs another model, the source account is started again and the reported account always matches the running CLI (the fallback used to report the target). A failed Codex handoff likewise resumes the same conversation on the source account.
- Fix: disabling, replacing or removing a plan withdraws the old plan's queued automatic switch and automatic continuation; a handoff already running finishes its safe sequence first.
- Fix: when an SSH terminal is closed or its connection drops, the server pauses quota checks and automatic switching from tmux's real client state and resumes when a terminal attaches again; the work stays in tmux.
- Improved: quota-query failure cool-downs are shared per account and environment across terminals; each terminal keeps its own rotation, advanced only by a successful switch and kept across an exact reopen; new work starts on the account its plan selects, resumed conversations keep theirs.
- Fix: the Windows build could bundle another program's ICU from PATH (LibreOffice's ICU 78) and then fail to load QtCore; it now uses the system ICU.
- Verified: the complete native TUI handoff through the launcher, offline with Codex 0.162.1; a manual switch between two real local subscription accounts; real quota recovery and SSH/tmux hosts are not yet verified.

## [0.2.2] - 2026-10-11

- 新增：關閉視窗時可縮到系統匣（設定 → 一般 → 視窗，預設關閉）；終端與 SSH 工作持續執行，可從匣圖示重新開啟或結束。
- 新增：更新改在專屬視窗顯示進度：四個步驟、下載大小、備份位置、這次更新的內容；安裝程式接手後顯示不確定進度、已經過時間與說明，不再卡在空白畫面。
- 新增：更新內容提供中英文兩種語言，依系統顯示語言選擇：繁體中文系統顯示中文，其餘一律英文；更新視窗與「關於與更新」可複製中英文全文，進度訊息依介面語言顯示。
- 修正：程式、安裝程式與工作列圖示與程式內一致（米白圓形底盤），不再是白色方塊。
- 修正：更新 helper 失敗不再被進度檔誤報為成功，重複按更新不會啟動多個下載；安裝程式接手後不再跳出第二次終端關閉確認。

### English

- New: closing the window can minimise it to the system tray (Settings → General → Window, off by default); terminals and SSH work keep running, and the tray icon reopens or quits the launcher.
- New: updates show their progress in their own window: four steps, download size, backup location and what the update contains. Once the installer takes over it shows an indeterminate bar, elapsed time and an explanation instead of a blank screen.
- New: update notes come in Chinese and English. Traditional Chinese system display languages show Chinese; all other systems show English. Both update screens can copy both languages; progress messages follow the interface language.
- Fix: the program, installer and taskbar icons match the in-app logo (cream round plate) instead of a white square.
- Fix: helper failures override success reported by the progress file, repeated update clicks reuse the active download, and installer handoff no longer opens a second terminal-close confirmation.

## [0.2.1] - 2026-10-11

- 修正：Windows 桌面啟動的打包版繼承舊 PATH 時，會補查 Claude 與 Codex 的使用者原生安裝目錄；偵測與啟動使用相同搜尋路徑。
- 修正：自建 local 環境可在確認並通過相依性檢查後刪除，只有內建的預設本機環境不可刪除。

### English

- Fix: a packaged build started from the Windows desktop with an old PATH now also searches the user's native Claude and Codex install folders; detection and launching use the same search path.
- Fix: custom local environments can be deleted after confirmation and the dependency check; only the built-in default local environment cannot be deleted.

## [0.2.0] - 2026-10-10

- 工作區改為可命名、可切換的多專案桌面，各自保留終端、排列與選取位置；重複切換不再新增執行工作，重複連線優先跳到既有終端。
- 專案抽屜的個別執行紀錄改稱「工作階段」，與多專案工作區區分。
- Codex／Claude 每次啟動自動取得本機或遠端使用者的 global skills、plugins 與對話；專案局部設定仍由官方 CLI 讀取，多帳號登入憑證維持隔離。
- Claude plugins 依目前使用者／專案的啟用狀態載入；Codex 有原生 SQLite 索引時直接使用，支援資料庫型歷史對話。
- 自動移除來源已刪除且未被使用者修改的舊擴充副本；對話只補齊相同內容的新增訊息，不覆蓋分歧的本機續寫。
- 打包前清理 PyInstaller 快取，自我檢查比對執行程式與 runtime 原始檔，避免封裝混用新舊程式。
- 修正：啟動時同步 skills／設定／對話失敗（例如 profile 內有連結、設定檔含 BOM、遠端 0644 權限）會讓 Codex／Claude 無法開啟；現在逐檔跳過並在終端提示，CLI 照常啟動，profile 內的連結不會被讀寫或刪除。
- 啟動同步只重讀有變動的 skills 與對話，重複開啟不再每次掃過全部檔案。
- 修正：本機終端關閉、Launcher 關閉、當機或重開機後仍顯示「執行中」；啟動時一併校正首頁、專案與工作區，PID 被系統重用時不再誤判為存活；只有本機終端時，關閉提示不再提到 tmux。
- 每個工作區（含預設工作區）都可自訂名稱與圖示；工作區選單、命令面板與首頁都會顯示。
- 首頁新增「工作區」卡片，顯示各工作區的專案數、開啟與執行中的終端，點一下即切換。
- 專案頁顯示專案所屬的工作區並可直接切換，新增「開啟到工作區…」與工作區篩選；工作階段抽屜依所在工作區分組。
- 專案的「執行中」改以工作區實際開著的終端為準，與工作區的數量一致；SSH 上沒開終端的 tmux 工作顯示為「背景執行」。
- 「恢復」改為回到專案所在的工作區：已開著的直接切過去，否則帶回該工作區的終端（tmux 重新接回，Codex／Claude 以 `--continue`／`resume --last` 接續上次對話）。
- 工作區在開關終端時即時保存（含預設工作區），強制關閉也不會遺失；下次啟動回到上次的工作區，按「恢復 N 個終端」一鍵帶回，不會自動啟動。本機已停止或失敗的工作階段紀錄不再保留。
- 重啟後在工作區等待恢復的專案標示「可恢復」並走恢復流程；恢復不重複開已開著的終端，失敗的終端留待重試、不清空配置；修正重啟後停在已存工作區時預設工作區配置被清空、外部終端模式也會清空的問題；SSH 直接執行（非 tmux）的工作沒開終端時顯示「狀態不明」而非「背景執行」；Claude plugins 啟用設定只讀專案資料夾本身。
- 首頁最近專案中「可恢復」的按鈕改為「恢復」；重新載入本機終端時，被取代的工作階段立即記為已結束。README、使用手冊與人工驗收清單同步更新（多語系介面、工作區恢復、測試指令）。
- 開發階段不再保留舊版相容：移除資料庫遷移與唯讀模式、帳號的 `share_extensions`／`import_history`／`manual_import_notice` 欄位、版本 1 設定檔匯入、`ui-state.json`／`settings.json` 保留未知欄位的機制，以及更新前的「設定變更」提醒；資料庫中不認得的欄位在下次儲存時移除。
- 工作階段頁改為「工作區 › 專案 › 終端」三層分組（取代依專案分組）：終端開著的歸在所在工作區，其餘依專案所屬的工作區，未加入工作區的放最後；工作區標題可直接切換過去。介面設定 `sessions_grouping` 的值改為 `workspace`／`time`。
- 分支規則改為 Git Flow（main 正式版、develop 與 hotfix 測試版、`feat/dev_*`／`fix_*` 只留在本機），新增 `.githooks/pre-push` 阻擋推送工作分支，CI 改為在 main／develop／hotfix 推送與 PR 到 main 時執行。
- 終端標題列新增「重新載入」；重新載入與輸入列按 Enter 送出前會確認，可勾選「不再詢問」，並可在「設定 → 終端 → 確認通知」改回。
- 新增介面設定：`workspace_icons`、`default_workspace_name`、`default_workspace`、`last_workspace`、`confirm_reload`、`confirm_send`（`ui-state.json`）。
- 修正：在某個環境檢查 Codex／Claude 版本找到新版後，其他環境仍顯示舊的檢查結果；現在各環境都會以任何一次檢查看到的最新版比較，落後的直接標示「可更新」。

### English

- Workspaces are named, switchable multi-project desktops that keep their terminals, layout and selection; switching no longer creates new jobs and reconnecting jumps to the existing terminal.
- The per-run records in the project drawer are now called "sessions", separate from multi-project workspaces.
- Codex and Claude now pick up the local or remote user's global skills, plugins and conversations on every launch; project-level settings are still read by the official CLI and multi-account sign-in credentials stay isolated.
- Claude plugins load according to the current user/project enablement; Codex uses its native SQLite index when present, including database-backed conversation history.
- Stale extension copies whose source was deleted and that the user never changed are removed automatically; conversations only gain identical new messages and a diverged local continuation is never overwritten.
- The PyInstaller cache is cleaned before packaging and the self-test compares the running program with the runtime sources, so old and new code are never mixed.
- Fix: a failed sync of skills, settings or conversations at launch (links in the profile, a BOM in a settings file, 0644 permissions on a remote) no longer stops Codex or Claude from opening; files are skipped one by one with a hint in the terminal and links in the profile are never read, written or deleted.
- The launch sync only re-reads changed skills and conversations instead of scanning everything each time.
- Fix: local sessions still showed "running" after the terminal or launcher closed, a crash or a reboot; start-up now corrects Home, projects and workspaces, a reused PID is no longer mistaken for a live process, and with only local terminals the close prompt no longer mentions tmux.
- Every workspace (including the default one) can be renamed and given an icon; they show in the workspace menu, the command palette and Home.
- Home has a new Workspaces card with each workspace's project count and open/running terminals; one click switches.
- The project page shows which workspace a project belongs to and can switch to it; new "Open in workspace…" and a workspace filter; the sessions drawer is grouped by workspace.
- A project's "running" state now follows the terminals actually open in the workspace, matching the counts; tmux work on SSH without an open terminal shows as "running in background".
- "Resume" returns to the project's workspace: if the terminal is already open it switches there, otherwise it brings back that workspace's terminals (tmux reattached; Codex/Claude continue the last conversation with `--continue` / `resume --last`).
- Workspaces are saved as terminals open and close (the default one too), so a forced close loses nothing; the next start returns to the last workspace and "Restore N terminals" brings them back in one click without starting anything automatically. Stopped or failed local session records are no longer kept.
- After a restart, projects waiting in a workspace are marked "Can resume" and follow the resume flow; resuming never reopens an open terminal, failed terminals stay for retry without clearing the layout; fixes the default workspace layout being cleared on restart (and external terminal mode too); SSH direct (non-tmux) work with no terminal shows "state unknown" instead of "running in background"; Claude plugin enablement is read from the project folder only.
- The "Can resume" button on Home's recent projects is now "Resume"; when a local terminal is reloaded the replaced session is recorded as ended immediately. README, user guide and the manual acceptance checklist are updated (multi-language UI, workspace restore, test commands).
- No more compatibility with older builds during development: database migrations and read-only mode, the account fields `share_extensions` / `import_history` / `manual_import_notice`, version-1 settings import, preserving unknown `ui-state.json` / `settings.json` fields and the pre-update "settings changes" reminder are removed; unknown database fields are dropped on the next save.
- The Sessions page is grouped Workspace › Project › Terminal (instead of by project): open terminals sit in their workspace, the rest follow their project's workspace, and projects without a workspace come last; workspace headers switch to it. The `sessions_grouping` setting now takes `workspace` / `time`.
- Branching follows Git Flow (main for releases, develop and hotfix for testing, `feat/dev_*` / `fix_*` stay local); a `.githooks/pre-push` hook blocks pushing work branches and CI runs on pushes to main/develop/hotfix and on PRs to main.
- Terminal title bar gets "Reload"; reload and sending with Enter in the input bar ask first, with a "don't ask again" box that can be changed in Settings → Terminal → Confirmations.
- New interface settings: `workspace_icons`, `default_workspace_name`, `default_workspace`, `last_workspace`, `confirm_reload`, `confirm_send` (`ui-state.json`).
- Fix: after checking Codex/Claude versions in one environment and finding a newer one, other environments kept showing old results; every environment now compares against the newest version seen by any check, and ones that fall behind show "Update available".

## [0.1.0] - 2026-10-06

- 整理首次發布內容，移除暫存、舊驗證紀錄與一次性腳本；版本規則採重大改動／功能新增／小幅修正。
- 停用 Dependabot 自動版本更新 PR，套件升級由維護者主動安排。
- 修正西班牙文介面中的省略號、引號與狀態符號，通過翻譯完整性檢查。

- 介面新增簡體中文（中國大陸）、日文、韓文、德文與西班牙文；英文仍為預設語言，從「設定 → 一般」切換並重新啟動後生效。

- 啟動器可檢查 git checkout 的新版本 tag，只列出比目前安裝版本新的版本與其更新說明。
- 更新前自動備份資料庫、`ui-state.json` 與 `settings.json`（保留最近 10 份）。
- 新版本寫入的設定欄位，舊版本讀寫時會原樣保留；較新版本建立的資料庫以唯讀方式開啟。
- 新增介面設定：`toast_anchor`（通知位置）、`update_check`（是否自動檢查更新）、`sessions_grouping`（工作階段頁依專案或依時間）。
- 首頁總覽（點左上角 logo）、合併的設定視窗（分類圖示、快捷鍵搜尋與衝突提示）、訊息改到上方中央且可拖曳。
- 工作階段頁預設依專案分組，可整組召回或清除已結束的工作。
- 焦點導覽改為編號標示，可用數字鍵直接跳轉。
- 終端文字可用 `Ctrl+=`（或 `Ctrl` 加 `+`）／`Ctrl+-`／`Ctrl+0` 或 `Ctrl`＋滾輪即時縮放；新增介面設定 `ui_scale`（整體介面縮放 80%–200%，設定頁即時預覽，儲存後可立即重新啟動套用）。
- Logo 線條固定為黑色，並放在米白色圓形底盤上（側邊欄、關於頁、視窗與工作列圖示），深色與淺色主題都清楚。
- 修正：開啟第一個內建終端（例如接回工作）時，整個視窗會消失再出現、看起來像程式重新啟動。
- 專案可自訂圖示：內建圖示或自己的 SVG／PNG／JPG／WebP，SVG 與內建圖示可換顏色；首頁最近的專案改用專案圖示。新增介面設定 `project_icons`。
- 內建專案圖示增至 39 個（新增資料庫、地球、雲端、程式碼、火箭、塔吊、安全帽等）；專案頁清單中直接點圖示即可更換，不必先進入右側詳情。
- 修正工作區頁左側面板與終端之間的分隔條在亮色主題下仍是黑色（它原本取用系統調色盤，現在跟著主題）。
- 外觀設定新增「介面字型」與「終端機字型」，可分別從已安裝的字型選擇（終端機只列等寬字型），即時套用到整個視窗與所有終端；預設維持內建字型組合。新增介面設定 `ui_font`、`terminal_font`。字型下拉選單點開後最上方有搜尋欄位，輸入即可篩選。
- 帳號可勾選「與同工具的其他帳號共用各專案的對話紀錄」：開啟專案時，把其他帳號（同一環境、同工具）在這個專案的對話複製到此帳號的 profile；已開著的終端也能按標題列的更新鈕隨時再同步，之後在終端用 `/resume` 就找得到。只複製該專案的對話檔，不碰登入憑證；新增帳號欄位 `share_project_history`（預設關閉）。
- 授權改為 GPL-3.0-or-later（新增 `LICENSE`、`THIRD_PARTY_NOTICES.md`）：使用或散布本程式、修改版者須以相同授權公開原始碼。
- 新增 Windows 安裝版：`packaging/` 以 PyInstaller（資料夾版、兩個執行檔）與 Inno Setup 打包，`vibe-launcher-cli --selftest` 自我檢查，tag `vX.Y.Z` 由 GitHub Actions 建立 Release（安裝程式＋`SHA256SUMS.txt`）。安裝版從 GitHub Releases 更新：備份資料、分段下載、核對 SHA-256 後靜默安裝並重新開啟；從原始碼執行時仍是 git tag 更新。所有「自己再啟動自己」的指令集中在 `selfcmd.py`。
- 「快速開啟」與帳號頁的服務顏色：Claude 改為品牌橘、終端機改為綠色，不再和強調色或警告色混淆。
- 專案頁右側「工作區」改為一般段落，移除外框底色，卡片改為兩行並把動作放到第二行行尾。
- 減少卡頓：背景狀態輪詢共用一個資料庫連線並只檢查一次共同的資料夾（每 2 秒的工作約減半）；終端輸出在下一個畫面即呈現，不再固定等 34 ms。

- 在本機或 SSH 主機開啟官方 Codex / Claude CLI，每個帳號使用獨立的 profile，不影響使用者自己的設定目錄。
- 專案不綁帳號，開啟時才選帳號與服務；訂閱帳號只在綁定的環境使用，API / endpoint 帳號可用於任何環境。
- SSH 專案以 tmux 保持執行，關閉終端後工作繼續，可接回、停止與召回。
- 同一專案可有多個工作區；內建終端（ConPTY + xterm.js）、輸入列與快速開啟。
- 帳號額度、CLI 版本檢查與更新、GitHub 帳號與專案綁定、標籤與工作階段整理。
- 介面支援繁體中文與英文、主題與配色、可自訂快捷鍵與終端 profile。
