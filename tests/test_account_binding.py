"""Subscription logins live where they were made; projects pick the account when opened; missing folders are offered."""
import json
import sqlite3
from pathlib import Path

import pytest
from PySide6.QtWidgets import QComboBox, QLineEdit

from vibe_launcher import launch_dialogs, runtime, ui
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import VibeLauncherWindow as MainWindow

SUB = {"id": "sub", "name": "S", "tool": "claude", "auth": "subscription"}


# -------------------------------------------------------------------------------------- the binding rule
def test_a_subscription_only_fits_the_environment_it_is_bound_to():
    bound = dict(SUB, environment_id="gpu")
    assert runtime.account_fits(bound, "gpu") and not runtime.account_fits(bound, "local")
    assert runtime.account_fits(SUB, "anything")                       # legacy / unbound: allowed everywhere
    assert runtime.account_fits({"auth": "api", "environment_id": ""}, "gpu")


def test_an_api_account_is_never_bound(tmp_path):
    store = Store(tmp_path / "s.db")
    api = store.save("accounts", {"id": "k", "name": "K", "tool": "codex", "auth": "api", "model": "m", "environment_id": "gpu"})
    assert api["environment_id"] == ""
    sub = store.save("accounts", {"id": "s", "name": "S", "tool": "codex", "auth": "subscription", "environment_id": "gpu"})
    assert sub["environment_id"] == "gpu"
    with pytest.raises(ValueError):
        store.save("accounts", {"id": "bad", "name": "B", "tool": "codex", "auth": "subscription", "environment_id": "../x"})


def _legacy_database(path: Path) -> None:
    """A database written before accounts had a binding and before projects could lack an account."""
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE accounts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE environments (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE presets (id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE RESTRICT);
        CREATE TABLE sessions (id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            preset_id TEXT REFERENCES presets(id) ON DELETE RESTRICT,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE RESTRICT);
        CREATE TABLE auth_states (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    """)

    def put(table, record, **columns):
        names = ["id", "payload", *columns]
        connection.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                           [record["id"], json.dumps(record), *columns.values()])

    for env in ({"id": "local", "name": "本機", "kind": "local", "ssh_target": ""}, {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu"}):
        put("environments", env)
    put("accounts", {"id": "one", "name": "One", "tool": "claude", "auth": "subscription"})
    put("accounts", {"id": "two", "name": "Two", "tool": "codex", "auth": "subscription"})
    put("accounts", {"id": "none", "name": "None", "tool": "codex", "auth": "subscription"})
    put("accounts", {"id": "key", "name": "Key", "tool": "codex", "auth": "api", "model": "m"})
    for state in ("one:gpu", "two:gpu", "two:local"):
        put("auth_states", {"id": state, "status": "authenticated"})
    put("presets", {"id": "p", "name": "P", "account_id": "one", "environment_id": "gpu", "path": "/w"}, account_id="one", environment_id="gpu")
    put("sessions", {"id": "s", "name": "P", "account_id": "one", "environment_id": "gpu", "path": "/w", "preset_id": "p"},
        preset_id="p", account_id="one", environment_id="gpu")
    connection.commit()
    connection.close()


def test_an_old_database_is_upgraded_without_losing_anything(tmp_path):
    path = tmp_path / "old.db"
    _legacy_database(path)
    store = Store(path)
    bound = {a["id"]: a["environment_id"] for a in store.list("accounts")}
    assert bound == {"one": "gpu", "two": "", "none": "", "key": ""}      # unambiguous -> bound; ambiguous stays unbound
    assert store.get("presets", "p")["account_id"] == "one" and store.get("sessions", "s")["preset_id"] == "p"
    # projects and shell sessions may now have no account at all
    store.save("presets", {"id": "free", "name": "Free", "account_id": "", "environment_id": "gpu", "path": "/x"})
    assert store.get("presets", "free")["ask_account"] is True
    store.save("sessions", {"id": "sh", "name": "Free", "account_id": "", "environment_id": "gpu", "path": "/x", "mode": "shell"})
    with pytest.raises(ValueError):
        store.save("sessions", {"id": "ag", "name": "Free", "account_id": "", "environment_id": "gpu", "path": "/x"})
    Store(path)                                                          # opening it again is harmless
    assert len(Store(path).list("presets")) == 2


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
    rows = [listing.item(i).text() for listing in view.accounts_page.lists.values() for i in range(listing.count())]
    assert any("GPU Claude" in r and "GPU" in r for r in rows)
    assert any("API" in r and "所有環境" in r for r in rows)


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
        def __init__(self, title, fields, values, choices, parent, checkboxes=None, draft=False):
            super().__init__(title, fields, values, choices, parent, checkboxes, draft)
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
