import pytest

from vibe_launcher.github_dialog import NO_REMOTE, GitHubDialog
from vibe_launcher.store import Store

IDENTITY = {"status": "authenticated", "login": "test-user", "user_id": "42", "display_name": "Test User",
            "noreply_email": "42+test-user@users.noreply.github.com", "credential_storage": "unverified"}
REPO = {"root": "C:/work/repo", "git": True, "branch": "main", "access": "unchecked",
        "remotes": [{"name": "origin", "url": "https://github.com/example/repo"},
                    {"name": "work", "url": "git@github.com:example/other.git"}],
        "author": {}}


def setup_dialog(qtbot, tmp_path, project=None, accounts=1):
    store = Store(tmp_path / "store.sqlite3")
    saved = [store.save("github_accounts", {"name": f"工作{n}", "environment_id": "local"}) for n in range(accounts)]
    project = store.save("presets", {"name": "Repo", "environment_id": "local", "path": "C:/work/repo", **(project or {})})
    dialog = GitHubDialog(store, project)
    qtbot.addWidget(dialog)
    operations, bindings = [], []
    dialog.operation_requested.connect(operations.append)
    dialog.binding_ready.connect(bindings.append)
    return dialog, (saved[0] if saved else None), operations, bindings


def open_ready(dialog, operations, repo=REPO, identity=IDENTITY):
    dialog.start()
    assert operations[-1]["operation"]["github_operation"] == "status"
    dialog.handle_result(identity)
    assert operations[-1]["operation"]["github_operation"] == "inspect"     # the repository is read right after
    dialog.handle_result(repo)


def test_start_checks_login_then_reads_repository_and_suggests_noreply(qtbot, tmp_path):
    dialog, account, operations, _ = setup_dialog(qtbot, tmp_path)
    assert not dialog.save_button.isEnabled()
    open_ready(dialog, operations)
    assert operations[0]["github_account_id"] == account["id"]
    assert dialog.author_email.text() == "42+test-user@users.noreply.github.com"
    assert dialog.author_name.text() == "Test User"
    assert dialog.remote.currentData() == "origin"              # sensible default, no forced choice
    assert dialog.save_button.isEnabled()


def test_save_previews_repository_settings_before_writing(qtbot, tmp_path):
    dialog, account, operations, bindings = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations)
    dialog._save()
    sent = operations[-1]["operation"]
    assert sent["github_operation"] == "bind" and "github_apply" not in sent
    assert sent["github_author_email"] == "42+test-user@users.noreply.github.com"
    assert dialog.store.get("github_accounts", account["id"])["author_email"] == sent["github_author_email"]
    dialog.handle_result({"status": "preview", "revision": "fingerprint", "changes": {
        "user.name": {"before": ["Old"], "after": ["Test User"]}}})
    assert not dialog.diff.isHidden() and "確認" in dialog.save_button.text()
    assert not bindings
    dialog._save()
    assert operations[-1]["operation"]["github_apply"] is True
    assert operations[-1]["operation"]["github_revision"] == "fingerprint"
    dialog.handle_result({"status": "bound", "root": "C:/work/repo", "remote": "origin", "url": "https://github.com/example/repo"})
    assert bindings[-1]["github_account_id"] == account["id"]
    assert bindings[-1]["github_repo"] == "https://github.com/example/repo"


def test_editing_after_preview_requires_a_new_preview(qtbot, tmp_path):
    dialog, _, operations, _ = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations)
    dialog._save()
    dialog.handle_result({"status": "preview", "revision": "r", "changes": {}})
    dialog.author_name.setText("Someone else")
    dialog.author_name.textEdited.emit("Someone else")
    dialog._save()
    assert "github_apply" not in operations[-1]["operation"]


def test_without_repository_write_only_associates(qtbot, tmp_path):
    dialog, _, operations, bindings = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations)
    dialog.write_repo.setChecked(False)
    dialog.remote.setCurrentIndex(dialog.remote.findData("work"))
    count = len(operations)
    dialog._save()
    assert len(operations) == count                          # no Git command
    assert bindings[-1]["github_repo"] == "https://github.com/example/other.git"
    assert bindings[-1]["github_remote"] == "work"
    assert "SSH" in dialog.remote_note.text()


def test_ssh_remote_written_as_https_of_the_same_repository(qtbot, tmp_path):
    dialog, _, operations, _ = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations)
    dialog.remote.setCurrentIndex(dialog.remote.findData("work"))
    dialog._save()
    sent = operations[-1]["operation"]
    assert sent["github_url"] == "https://github.com/example/other.git"
    assert sent["github_expected_remote_url"] == "git@github.com:example/other.git"


def test_commit_identity_is_required(qtbot, tmp_path):
    dialog, _, operations, bindings = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations)
    dialog.author_email.setText("")
    count = len(operations)
    dialog._save()
    assert len(operations) == count and not bindings
    assert "email" in dialog.message.text()


