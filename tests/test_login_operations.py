import json
import io
import time
from pathlib import Path

import pytest

from vibe_launcher import runner, runtime
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow, RecordDialog


@pytest.mark.parametrize("tool", ["codex", "claude", "github"])
@pytest.mark.parametrize("environment", ["local", "ssh"])
@pytest.mark.parametrize("status,code", [("authenticated", 0), ("failed", 1), ("unknown", 130)])
def test_login_job_result_and_external_exit(tmp_path, monkeypatch, tool, environment, status, code):
    store = Store(tmp_path / "data.db")
    if environment == "ssh":
        store.save("environments", {"id": "ssh", "name": "SSH", "kind": "ssh", "ssh_target": "example"})
    kind = "github_accounts" if tool == "github" else "accounts"
    account = {"id": "a", "name": "Account", "environment_id": environment}
    if tool != "github":
        account.update(tool=tool, auth="subscription")
    store.save(kind, account)
    argv, context = [], {}
    kwargs = ({"github_account_id": "a", "operation": {"github_operation": "login"}} if tool == "github" else {"account_id": "a"})
    operation = LauncherService(store).start("github" if tool == "github" else "login", environment_id=environment,
                                            spawn=lambda args, ctx: (argv.extend(args), context.update(ctx)), **kwargs)
    job = json.loads(runtime.read_private(Path(argv[-1])))
    assert context["operation_id"] == job["request"]["operation_id"] == operation
    assert context["login_operation"] is True
    monkeypatch.setattr(runner.sys, "argv", ["runner", argv[-1]])
    monkeypatch.delenv("VIBE_EMBEDDED", raising=False)
    monkeypatch.setattr(runtime, "emit", runtime.emit)
    def perform(request, root):
        runtime.operation_result(request, root, {"status": status})
        return code
    monkeypatch.setattr(runtime, "perform", perform)
    monkeypatch.setattr(runner, "run_remote", lambda db, request, env: perform(request, db.path.parent))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt))
    assert runner.main() == code
    assert bool(prompts) == (status != "authenticated" or code != 0)
    report = json.loads(runtime.read_private(tmp_path / "results" / (operation + ".json")))
    assert report["runner_exit_code"] == code


@pytest.mark.parametrize("auth,status", [("subscription", "authenticated"), ("api", "configured")])
def test_runtime_login_reports_only_after_current_authentication(tmp_path, monkeypatch, auth, status):
    request = {"action": "login", "operation_id": "new", "nonce": "n", "account": {"id": "a", "tool": "codex", "auth": auth, "model": "model"}}
    monkeypatch.setattr(runtime, "doctor", lambda *args: None)
    monkeypatch.setattr(runtime, "authenticate", lambda *args, **kwargs: None)
    assert runtime.perform(request, tmp_path) == 0
    assert json.loads(runtime.read_private(tmp_path / "results/new.json"))["result"] == {"status": status}
    def fail(*args, **kwargs):
        raise RuntimeError("cancelled")
    monkeypatch.setattr(runtime, "authenticate", fail)
    with pytest.raises(RuntimeError):
        runtime.perform({**request, "operation_id": "cancelled"}, tmp_path)
    assert not (tmp_path / "results/cancelled.json").exists()


def window(tmp_path, qtbot):
    store = Store(tmp_path / "ui.db")
    store.save("accounts", {"id": "ai", "name": "AI", "tool": "codex", "auth": "subscription", "environment_id": "local"})
    store.save("github_accounts", {"id": "gh", "name": "GitHub", "environment_id": "local", "login": "octocat", "status": "authenticated"})
    view = MainWindow(store, LauncherService(store))
    qtbot.addWidget(view, before_close_func=lambda w: w.workspace.shutdown())
    view.timer.stop()
    return view, store


def test_ui_waits_for_runner_and_confirms_only_matching_login(tmp_path, qtbot, monkeypatch):
    view, store = window(tmp_path, qtbot)
    confirmations, results = [], []
    monkeypatch.setattr(view.workspace, "confirm_login", lambda *args: confirmations.append(args))
    view._operations["op"] = {"callback": results.append, "since": time.monotonic(), "action": "login", "login": True, "embedded": True}
    path = tmp_path / "results/op.json"
    payload = {"action": "login", "operation_id": "op", "result": {"status": "authenticated"}}
    runtime.write_json(path, payload)
    view._poll_operations()
    assert not confirmations and not results and path.exists()
    runtime.write_json(path, {**payload, "operation_id": "wrong", "runner_exit_code": 0})
    with pytest.raises(ValueError):
        view._poll_operations()
    assert not confirmations
    runtime.write_json(path, {**payload, "runner_exit_code": 0})
    view._poll_operations()
    view._poll_operations()
    assert confirmations == [("op", "authenticated")]
    assert results == [{"status": "authenticated"}]


