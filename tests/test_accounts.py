"""Accounts: where logins live, the accounts page and login operations.

- account binding: Subscription logins live where they were made; projects pick the account when opened; missing folders are offered.
- accounts page
- login operations
"""
import io
import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit
from vibe_launcher import launch_dialogs, runner, runtime, ui
from vibe_launcher.accounts_page import AccountsPage
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow as OperationsMainWindow, RecordDialog, VibeLauncherWindow as MainWindow


# --- account binding --------------------------------------------------------------------------------------------------
SUB = {"id": "sub", "name": "S", "tool": "claude", "auth": "subscription"}


# -------------------------------------------------------------------------------------- the binding rule
def test_a_subscription_only_fits_the_environment_it_is_bound_to():
    bound = dict(SUB, environment_id="gpu")
    assert runtime.account_fits(bound, "gpu") and not runtime.account_fits(bound, "local")
    assert runtime.account_fits(SUB, "anything")                       # unbound: allowed everywhere
    assert runtime.account_fits({"auth": "api", "environment_id": ""}, "gpu")


def test_an_api_account_is_never_bound(tmp_path):
    store = Store(tmp_path / "s.db")
    api = store.save("accounts", {"id": "k", "name": "K", "tool": "codex", "auth": "api", "model": "m", "environment_id": "gpu"})
    assert api["environment_id"] == ""
    sub = store.save("accounts", {"id": "s", "name": "S", "tool": "codex", "auth": "subscription", "environment_id": "gpu"})
    assert sub["environment_id"] == "gpu"
    with pytest.raises(ValueError):
        store.save("accounts", {"id": "bad", "name": "B", "tool": "codex", "auth": "subscription", "environment_id": "../x"})


# -------------------------------------------------------------------------------------- the service
@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "svc.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("accounts", {"id": "home", "name": "Home", "tool": "claude", "auth": "subscription", "environment_id": "local"})
    store.save("accounts", {"id": "lab", "name": "Lab", "tool": "claude", "auth": "subscription", "environment_id": "gpu"})
    store.save("presets", {"id": "proj", "name": "Proj", "account_id": "", "environment_id": "gpu", "path": "/w"})
    return LauncherService(store), []


def start(svc, action="launch", **kwargs):
    service, jobs = svc
    return service.start(action, spawn=lambda args, ctx: jobs.append(ctx), **kwargs)


def test_launching_with_the_wrong_environments_login_is_refused_clearly(service):
    with pytest.raises(ValueError, match="登入綁定在「本機」"):
        start(service, preset_id="proj", account_id="home")
    assert service[1] == []


def test_a_project_needs_no_account_of_its_own(service):
    session = start(service, preset_id="proj", account_id="lab")
    assert session and service[0].store.get("presets", "proj")["account_id"] == "lab"     # remembered as the suggestion
    service[0].store.save("presets", {"id": "blank", "name": "Blank", "account_id": "", "environment_id": "gpu", "path": "/w"})
    with pytest.raises(ValueError, match="Select an account"):
        start(service, preset_id="blank")


def test_ssh_only_sessions_need_no_account(service):
    session = start(service, preset_id="proj", mode="shell")
    assert service[0].store.get("sessions", session)["mode"] == "shell"


def test_login_goes_to_the_environment_the_account_lives_in(service):
    start(service, "login", account_id="lab")
    assert service[1][-1]["environment_id"] == "gpu"
    with pytest.raises(ValueError, match="登入綁定"):
        start(service, "login", account_id="lab", environment_id="local")


# -------------------------------------------------------------------------------------- missing folders
def test_folder_plan_names_exactly_the_folders_that_do_not_exist(tmp_path):
    existing, missing = runtime.folder_plan(tmp_path / "a" / "b" / "c")
    assert existing == tmp_path and missing == [tmp_path / "a", tmp_path / "a" / "b", tmp_path / "a" / "b" / "c"]
    assert runtime.folder_plan(tmp_path) == (tmp_path, [])
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="is a file"):
        runtime.folder_plan(tmp_path / "file" / "sub")


def test_a_missing_project_folder_is_created_only_after_a_yes(tmp_path):
    target = tmp_path / "new" / "app"
    seen = []
    with pytest.raises(ValueError, match="not created"):
        runtime.ensure_project_dir(target, ask=lambda existing, missing: seen.append((existing, missing)) or False)
    assert not target.exists() and seen == [(tmp_path, [tmp_path / "new", target])]
    assert runtime.ensure_project_dir(target, ask=lambda *_: True) == target and target.is_dir()
    assert runtime.ensure_project_dir(target, ask=lambda *_: pytest.fail("existing folders are never asked about")) == target


