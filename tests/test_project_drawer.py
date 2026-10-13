"""The project page's workspace drawer: every session of a project, not just the newest one."""
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QAbstractButton

from vibe_launcher import env_state
from vibe_launcher.store import Store
from vibe_launcher.ui import VibeLauncherWindow as MainWindow


def stamp(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def sess(sid, status, preset="p", minutes_ago=0, **extra):
    return {"id": sid, "preset_id": preset, "status": status, "updated_at": stamp(minutes_ago), **extra}


# ------------------------------------------------------------------------------ view-model
def test_a_projects_sessions_list_live_ones_first_then_finished_newest_first():
    sessions = [sess("old", "ended", minutes_ago=50), sess("run", "running", minutes_ago=30), sess("new", "failed", minutes_ago=1),
                sess("unk", "unknown", minutes_ago=5), sess("other", "running", preset="q")]
    assert [s["id"] for s in env_state.project_sessions("p", sessions)] == ["unk", "run", "new", "old"]


def test_the_row_badge_counts_live_workspaces_when_there_are_several():
    one = [sess("a", "running")]
    assert env_state.project_badge("p", one)[0] == "運行中"
    two = [sess("a", "running"), sess("b", "detached", minutes_ago=3), sess("c", "ended", minutes_ago=9)]
    assert env_state.project_badge("p", two)[0] == "運行中 ×2"
    assert env_state.project_badge("p", [sess("c", "ended")])[0] == "已停止"
    assert env_state.project_badge("nobody", two) is None


# ------------------------------------------------------------------------------ the page
class FakeService:
    supports_spawn = False

    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        kwargs.pop("workspace_id", None)      # which tmux session a launch joins is tested in test_workspace
        self.calls.append((action, kwargs))


@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "d.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("accounts", {"id": "a", "name": "Alpha", "tool": "claude", "auth": "api", "model": "m"})
    store.save("accounts", {"id": "b", "name": "Beta", "tool": "codex", "auth": "api", "model": "m"})
    store.save("presets", {"id": "p", "name": "Site", "account_id": "", "environment_id": "gpu", "path": "/w"})
    store.save("presets", {"id": "q", "name": "Other", "account_id": "", "environment_id": "gpu", "path": "/o"})
    window = MainWindow(store, FakeService())
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.resize(1200, 800)
    window.show()
    return window


def add(view, sid, status, account="a", preset="p", minutes_ago=0, **extra):
    view.store.save("sessions", {"id": sid, "preset_id": preset, "account_id": account, "environment_id": "gpu", "name": "Site",
                                 "path": "/w", "status": status, "updated_at": stamp(minutes_ago), **extra})


def select_project(view, preset):
    for index in range(view.project_list.count()):
        if view.project_list.item(index).data(0x0100) == preset:
            view.project_list.setCurrentRow(index)
    view.reload()


def buttons(view, session_id, action):
    return [b for b in view.work_drawer.findChildren(QAbstractButton)
            if b.property("workAction") == action and b.parent().property("workRow") == session_id and not b.parent().property("disposed")]


def test_the_drawer_lists_every_workspace_of_the_selected_project_only(view):
    add(view, "s1", "running")
    add(view, "s2", "detached", account="b", minutes_ago=4)
    add(view, "s3", "ended", minutes_ago=30)
    add(view, "x1", "running", preset="q")
    select_project(view, "p")
    rows = view.work_drawer.rows()
    assert [r.session_id for r in rows] == ["s1", "s2", "s3"]
    assert [r.title for r in rows][:2] == ["Claude · Alpha", "Codex · Beta"]
    assert "3 個" in view.work_drawer.summary.text() and "進行中 2" in view.work_drawer.summary.text()


def test_the_project_row_shows_how_many_are_live(view):
    add(view, "s1", "running")
    add(view, "s2", "detached", minutes_ago=2)
    view.reload()
    badge = next(view.project_list.item(i).data(0x0101) for i in range(view.project_list.count()) if view.project_list.item(i).data(0x0100) == "p")
    assert badge[0] == "背景執行 ×2", "no terminal is open for them here: they run in the background on the server"


def test_a_project_with_no_workspaces_says_so_and_switching_projects_switches_the_drawer(view):
    add(view, "x1", "running", preset="q")
    select_project(view, "p")
    assert view.work_drawer.rows() == [] and "尚未" in view.work_drawer.summary.text()
    select_project(view, "q")
    assert [r.session_id for r in view.work_drawer.rows()] == ["x1"]


def test_the_drawer_collapses_and_expands(view):
    add(view, "s1", "running")
    select_project(view, "p")
    assert view.work_drawer.body.isVisibleTo(view.work_drawer)
    view.work_drawer.toggle.click()
    assert not view.work_drawer.is_open() and not view.work_drawer.body.isVisibleTo(view.work_drawer)
    view.reload()                                             # a refresh must not pop it open again
    assert not view.work_drawer.is_open()
    view.work_drawer.toggle.click()
    assert view.work_drawer.is_open()


def test_each_row_acts_on_its_own_session(view, monkeypatch):
    add(view, "s1", "detached")
    add(view, "s2", "detached", minutes_ago=3)
    add(view, "s3", "ended", minutes_ago=9)
    select_project(view, "p")
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    buttons(view, "s2", "attach")[0].click()
    buttons(view, "s1", "stop")[0].click()
    assert ("attach", {"session_id": "s2"}) in view.service.calls
    assert ("stop", {"session_id": "s1"}) in view.service.calls
    assert not buttons(view, "s3", "stop") and buttons(view, "s3", "remove")
    buttons(view, "s3", "remove")[0].click()
    assert view.store.get("sessions", "s2") and not any(s["id"] == "s3" for s in view.store.list("sessions"))


def test_a_workspace_that_is_open_offers_to_jump_to_it(view):
    add(view, "s1", "running", account="a")
    select_project(view, "p")
    assert buttons(view, "s1", "attach") and not buttons(view, "s1", "switch")

    class Terminal:
        def is_running(self):
            return True

    class Info:
        id, session_id, workspace_id = "tab-1", "s1", ""

    class Tab:
        info = Info()
        exit_code = None

        @staticmethod
        def alive():
            return True

    original_tabs = view.workspace.tabs
    view.workspace.tabs = lambda: [Tab()]
    view.workspace.select = lambda tab_id: setattr(view, "_jumped", tab_id)
    view._refresh_project_drawer()
    assert view.work_drawer.rows()[0].tab_id == "tab-1" and buttons(view, "s1", "switch") and not buttons(view, "s1", "attach")
    buttons(view, "s1", "switch")[0].click()
    assert view._jumped == "tab-1"
    view.workspace.tabs = original_tabs


def test_the_new_work_button_opens_the_project_again(view, monkeypatch):
    opened = []
    monkeypatch.setattr(view, "open_terminal", lambda preset_id, *a, **k: opened.append(preset_id))
    select_project(view, "p")
    view.work_drawer.add.click()
    assert opened == ["p"]


# ------------------------------------------------------------------------------ bringing workspaces back
ENVS = [{"id": "gpu", "kind": "ssh"}, {"id": "local", "kind": "local"}]
ACCOUNTS = [{"id": "a", "tool": "claude", "auth": "api"}, {"id": "b", "tool": "codex", "auth": "api"}]


def plan(sessions, open_ids=frozenset(), accounts=ACCOUNTS):
    return [(s.action, s.session["id"]) for s in env_state.restore_plan("p", sessions, ENVS, accounts, open_ids)]


def ses(sid, status, account="a", env="gpu", minutes_ago=0, **extra):
    return sess(sid, status, minutes_ago=minutes_ago, account_id=account, environment_id=env, **extra)


def test_running_ones_are_reattached_and_stopped_ones_started_again():
    sessions = [ses("run", "detached"), ses("dead", "ended", account="b", minutes_ago=5)]
    assert plan(sessions) == [("attach", "run"), ("relaunch", "dead")]


def test_only_the_newest_stopped_record_of_a_combination_comes_back():
    sessions = [ses("new", "ended", minutes_ago=1), ses("old", "ended", minutes_ago=50), ses("fail", "failed", minutes_ago=80)]
    assert plan(sessions) == [("relaunch", "new")]


def test_a_combination_that_is_already_open_or_coming_up_is_not_started_twice():
    assert plan([ses("run", "running"), ses("dead", "ended", minutes_ago=5)], open_ids={"run"}) == []
    assert plan([ses("up", "connecting"), ses("dead", "ended", minutes_ago=5)]) == []
    assert plan([ses("run", "detached"), ses("dead", "ended", minutes_ago=5)]) == [("attach", "run")]


def test_two_live_workspaces_of_the_same_kind_are_both_reattached():
    assert plan([ses("one", "detached"), ses("two", "detached", minutes_ago=3)]) == [("attach", "one"), ("attach", "two")]


def test_local_work_cannot_be_reattached_but_a_dead_one_restarts():
    assert plan([ses("l1", "unknown", env="local")]) == [("relaunch", "l1")]
    assert plan([ses("l2", "running", env="local")]) == []
    assert plan([ses("n", "detached", persistent=False)]) == []       # no tmux, nothing to attach to


def test_a_workspace_whose_account_is_gone_is_left_out_and_a_shell_needs_none():
    assert plan([ses("x", "ended", account="gone")]) == []
    assert plan([ses("sh", "ended", account="", mode="shell")]) == [("relaunch", "sh")]


def test_the_attach_button_restarts_stopped_workspaces_and_replaces_their_records(view, monkeypatch):
    add(view, "live", "detached")
    add(view, "dead", "ended", account="b", minutes_ago=7)
    add(view, "older", "ended", account="b", minutes_ago=70)
    view.reload()
    select_project(view, "p")
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda title, message, **k: asked.append(message) or True)
    view.attach_project()
    calls = view.service.calls
    assert ("attach", {"session_id": "live"}) in calls
    launch = next(c for c in calls if c[0] == "launch")[1]
    assert launch == {"preset_id": "p", "environment_id": "gpu", "account_id": "b", "resume": True}      # same service, account and host
    assert "重新連線 Claude（Alpha）" in asked[0] and "重新開啟 Codex（Beta）" in asked[0]
    ids = {s["id"] for s in view.store.list("sessions")}
    assert "dead" not in ids and "live" in ids and "older" in ids                          # the restarted record was replaced