def test_binding_without_remote_and_adding_origin_later(qtbot, tmp_path):
    repo = {**REPO, "remotes": []}
    dialog, account, operations, bindings = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations, repo)
    assert dialog.remote.currentData() == NO_REMOTE
    dialog.write_repo.setChecked(False)
    dialog._save()
    assert bindings[-1]["github_account_id"] == account["id"] and bindings[-1]["github_repo"] == ""
    assert "之後" in dialog.message.text()
    dialog.remote.setCurrentIndex(dialog.remote.findData("+origin"))
    assert not dialog.remote_url.isHidden()
    dialog.remote_url.setText("https://github.com/example/new.git")
    dialog._save()                                             # a new remote always goes through the preview
    sent = operations[-1]["operation"]
    assert sent["github_remote"] == "origin" and sent["github_new_remote"] is True
    dialog.handle_result({"status": "preview", "revision": "r", "changes": {}})
    dialog._save()
    dialog.handle_result({"status": "bound", "root": "C:/work/repo", "remote": "origin", "url": "https://github.com/example/new.git"})
    assert bindings[-1]["github_repo"] == "https://github.com/example/new.git"
    assert bindings[-1]["github_remote"] == "origin"


def test_folder_without_git_offers_init_and_association(qtbot, tmp_path):
    dialog, _, operations, bindings = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations, {"root": "C:/work/repo", "git": False, "remotes": [], "branch": "", "author": {}, "access": "unchecked"})
    assert dialog.remote.findData("+origin") < 0
    assert not dialog.init_button.isHidden() and not dialog.write_repo.isEnabled()
    dialog._save()
    assert bindings and bindings[-1]["github_repo"] == ""
    dialog._request("init")
    assert operations[-1]["operation"]["github_operation"] == "init"
    dialog.handle_result({**REPO, "remotes": [], "status": "initialized"})
    assert dialog.init_button.isHidden() and dialog.write_repo.isEnabled()


def test_new_account_duplicating_an_existing_login_is_dropped(qtbot, tmp_path):
    dialog, account, operations, _ = setup_dialog(qtbot, tmp_path)
    dialog.store.save("github_accounts", {**account, "login": "test-user"})
    dialog._load_accounts(account["id"])
    dialog._new_account()
    fresh = dialog.account.currentData()
    assert fresh != account["id"] and operations[-1]["operation"]["github_operation"] == "login"
    dialog.handle_result(IDENTITY)
    with pytest.raises(KeyError):
        dialog.store.get("github_accounts", fresh)
    assert dialog.account.currentData() == account["id"]
    assert "瀏覽器" in dialog.message.text()


def test_new_account_is_named_after_its_login(qtbot, tmp_path):
    dialog, _, operations, _ = setup_dialog(qtbot, tmp_path, accounts=0)
    dialog.start()
    assert not operations and "登入新帳號" in dialog.auth_status.text()
    dialog._new_account()
    fresh = dialog.account.currentData()
    dialog.handle_result({**IDENTITY, "login": "second-user"})
    assert dialog.store.get("github_accounts", fresh)["name"] == "second-user"
    assert operations[-1]["operation"]["github_operation"] == "inspect"


def test_cancelled_new_account_leaves_no_record(qtbot, tmp_path):
    dialog, account, _, _ = setup_dialog(qtbot, tmp_path)
    dialog._new_account()
    dialog.handle_result({"status": "failed", "credential_storage": "unverified"})
    assert [item["id"] for item in dialog.store.list("github_accounts")] == [account["id"]]


def test_failure_unlocks_fields_and_keeps_binding(qtbot, tmp_path):
    dialog, _, operations, bindings = setup_dialog(qtbot, tmp_path)
    dialog.start()
    dialog.handle_result({"error": "連線失敗，請重試"})
    assert dialog.account.isEnabled() and "連線失敗" in dialog.message.text()
    assert not bindings


def test_unknown_unbind_never_clears_project_binding(qtbot, tmp_path):
    dialog, account, operations, bindings = setup_dialog(qtbot, tmp_path)
    dialog.project["github_account_id"] = account["id"]
    dialog._unbind()
    assert not operations                                     # asks first
    dialog._unbind()
    assert operations[-1]["operation"]["github_operation"] == "unbind"
    dialog.handle_result({"status": "unknown"})
    assert not bindings and "狀態不明" in dialog.message.text()


def test_unbind_uses_the_account_that_wrote_the_settings(qtbot, tmp_path):
    dialog, account, operations, bindings = setup_dialog(qtbot, tmp_path, accounts=2)
    dialog.project["github_account_id"] = account["id"]
    dialog.account.setCurrentIndex(1)
    dialog._unbind()
    dialog._unbind()
    assert operations[-1]["github_account_id"] == account["id"]
    dialog.handle_result({"status": "unbound", "preserved": []})
    assert bindings[-1]["github_account_id"] == ""


def test_running_service_blocks_binding_change(qtbot, tmp_path, monkeypatch):
    dialog, _, operations, _ = setup_dialog(qtbot, tmp_path)
    open_ready(dialog, operations)
    dialog.project["github_account_id"] = "b" * 32
    original = dialog.store.list
    monkeypatch.setattr(dialog.store, "list", lambda kind: [{"environment_id": "local", "github_account_id": "b" * 32, "status": "unknown"}]
                        if kind == "web_services" else original(kind))
    count = len(operations)
    dialog._save()
    assert len(operations) == count
    assert "先停止" in dialog.message.text()


def test_typed_identity_survives_reopening(qtbot, tmp_path):
    dialog, _, operations, _ = setup_dialog(qtbot, tmp_path)
    dialog.author_name.setText("Typed Name")
    dialog.draft.save()
    again = GitHubDialog(dialog.store, dialog.project)
    qtbot.addWidget(again)
    assert again.author_name.text() == "Typed Name" and again.draft.restored