def test_a_relative_project_path_is_still_rejected(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        runtime.ensure_project_dir(Path("relative/dir"))


def test_the_terminal_prompt_colours_the_folders_to_be_created(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": print(prompt, end="") or "y")
    assert runtime.terminal_confirm_create(tmp_path, [tmp_path / "a", tmp_path / "a" / "b"]) is True
    out = capsys.readouterr().out
    assert out.count("將建立") == 2 and "已存在" in out and "\x1b[1;32m" in out and "這 2 個資料夾" in out


def test_without_a_terminal_nothing_is_created_silently(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        runtime.terminal_confirm_create(tmp_path, [tmp_path / "a"])


def test_a_signed_in_claude_profile_skips_the_welcome_wizard(tmp_path):
    account = dict(SUB, environment_id="local")
    profile = runtime.profile_dir(tmp_path, account)
    runtime.private_dir(profile)
    runtime.write_json(profile / ".claude.json", {"oauthAccount": {"emailAddress": "x"}})
    runtime.mark_onboarded(account, tmp_path)
    assert json.loads((profile / ".claude.json").read_text(encoding="utf-8"))["hasCompletedOnboarding"] is True
    runtime.write_json(profile / ".claude.json", {})                         # not signed in: leave it alone
    runtime.mark_onboarded(account, tmp_path)
    assert "hasCompletedOnboarding" not in json.loads((profile / ".claude.json").read_text(encoding="utf-8"))


# -------------------------------------------------------------------------------------- UI
class FakeService:
    supports_spawn = False

    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        kwargs.pop("workspace_id", None)      # which tmux session a launch joins is tested in test_workspace
        self.calls.append((action, kwargs))


@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "ui.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("accounts", {"id": "home", "name": "本機 Claude", "tool": "claude", "auth": "subscription", "environment_id": "local"})
    store.save("accounts", {"id": "lab", "name": "GPU Claude", "tool": "claude", "auth": "subscription", "environment_id": "gpu"})
    store.save("accounts", {"id": "labx", "name": "GPU Codex", "tool": "codex", "auth": "subscription", "environment_id": "gpu"})
    store.save("accounts", {"id": "key", "name": "API", "tool": "codex", "auth": "api", "model": "m"})
    store.save("presets", {"id": "gp", "name": "遠端", "account_id": "", "environment_id": "gpu", "path": "/w"})
    store.save("presets", {"id": "lp", "name": "本機專案", "account_id": "", "environment_id": "local", "path": str(tmp_path)})
    window = MainWindow(store, FakeService())
    qtbot.addWidget(window)
    return window


def test_only_accounts_that_can_work_there_are_offered(view):
    assert [a["id"] for a in view._eligible_accounts("gpu")] == ["lab", "key", "labx"] or \
        {a["id"] for a in view._eligible_accounts("gpu")} == {"lab", "labx", "key"}
    assert {a["id"] for a in view._eligible_accounts("local")} == {"home", "key"}
    assert [a["id"] for a in view._eligible_accounts("gpu", "claude")] == ["lab"]


def test_opening_a_project_asks_which_account_and_service(view, monkeypatch):
    asked = []

    def ask(preset, environment_name, options, selected):
        asked.append((environment_name, [o["id"] for o in options]))
        return "labx", False
    monkeypatch.setattr(view, "_ask_account", ask)
    view.open_terminal("gp")
    assert asked[0][0] == "GPU" and set(asked[0][1]) == {"lab", "labx", "key"}
    assert view.service.calls[-1] == ("launch", {"preset_id": "gp", "account_id": "labx"})


def test_cancelling_the_chooser_opens_nothing(view, monkeypatch):
    monkeypatch.setattr(view, "_ask_account", lambda *a: None)
    view.open_terminal("gp")
    assert view.service.calls == []


def test_a_remembered_account_is_used_without_asking_when_the_project_says_so(view, monkeypatch):
    monkeypatch.setattr(view, "_ask_account", lambda *a: pytest.fail("nothing to choose"))
    view.store.save("presets", {**view._by_id("presets", "lp"), "ask_account": False, "account_id": "home"})
    view.reload()
    view.open_terminal("lp")
    assert view.service.calls[-1] == ("launch", {"preset_id": "lp", "account_id": "home"})


def test_next_time_directly_remembers_the_choice(view, monkeypatch):
    monkeypatch.setattr(view, "_ask_account", lambda *a: ("lab", True))
    view.open_terminal("gp")
    saved = view.store.get("presets", "gp")
    assert (saved["account_id"], saved["ask_account"]) == ("lab", False)
    monkeypatch.setattr(view, "_ask_account", lambda *a: pytest.fail("should not ask again"))
    view.reload()
    view.open_terminal("gp")
    assert view.service.calls[-1] == ("launch", {"preset_id": "gp", "account_id": "lab"})


def test_ssh_only_never_asks_for_an_account(view, monkeypatch):
    monkeypatch.setattr(view, "_ask_account", lambda *a: pytest.fail("a shell has no account"))
    view.open_terminal("gp", "shell")
    assert view.service.calls[-1] == ("launch", {"preset_id": "gp", "mode": "shell"})


def test_when_exactly_one_account_fits_it_is_used_without_asking(view, monkeypatch):
    view.store.delete("accounts", "key")
    view.reload()
    monkeypatch.setattr(view, "_ask_account", lambda *a: pytest.fail("nothing to choose"))
    view.open_terminal("lp")
    assert view.service.calls[-1] == ("launch", {"preset_id": "lp", "account_id": "home"})


def test_an_environment_without_a_usable_account_says_what_to_do(view, monkeypatch):
    for account in ("lab", "labx", "key"):
        view.store.delete("accounts", account)
    view.reload()
    notes = []
    monkeypatch.setattr(view, "_notice", notes.append)
    view.open_terminal("gp")
    assert view.service.calls == [] and "綁定" in notes[0]


def test_account_rows_say_where_the_login_lives(view):
    # each environment plate names its environment; a row's tooltip still says it in full
    lists = view.accounts_page.lists
    tips = [listing.item(i).toolTip() for listing in lists.values() for i in range(listing.count())]
    assert any("GPU Claude" in tip and "GPU" in tip for tip in tips)
    assert any("API" in tip and "所有環境" in tip for tip in tips)
    assert any(key.endswith("/gpu") for key in lists) and any(key.endswith("/") for key in lists)


def test_quota_for_a_bound_account_only_offers_its_own_environment(view):
    view.accounts_page.select("accounts", "labx")
    assert view.quota_panel.environment.count() == 1 and view.quota_panel.selected_environment() == "gpu"


def test_a_missing_local_folder_is_created_after_confirming_and_lists_each_new_level(view, tmp_path, monkeypatch):
    target = tmp_path / "x" / "y"
    seen = []
    monkeypatch.setattr(view, "_confirm_create_folders", lambda text, existing, missing: seen.append((existing, missing)) or True)
    assert view._ensure_local_folder(str(target)) is True and target.is_dir()
    assert seen == [(tmp_path, [tmp_path / "x", target])]


def test_declining_to_create_keeps_the_project_but_does_not_create(view, tmp_path, monkeypatch):
    target = tmp_path / "later"
    monkeypatch.setattr(view, "_confirm_create_folders", lambda *a: False)
    assert view._ensure_local_folder(str(target)) is True and not target.exists()


def test_existing_and_relative_paths_never_prompt(view, tmp_path, monkeypatch):
    monkeypatch.setattr(view, "_confirm_create_folders", lambda *a: pytest.fail("nothing to create"))
    assert view._ensure_local_folder(str(tmp_path)) is True
    assert view._ensure_local_folder("relative") is True


def test_the_project_dialog_rejects_an_account_that_cannot_work_in_that_environment(view, monkeypatch):
    captured = {}

    class Dialog(ui.RecordDialog):
        def __init__(self, title, fields, values, choices, parent, checkboxes=None, draft=False, validate=None):
            super().__init__(title, fields, values, choices, parent, checkboxes, draft, validate)
            captured["choices"] = choices

        def exec(self):
            return ui.QDialog.DialogCode.Accepted

        def values(self):
            return {"name": "N", "path": "/x", "account_id": "home", "environment_id": "gpu", "ask_account": True, "persistent": True}
    monkeypatch.setattr(ui, "RecordDialog", Dialog)
    notes = []
    monkeypatch.setattr(view, "_notice", notes.append)
    assert view.add_project() is False and "登入綁定在" in notes[0]
    assert captured["choices"]["account_id"][0] == ("每次開啟時選擇", "")


def test_path_assist_describes_the_path_as_you_type(qtbot, tmp_path):
    dialog = ui.RecordDialog("t", [("environment_id", "env", ""), ("path", "path", "")], {"environment_id": "local"},
                             {"environment_id": [("本機", "local"), ("GPU", "gpu")]}, None)
    qtbot.addWidget(dialog)
    assist = launch_dialogs.PathAssist(dialog, "path", "environment_id", {"local": "local", "gpu": "ssh"}, {"local": [str(tmp_path)]})
    field: QLineEdit = dialog.fields["path"]
    field.setText(str(tmp_path))
    assert "已存在" in assist.note.text() and assist.browse.isEnabled()
    field.setText(str(tmp_path / "nope" / "deeper"))
    assert "會建立 2 層" in assist.note.text()
    field.setText("relative")
    assert "完整路徑" in assist.note.text()
    combo: QComboBox = dialog.fields["environment_id"]
    combo.setCurrentIndex(combo.findData("gpu"))
    field.setText("/home/me/app")
    assert "啟動時確認" in assist.note.text() and not assist.browse.isEnabled()
    assert assist.model.stringList() == []                       # suggestions are per environment


def test_the_folder_dialog_marks_new_levels(qtbot, tmp_path, monkeypatch):
    monkeypatch.undo()                                          # build the real thing (only its exec() is guarded)
    dialog = launch_dialogs.CreateFoldersDialog(str(tmp_path / "a" / "b"), tmp_path, [tmp_path / "a", tmp_path / "a" / "b"])
    qtbot.addWidget(dialog)
    assert [new for _, new in dialog.rows] == [False, True, True]


# --- accounts page ----------------------------------------------------------------------------------------------------
ENVS = [{"id": "local", "name": "本機"}, {"id": "ssh", "name": "開發主機"}]
ACCOUNTS = [
    {"id": "c", "name": "個人 Codex", "tool": "codex", "auth": "subscription", "environment_id": "local"},
    {"id": "a", "name": "Claude API", "tool": "claude", "auth": "api", "environment_id": "", "model": "model"},
    {"id": "h", "name": "Harness", "tool": "deepseek-harness", "environment_id": "ssh"},
]
GITHUB = [{"id": "g", "name": "GitHub 個人", "login": "octocat", "status": "authenticated", "environment_id": "local"}]
AUTH = [{"id": "c:local", "status": "authenticated"}]


def page(qtbot, accounts=ACCOUNTS, github=GITHUB):
    widget = AccountsPage()
    qtbot.addWidget(widget)
    widget.set_records(accounts, github, ENVS, AUTH)
    widget.resize(1180, 760)
    widget.show()
    return widget


def test_grouped_rows_show_real_github_login_and_status(qtbot):
    widget = page(qtbot)
    assert list(widget.plates) == ["codex", "claude", "deepseek-harness", "github"]
    assert "octocat" in widget.lists["github/local"].item(0).text()
    assert "已登入" in widget.lists["codex/local"].item(0).text()
    assert "設定完成" in widget.lists["claude/"].item(0).text()
    assert "實驗性" in widget.lists["deepseek-harness/ssh"].item(0).text()


def test_empty_plates_hidden_and_first_account_add_menu(qtbot):
    widget = page(qtbot, [], [])
    assert not widget.plates
    assert widget.empty.isVisible()
    with qtbot.waitSignal(widget.add_requested) as signal:
        widget.add_menu.actions()[3].trigger()
    assert signal.args == ["github"]
    widget.set_records([], GITHUB, ENVS, [])
    assert list(widget.plates) == ["github"]


def test_search_environment_filter_and_one_empty_hint(qtbot):
    widget = page(qtbot)
    widget.search.setText("octocat")
    assert list(widget.plates) == ["github"]
    widget.search.clear()
    widget.environment.setCurrentIndex(widget.environment.findData("ssh"))
    assert list(widget.plates) == ["claude", "deepseek-harness"]
    widget.search.setText("沒有這個帳號")
    assert not widget.plates
    assert widget.empty.isVisible()
    assert "找不到" in widget.empty.text()


def test_identical_data_preserves_widgets_search_and_selection(qtbot):
    widget = page(qtbot)
    widget.select("github_accounts", "g")
    widget.search.setText("octocat")
    listing = widget.lists["github/local"]
    item = listing.currentItem()
    widget.set_records(ACCOUNTS, GITHUB, ENVS, AUTH)
    assert widget.lists["github/local"] is listing
    assert listing.currentItem() is item
    assert widget.search.text() == "octocat"
    assert widget.current_selection == ("github_accounts", "g")


def test_selection_unique_and_action_matches_identity(qtbot):
    widget = page(qtbot)
    widget.add_detail_widget(QLabel("帳號操作"))
    widget.lists["codex/local"].setCurrentRow(0)
    widget.lists["github/local"].setCurrentRow(0)
    assert widget.current_selection == ("github_accounts", "g")
    assert not widget.lists["codex/local"].selectedItems()
    assert widget.details_host.isVisible()
    with qtbot.waitSignal(widget.action_requested) as signal:
        widget.request_action("login")
    assert signal.args == ["login", "github_accounts", "g"]


def test_responsive_plate_height_and_scroll(qtbot):
    widget = page(qtbot)
    qtbot.waitUntil(lambda: widget._columns == 2)
    assert widget.grid.getItemPosition(1)[:2] == (0, 1)
    assert widget.lists["codex/local"].height() < 100
    widget.resize(640, 520)
    qtbot.waitUntil(lambda: widget._columns == 1)
    assert widget.grid.getItemPosition(1)[:2] == (1, 0)
    qtbot.waitUntil(lambda: widget.scroll.verticalScrollBar().maximum() > 0)
    assert widget.scroll.horizontalScrollBar().maximum() == 0


def test_record_change_preserves_existing_selection_and_filter(qtbot):
    widget = page(qtbot)
    widget.select("github_accounts", "g")
    widget.environment.setCurrentIndex(widget.environment.findData("local"))
    widget.set_records(ACCOUNTS, [{**GITHUB[0], "login": "updated-login"}], ENVS, AUTH)
    assert widget.environment.currentData() == "local"
    assert widget.current_selection == ("github_accounts", "g")
    assert "updated-login" in widget.lists["github/local"].currentItem().text()


def test_programmatic_selection_notifies_once_and_keyboard_actions(qtbot):
    widget = page(qtbot)
    events = []
    widget.selected.connect(lambda kind, record_id: events.append((kind, record_id)))
    widget.select("github_accounts", "g")
    widget.select("github_accounts", "g")
    assert events == [("github_accounts", "g")]
    listing = widget.lists["github/local"]
    with qtbot.waitSignal(widget.action_requested) as signal:
        qtbot.keyClick(listing, Qt.Key.Key_Return)
    assert signal.args == ["edit", "github_accounts", "g"]
    with qtbot.waitSignal(widget.action_requested) as signal:
        qtbot.keyClick(listing, Qt.Key.Key_Delete)
    assert signal.args == ["delete", "github_accounts", "g"]


def test_each_tool_plate_holds_one_plate_per_environment_that_scrolls(qtbot):
    many = [{"id": f"r{i}", "name": f"遠端 {i}", "tool": "codex", "auth": "subscription", "environment_id": "ssh"} for i in range(6)]
    widget = page(qtbot, ACCOUNTS + many)
    assert [key for key in widget.lists if key.startswith("codex/")] == ["codex/local", "codex/ssh"]     # this PC first
    remote = widget.lists["codex/ssh"]
    assert remote.count() == 6 and "開發主機" not in remote.item(0).text()      # the plate names the environment
    assert "開發主機" in remote.item(0).toolTip()
    qtbot.waitUntil(lambda: remote.verticalScrollBar().maximum() > 0)          # more than four rows: it scrolls
    assert widget.lists["codex/local"].verticalScrollBar().maximum() == 0


def test_short_environment_lists_show_every_row_without_a_scrollbar_under_the_theme(qtbot):
    from vibe_launcher.theme import build_stylesheet
    two = [{"id": f"r{i}", "name": f"遠端 {i}", "tool": "codex", "auth": "subscription", "environment_id": "ssh"} for i in range(2)]
    widget = AccountsPage()
    qtbot.addWidget(widget)
    widget.setStyleSheet(build_stylesheet())          # the window's theme pads lists and rows
    widget.set_records(two, [], ENVS, AUTH)
    widget.resize(1180, 760)
    widget.show()
    listing = widget.lists["codex/ssh"]
    qtbot.wait(50)
    assert listing.verticalScrollBar().maximum() == 0


def test_right_click_selects_the_account_and_asks_the_window_for_its_actions(qtbot):
    widget = page(qtbot)
    asked = []
    widget.context_requested = lambda kind, record_id, where: asked.append((kind, record_id))
    listing = widget.lists["codex/local"]
    listing.customContextMenuRequested.emit(listing.visualItemRect(listing.item(0)).center())
    assert asked == [("accounts", "c")] and widget.current_selection == ("accounts", "c")


# --- login operations -------------------------------------------------------------------------------------------------
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
    view = OperationsMainWindow(store, LauncherService(store))
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
