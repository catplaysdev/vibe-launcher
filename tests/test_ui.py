import pytest
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow, RecordDialog


class FakeService:
    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        kwargs.pop("workspace_id", None)      # which tmux session a launch joins is tested in test_workspace
        self.calls.append((action, kwargs))


@pytest.fixture
def window(tmp_path, qtbot):
    store = Store(tmp_path / "gui.db")
    account = store.save("accounts", {"name": "工作帳號", "tool": "codex", "auth": "subscription"})
    preset = store.save("presets", {"name": "工程分析", "account_id": account["id"],
                                    "environment_id": "local", "path": "C:/中文 專案"})
    service = FakeService()
    view = MainWindow(store, service)
    qtbot.addWidget(view)
    view.show()
    return view, store, service, account, preset


def test_search_never_launches_hidden_project(window):
    view, store, service, account, preset = window
    view.search.setText("no-match")
    view.launch_project()
    assert service.calls == []


def test_favorite_and_account_details_refresh(window):
    view, store, service, account, preset = window
    assert "工作帳號" in view.project_info.text()
    view.toggle_favorite()
    assert view.project_title.text().startswith("★")
    assert store.get("presets", preset["id"])["favorite"]
    view.launch_project()
    assert service.calls == [("launch", {"preset_id": preset["id"], "account_id": account["id"]})]


def test_auth_status_preserves_selection(window):
    view, store, service, account, preset = window
    view.accounts_page.select("accounts", account["id"])
    store.save("auth_states", {"id": account["id"] + ":local", "status": "authenticated",
                               "checked_at": "2026-10-04T00:00:00+00:00"})
    view._poll()
    listing = next(found for key, found in view.accounts_page.lists.items() if key.startswith("codex/"))
    assert "已登入" in listing.currentItem().text()
    assert view.accounts_page.current_selection == ("accounts", account["id"])


def test_http_loopback_permission_defaults_off_and_is_explicit(qtbot):
    dialog = RecordDialog(
        "帳號",
        [("base_url", "API Base URL", "")],
        {"base_url": "http://127.0.0.1:8000/v1"},
        checkboxes=[("allow_insecure_loopback", "允許 HTTP loopback")],
    )
    qtbot.addWidget(dialog)
    assert dialog.values()["allow_insecure_loopback"] is False
    dialog.fields["allow_insecure_loopback"].setChecked(True)
    assert dialog.values()["allow_insecure_loopback"] is True


def test_security_page_explains_password_cache_boundary(window):
    view, *_ = window
    view.show_page("environments")
    text = view.security_status_label.text()
    assert "同一 Windows 使用者" in text
    assert "OTP" in text


def test_version_check_targets_selected_environment_without_account(window):
    view, store, service, *_ = window
    view.environments_list.setCurrentRow(0)
    environment = view._selected_record("environments")
    view.check_tool_versions()
    assert service.calls == [("check-tools", {"environment_id": environment["id"]})]


def test_version_result_appears_on_poll_without_reselecting_environment(window):
    from vibe_launcher import runtime
    view, store, *_ = window
    view.environments_list.setCurrentRow(0)
    environment = view._selected_record("environments")
    runtime.write_json(store.path.parent / "tool-versions" / (environment["id"] + ".json"),
                       {"reports": [{"tool":"codex", "installed":"0.160.0", "latest":"0.160.0",
                                     "source":"CLI updater", "status":"current", "path":"C:/tools/codex.exe"}]})
    view._poll()
    assert "0.160.0" in view.tool_versions_label.text()


def test_lock_and_forget_revoke_selected_environment_secret(window, monkeypatch):
    view, store, *_ = window
    environment = store.save("environments", {"name": "測試主機", "kind": "ssh", "ssh_target": "dev@example"})
    view.reload()
    for row in range(view.environments_list.count()):
        item = view.environments_list.item(row)
        if item.data(256) == environment["id"]:
            view.environments_list.setCurrentItem(item)
            break
    calls = []
    from vibe_launcher import askpass
    monkeypatch.setattr(askpass, "revoke_password", lambda environment_id, root: calls.append((environment_id, root)))
    view.lock_ssh_password()
    view.forget_ssh_password()
    assert calls == [(environment["id"], store.path.parent), (environment["id"], store.path.parent)]