def test_nothing_to_bring_back_says_why(view, monkeypatch):
    notes = []
    monkeypatch.setattr(view, "_notice", notes.append)
    select_project(view, "p")
    view.attach_project()
    assert "還沒有工作階段" in notes[-1]
    add(view, "up", "connecting")
    view.reload()
    view.attach_project()
    assert "都已經開著" in notes[-1]
    assert view.service.calls == []


def test_a_stopped_row_offers_restart_and_the_drawer_recalls_everything(view, monkeypatch):
    add(view, "live", "detached")
    add(view, "dead", "failed", account="b", minutes_ago=7)
    select_project(view, "p")
    assert buttons(view, "dead", "restart") and not buttons(view, "live", "restart")
    assert view.work_drawer.restore.text() == "還原全部（2）" and not view.work_drawer.restore.isHidden()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    buttons(view, "dead", "restart")[0].click()
    assert [c[0] for c in view.service.calls] == ["launch"]
    view.service.calls.clear()
    add(view, "dead2", "ended", account="b", minutes_ago=9)
    view.reload()
    view.work_drawer.restore.click()
    assert sorted(c[0] for c in view.service.calls) == ["attach", "launch"]


def test_a_workspace_of_a_deleted_project_or_account_cannot_be_restarted(view, monkeypatch):
    notes = []
    monkeypatch.setattr(view, "_notice", notes.append)
    assert view._relaunch({"id": "z", "preset_id": "gone", "status": "ended"}) is False
    assert view._relaunch({"id": "z", "preset_id": "p", "account_id": "gone", "environment_id": "gpu", "status": "ended"}) is False
    assert len(notes) == 2 and view.service.calls == []


