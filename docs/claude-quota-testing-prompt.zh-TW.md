# Claude 配額管理驗收交接提示詞

請在主實作工作完成並提供最終測試結果後，將以下內容交給 Claude。

---

請對本工作目錄的 Codex 配額管理功能進行**驗收與缺陷報告，僅測試，不修改產品程式或測試期待值**。
先讀 `AGENTS.md`、`docs/quota-management-plan.md`、`docs/codex-switch-feasibility.md` 與 `docs/cliproxyapi-reference.md`。
記錄目前 branch、commit、未提交變更及已安裝 Codex 版本；保留所有既有變更，不切分支、不 commit／push／reset／clean。

## 範圍與安全界線

- 只使用新建的隔離暫存資料、虛構帳號及 loopback 模擬 provider，不讀取真實憑證、OAuth token 或使用者對話。
- 不修改使用者的 `.codex`、`.claude`、SSH 設定與既有 Launcher 資料；不登入真實帳號、不發出付費推論。
- 不連線任意真實 SSH 主機。SSH 行為先以 mocks 驗證；需要真實主機或帳號時列為待人工驗收，說明所需條件。
- 不把 terminal 輸出當產品執行狀態的依據；觀察 UI 外觀可用 `widget.grab()`，不可擷取真實桌面。
- 測試輸出只記錄必要診斷，勿印出 capability、環境變數或秘密。不要因測試失敗就放寬 fail-closed 規則。

## 自動驗證

使用既有 `.venv/Scripts/python.exe`，設定 `QT_QPA_PLATFORM=offscreen`、`QT_QPA_FONTDIR=C:/Windows/Fonts`。
為每輪測試選擇新的 checkout 外暫存目錄；保留 faulthandler，不使用 `-p no:faulthandler`。

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:QT_QPA_FONTDIR = 'C:/Windows/Fonts'
.venv/Scripts/python.exe -m pytest tests/test_quota.py tests/test_quota_ui.py tests/test_managed.py -q --basetemp="$env:TEMP/vibe-quota-acceptance" -p no:cacheprovider -o faulthandler_timeout=90
.venv/Scripts/python.exe scripts/check_translations.py
```

再執行實作新增的 UI／監測測試與 `tests/test_packaging.py`；確認哪些檔案實際存在，不虛構測試名稱。
最後跑完整 pytest，仍使用 checkout 外獨立 `--basetemp`。分別報告通過、失敗、環境阻擋與未執行。

## 官方 CLI 離線驗證

先讀兩個腳本，確認它们只用隔離 profile 與 loopback mock provider。輸出目錄必須尚不存在。

```powershell
.venv/Scripts/python.exe scripts/check_codex_control.py --output "$env:TEMP/vibe-direct-codex-acceptance"
.venv/Scripts/python.exe scripts/check_managed_codex.py --output "$env:TEMP/vibe-managed-codex-acceptance"
```

第一個腳本驗證官方 app-server 的結構化回合完成與跨空 profile 精確續接；第二個在 Windows 用 ConPTY 將原生 TUI 接到本專案 ManagedCodexBridge。
核對 report：native thread 綁定、模擬回合完成、相同 thread ID、舊 writer 已結束、新 writer 唯一、frontend 保留。
區分「啟動成功」「完成交接」「交接後新回合成功」，不得以較弱證據宣稱較強結果。
腳本沒有測到的設定、工具、待審核請求及原生 `/model` 變更，需另列未驗證。

## 行為驗收重点

1. 多配額桶不可互相覆蓋；未知／過期／NaN／Infinity／bool 資料不可當成可用額度。所有適用視窗都須符合門檻；reset 只觸發重查，不直接認定恢復。
2. 帳號池只允許相同環境的 Codex 訂閱帳號；priority、round-robin、available-quota 選擇具決定性。輪替游標只在成功交接後更新。
3. 視覺流程區塊可滑鼠與鍵盤操作；帳號優先序、監測間隔、門檻、無帳號策略可儲存與重開，模擬選擇不啟動工作。
4. Workspace 手動切換與自動觸發走同一交接流程。取消前置切換保留來源；目標不相容不能啟動新工作。
5. 回合進行中、工具未結束、exitCode 不明、approval pending、子代理狀態未知、舊 turn completion、未綁定 thread 事件，均不能誤判可切換。
6. profile 交接前保存精確 thread ID 與有效設定；不使用 `resume --last` 猜對話，不替換模型。安全終止來源後才啟動目標 writer。
7. 非 quota 失敗不得自動續作；成功完成的 turn 不應自動多送一輪；已允許的 quota 續作至多一次，不重播未知工具效果。
8. RPC ID 碰撞、逾時、斷線、延遲舊事件、雙前端、無效 capability 均應有明確且保守的結果。
9. 監測按 account/environment 去重；停用流程／終端結束停止排程；等待配額時按可信 reset 重查，notify-only 不自動啟動。
10. SSH mocks 確認 runtime 單檔可部署、tmux 與 UI detach 的生命週期不回歸；真實 SSH 留待指定測試環境。

## 回報格式

先列阻擋發布的缺陷，附具體檔案／行號、重現條件、預期／實際結果、測試證據與建議修正，不直接修程式。
再列命令與結果、UI 截圖位置、CLI 版本、離線模擬的限制及人工驗收清單。
沒有發現缺陷也須說明覆蓋範圍；不得把「mock 通過」寫成真實帳號、SSH、打包或端到端切換已通過。
