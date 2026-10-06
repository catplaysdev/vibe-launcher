"""Per-project keep-alive (tmux) switch, per-project status, and honest session actions."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vibe_launcher import runner, runtime
from vibe_launcher.env_state import project_status, session_badge
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow

ACCOUNT = {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"}


def iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# ----------------------------------------------------------------------------- store / service


def test_projects_keep_running_after_the_terminal_closes_by_default(tmp_path):
    store = Store(tmp_path / "s.db")
    account = store.save("accounts", {"name": "A", "tool": "codex", "auth": "subscription"})
    preset = store.save("presets", {"name": "P", "account_id": account["id"], "environment_id": "local", "path": "C:/p"})
    assert preset["persistent"] is True
    off = store.save("presets", {**preset, "persistent": False})
    assert store.get("presets", off["id"])["persistent"] is False
    with pytest.raises(ValueError):
        store.save("presets", {**preset, "persistent": "yes"})
    with pytest.raises(ValueError):
        store.save("sessions", {"id": "x", "account_id": account["id"], "environment_id": "local", "name": "n", "path": "p",
                                "persistent": "no"})


@pytest.mark.parametrize("persistent", [True, False])
def test_launch_request_and_session_carry_the_projects_choice(tmp_path, monkeypatch, persistent):
    store = Store(tmp_path / "s.db")
    account = store.save("accounts", {"name": "A", "tool": "codex", "auth": "subscription"})
    preset = store.save("presets", {"name": "P", "account_id": account["id"], "environment_id": "local", "path": "C:/p",
                                    "persistent": persistent})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    LauncherService(store).start("launch", preset_id=preset["id"])
    job = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    assert job["request"]["persistent"] is persistent
    assert store.list("sessions")[0]["persistent"] is persistent


def test_request_validation_rejects_a_non_boolean_flag():
    with pytest.raises(ValueError):
        runtime.validate_request({"action": "launch", "persistent": "false"})
    runtime.validate_request({"action": "launch", "persistent": False})


# ----------------------------------------------------------------------------- remote behaviour without tmux


def remote(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "doctor", lambda *a: None)
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    monkeypatch.setattr(runtime, "authenticate", lambda *a: {})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: {111: True, 222: False}.get(pid))
    return tmp_path / "state"


def launch_request(tmp_path, **extra):
    return {"action": "launch", "account": ACCOUNT, "session_id": "s", "environment_id": "host", "path": str(tmp_path), **extra}


def test_direct_launch_runs_in_the_terminal_without_tmux(tmp_path, monkeypatch):
    root = remote(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: pytest.fail("a direct launch must not touch tmux"))

    def worker(request, root_, env=None):
        runtime.save_status(root_, request, "ended")
        return 0
    monkeypatch.setattr(runtime, "run_worker", worker)
    runtime._worker_created = False
    code = runtime.perform(launch_request(tmp_path, persistent=False), root)
    assert code == runtime.EXIT_SESSION_ENDED
    assert runtime._worker_created, "a failure after this point must not be reported as 'never started'"
    assert json.loads(runtime.read_private(runtime.session_path(root, "s")))["persistent"] is False


def test_direct_launch_that_crashes_is_reported_failed(tmp_path, monkeypatch):
    root = remote(monkeypatch, tmp_path)

    def worker(request, root_, env=None):
        runtime.save_status(root_, request, "failed")
        return 1
    monkeypatch.setattr(runtime, "run_worker", worker)
    assert runtime.perform(launch_request(tmp_path, persistent=False), root) == runtime.EXIT_SESSION_FAILED


def test_direct_sessions_cannot_be_reattached(tmp_path, monkeypatch):
    root = remote(monkeypatch, tmp_path)
    runtime.write_json(runtime.session_path(root, "s"), {"session_id": "s", "environment_id": "host", "persistent": False,
                                                          "status": "running", "pid": 111})
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: pytest.fail("no tmux for a direct session"))
    with pytest.raises(RuntimeError, match="without tmux"):
        runtime.perform({"action": "attach", "session_id": "s", "environment_id": "host"}, root)


def test_stopping_a_direct_session_terminates_its_process(tmp_path, monkeypatch):
    root = remote(monkeypatch, tmp_path)
    runtime.write_json(runtime.session_path(root, "s"), {"session_id": "s", "environment_id": "host", "persistent": False,
                                                          "status": "running", "pid": 111})
    killed = []
    monkeypatch.setattr(runtime.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: pytest.fail("no tmux for a direct session"))
    assert runtime.perform({"action": "stop", "session_id": "s", "environment_id": "host"}, root) == 0
    assert killed == [(111, runtime.signal.SIGTERM)]
    assert json.loads(runtime.read_private(runtime.session_path(root, "s")))["status"] == "ended"


def refresh(root, monkeypatch):
    seen = {}
    monkeypatch.setattr(runtime, "emit", lambda request, event: seen.update({event["session_id"]: event["status"]}))
    runtime.list_sessions({"environment_id": "host"}, root)
    return seen


def test_refresh_uses_the_cli_pid_for_sessions_started_without_tmux(tmp_path, monkeypatch):
    root = remote(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: pytest.fail("direct sessions are not looked up in tmux"))
    for name, pid, status in (("alive", 111, "running"), ("dead", 222, "running"), ("unsure", 999, "running"),
                              ("old", 222, "ended")):
        runtime.write_json(runtime.session_path(root, name), {"session_id": name, "environment_id": "host",
                                                              "persistent": False, "status": status, "pid": pid})
    seen = refresh(root, monkeypatch)
    # alive process -> running; dead process cannot still be running -> ended; cannot tell -> unknown; finished stays finished
    assert seen == {"alive": "running", "dead": "ended", "unsure": "unknown", "old": "ended"}


def test_refresh_still_uses_tmux_for_keep_alive_sessions(tmp_path, monkeypatch):
    root = remote(monkeypatch, tmp_path)
    runtime.write_json(runtime.session_path(root, "k"), {"session_id": "k", "environment_id": "host", "status": "detached"})
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert refresh(root, monkeypatch) == {"k": "running"}


def test_pid_alive_never_signals_a_process_on_windows(monkeypatch):
    monkeypatch.setattr(runtime.os, "kill", lambda *a: pytest.fail("os.kill(pid, 0) terminates processes on Windows"))
    assert runtime.pid_alive(1234) is None
    assert runner.process_alive(0) is False
    import os as _os
    assert runner.process_alive(_os.getpid()) is True


# ----------------------------------------------------------------------------- per-project status


def sess(preset, status, minutes):
    return {"preset_id": preset, "status": status, "updated_at": iso(minutes=minutes)}


def test_project_status_prefers_a_possibly_live_session_over_old_history():
    sessions = [sess("p", "ended", 60), sess("p", "running", 30), sess("p", "failed", 5), sess("other", "running", 1)]
    assert project_status("p", sessions) == "running"
    assert project_status("p", [sess("p", "ended", 60), sess("p", "failed", 5)]) == "failed"
    assert project_status("p", [sess("p", "unknown", 5), sess("p", "ended", 1)]) == "unknown", "unknown may still be alive"
    assert project_status("nobody", sessions) is None


def test_every_status_a_session_can_have_has_a_label_and_a_tone():
    labels = {s: session_badge(s) for s in ("queued", "connecting", "waiting_auth", "running", "detached", "ended", "failed", "unknown")}
    assert labels["connecting"][0] == "連線中" and labels["ended"][0] == "已停止" and labels["running"][0] == "運行中"
    assert len({tone for label, tone in labels.values()}) >= 5


# ----------------------------------------------------------------------------- window


class Service:
    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        self.calls.append((action, kwargs))


@pytest.fixture
def window(tmp_path, qtbot):
    store = Store(tmp_path / "gui.db")
    account = store.save("accounts", {"name": "工作帳號", "tool": "codex", "auth": "subscription"})
    ssh = store.save("environments", {"name": "伺服器", "kind": "ssh", "ssh_target": "user@server"})
    projects = {}
    for name, env in (("連線中專案", ssh["id"]), ("已停止專案", ssh["id"]), ("失敗專案", ssh["id"]), ("從未啟動", ssh["id"]),
                      ("本機專案", "local")):
        projects[name] = store.save("presets", {"name": name, "account_id": account["id"], "environment_id": env, "path": "/" + name})
    view = MainWindow(store, Service())
    qtbot.addWidget(view)
    view.resize(1200, 800)
    view.show()
    return view, store, account, ssh, projects


def add_session(store, account, env_id, project, status, sid, minutes=1, **extra):
    return store.save("sessions", {"id": sid, "preset_id": project["id"], "account_id": account["id"], "environment_id": env_id,
                                   "name": project["name"], "path": project["path"], "status": status,
                                   "updated_at": iso(minutes=minutes), **extra})


def project_badges(view):
    from vibe_launcher.kit import BADGE_ROLE
    out = {}
    for row in range(view.project_list.count()):
        item = view.project_list.item(row)
        project = view._by_id("presets", item.data(256))
        out[project["name"]] = item.data(BADGE_ROLE)
    return out


def test_each_project_shows_its_own_status(window):
    view, store, account, ssh, projects = window
    add_session(store, account, ssh["id"], projects["連線中專案"], "connecting", "s1")
    add_session(store, account, ssh["id"], projects["已停止專案"], "ended", "s2")
    add_session(store, account, ssh["id"], projects["失敗專案"], "failed", "s3")
    add_session(store, account, "local", projects["本機專案"], "running", "s4")
    view.reload()
    badges = project_badges(view)
    assert tuple(badges["連線中專案"]) == ("連線中", "info")
    assert tuple(badges["已停止專案"]) == ("已停止", "unknown")
    assert tuple(badges["失敗專案"]) == ("失敗", "danger")
    assert tuple(badges["本機專案"]) == ("運行中", "ok")
    assert badges["從未啟動"] is None
    # the list follows status changes without the user reloading anything
    store.save("sessions", {**store.get("sessions", "s1"), "status": "detached"})
    view._poll()
    assert tuple(project_badges(view)["連線中專案"]) == ("背景執行", "info")


def test_project_detail_states_status_and_run_mode(window):
    view, store, account, ssh, projects = window
    add_session(store, account, ssh["id"], projects["已停止專案"], "ended", "s2")
    view.reload()
    for row in range(view.project_list.count()):
        if view.project_list.item(row).data(256) == projects["已停止專案"]["id"]:
            view.project_list.setCurrentRow(row)
    text = view.project_info.text()
    assert "已停止" in text and "背景保持（tmux）" in text
    store.save("presets", {**projects["已停止專案"], "persistent": False})
    view.reload()
    assert "直接執行（不用 tmux）" in view.project_info.text()


def test_project_dialog_offers_the_keep_alive_switch_defaulting_to_on(window, monkeypatch):
    view, store, account, ssh, projects = window
    from vibe_launcher import ui
    captured = {}

    class Dialog(ui.RecordDialog):
        def __init__(self, title, fields, values, choices, parent, checkboxes=None, draft=False, validate=None):
            super().__init__(title, fields, values, choices, parent, checkboxes, draft, validate)
            captured.update(values=values, checkboxes=checkboxes)
            self._values = {"name": "新", "path": "/x", "account_id": account["id"], "environment_id": ssh["id"],
                            "persistent": False}

        def exec(self):
            return ui.QDialog.DialogCode.Accepted

        def values(self):
            return dict(self._values)
    monkeypatch.setattr(ui, "RecordDialog", Dialog)
    assert view.add_project() is True
    assert captured["values"]["persistent"] is True and "persistent" in [key for key, _ in captured["checkboxes"]]
    saved = next(p for p in store.list("presets") if p["name"] == "新")
    assert saved["persistent"] is False


def select_session(view, session_id):
    view.set_sessions_grouping("time")
    for row in range(view.sessions_list.count()):
        if view.sessions_list.item(row).data(256) == session_id:
            view.sessions_list.setCurrentRow(row)
            return
    raise AssertionError("session not listed")


def test_session_buttons_only_offer_what_can_work(window):
    view, store, account, ssh, projects = window
    cases = {
        "keep": add_session(store, account, ssh["id"], projects["連線中專案"], "detached", "keep", persistent=True),
        "direct": add_session(store, account, ssh["id"], projects["已停止專案"], "running", "direct", persistent=False),
        "ended": add_session(store, account, ssh["id"], projects["失敗專案"], "ended", "ended"),
        "local": add_session(store, account, "local", projects["本機專案"], "running", "local"),
        "unknown": add_session(store, account, ssh["id"], projects["從未啟動"], "unknown", "unknown"),
    }
    view.reload()
    expected = {  # attach, stop, remove
        "keep": (True, True, False), "direct": (False, True, False), "ended": (False, False, True),
        "local": (False, True, False), "unknown": (True, True, True),
    }
    for name, flags in expected.items():
        select_session(view, name)
        got = (view.session_attach_button.isEnabled(), view.session_stop_button.isEnabled(), view.session_remove_button.isEnabled())
        assert got == flags, name
    select_session(view, "keep")
    assert "tmux" in view.session_hint.text() and "關閉終端" in view.session_hint.text()
    select_session(view, "direct")
    assert "沒有使用 tmux" in view.session_hint.text()
    select_session(view, "ended")
    assert "已停止" in view.session_hint.text()


def test_a_closed_terminal_stops_looking_connected(window, monkeypatch):
    view, store, account, ssh, projects = window
    add_session(store, account, ssh["id"], projects["連線中專案"], "connecting", "gone", runner_pid=424242)
    add_session(store, account, ssh["id"], projects["已停止專案"], "connecting", "open", runner_pid=313131)
    add_session(store, account, ssh["id"], projects["失敗專案"], "detached", "detached", runner_pid=424242)
    monkeypatch.setattr(runner, "process_alive", lambda pid: pid == 313131)
    view.reload()
    view._poll()
    assert store.get("sessions", "gone")["status"] == "unknown", "its terminal is gone, so 'connecting' can never advance"
    assert store.get("sessions", "open")["status"] == "connecting", "its terminal is still open"
    assert store.get("sessions", "detached")["status"] == "detached", "a remote tmux session does not depend on the terminal"


def test_the_runner_records_its_own_pid_on_the_session(tmp_path):
    store = Store(tmp_path / "s.db")
    account = store.save("accounts", ACCOUNT)
    store.save("sessions", {"id": "s", "account_id": account["id"], "environment_id": "local", "name": "n", "path": "p",
                            "status": "queued"})
    runner.remember_runner(store, {"session_id": "s"})
    assert store.get("sessions", "s")["runner_pid"] > 0
    runner.remember_runner(store, {})  # no session: nothing to do, and no error
