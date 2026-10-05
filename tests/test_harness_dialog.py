from PySide6.QtWidgets import QWidget

from vibe_launcher.harness_dialog import HarnessDialog


class MemoryStore:
    def __init__(self):
        self.records = {
            "environments": [{"id": "local", "name": "本機", "kind": "local"},
                             {"id": "ssh1", "name": "測試主機", "kind": "ssh"}],
            "accounts": [{"id": "ai", "name": "Harness 帳號", "tool": "deepseek-harness", "environment_id": "ssh1"},
                         {"id": "codex", "name": "Codex", "tool": "codex", "environment_id": "ssh1"}],
            "github_accounts": [{"id": "gh", "name": "工程 GitHub", "environment_id": "ssh1"}],
            "presets": [{"id": "project", "name": "工程分析", "environment_id": "ssh1", "github_account_id": "gh"}],
            "web_services": [],
        }

    def list(self, kind):
        return self.records[kind]


def test_dialog_operation_and_remote_local_status_are_separate(qtbot):
    store = MemoryStore()
    dialog = HarnessDialog(store, store.list("presets")[0])
    qtbot.addWidget(dialog)
    assert not dialog.isModal()
    assert dialog.environment.count() == 1 and dialog.account.count() == 1
    received = []
    dialog.operation_requested.connect(received.append)
    dialog.start_button.click()
    assert received[0]["account_id"] == "ai" and received[0]["github_account_id"] == "gh"
    assert received[0]["operation"]["harness_operation"] == "start"
    assert not dialog.github.isEnabled()
    dialog.handle_result({"service_key": "a" * 32, "status": "running", "port": 3080, "local_port": 3080})
    assert "執行中" in dialog.remote_status.text()
    assert "未建立" in dialog.local_status.text()
    assert not dialog.account.isEnabled()
    opened = []
    dialog.open_requested.connect(opened.append)
    dialog.open_button.click()
    assert opened[0]["service_key"] == "a" * 32
    dialog.set_local_status("connected")
    assert "已連線" in dialog.local_status.text()
    dialog.handle_result({"status": "stopped"})
    assert dialog.github.isEnabled() and not dialog.open_button.isEnabled()
    assert "未連線" in dialog.local_status.text()


def test_stop_requires_confirmation_and_names_affected_projects(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    calls = []
    parent._confirm = lambda title, message, **kwargs: calls.append(message) or False
    store = MemoryStore()
    dialog = HarnessDialog(store, store.list("presets")[0], parent)
    qtbot.addWidget(dialog)
    sent = []
    dialog.operation_requested.connect(sent.append)
    dialog.stop_button.click()
    assert "工程分析" in calls[0] and not sent
    parent._confirm = lambda *a, **k: True
    dialog.stop_button.click()
    assert sent[0]["operation"]["harness_operation"] == "stop"


def test_starting_and_unknown_do_not_open_browser(qtbot):
    store = MemoryStore()
    dialog = HarnessDialog(store, store.list("presets")[0])
    qtbot.addWidget(dialog)
    for status in ("starting", "unknown"):
        dialog.handle_result({"status": status})
        assert not dialog.open_button.isEnabled()
        assert not dialog.github.isEnabled()
        assert dialog.check_button.isEnabled()


def test_dialog_widget_capture(qtbot, tmp_path):
    store = MemoryStore()
    dialog = HarnessDialog(store, store.list("presets")[0])
    qtbot.addWidget(dialog)
    dialog.resize(740, 420)
    dialog.show()
    qtbot.wait(30)
    assert dialog.grab().save(str(tmp_path / "harness-dialog.png"))
