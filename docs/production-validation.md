# 生產環境優化驗證紀錄

驗證日期：2026-10-05。測試只使用合成終端輸出、隔離 Git repository、mock 官方 CLI 回應與資料庫備份副本，不使用真實 token 或 SSH 主機。

## GitHub 操作入口修正

GitHub、Harness 與手動匯入原本未從 `runtime.perform` 分派到各自處理器，會落入 Codex／Claude 的 doctor／啟動流程。已修正分派位置，GitHub 不需 AI 帳號。回歸測試涵蓋 GUI service job → runner → runtime → 結果檔 → GUI callback；四種操作均不呼叫 CLI doctor。

完整自動測試：496 passed、2 skipped。最後 UI／GitHub／Harness／資料保存相關檢查：146 passed、2 skipped。跳過項目是平台條件限制，不列為通過。已用 `widget.grab()` 檢查精靈與 1180×760／640×520 主畫面；離屏中文缺字，只驗證版面。專案次要操作放在「⋯」，避免窄視窗的按鈕列裁切。

## 資料保護

已在既有資料庫的備份副本執行 migration，確認原帳號、環境、專案、工作階段、工作區與認證狀態紀錄均保留；只有舊自動匯入偏好切換為手動，並提供一次提示。SQLite integrity check、foreign key check 與重複 migration 均通過；未遷移或寫入正式使用者資料庫以做驗證。

Git 設定測試涵蓋兩個隔離 GitHub profile、多 remote、SSH URL、作者有效值／來源、預覽後外部修改、部分套用中斷與保守回復。取消登入、credential store 未驗證、失效登入與存取拒絕使用 mocked 官方回應。登入過程可能觸發的官方 Git 設定寫入被導向 profile 的隔離檔；正式 repository helper 仍需預覽確認。

## 同機終端量測

Windows、ConPTY、WebEngine、離屏模式、軟體繪圖。每個終端產生持續高頻的合成 CJK 輸出。CPU 百分比以一個核心為 100%；RAM 包含量測程序及子程序。0／4 終端為短控制測試，8 終端為 30 分鐘壓測，時間及暖機條件不同，數值不能直接用來宣稱效能改善。

| 終端數 | 持續輸出 | CPU 中位數（一核心=100%） | RAM 中位數 | 切換 JS 回應中位數 | 結果 |
| --- | --- | --- | --- | --- | --- |
| 0 | 10 秒控制 | 約 0.6% | 約 56 MiB | 無終端 | 通過 |
| 4 | 10 秒 | 約 433% | 約 2464 MiB | 約 35.9 ms | 全部 exit 0，排空 |
| 8 | 1800 秒 | 約 897% | 約 5389 MiB | 約 20.7 ms | 全部 exit 0，排空 |

4／8 終端各自的佇列峰值均為 262144 bytes，上限包含待送與待確認資料。最後各端積壓均為 0；於輸出停止後下一個 5 秒取樣確認排空，不能視為精確的排空延遲。測試腳本與原始 JSON 在工作區保留；先前 renderer 未就緒的 run 已標為 unavailable，不算通過。WebEngine 仍消耗相當 RAM，沒有修改前的同條件基線，所以不宣稱 CPU／RAM 已降低。

## 尚未驗證／目前限制

2026-10-05 帳號分區與登入收尾：完整測試 540 passed／2 skipped；後續新增 SSH 結果解析、Harness 帳號入口及 runner 完成前逾時測試後，相關測試 123 passed／2 skipped。覆蓋 Codex／Claude／GitHub 的本機與 mocked SSH 結果回報、退出前後順序、最後輸出 ACK、錯誤識別碼、取消／失敗、並行登入與使用者先關閉終端；API 僅回報設定完成。GitHub 管理與 AI 選取保持獨立，紀錄可重開資料庫讀取，移除保留 profile。沒有新增資料庫 schema。

`scripts/preview_accounts.py` 使用合成帳號及 `widget.grab()`，主視窗實際尺寸 1180×760／640×520；寬版兩欄、窄版單欄，詳情操作可捲動到達。離屏中文方塊不作文字呈現驗收。登入成功需本次 operation 結果、runner exit 0 與排空共同成立，不以既有 auth state 或終端文字作判斷。

- 真實 GitHub OAuth、取消 device flow、缺少 credential store 的實際 fallback，以及兩個 GitHub 帳號同時存取私有 repository。
- 真實 SSH 斷線／重啟／停止超時、host key 與認證流程。
- 指定 dsh／Node／tmux 版本的官方 Web 端到端流程、對話續接與上傳關閉效果；Harness 維持實驗性。
- Harness 本機轉發目前需 SSH key／agent 與已驗證 host key；密碼與 OTP 尚未支援。關閉 Launcher 清理本機轉發，不停止遠端服務。
- 可見桌面 GPU 的 CPU／RAM、內容呈現與鍵盤切換延遲；目前量測的切換延遲是 JS 回應時間。
