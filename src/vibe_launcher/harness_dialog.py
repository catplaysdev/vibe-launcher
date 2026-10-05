"""Explicit, nonmodal controls for an experimental shared Harness service."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout

from .kit import make_button


class HarnessDialog(QDialog):
    operation_requested = Signal(dict)
    open_requested = Signal(dict)

    def __init__(self, store, project: dict, parent=None):
        super().__init__(parent)
        self.store, self.project = store, project
        self._result: dict = {}
        self._pending = False
        self.setWindowTitle("DeepSeek Harness Web（實驗性）")
        self.setMinimumWidth(580)
        layout = QVBoxLayout(self)
        note = QLabel("使用官方 Web 與外部瀏覽器。僅支援 SSH Linux 主機；需要已安裝的 dsh、Node、tmux。\n"
                      "指定版本尚未通過真實端到端驗收。模型、登入與對話請在官方介面管理。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.environment = QComboBox()
        self.account = QComboBox()
        self.github = QComboBox()
        self.remote_port = QSpinBox()
        self.local_port = QSpinBox()
        for spin in (self.remote_port, self.local_port):
            spin.setRange(1024, 65535)
            spin.setValue(3080)
        for label, widget in (("SSH 環境", self.environment), ("Harness 帳號", self.account),
                              ("GitHub 帳號", self.github), ("遠端 loopback 埠", self.remote_port),
                              ("本機固定入口埠", self.local_port)):
            form.addRow(label, widget)
            widget.setAccessibleName(label)
        layout.addLayout(form)
        self.remote_status = QLabel("遠端服務：尚未檢查")
        self.local_status = QLabel("本機連線：未建立")
        self.detail = QLabel("關閉瀏覽器或 Launcher 不會停止共享服務；Launcher 結束會清理本機轉發。")
        self.detail.setWordWrap(True)
        for label in (self.remote_status, self.local_status, self.detail):
            layout.addWidget(label)
        buttons = QHBoxLayout()
        self.start_button = make_button("啟動／接回", "primary", callback=lambda: self._send("start"))
        self.check_button = make_button("檢查遠端", callback=lambda: self._send("status"))
        self.open_button = make_button("開啟官方 Web", callback=self._open)
        self.stop_button = make_button("停止共享服務", callback=lambda: self._send("stop"))
        self.open_button.setEnabled(False)
        for button in (self.start_button, self.check_button, self.open_button, self.stop_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        for item in self.store.list("environments"):
            if item.get("kind") == "ssh":
                self.environment.addItem(item["name"], item["id"])
        index = self.environment.findData(project.get("environment_id"))
        if index >= 0:
            self.environment.setCurrentIndex(index)
        self.environment.currentIndexChanged.connect(self._choices)
        self._choices()

    def _choices(self) -> None:
        environment_id = self.environment.currentData()
        self.account.clear()
        self.github.clear()
        for item in self.store.list("accounts"):
            if item.get("tool") == "deepseek-harness" and item.get("environment_id") == environment_id:
                self.account.addItem(item["name"], item["id"])
        self.github.addItem("未綁定（不繼承 GitHub 認證）", "")
        for item in self.store.list("github_accounts"):
            if item.get("environment_id") == environment_id:
                self.github.addItem(item["name"], item["id"])
        index = self.github.findData(self.project.get("github_account_id", ""))
        if index >= 0:
            self.github.setCurrentIndex(index)
        index = self.account.findData(self.project.get("account_id"))
        if index >= 0:
            self.account.setCurrentIndex(index)
        self.start_button.setEnabled(bool(self.account.currentData()))
        self.check_button.setEnabled(bool(self.account.currentData()))
        self.stop_button.setEnabled(bool(self.account.currentData()))

    def _send(self, operation: str) -> None:
        if self._pending or not self.account.currentData():
            return
        environment_id = self.environment.currentData()
        account_id = self.account.currentData()
        github_id = self.github.currentData()
        if operation == "stop":
            affected = [item["name"] for item in self.store.list("presets")
                        if item.get("environment_id") == environment_id and item.get("github_account_id", "") == github_id]
            confirm = getattr(self.parent(), "_confirm", None)
            if not confirm or not confirm("停止共享 Harness 服務", "相同 Harness 帳號、GitHub 帳號與環境共用此服務。\n"
                                          "可能受影響的專案：" + ("、".join(affected) or "此服務的所有官方 Web 工作區") +
                                          "\n官方對話保留，但所有瀏覽器連線會中斷。", confirm_text="停止服務", danger=True):
                return
        self._pending = True
        self._lock(True)
        self.remote_status.setText("遠端服務：操作中")
        self.operation_requested.emit({"account_id": account_id, "environment_id": environment_id,
                                       "preset_id": self.project.get("id"), "github_account_id": github_id,
                                       "operation": {"harness_operation": operation,
                                                     "harness_port": self.remote_port.value(),
                                                     "local_port": self.local_port.value()}})

    def _lock(self, locked: bool) -> None:
        for widget in (self.environment, self.account, self.github, self.remote_port, self.local_port):
            widget.setEnabled(not locked)
        for button in (self.start_button, self.check_button, self.stop_button):
            button.setEnabled(not self._pending and bool(self.account.currentData()))

    def handle_result(self, result: dict) -> None:
        self._pending = False
        self._result = dict(result)
        status = result.get("status", "unknown")
        text = {"running": "執行中", "starting": "啟動中，請再檢查", "stopped": "已停止", "failed": "操作失敗", "unknown": "狀態不明"}.get(status, "狀態不明")
        self.remote_status.setText("遠端服務：" + text)
        self._lock(status in {"running", "starting", "unknown"})
        self.open_button.setEnabled(status == "running")
        self.detail.setText(result.get("error") or "請在官方 Web 選擇專案資料夾；同一組身分的多個專案共用服務。")
        if status == "stopped":
            self.set_local_status("disconnected")

    def set_local_status(self, status: str) -> None:
        text = {"connected": "已連線", "disconnected": "未連線", "connecting": "連線中", "failed": "連線失敗"}.get(status, "狀態不明")
        self.local_status.setText("本機連線：" + text)

    def _open(self) -> None:
        if self._result.get("status") == "running":
            self.open_requested.emit(dict(self._result))
