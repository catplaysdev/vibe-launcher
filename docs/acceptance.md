# 人工端到端驗收

自動化測試不等於真實帳號、SSH 主機與終端互動驗證。以下項目需要你的帳號與可測試 Linux 主機；尚未執行的項目不要標為通過。

| 情境 | 操作 | 預期 |
|---|---|---|
| 首次主機驗證 | 連線至未記錄 host key 的測試主機 | 顯示 fingerprint；使用者核對可信來源後才接受，取消時不保存信任或密碼 |
| 主機金鑰改變 | 測試環境替換 host key 後重連 | 連線中止，不送出快取密碼，也不自動接受新 key |
| SSH 安全選項 | 使用含 ProxyJump 的 SSH alias 啟動；檢查實際 SSH 選項 | 跳板可正常使用；agent forwarding 與 X11 forwarding 預設關閉；callback forwarding 僅綁 loopback |
| 雙訂閱帳號 | 各新增兩筆 Codex／Claude，分別登入並同時啟動 | 官方 CLI 顯示各自帳號，互不登出 |
| SSH key | 使用已有 key／agent 的 alias | terminal 完成 passphrase，GUI 保留選擇 |
| SSH OTP | SSH 驗證設互動模式，使用密碼＋OTP 的主機 | 密碼與 OTP 均由使用者在終端輸入，不進入 App 設定或快取 |
| 密碼保存 | SSH 驗證設為「密碼（可選快取）」並設定 15 分鐘，成功驗證後立即重連 | 同一已核對主機的 password prompt 可使用快取；不會把 OTP 或跳板機提示當作密碼 |
| 保存期限 | 等待到期再連線 | 重新要求密碼，有效期不因重用延長 |
| 關閉／忘記／鎖定 | 按「忘記 SSH 密碼」或「立即鎖定密碼」，或改模式為關閉 | 快取撤銷，已開啟的 askpass 視窗也不能重新保存舊密碼 |
| 錯誤密碼 | 已存密碼被主機拒絕 | 重新要求输入，不連續使用舊密碼 |
| 取消登入 | 在 SSH 或官方登入時按 Ctrl+C | 可重試；不標示已驗證，不重複啟動工作 |
| 2FA 逾時 | 等待官方授權碼逾時 | 官方錯誤可見；App 保留設定可重試 |
| Codex callback | 選 browser，完成 localhost:1455 callback | forwarding 只監聽本機 loopback；占用時明確失敗 |
| API | 測試官方 API 與自訂 endpoint | key 遮罩；指定模型正確；錯誤 endpoint 保留可診斷訊息 |
| 特殊路徑 | 使用空白、中文、單引號及 shell 字元的資料夾 | 工作 cwd 正確，不執行路徑中的命令文字 |
| 中斷重連 | SSH 啟動後關視窗或斷網，再接回 | 同一 tmux session，原工作繼續 |
| 主機離線 | 停止主機網路後更新狀態 | 顯示 unknown，不當成正常結束 |
| 結束工作 | 選一筆工作按停止 | 只停止所選工作；其他帳號 session 不受影響 |
| 匯出／匯入 | 匯出後檢查 JSON 並匯入隔離資料目錄 | 不含 token、key、session 或登入狀態 |
| endpoint 變更 | 設定 API key 後改 Base URL，再啟動測試 | 舊 key 不會自動傳送至新 endpoint；HTTP 僅可對明確允許的 loopback 使用 |
| 偽造 session event | 從 CLI/PTy 輸出注入格式正確但 nonce／session 不符的事件 | 不更新其他 session 狀態，也不因此標記已登入 |
| 專案模組注入 | 專案目錄放置同名 Python 模組並啟動 launcher 子程序 | Launcher 使用隔離啟動，不載入專案中的同名模組 |
| Credential Manager 邊界 | 啟用密碼快取並閱讀安全提示 | UI 說明期限是 App 取用政策；同一 Windows 使用者的其他程式仍可能存取該憑證 |
| SSH 輔助工具 | 按 README 說明試用 KeePassXC 或 Tailscale（若採用） | 不要求工具自動修改 sshd、防火牆或 SSH key；撤回選配後一般 OpenSSH 流程仍可用 |
| 工作區強制關閉 | 在預設與另一個工作區各開 Codex／Claude（本機與 SSH tmux 各一），用工作管理員結束 Launcher 後重開 | 回到上次的工作區並顯示「恢復 N 個終端」，不自動啟動；切回預設工作區也有「恢復」；專案頁與首頁標示「可恢復」 |
| 恢復與續接對話 | 按「恢復 N 個終端」 | SSH tmux 接回同一工作；本機 Codex／Claude 接續該資料夾上次的對話（Codex 指定模型的 API 帳號也確認模型正確）；已開著的不重複開 |
| 恢復失敗重試 | 恢復前讓某個 SSH 主機離線 | 該終端留在待恢復清單、配置不被清掉；主機恢復後再按「恢復」成功 |
| 重新載入 | 在本機與 SSH tmux 終端各按標題列「重新載入」，確認框勾「不再詢問」 | 本機在原位置重開並接續對話；tmux 重新接回；之後不再詢問，可在「設定 → 終端 → 確認通知」改回 |
| 輸入列送出確認 | 在輸入列打字後按 Enter，先取消再確認 | 取消時文字保留、不送出；確認後送出 |

診斷僅檢查工具是否存在，不測試網路連線、付費 API 請求或帳號可用額度。你不需要在聊天提供任何密碼、key 或 OTP。
