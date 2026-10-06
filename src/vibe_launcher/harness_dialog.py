"""Explicit, nonmodal controls for an experimental shared Harness service."""
from __future__ import annotations

from .i18n import tr

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
        self.setWindowTitle(tr('harness_dialog.206bf18ad619'))
        self.setMinimumWidth(580)
        layout = QVBoxLayout(self)
        note = QLabel(tr('harness_dialog.e75ecf80e201'))
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
        for label, widget in ((tr('harness_dialog.57afa3f9c3d5'), self.environment), (tr('harness_dialog.085e15e985bb'), self.account),
                              (tr('harness_dialog.bc3a0bab5dfe'), self.github), (tr('harness_dialog.11070a20d80f'), self.remote_port),
                              (tr('harness_dialog.ddd7f3615573'), self.local_port)):
            form.addRow(label, widget)
            widget.setAccessibleName(label)
        layout.addLayout(form)
        self.remote_status = QLabel(tr('harness_dialog.4ec259dcf33e'))
        self.local_status = QLabel(tr('harness_dialog.6d5adae2d384'))
        self.detail = QLabel(tr('harness_dialog.59bb3abcee32'))
        self.detail.setWordWrap(True)
        for label in (self.remote_status, self.local_status, self.detail):
            layout.addWidget(label)
        buttons = QHBoxLayout()
        self.start_button = make_button(tr('harness_dialog.b3afd34b8c80'), "primary", callback=lambda: self._send("start"))
        self.check_button = make_button(tr('harness_dialog.92db4a57e5f0'), callback=lambda: self._send("status"))
        self.open_button = make_button(tr('harness_dialog.04b0209fca84'), callback=self._open)
        self.stop_button = make_button(tr('harness_dialog.9e2d58900306'), callback=lambda: self._send("stop"))
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
        self.github.addItem(tr('harness_dialog.269b2bb356c7'), "")
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
            if not confirm or not confirm(tr('harness_dialog.82eaea1b0569'), tr('harness_dialog.45b94602dfc4') + ("、".join(affected) or tr('harness_dialog.2ba0f7af6b5a')) +
                                          tr('harness_dialog.6d46da94930e'), confirm_text=tr('harness_dialog.5060318c5124'), danger=True):
                return
        self._pending = True
        self._lock(True)
        self.remote_status.setText(tr('harness_dialog.3d9e688fb0b8'))
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
        text = {"running": tr('harness_dialog.f51b8fa9a805'), "starting": tr('harness_dialog.30bfcfd206ef'), "stopped": tr('harness_dialog.f006455e3baf'), "failed": tr('harness_dialog.aa065ac6118d'), "unknown": tr('harness_dialog.f64524f3876d')}.get(status, tr('harness_dialog.f64524f3876d'))
        self.remote_status.setText(tr('harness_dialog.1149f8bb47ad') + text)
        self._lock(status in {"running", "starting", "unknown"})
        self.open_button.setEnabled(status == "running")
        self.detail.setText(result.get("error") or tr('harness_dialog.110d8661d449'))
        if status == "stopped":
            self.set_local_status("disconnected")

    def set_local_status(self, status: str) -> None:
        text = {"connected": tr('harness_dialog.866cb506850c'), "disconnected": tr('harness_dialog.72c69bc884ab'), "connecting": tr('harness_dialog.64d23a306a17'), "failed": tr('harness_dialog.225448a30728')}.get(status, tr('harness_dialog.f64524f3876d'))
        self.local_status.setText(tr('harness_dialog.9706f5798255') + text)

    def _open(self) -> None:
        if self._result.get("status") == "running":
            self.open_requested.emit(dict(self._result))