def test_the_workspace_page_offers_the_recall_for_a_project(view, monkeypatch):
    add(view, "dead", "ended", account="b")
    view.reload()
    assert view.workspace.launcher.recoverable == {"p": 1}
    row_buttons = [b.text() for b in view.workspace.launcher.findChildren(QAbstractButton) if b.isVisibleTo(view.workspace.launcher)]
    assert any("還原 1" in t for t in row_buttons)
    view.workspace.launcher.restore.emit("p")                          # the card's button reaches the window's recall
    assert [c[0] for c in view.service.calls] == ["launch"]
    view.reload()
    assert view.workspace.launcher.recoverable == {}                  # the stopped record was replaced by the new one's launch


# ------------------------------------------------------------------------------ a project never moves to another host
def test_the_service_refuses_to_open_a_project_on_another_environment(tmp_path):
    from vibe_launcher.service import LauncherService
    store = Store(tmp_path / "bind.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("environments", {"id": "box", "name": "Box", "kind": "ssh", "ssh_target": "box"})
    store.save("accounts", {"id": "a", "name": "A", "tool": "claude", "auth": "api", "model": "m"})
    store.save("presets", {"id": "p", "name": "Site", "account_id": "", "environment_id": "gpu", "path": "/w"})
    with pytest.raises(ValueError, match="已綁定環境「GPU」"):
        LauncherService(store).start("launch", preset_id="p", account_id="a", environment_id="box", spawn=lambda *a: None)
    started = []
    LauncherService(store).start("launch", preset_id="p", account_id="a", environment_id="gpu", spawn=lambda args, ctx: started.append(ctx))
    LauncherService(store).start("launch", preset_id="p", account_id="a", spawn=lambda args, ctx: started.append(ctx))
    assert [c["environment_id"] for c in started] == ["gpu", "gpu"]


def test_the_workspace_has_no_open_on_another_host_button(view):
    add(view, "dead", "ended", account="b")
    view.reload()
    assert not hasattr(view.workspace.launcher, "more")
    assert not hasattr(view.workspace, "_launcher_menu")


def test_a_workspace_from_before_the_binding_is_not_restarted_elsewhere(view, monkeypatch):
    view.store.save("environments", {"id": "box", "name": "Box", "kind": "ssh", "ssh_target": "box"})
    add(view, "stray", "ended", account="b")
    view.store.save("sessions", {**view.store.get("sessions", "stray"), "environment_id": "box"})
    view.reload()
    select_project(view, "p")
    assert view._restore_steps("p") == []
    assert not buttons(view, "stray", "restart")
    notes = []
    monkeypatch.setattr(view, "_notice", notes.append)
    assert view._relaunch(view.store.get("sessions", "stray")) is False and notes