def test_github_account_management_without_project(tmp_path, qtbot, monkeypatch):
    view, store = window(tmp_path, qtbot)
    view.accounts_page.select("github_accounts", "gh")
    assert "octocat" in view.account_details.text()
    assert not view.account_buttons["settings"].isVisible()
    assert view._selected_record("accounts") == {}
    calls = []
    monkeypatch.setattr(view, "_start_operation", lambda *args: calls.append(args))
    view.accounts_page.request_action("login")
    view.accounts_page.request_action("status")
    assert [call[1]["operation"]["github_operation"] for call in calls] == ["login", "status"]
    view._github_result(None, calls[0][1], {"status": "authenticated", "login": "actual-user"})
    view.accounts_page.select("accounts", "ai")
    assert store.get("github_accounts", "gh")["login"] == "actual-user"
    assert Store(store.path).get("github_accounts", "gh")["login"] == "actual-user"
    view.accounts_page.select("github_accounts", "gh")
    profile = tmp_path / "profiles/github/gh"
    runtime.private_dir(profile)
    marker = profile / "preserved.txt"
    marker.write_text("profile", encoding="utf-8")
    monkeypatch.setattr(view, "_confirm", lambda *args, **kwargs: True)
    view.accounts_page.request_action("delete")
    assert store.list("github_accounts") == [] and marker.exists()


def test_add_first_github_and_edit_name_preserves_binding(tmp_path, qtbot, monkeypatch):
    view, store = window(tmp_path, qtbot)
    values = {"name": "Second", "environment_id": "local"}
    monkeypatch.setattr(RecordDialog, "exec", lambda self: self.DialogCode.Accepted)
    monkeypatch.setattr(RecordDialog, "values", lambda self: values)
    view._add_account_tool("github")
    record = next(a for a in store.list("github_accounts") if a["name"] == "Second")
    values = {"name": "Renamed"}
    view._edit_github_account(record)
    assert store.get("github_accounts", record["id"])["environment_id"] == "local"
    assert store.get("github_accounts", record["id"])["name"] == "Renamed"


@pytest.mark.parametrize("tool", ["codex", "claude", "github"])
@pytest.mark.parametrize("code,reported_id,committed", [(0, "op", True), (255, "op", False), (0, "other", False)])
def test_remote_login_result_requires_matching_operation_and_success_exit(tmp_path, monkeypatch, capsys, tool, code, reported_id, committed):
    store = Store(tmp_path / "remote.db")
    env = store.save("environments", {"id": "ssh", "name": "SSH", "kind": "ssh", "ssh_target": "example"})
    request = {"action": "github" if tool == "github" else "login", "operation_id": "op", "nonce": "nonce", "environment_id": "ssh"}
    if tool != "github":
        request["account"] = store.save("accounts", {"id": "a", "name": "AI", "tool": tool, "auth": "subscription", "environment_id": "ssh"})
    else:
        request.update(github_operation="login", github_account={"id": "gh", "environment_id": "ssh"})
    payload = {"action": request["action"], "operation_id": reported_id, "result": {"status": "authenticated"}}
    class SSH:
        stdout = io.BytesIO(("VIBE_RESULT_nonce:" + json.dumps(payload) + "\r\n").encode())
        def wait(self):
            return code
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: SSH())
    assert runner.run_remote(store, request, env) == code
    assert (tmp_path / "results/op.json").exists() is committed


def test_harness_account_login_opens_official_web_controls_without_project(tmp_path, qtbot):
    view, store = window(tmp_path, qtbot)
    store.save("environments", {"id": "ssh", "name": "SSH", "kind": "ssh", "ssh_target": "example"})
    store.save("accounts", {"id": "h", "name": "Harness", "tool": "deepseek-harness", "auth": "subscription", "environment_id": "ssh"})
    view.reload()
    view.accounts_page.select("accounts", "h")
    view.accounts_page.request_action("login")
    dialog = view._harness_dialog
    assert dialog.account.currentData() == "h" and dialog.github.currentData() == ""
    assert dialog.open_button.text() == "開啟官方 Web"
    dialog.close()


def test_login_starts_and_tracks_the_same_operation_in_ui(tmp_path, qtbot, monkeypatch):
    view, store = window(tmp_path, qtbot)
    contexts = []
    monkeypatch.setattr(view, "_embedded_enabled", lambda: True)
    monkeypatch.setattr(view.workspace, "spawn", lambda args, ctx: contexts.append(ctx))
    assert view._invoke("login", account_id="ai", environment_id="local")
    operation = contexts[0]["operation_id"]
    assert view._operations[operation]["login"] is True
    assert contexts[0]["login_operation"] is True
    view._start_operation("github", {"github_account_id": "gh", "environment_id": "local", "operation": {"github_operation": "login"}}, lambda result: None)
    assert len(contexts) == 2 and contexts[1]["operation_id"] != operation
    assert view._operations[contexts[1]["operation_id"]]["login"] is True


def test_unfinished_runner_result_times_out_without_success(tmp_path, qtbot, monkeypatch):
    view, store = window(tmp_path, qtbot)
    results = []
    view._operations["op"] = {"callback": results.append, "since": time.monotonic() - 901, "action": "login", "login": True, "embedded": True}
    runtime.write_json(tmp_path / "results/op.json", {"action": "login", "operation_id": "op", "result": {"status": "authenticated"}})
    monkeypatch.setattr(view.workspace, "confirm_login", lambda *args: pytest.fail("unfinished result"))
    view._poll_operations()
    assert results[0]["status"] == "unknown" and "op" not in view._operations
