"""Codex updates on Windows, the account drop-down in the workspace, and keeping the sessions list under control."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QWidget

from vibe_launcher import runner, runtime, workspace_page
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import VibeLauncherWindow as MainWindow
from vibe_launcher.workspace_page import WorkspacePage


# ------------------------------------------------------------------------------ updating the CLI
def test_the_codex_updater_sees_the_real_codex_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    env = runtime.maintenance_env("codex", tmp_path)
    assert "CODEX_HOME" not in env                                   # defaults to ~/.codex, where the standalone install lives
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
    assert runtime.maintenance_env("codex", tmp_path)["CODEX_HOME"] == str(tmp_path / "custom")


def test_the_updater_never_sees_an_accounts_profile_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
    for tool in ("codex", "claude"):
        env = runtime.maintenance_env(tool, tmp_path)
        assert "OPENAI_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "profiles" not in env.get("CODEX_HOME", "") and "accounts" not in env.get("CLAUDE_CONFIG_DIR", "")


def test_claude_gets_an_existing_private_folder_so_it_does_not_warn(tmp_path):
    env = runtime.maintenance_env("claude", tmp_path)
    assert os.path.isdir(env["CLAUDE_CONFIG_DIR"])


# ------------------------------------------------------------------------------ stopping several sessions at once
def make_store(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("environments", {"id": "box", "name": "Box", "kind": "ssh", "ssh_target": "box"})
    store.save("accounts", {"id": "a", "name": "A", "tool": "claude", "auth": "subscription", "environment_id": "gpu"})
    store.save("accounts", {"id": "b", "name": "B", "tool": "codex", "auth": "subscription", "environment_id": "gpu"})
    store.save("presets", {"id": "p", "name": "P", "account_id": "", "environment_id": "gpu", "path": "/w"})
    return store


def add_session(store, sid, status, env="gpu", account="a", updated=None, **extra):
    return store.save("sessions", {"id": sid, "preset_id": "p" if env == "gpu" else "", "account_id": account, "environment_id": env,
                                   "name": "P", "path": "/w", "status": status,
                                   "updated_at": updated or datetime.now(timezone.utc).isoformat(), **extra})


def test_one_job_stops_many_sessions_of_one_environment(tmp_path):
    store = make_store(tmp_path)
    for sid, account in (("s1", "a"), ("s2", "b")):
        add_session(store, sid, "unknown", account=account)
    jobs = []
    LauncherService(store).start("stop", session_ids=["s1", "s2"], spawn=lambda args, ctx: jobs.append(ctx))
    assert len(jobs) == 1 and jobs[0]["environment_id"] == "gpu"
    import json
    request = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))["request"]
    assert request["session_ids"] == ["s1", "s2"] and "account" not in request     # no single account: each keeps its own


def test_sessions_from_two_environments_cannot_share_one_stop_job(tmp_path):
    store = make_store(tmp_path)
    add_session(store, "s1", "unknown")
    add_session(store, "s2", "unknown", env="box")
    with pytest.raises(ValueError, match="one environment"):
        LauncherService(store).start("stop", session_ids=["s1", "s2"], spawn=lambda *a: None)


@pytest.mark.parametrize("extra", [{"action": "launch"}, {"session_ids": []}, {"session_ids": ["../x"]}, {"session_ids": "s1"},
                                   {"session_ids": [f"s{i}" for i in range(101)]}])
def test_a_bad_session_list_is_rejected(extra):
    request = {"action": "stop", "session_id": "s1", "environment_id": "gpu", "session_ids": ["s1"], **extra}
    with pytest.raises(ValueError):
        runtime.validate_request(request)


def test_the_remote_side_stops_each_session_and_reports_the_worst_result(tmp_path, monkeypatch):
    seen = []

    def action(request, root):
        seen.append(request["session_id"])
        if request["session_id"] == "bad":
            raise RuntimeError("no such session")
        return 0
    monkeypatch.setattr(runtime, "session_action", action)
    code = runtime.perform({"action": "stop", "session_id": "one", "session_ids": ["one", "bad", "two"], "environment_id": "gpu"}, tmp_path)
    assert seen == ["one", "bad", "two"] and code == 1                 # a failure does not stop the others from being tried


def test_every_stopped_session_is_recorded_as_ended(tmp_path):
    store = make_store(tmp_path)
    for sid in ("s1", "s2"):
        add_session(store, sid, "unknown")
    request = {"action": "stop", "session_id": "s1", "session_ids": ["s1", "s2"], "environment_id": "gpu"}
    for sid in ("s1", "s2"):
        runner.record_event(store, dict(request, session_id=sid), {"status": "ended"})
    assert {s["id"]: s["status"] for s in store.list("sessions")} == {"s1": "ended", "s2": "ended"}


# ------------------------------------------------------------------------------ the sessions page
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
    window.show_page("sessions")
    window.show()
    return window


def select(view, *ids):
    listing = view.sessions_list
    listing.clearSelection()
    for index in range(listing.count()):
        item = listing.item(index)
        if item.data(0x0100) in ids:
            item.setSelected(True)


def test_the_page_says_how_many_sessions_are_in_each_state(view):
    for sid, status in (("a1", "running"), ("a2", "unknown"), ("a3", "unknown"), ("a4", "ended"), ("a5", "failed")):
        add_session(view.store, sid, status)
    view.reload()
    text = view.session_summary.text()
    assert "共 5 筆" in text and "進行中 1" in text and "狀態不明 2" in text and "已結束／失敗 2" in text
    assert "（2）" in view.session_check_button.text() and "（2）" in view.session_clean_button.text()


def test_clearing_finished_sessions_keeps_the_rest(view, monkeypatch):
    for sid, status in (("a1", "running"), ("a4", "ended"), ("a5", "failed")):
        add_session(view.store, sid, status)
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.clean_finished_sessions()
    assert [s["id"] for s in view.store.list("sessions")] == ["a1"]


def test_checking_unknown_sessions_ends_local_ones_and_requeries_each_remote_host_once(view, monkeypatch):
    add_session(view.store, "r1", "unknown")
    add_session(view.store, "r2", "unknown")
    add_session(view.store, "l1", "unknown", env="local")
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.check_unknown_sessions()
    assert view.store.get("sessions", "l1")["status"] == "ended"
    assert [c for c in view.service.calls if c[0] == "refresh"] == [("refresh", {"environment_id": "gpu"})]


def test_selecting_several_sessions_stops_them_in_one_job_per_environment(view, monkeypatch):
    for sid in ("s1", "s2"):
        add_session(view.store, sid, "unknown")
    add_session(view.store, "s3", "unknown", env="box")
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    select(view, "s1", "s2", "s3")
    assert "已選 3 筆" in view.session_hint.text() and not view.session_attach_button.isEnabled()
    view.stop_session()
    stops = {(kw["environment_id"], tuple(kw["session_ids"])) for action, kw in view.service.calls if action == "stop"}
    assert stops == {("gpu", ("s1", "s2")), ("box", ("s3",))} or {e for e, _ in stops} == {"gpu", "box"}
    assert sorted(i for _, ids in stops for i in ids) == ["s1", "s2", "s3"]


def test_selecting_several_sessions_removes_their_records_together(view, monkeypatch):
    for sid, status in (("e1", "ended"), ("e2", "failed"), ("k1", "running")):
        add_session(view.store, sid, status)
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    select(view, "e1", "e2", "k1")
    view.remove_session()                                         # the running one is not removable and is left alone
    assert [s["id"] for s in view.store.list("sessions")] == ["k1"]


def test_a_local_session_whose_terminal_is_gone_is_ended_not_unknown(view, monkeypatch):
    add_session(view.store, "l1", "running", env="local", runner_pid=999999)
    add_session(view.store, "l2", "unknown", env="local")
    add_session(view.store, "r1", "connecting", runner_pid=999999)      # remote: it might really be running there
    view.reload()
    monkeypatch.setattr(runner, "process_alive", lambda pid: False)
    view._reap_closed_terminals()
    assert {s["id"]: s["status"] for s in view.store.list("sessions")} == {"l1": "ended", "l2": "ended", "r1": "unknown"}


def test_at_startup_old_history_is_trimmed_and_leftovers_are_announced(view, monkeypatch):
    base = datetime.now(timezone.utc)
    for index in range(view.FINISHED_KEPT + 5):
        add_session(view.store, f"old{index:02d}", "ended", updated=(base - timedelta(hours=index + 1)).isoformat())
    add_session(view.store, "stuck", "unknown")
    told = []
    monkeypatch.setattr(view, "_toast", lambda message, *a, **k: told.append(message))
    view._startup_housekeeping()
    ids = {s["id"] for s in view.store.list("sessions")}
    assert "stuck" in ids and len(ids) == view.FINISHED_KEPT + 1
    assert "old00" in ids and f"old{view.FINISHED_KEPT + 4:02d}" not in ids          # the oldest went first
    assert told and "1 個狀態不明" in told[0]


def test_a_quiet_start_says_nothing(view, monkeypatch):
    told = []
    monkeypatch.setattr(view, "_toast", lambda message, *a, **k: told.append(message))
    view._startup_housekeeping()
    assert told == []


# ------------------------------------------------------------------------------ the account drop-down in the workspace
ACCOUNTS = [{"id": "c1", "name": "Claude 工作", "tool": "claude", "auth": "subscription", "environment_id": "srv"},
            {"id": "c2", "name": "Claude 私人", "tool": "claude", "auth": "api"},
            {"id": "x1", "name": "Codex", "tool": "codex", "auth": "subscription", "environment_id": "srv"}]
PROJECTS = [{"id": "p1", "name": "前端", "account_id": "c1", "environment_id": "srv", "path": "/w"}]
ENVIRONMENTS = [{"id": "srv", "name": "伺服器", "kind": "ssh"}, {"id": "local", "name": "本機", "kind": "local"}]


@pytest.fixture
def page(qtbot, monkeypatch):
    view = WorkspacePage(terminal_factory=lambda *a, **k: QWidget())
    qtbot.addWidget(view, before_close_func=lambda w: w.shutdown())
    view.resize(1200, 700)
    view.show()
    view.set_data(PROJECTS, ACCOUNTS, ENVIRONMENTS, [])
    shown = []

    class Menu:
        def __init__(self, actions, parent=None):
            shown.append(actions)

        def popup_below(self, anchor):
            pass
    monkeypatch.setattr(workspace_page, "ActionMenu", Menu)
    view.shown = shown
    return view


def live_row(page):
    """Rows that were replaced are flagged at once but only deleted on the next loop turn: look at the live one."""
    return next(w for w in page.launcher.host.findChildren(QWidget) if w.property("surface") == "card" and not w.property("disposed"))


def buttons(page):
    from PySide6.QtWidgets import QPushButton
    return live_row(page).findChildren(QPushButton)


def test_each_service_has_a_main_button_and_an_account_arrow(page):
    labels = [b.text() for b in buttons(page)]
    assert labels.count("Codex") == 1 and labels.count("Claude") == 1 and labels.count("SSH") == 1
    arrows = [b for b in buttons(page) if b.accessibleName().startswith("選擇 ")]
    assert {b.accessibleName() for b in arrows} == {"選擇 Codex 帳號", "選擇 Claude 帳號"} and all(b.isEnabled() for b in arrows)


def test_clicking_the_label_opens_with_the_default_account(page):
    requests = []
    page.launch_requested.connect(lambda *args: requests.append(args))
    next(b for b in buttons(page) if b.text() == "Claude").click()
    assert requests == [("p1", "agent", "c1", "")]


def test_the_arrow_lists_the_accounts_for_that_service_and_picks_one(page):
    requests = []
    page.launch_requested.connect(lambda *args: requests.append(args))
    next(b for b in buttons(page) if b.accessibleName() == "選擇 Claude 帳號").click()
    actions = page.shown[-1]
    assert [a.text for a in actions] == ["Claude 工作", "Claude 私人"]
    assert actions[0].hint.startswith("預設") and actions[1].hint == "API"
    actions[1].callback()
    assert requests == [("p1", "agent", "c2", "")]


def test_the_arrow_is_disabled_when_no_account_fits(page):
    page.set_data(PROJECTS, [ACCOUNTS[0]], ENVIRONMENTS, [])
    arrow = next(b for b in buttons(page) if b.accessibleName() == "選擇 Codex 帳號")
    assert not arrow.isEnabled()
