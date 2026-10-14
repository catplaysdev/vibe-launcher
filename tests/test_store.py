"""The store: records, deletion and tags.

- store
- tags and delete: Deleting must not be blocked by history; projects can carry tags that filter the Projects page and the workspace.
"""
import json

import pytest
from vibe_launcher import ui
from vibe_launcher.prefs import UiPrefs
from vibe_launcher.store import normalize_tags, Store
from vibe_launcher.tag_bar import collect_tags, matches_tags, TagFilterBar
from vibe_launcher.ui import VibeLauncherWindow as MainWindow


# --- store ------------------------------------------------------------------------------------------------------------
def test_initializes_local_environment_and_round_trips_records(tmp_path):
    store = Store(tmp_path / "launcher.db")
    local = store.get("environments", "local")
    assert (local["name"], local["kind"], local["ssh_target"]) == ("本機", "local", "")

    account = store.save(
        "accounts",
        {"name": "Codex", "tool": "codex", "auth": "subscription", "model": ""},
    )
    preset = store.save(
        "presets",
        {
            "name": "專案", "account_id": account["id"], "environment_id": "local",
            "path": "C:/work/project", "favorite": True,
        },
    )
    assert store.get("accounts", account["id"]) == account
    assert store.get("presets", preset["id"]) == preset


def test_rejects_invalid_account_and_unsafe_url(tmp_path):
    store = Store(tmp_path / "launcher.db")
    with pytest.raises(ValueError):
        store.save("accounts", {"name": "x", "tool": "unknown", "auth": "api", "model": "m"})
    with pytest.raises(ValueError):
        store.save(
            "accounts",
            {"name": "x", "tool": "codex", "auth": "api", "model": "m", "base_url": "https://u:p@example.test"},
        )


def test_rejects_invalid_ssh_and_project_paths(tmp_path):
    store = Store(tmp_path / "launcher.db")
    with pytest.raises(ValueError):
        store.save("environments", {"name": "bad", "kind": "ssh", "ssh_target": "-oProxyCommand=x"})
    account = store.save("accounts", {"name": "Codex", "tool": "codex", "auth": "subscription"})
    with pytest.raises(ValueError):
        store.save(
            "presets",
            {"name": "bad", "account_id": account["id"], "environment_id": "local", "path": "bad\npath"},
        )


def test_an_environment_in_use_is_protected_but_an_account_is_only_a_suggestion(tmp_path):
    store = Store(tmp_path / "launcher.db")
    account = store.save("accounts", {"name": "Codex", "tool": "codex", "auth": "subscription"})
    env = store.save("environments", {"name": "Box", "kind": "ssh", "ssh_target": "box"})
    preset = store.save(
        "presets",
        {"name": "P", "account_id": account["id"], "environment_id": env["id"], "path": "/p"},
    )
    with pytest.raises(ValueError, match="P"):
        store.delete("environments", env["id"])             # a project still lives there
    store.delete("accounts", account["id"])                  # the project merely loses its suggested account
    assert store.get("presets", preset["id"])["account_id"] == ""
    store.delete("presets", preset["id"])
    store.delete("environments", env["id"])
    with pytest.raises(KeyError):
        store.get("environments", env["id"])


def test_export_import_only_nonsecret_config_and_is_atomic(tmp_path):
    source = Store(tmp_path / "source.db")
    account = source.save(
        "accounts",
        {"name": "API", "tool": "claude", "auth": "api", "model": "model-x", "base_url": "http://localhost:8000", "allow_insecure_loopback": True},
    )
    source.save("auth_states", {"id": f"{account['id']}:local", "status": "ok", "checked_at": "now"})
    source.save("sessions", {"preset_id": "", "account_id": account["id"], "environment_id": "local", "name": "secret-session", "path": "C:/p"})
    exported = tmp_path / "config.json"
    source.export_config(exported)
    data = json.loads(exported.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert "sessions" not in data and "auth_states" not in data
    assert "secret-session" not in exported.read_text(encoding="utf-8")

    target = Store(tmp_path / "target.db")
    target.import_config(exported)
    assert target.get("accounts", account["id"])["model"] == "model-x"
    assert target.list("sessions") == []

    before = target.list("accounts")
    exported.write_text(json.dumps({"version": 2, "accounts": [{"id": "x", "name": "x", "token": "secret"}], "environments": [], "presets": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        target.import_config(exported)
    assert target.list("accounts") == before


def test_rejects_import_with_broken_references(tmp_path):
    store = Store(tmp_path / "launcher.db")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"version": 2, "accounts": [], "environments": [], "presets": [{"id": "p", "name": "p", "account_id": "missing", "environment_id": "local", "path": "C:/p", "favorite": False}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        store.import_config(config)
    assert store.list("presets") == []
@pytest.mark.parametrize("kind,original,changed", [
    ("accounts", {"name": "a", "tool": "codex", "auth": "subscription"}, {"auth": "api", "model": "m"}),
    ("environments", {"name": "h", "kind": "ssh", "ssh_target": "host-a"}, {"ssh_target": "host-b"}),
])
def test_identity_cannot_be_retargeted(tmp_path, kind, original, changed):
    store = Store(tmp_path / "identity.db")
    record = store.save(kind, original)
    with pytest.raises(ValueError, match="identity"):
        store.save(kind, {**record, **changed})


def test_account_id_cannot_be_a_path(tmp_path):
    store = Store(tmp_path / "identity.db")
    with pytest.raises(ValueError):
        store.save("accounts", {"id": "../../escape", "name": "a", "tool": "codex", "auth": "subscription"})


def test_import_cannot_retarget_identity(tmp_path):
    store = Store(tmp_path / "identity.db")
    record = store.save("environments", {"name": "h", "kind": "ssh", "ssh_target": "host-a"})
    path = tmp_path / "import.json"
    store.export_config(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    next(env for env in payload["environments"] if env["id"] == record["id"])["ssh_target"] = "host-b"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        store.import_config(path)


@pytest.mark.parametrize("url,allow,valid", [
    ("https://api.example.test/v1", False, True),
    ("http://localhost:8000/v1", True, True),
    ("http://127.0.0.8:8000", True, True),
    ("http://[::1]:8000", True, True),
    ("http://localhost:8000", False, False),
    ("http://example.test", True, False),
    ("https://u:p@example.test", False, False),
    ("https://example.test?key=x", False, False),
    ("https://example.test:bad", False, False),
    ("https://example.test\nX-Evil: yes", False, False),
])
def test_api_url_requires_https_except_opted_in_loopback(tmp_path, url, allow, valid):
    store = Store(tmp_path / "urls.db")
    account = {
        "name": "API", "tool": "codex", "auth": "api", "model": "m",
        "base_url": url, "allow_insecure_loopback": allow,
    }
    if valid:
        store.save("accounts", account)
    else:
        with pytest.raises(ValueError):
            store.save("accounts", account)


def test_account_endpoint_rotates_revision_and_clears_auth_state_even_when_changed_back(tmp_path):
    store = Store(tmp_path / "revision.db")
    account = store.save("accounts", {
        "name": "API", "tool": "codex", "auth": "api", "model": "m",
        "base_url": "https://one.example",
    })
    old_revision = account["credential_revision"]
    store.save("auth_states", {"id": f"{account['id']}:local", "status": "ok"})
    changed = store.save("accounts", {**account, "base_url": "https://two.example"})
    assert changed["credential_revision"] != old_revision
    assert store.list("auth_states") == []
    changed_back = store.save("accounts", {**changed, "base_url": "https://one.example"})
    assert changed_back["credential_revision"] != old_revision


def test_ssh_security_policy_rotates_epoch_and_revoke_preserves_remember_policy(tmp_path):
    store = Store(tmp_path / "ssh-security.db")
    environment = store.save("environments", {
        "name": "SSH", "kind": "ssh", "ssh_target": "host", "ssh_auth_mode": "password",
        "remember_seconds": 3600,
    })
    original_epoch = environment["credential_epoch"]
    changed = store.save("environments", {**environment, "ssh_auth_mode": "key"})
    assert changed["credential_epoch"] != original_epoch
    assert changed["remember_seconds"] == 0
    revoked = store.revoke_ssh(environment["id"])
    assert revoked["credential_epoch"] != changed["credential_epoch"]
    assert revoked["remember_seconds"] == 0
    with pytest.raises(ValueError):
        store.revoke_ssh("local")


def test_import_drops_internal_revisions_and_disables_password_memory(tmp_path):
    source = Store(tmp_path / "import-source.db")
    account = source.save("accounts", {
        "name": "API", "tool": "claude", "auth": "api", "model": "m",
        "base_url": "https://api.example",
    })
    environment = source.save("environments", {
        "name": "SSH", "kind": "ssh", "ssh_target": "host", "ssh_auth_mode": "password",
        "remember_seconds": 3600,
    })
    exported = tmp_path / "safe-config.json"
    source.export_config(exported)
    raw = json.loads(exported.read_text(encoding="utf-8"))
    account_row = next(row for row in raw["accounts"] if row["id"] == account["id"])
    environment_row = next(row for row in raw["environments"] if row["id"] == environment["id"])
    assert "credential_revision" not in account_row
    assert "credential_epoch" not in environment_row
    assert environment_row["remember_seconds"] == 0

    target = Store(tmp_path / "import-target.db")
    target.import_config(exported)
    imported_account = target.get("accounts", account["id"])
    imported_environment = target.get("environments", environment["id"])
    assert imported_account["credential_revision"] != account["credential_revision"]
    assert imported_environment["credential_epoch"] != environment["credential_epoch"]
    assert imported_environment["remember_seconds"] == 0

    account_row["credential_revision"] = "forged"
    exported.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        target.import_config(exported)


# --- tags and delete --------------------------------------------------------------------------------------------------
def make_store(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("accounts", {"id": "acc", "name": "Acc", "tool": "claude", "auth": "subscription", "environment_id": "gpu"})
    store.save("presets", {"id": "p1", "name": "前端", "account_id": "acc", "environment_id": "gpu", "path": "/w/web", "tags": ["工地", "前端"]})
    store.save("presets", {"id": "p2", "name": "後端", "account_id": "acc", "environment_id": "gpu", "path": "/w/api", "tags": ["工地", "後端"]})
    store.save("presets", {"id": "p3", "name": "雜項", "account_id": "", "environment_id": "local", "path": "/w/misc"})
    return store


def session(store, sid, preset, status):
    return store.save("sessions", {"id": sid, "preset_id": preset, "account_id": "acc", "environment_id": "gpu", "name": "n",
                                   "path": "/w/web", "status": status})


# ----------------------------------------------------------------------------------------------- deleting
def test_a_project_with_session_history_can_be_deleted(tmp_path):
    store = make_store(tmp_path)
    session(store, "old", "p1", "ended")
    session(store, "live", "p1", "unknown")           # may still be running on the server
    store.delete("presets", "p1")
    assert {p["id"] for p in store.list("presets")} == {"p2", "p3"}
    kept = {s["id"]: s for s in store.list("sessions")}
    assert set(kept) == {"old", "live"} and kept["live"]["preset_id"] == "" and kept["live"]["status"] == "unknown"


def test_deleting_an_unknown_project_is_still_a_clear_error(tmp_path):
    with pytest.raises(KeyError):
        make_store(tmp_path).delete("presets", "nope")


def test_deleting_an_account_clears_the_suggestion_but_not_the_projects(tmp_path):
    store = make_store(tmp_path)
    session(store, "old", "p1", "ended")
    store.save("workspaces", {"id": "w", "name": "W", "layout": "tabs", "slots": [{"preset_id": "p1", "kind": "agent", "account_id": "acc"}]})
    store.delete("accounts", "acc")
    assert store.get("presets", "p1")["account_id"] == "" and store.list("sessions") == []
    assert "account_id" not in store.get("workspaces", "w")["slots"][0]


def test_an_account_with_a_live_session_is_refused_in_plain_words(tmp_path):
    store = make_store(tmp_path)
    session(store, "live", "p1", "running")
    with pytest.raises(ValueError, match="執行中的工作階段"):
        store.delete("accounts", "acc")
    assert store.get("accounts", "acc")


def test_an_environment_still_used_by_projects_names_them(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="前端、後端"):
        store.delete("environments", "gpu")
    store.delete("presets", "p1")
    store.delete("presets", "p2")
    session(store, "old", "", "ended")
    store.delete("environments", "gpu")
    assert store.get("accounts", "acc")["environment_id"] == "" and store.list("sessions") == []


# ----------------------------------------------------------------------------------------------- tag rules
def test_tags_are_cleaned_and_deduplicated():
    assert normalize_tags("工地, #前端；  前端 ，UI、ui\n  ") == ["工地", "前端", "UI"]
    assert normalize_tags(["a", "A", " b  c "]) == ["a", "b c"]
    assert normalize_tags(None) == [] and normalize_tags("") == []


@pytest.mark.parametrize("bad", ["x" * 25, ",".join(f"t{i}" for i in range(13)), "a\x00b", 5, [1]])
def test_bad_tags_are_refused(bad):
    with pytest.raises(ValueError):
        normalize_tags(bad)


def test_tags_survive_saving_and_export(tmp_path):
    store = make_store(tmp_path)
    store.save("presets", {**store.get("presets", "p3"), "tags": "a, b"})
    assert store.get("presets", "p3")["tags"] == ["a", "b"]
    store.export_config(tmp_path / "out.json")
    other = Store(tmp_path / "o.db")
    other.import_config(tmp_path / "out.json")
    assert other.get("presets", "p1")["tags"] == ["工地", "前端"]


def test_collect_and_match():
    projects = [{"tags": ["UI", "工地"]}, {"tags": ["ui"]}, {}]
    assert collect_tags(projects) == {"UI": 2, "工地": 1}
    assert matches_tags({"tags": ["UI", "工地"]}, ["ui", "工地"]) and not matches_tags({"tags": ["UI"]}, ["ui", "工地"])
    assert matches_tags({}, [])


def test_the_tag_bar_prunes_vanished_tags_and_only_emits_on_clicks(qtbot):
    bar = TagFilterBar()
    qtbot.addWidget(bar)
    heard = []
    bar.changed.connect(heard.append)
    bar.set_selected(["a", "gone"])
    assert bar.set_tags({"a": 2, "b": 1}) is True and bar.selected() == ["a"] and heard == []
    bar.show()
    assert set(bar.chips) == {"a", "b"} and bar.chips["a"].isChecked() and bar.clear_button is not None
    bar.chips["b"].click()
    assert heard[-1] == ["a", "b"]
    bar.clear_button.click()
    assert heard[-1] == [] and bar.clear_button is None
    bar.set_tags({})
    assert bar.isHidden()


# ----------------------------------------------------------------------------------------------- the pages
class FakeService:
    supports_spawn = False

    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        self.calls.append((action, kwargs))


@pytest.fixture
def view(tmp_path, qtbot):
    store = make_store(tmp_path)
    window = MainWindow(store, FakeService())
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    return window


def project_ids(view):
    return [view.project_list.item(i).data(0x0100) for i in range(view.project_list.count())]


def test_the_projects_page_filters_by_tag_and_remembers_it(view, tmp_path):
    assert set(project_ids(view)) == {"p1", "p2", "p3"}
    view.tag_bar.chips["工地"].click()
    assert set(project_ids(view)) == {"p1", "p2"}
    view.tag_bar.chips["前端"].click()                         # a second tag narrows further
    assert project_ids(view) == ["p1"]
    assert UiPrefs(view.prefs.path).project_tags == ["工地", "前端"]
    view.tag_bar.clear_button.click()
    assert set(project_ids(view)) == {"p1", "p2", "p3"} and UiPrefs(view.prefs.path).project_tags == []


def test_the_saved_filter_is_applied_on_the_next_start(tmp_path, qtbot):
    store = make_store(tmp_path)
    first = MainWindow(store, FakeService())
    qtbot.addWidget(first)
    first.tag_bar.chips["後端"].click()
    first.close()
    again = MainWindow(store, FakeService())
    qtbot.addWidget(again)
    assert project_ids(again) == ["p2"]


def test_a_tag_nobody_uses_any_more_leaves_the_filter(view):
    view.tag_bar.chips["後端"].click()
    view.store.save("presets", {**view.store.get("presets", "p2"), "tags": ["工地"]})
    view.reload()
    assert view.tag_bar.selected() == [] and set(project_ids(view)) == {"p1", "p2", "p3"}


def test_rows_and_details_show_the_tags(view):
    row = next(view.project_list.item(i) for i in range(view.project_list.count()) if view.project_list.item(i).data(0x0100) == "p1")
    assert "#工地 #前端" in row.text()
    view.project_list.setCurrentItem(row)
    assert "工地、前端" in view.project_info.text()


def test_the_workspace_list_filters_by_tag_independently_and_remembers_it(view):
    launcher = view.workspace.launcher
    assert {p["id"] for p in launcher.visible_projects()} == {"p1", "p2", "p3"}
    launcher.tag_bar.chips["後端"].click()
    assert [p["id"] for p in launcher.visible_projects()] == ["p2"]
    assert UiPrefs(view.prefs.path).workspace_tags == ["後端"]
    assert set(project_ids(view)) == {"p1", "p2", "p3"}        # the Projects page is unaffected
    assert "沒有" not in launcher.hint.text() or launcher.hint.isHidden()
    launcher.search.setText("zzz")
    assert launcher.visible_projects() == [] and not launcher.hint.isHidden() and "沒有符合" in launcher.hint.text()


def test_project_dialog_saves_normalised_tags_and_rejects_bad_ones(view, monkeypatch):
    seen = {}

    class Dialog(ui.RecordDialog):
        text = "新, #工地"

        def __init__(self, title, fields, values, choices, parent, checkboxes=None, draft=False, validate=None):
            super().__init__(title, fields, values, choices, parent, checkboxes, draft, validate)
            seen["tags_field"] = values.get("tags")

        def exec(self):
            return ui.QDialog.DialogCode.Accepted

        def values(self):
            return {"name": "N", "path": "/x", "account_id": "", "environment_id": "gpu", "ask_account": True,
                    "persistent": True, "tags": Dialog.text}
    monkeypatch.setattr(ui, "RecordDialog", Dialog)
    assert view.add_project() is True
    assert next(p for p in view.store.list("presets") if p["name"] == "N")["tags"] == ["新", "工地"]
    Dialog.text = "x" * 40
    notes = []
    monkeypatch.setattr(view, "_notice", notes.append)
    assert view.add_project() is False and "24" in notes[0]
    view.project_list.setCurrentRow(0)
    view.edit_project()                                      # editing shows the existing tags as text
    assert isinstance(seen["tags_field"], str)


def test_deleting_a_project_from_the_page_works_even_with_history(view, monkeypatch):
    session(view.store, "old", "p1", "ended")
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.reload()
    view._selected_project = "p1"
    view.delete_project()
    assert "p1" not in project_ids(view) and view.store.get("sessions", "old")["preset_id"] == ""
