"""Sessions: keep-alive modes, outcomes, cleanup and the project drawer.

- session modes: Per-project keep-alive (tmux) switch, per-project status, and honest session actions.
- session outcome
- sessions cleanup: Codex updates on Windows, the account drop-down in the workspace, and keeping the sessions list under control.
- project drawer: The project page's workspace drawer: every session of a project, not just the newest one.
"""
import io
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QAbstractButton, QWidget
from vibe_launcher import askpass, env_state, runner, runtime, workspace_page
from vibe_launcher.env_state import project_status, session_badge
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow, VibeLauncherWindow as CleanupMainWindow, VibeLauncherWindow as MainWindow
from vibe_launcher.workspace_page import WorkspacePage


# --- session modes ----------------------------------------------------------------------------------------------------
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
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"@3 k\n", b""))
    assert refresh(root, monkeypatch) == {"k": "running"}


def test_a_windows_refresh_never_reports_a_recorded_state_as_live(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", True)
    for name, status in (("r", "running"), ("w", "waiting_auth"), ("d", "detached"), ("e", "ended")):
        runtime.write_json(runtime.session_path(tmp_path, name), {"session_id": name, "environment_id": "host", "status": status})
    assert refresh(tmp_path, monkeypatch) == {"r": "unknown", "w": "unknown", "d": "unknown", "e": "ended"}


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


# --- session outcome --------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------- worker: Ctrl+C is a normal way to leave the CLI


def run_worker_with(tmp_path, monkeypatch, exit_code):
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    monkeypatch.setattr(runtime, "cli_command", lambda *a, **k: ["codex"])

    class Cli:
        pid = 4242

        def poll(self):
            return exit_code

        def wait(self):
            return exit_code
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: Cli())
    request = {"session_id": "s", "environment_id": "host", "path": str(tmp_path), "account": ACCOUNT}
    runtime.run_worker(request, tmp_path, {"PATH": "test"})
    return runtime.json.loads(runtime.read_private(runtime.session_path(tmp_path, "s")))["status"]


@pytest.mark.parametrize("code", [0, 130, 143, 129, -2, -15])
def test_quitting_the_cli_is_recorded_as_ended(tmp_path, monkeypatch, code):
    assert run_worker_with(tmp_path, monkeypatch, code) == "ended"


@pytest.mark.parametrize("code", [1, 2, 127, 137])
def test_a_crashing_cli_is_still_a_failure(tmp_path, monkeypatch, code):
    assert run_worker_with(tmp_path, monkeypatch, code) == "failed"


# ---------------------------------------------------------------- supervisor exit status


def outcome_for(tmp_path, monkeypatch, *, alive=False, record=None, verify=None, attach_code=0):
    emitted = []
    monkeypatch.setattr(runtime, "emit", lambda request, event: emitted.append(event["status"]))
    if verify is not None:
        monkeypatch.setattr(runtime, "tmux_session_exists", verify)
    else:
        monkeypatch.setattr(runtime, "tmux_session_exists", lambda session_id: alive)
    if record is not None:
        runtime.write_json(runtime.session_path(tmp_path, "s"), {"status": record})
    code = runtime.session_outcome({"session_id": "s"}, tmp_path, attach_code)
    return code, emitted


def test_session_still_in_tmux_is_reported_detached(tmp_path, monkeypatch):
    assert outcome_for(tmp_path, monkeypatch, alive=True) == (runtime.EXIT_SESSION_DETACHED, ["detached"])


def test_finished_session_is_reported_by_the_workers_own_record(tmp_path, monkeypatch):
    assert outcome_for(tmp_path, monkeypatch, record="ended") == (runtime.EXIT_SESSION_ENDED, ["ended"])
    assert outcome_for(tmp_path, monkeypatch, record="failed") == (runtime.EXIT_SESSION_FAILED, ["failed"])


def test_uncertain_cases_stay_unknown_and_keep_the_attach_status(tmp_path, monkeypatch):
    # dead session whose record still says "running" (e.g. the host rebooted)
    assert outcome_for(tmp_path, monkeypatch, record="running", attach_code=0) == (0, ["unknown"])
    # no record at all
    assert outcome_for(tmp_path / "nothing", monkeypatch, attach_code=1) == (1, ["unknown"])

    def cannot_verify(session_id):
        raise RuntimeError("Cannot verify tmux session state")
    assert outcome_for(tmp_path, monkeypatch, verify=cannot_verify, attach_code=0) == (0, ["unknown"])


# ---------------------------------------------------------------- runner trusts the exit status, not the terminal output


def setup(tmp_path, status="connecting"):
    store = Store(tmp_path / "state.db")
    account = store.save("accounts", ACCOUNT)
    env = store.save("environments", {"id": "host", "name": "H", "kind": "ssh", "ssh_target": "user@server"})
    store.save("sessions", {"id": "s", "name": "S", "account_id": "a", "environment_id": "host", "path": "/tmp", "status": status})
    return store, env, {"action": "launch", "nonce": "n", "session_id": "s", "environment_id": "host", "account": account}


def fake_ssh(monkeypatch, exit_code, output=b""):
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])

    class Remote:
        stdout = io.BytesIO(output)

        def wait(self):
            return exit_code
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Remote())


@pytest.mark.parametrize("code, expected", [(runtime.EXIT_SESSION_ENDED, "ended"), (runtime.EXIT_SESSION_DETACHED, "detached"),
                                            (runtime.EXIT_SESSION_FAILED, "failed")])
@pytest.mark.parametrize("action", ["launch", "attach"])
def test_session_outcome_exit_status_sets_the_local_state(tmp_path, monkeypatch, code, expected, action):
    store, env, request = setup(tmp_path, status="connecting" if action == "launch" else "detached")
    request["action"] = action
    fake_ssh(monkeypatch, code)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == expected


def test_a_plain_exit_still_means_unknown_so_old_behaviour_is_conservative(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    fake_ssh(monkeypatch, 0)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "unknown"


def test_cli_output_cannot_forge_the_outcome(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    fake_ssh(monkeypatch, 0, output=b'VIBE_EVENT_n:{"status": "ended"}\n')
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "unknown"


def test_outcome_codes_are_ignored_for_other_actions(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path, status="running")
    request["action"] = "list-sessions"
    fake_ssh(monkeypatch, runtime.EXIT_SESSION_ENDED)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "unknown"


@pytest.mark.parametrize("action, piped", [("launch", False), ("attach", False), ("list-sessions", True), ("login", True)])
def test_a_terminal_session_gives_ssh_the_terminal_itself(tmp_path, monkeypatch, action, piped):
    # Windows ssh reads the window size from its stdout console; through a pipe the server's pty stays 80x24.
    store, env, request = setup(tmp_path)
    request["action"] = action
    seen = []
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])

    class Remote:
        stdout = io.BytesIO(b"")

        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("stdout"))

        def wait(self):
            return 0
    monkeypatch.setattr(runner.subprocess, "Popen", Remote)
    runner.run_remote(store, request, env)
    assert seen == [subprocess.PIPE if piped else None]


@pytest.mark.parametrize("code, promoted", [(runtime.EXIT_SESSION_DETACHED, True), (runtime.EXIT_NOT_STARTED, True),
                                            (runner.SSH_FAILED, False)])
def test_a_terminal_session_keeps_a_new_password_only_after_the_server_answered(tmp_path, monkeypatch, code, promoted):
    store, env, request = setup(tmp_path)
    calls = []
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: {"VIBE_SSH_ROOT": str(tmp_path), "VIBE_SSH_ATTEMPT": "x"})
    monkeypatch.setattr(askpass, "password_operation", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(askpass, "cleanup", lambda *a: None)
    monkeypatch.setattr(askpass, "was_cancelled", lambda *a: False)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])

    class Remote:
        stdout = None

        def wait(self):
            return code
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Remote())
    runner.run_remote(store, request, env)
    assert calls == ([{"promote": True}] if promoted else [])


# --- sessions cleanup -------------------------------------------------------------------------------------------------
# ------------------------------------------------------------------------------ updating the CLI
def test_the_codex_updater_sees_the_real_codex_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    env = runtime.maintenance_env("codex", tmp_path)
    assert "CODEX_HOME" not in env                                   # defaults to ~/.codex, where the standalone install lives
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
    assert runtime.maintenance_env("codex", tmp_path)["CODEX_HOME"] == str(tmp_path / "custom")


def test_the_updater_never_sees_an_accounts_profile_or_credentials(tmp_path, monkeypatch):
    # Test the default installation, independent of the CLI that runs pytest.
    monkeypatch.delenv("CODEX_HOME", raising=False)
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


def cleanup_add_session(store, sid, status, env="gpu", account="a", updated=None, **extra):
    return store.save("sessions", {"id": sid, "preset_id": "p" if env == "gpu" else "", "account_id": account, "environment_id": env,
                                   "name": "P", "path": "/w", "status": status,
                                   "updated_at": updated or datetime.now(timezone.utc).isoformat(), **extra})


def test_one_job_stops_many_sessions_of_one_environment(tmp_path):
    store = make_store(tmp_path)
    for sid, account in (("s1", "a"), ("s2", "b")):
        cleanup_add_session(store, sid, "unknown", account=account)
    jobs = []
    LauncherService(store).start("stop", session_ids=["s1", "s2"], spawn=lambda args, ctx: jobs.append(ctx))
    assert len(jobs) == 1 and jobs[0]["environment_id"] == "gpu"
    import json
    request = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))["request"]
    assert request["session_ids"] == ["s1", "s2"] and "account" not in request     # no single account: each keeps its own


def test_sessions_from_two_environments_cannot_share_one_stop_job(tmp_path):
    store = make_store(tmp_path)
    cleanup_add_session(store, "s1", "unknown")
    cleanup_add_session(store, "s2", "unknown", env="box")
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
        cleanup_add_session(store, sid, "unknown")
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
    window = CleanupMainWindow(store, FakeService())
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
        cleanup_add_session(view.store, sid, status)
    view.reload()
    text = view.session_summary.text()
    assert "共 5 筆" in text and "進行中 1" in text and "狀態不明 2" in text and "已結束／失敗 2" in text
    assert "（2）" in view.session_check_button.text() and "（2）" in view.session_clean_button.text()


def test_clearing_finished_sessions_keeps_the_rest(view, monkeypatch):
    for sid, status in (("a1", "running"), ("a4", "ended"), ("a5", "failed")):
        cleanup_add_session(view.store, sid, status)
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.clean_finished_sessions()
    assert [s["id"] for s in view.store.list("sessions")] == ["a1"]


def test_checking_unknown_sessions_ends_local_ones_and_requeries_each_remote_host_once(view, monkeypatch):
    cleanup_add_session(view.store, "r1", "unknown")
    cleanup_add_session(view.store, "r2", "unknown")
    cleanup_add_session(view.store, "l1", "unknown", env="local")
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.check_unknown_sessions()
    assert view.store.get("sessions", "l1")["status"] == "ended"
    assert [c for c in view.service.calls if c[0] == "refresh"] == [("refresh", {"environment_id": "gpu"})]


def test_selecting_several_sessions_stops_them_in_one_job_per_environment(view, monkeypatch):
    for sid in ("s1", "s2"):
        cleanup_add_session(view.store, sid, "unknown")
    cleanup_add_session(view.store, "s3", "unknown", env="box")
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
        cleanup_add_session(view.store, sid, status)
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    select(view, "e1", "e2", "k1")
    view.remove_session()                                         # the running one is not removable and is left alone
    assert [s["id"] for s in view.store.list("sessions")] == ["k1"]


def test_a_local_session_whose_terminal_is_gone_is_ended_not_unknown(view, monkeypatch):
    cleanup_add_session(view.store, "l1", "running", env="local", runner_pid=999999)
    cleanup_add_session(view.store, "l2", "unknown", env="local")
    cleanup_add_session(view.store, "r1", "connecting", runner_pid=999999)      # remote: it might really be running there
    view.reload()
    monkeypatch.setattr(runner, "process_alive", lambda pid: False)
    view._reap_closed_terminals()
    assert {s["id"]: s["status"] for s in view.store.list("sessions")} == {"l1": "ended", "l2": "ended", "r1": "unknown"}


@pytest.mark.skipif(os.name != "nt", reason="process creation times are read on Windows")
def test_a_reused_pid_does_not_keep_a_local_session_alive(view):
    root = view.store.path.parent
    long_ago = "2000-01-01T00:00:00+00:00"
    cleanup_add_session(view.store, "l1", "running", env="local", updated=long_ago, runner_pid=os.getpid())   # pid now belongs to us
    cleanup_add_session(view.store, "l2", "running", env="local", runner_pid=os.getpid())                      # really ours
    runtime.write_json(runtime.session_path(root, "l1"), {"session_id": "l1", "environment_id": "local", "status": "running"})
    view.reload()
    view._reap_closed_terminals()
    assert view.store.get("sessions", "l1")["status"] == "ended" and view.store.get("sessions", "l2")["status"] == "running"
    assert runtime.read_private(runtime.session_path(root, "l1")).count('"ended"') == 1
    assert runner.runner_alive(os.getpid(), datetime.now(timezone.utc).isoformat(), local=True)
    assert not runner.runner_alive(os.getpid(), long_ago, local=True)


@pytest.mark.skipif(os.name != "nt", reason="process creation times are read on Windows")
def test_a_runner_that_cannot_be_inspected_counts_only_for_remote_work(view, monkeypatch):
    cleanup_add_session(view.store, "l1", "running", env="local", runner_pid=4242)
    cleanup_add_session(view.store, "r1", "connecting", runner_pid=4242)
    monkeypatch.setattr(runner, "process_alive", lambda pid: True)
    monkeypatch.setattr(runner, "process_started", lambda pid: None)
    view.reload()
    view._reap_closed_terminals()
    assert view.store.get("sessions", "l1")["status"] == "ended" and view.store.get("sessions", "r1")["status"] == "connecting"


def test_queued_sessions_get_a_grace_period_then_are_reaped(view, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    cleanup_add_session(view.store, "fresh", "queued", env="local")
    cleanup_add_session(view.store, "stuck-local", "queued", env="local", updated=old)
    cleanup_add_session(view.store, "stuck-remote", "queued", updated=old)
    monkeypatch.setattr(runner, "process_alive", lambda pid: False)
    view.reload()
    view._reap_closed_terminals()
    status = {s["id"]: s["status"] for s in view.store.list("sessions")}
    assert status == {"fresh": "queued", "stuck-local": "ended", "stuck-remote": "unknown"}


def test_startup_ends_stale_local_records_and_redraws_every_page(view, monkeypatch):
    root = view.store.path.parent
    cleanup_add_session(view.store, "l1", "running", env="local", runner_pid=999999)
    view.reload()
    assert view.home_page.running.value.text() == "1"
    runtime.write_json(runtime.session_path(root, "gone"), {"session_id": "gone", "environment_id": "local", "status": "running"})
    runtime.write_json(runtime.session_path(root, "far"), {"session_id": "far", "environment_id": "gpu", "status": "running"})
    monkeypatch.setattr(runner, "process_alive", lambda pid: False)
    monkeypatch.setattr(view, "_toast", lambda *a, **k: None)
    view._startup_housekeeping()
    assert not [s for s in view.store.list("sessions") if s["id"] == "l1"], "a finished local run leaves no record"
    assert view.home_page.running.value.text() == "0", "Home no longer shows the old 'running'"
    assert '"ended"' in runtime.read_private(runtime.session_path(root, "gone"))
    assert '"running"' in runtime.read_private(runtime.session_path(root, "far")), "remote work is never assumed over"


def test_at_startup_old_history_is_trimmed_and_leftovers_are_announced(view, monkeypatch):
    base = datetime.now(timezone.utc)
    for index in range(view.FINISHED_KEPT + 5):
        cleanup_add_session(view.store, f"old{index:02d}", "ended", updated=(base - timedelta(hours=index + 1)).isoformat())
    cleanup_add_session(view.store, "stuck", "unknown")
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
    assert labels.count("Codex") == 1 and labels.count("Claude") == 1 and labels.count("終端機") == 1
    arrows = [b for b in buttons(page) if b.accessibleName().startswith("選擇 ")]
    from vibe_launcher.i18n import tr
    assert {b.accessibleName() for b in arrows} == {"選擇 Codex 帳號", "選擇 Claude 帳號", tr("Choose shell or CLI")} and all(b.isEnabled() for b in arrows)


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


# --- project drawer ---------------------------------------------------------------------------------------------------
def stamp(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def drawer_sess(sid, status, preset="p", minutes_ago=0, **extra):
    return {"id": sid, "preset_id": preset, "status": status, "updated_at": stamp(minutes_ago), **extra}


# ------------------------------------------------------------------------------ view-model
def test_a_projects_sessions_list_live_ones_first_then_finished_newest_first():
    sessions = [drawer_sess("old", "ended", minutes_ago=50), drawer_sess("run", "running", minutes_ago=30), drawer_sess("new", "failed", minutes_ago=1),
                drawer_sess("unk", "unknown", minutes_ago=5), drawer_sess("other", "running", preset="q")]
    assert [s["id"] for s in env_state.project_sessions("p", sessions)] == ["unk", "run", "new", "old"]


def test_the_row_badge_counts_live_workspaces_when_there_are_several():
    one = [drawer_sess("a", "running")]
    assert env_state.project_badge("p", one)[0] == "運行中"
    two = [drawer_sess("a", "running"), drawer_sess("b", "detached", minutes_ago=3), drawer_sess("c", "ended", minutes_ago=9)]
    assert env_state.project_badge("p", two)[0] == "運行中 ×2"
    assert env_state.project_badge("p", [drawer_sess("c", "ended")])[0] == "已停止"
    assert env_state.project_badge("nobody", two) is None


# ------------------------------------------------------------------------------ the page
class DrawerFakeService:
    supports_spawn = False

    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        kwargs.pop("workspace_id", None)      # which tmux session a launch joins is tested in test_workspace
        self.calls.append((action, kwargs))


@pytest.fixture
def drawer_view(tmp_path, qtbot):
    store = Store(tmp_path / "d.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("accounts", {"id": "a", "name": "Alpha", "tool": "claude", "auth": "api", "model": "m"})
    store.save("accounts", {"id": "b", "name": "Beta", "tool": "codex", "auth": "api", "model": "m"})
    store.save("presets", {"id": "p", "name": "Site", "account_id": "", "environment_id": "gpu", "path": "/w"})
    store.save("presets", {"id": "q", "name": "Other", "account_id": "", "environment_id": "gpu", "path": "/o"})
    window = MainWindow(store, DrawerFakeService())
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.resize(1200, 800)
    window.show()
    return window


def add(drawer_view, sid, status, account="a", preset="p", minutes_ago=0, **extra):
    drawer_view.store.save("sessions", {"id": sid, "preset_id": preset, "account_id": account, "environment_id": "gpu", "name": "Site",
                                 "path": "/w", "status": status, "updated_at": stamp(minutes_ago), **extra})


def select_project(drawer_view, preset):
    for index in range(drawer_view.project_list.count()):
        if drawer_view.project_list.item(index).data(0x0100) == preset:
            drawer_view.project_list.setCurrentRow(index)
    drawer_view.reload()


def drawer_buttons(drawer_view, session_id, action):
    return [b for b in drawer_view.work_drawer.findChildren(QAbstractButton)
            if b.property("workAction") == action and b.parent().property("workRow") == session_id and not b.parent().property("disposed")]


def test_the_drawer_lists_every_workspace_of_the_selected_project_only(drawer_view):
    add(drawer_view, "s1", "running")
    add(drawer_view, "s2", "detached", account="b", minutes_ago=4)
    add(drawer_view, "s3", "ended", minutes_ago=30)
    add(drawer_view, "x1", "running", preset="q")
    select_project(drawer_view, "p")
    rows = drawer_view.work_drawer.rows()
    assert [r.session_id for r in rows] == ["s1", "s2", "s3"]
    assert [r.title for r in rows][:2] == ["Claude · Alpha", "Codex · Beta"]
    assert "3 個" in drawer_view.work_drawer.summary.text() and "進行中 2" in drawer_view.work_drawer.summary.text()


def test_the_project_row_shows_how_many_are_live(drawer_view):
    add(drawer_view, "s1", "running")
    add(drawer_view, "s2", "detached", minutes_ago=2)
    drawer_view.reload()
    badge = next(drawer_view.project_list.item(i).data(0x0101) for i in range(drawer_view.project_list.count()) if drawer_view.project_list.item(i).data(0x0100) == "p")
    assert badge[0] == "背景執行 ×2", "no terminal is open for them here: they run in the background on the server"


def test_a_project_with_no_workspaces_says_so_and_switching_projects_switches_the_drawer(drawer_view):
    add(drawer_view, "x1", "running", preset="q")
    select_project(drawer_view, "p")
    assert drawer_view.work_drawer.rows() == [] and "尚未" in drawer_view.work_drawer.summary.text()
    select_project(drawer_view, "q")
    assert [r.session_id for r in drawer_view.work_drawer.rows()] == ["x1"]


def test_the_drawer_collapses_and_expands(drawer_view):
    add(drawer_view, "s1", "running")
    select_project(drawer_view, "p")
    assert drawer_view.work_drawer.body.isVisibleTo(drawer_view.work_drawer)
    drawer_view.work_drawer.toggle.click()
    assert not drawer_view.work_drawer.is_open() and not drawer_view.work_drawer.body.isVisibleTo(drawer_view.work_drawer)
    drawer_view.reload()                                             # a refresh must not pop it open again
    assert not drawer_view.work_drawer.is_open()
    drawer_view.work_drawer.toggle.click()
    assert drawer_view.work_drawer.is_open()


def test_each_row_acts_on_its_own_session(drawer_view, monkeypatch):
    add(drawer_view, "s1", "detached")
    add(drawer_view, "s2", "detached", minutes_ago=3)
    add(drawer_view, "s3", "ended", minutes_ago=9)
    select_project(drawer_view, "p")
    monkeypatch.setattr(drawer_view, "_confirm", lambda *a, **k: True)
    drawer_buttons(drawer_view, "s2", "attach")[0].click()
    drawer_buttons(drawer_view, "s1", "stop")[0].click()
    assert ("attach", {"session_id": "s2"}) in drawer_view.service.calls
    assert ("stop", {"session_id": "s1"}) in drawer_view.service.calls
    assert not drawer_buttons(drawer_view, "s3", "stop") and drawer_buttons(drawer_view, "s3", "remove")
    drawer_buttons(drawer_view, "s3", "remove")[0].click()
    assert drawer_view.store.get("sessions", "s2") and not any(s["id"] == "s3" for s in drawer_view.store.list("sessions"))


def test_a_workspace_that_is_open_offers_to_jump_to_it(drawer_view):
    add(drawer_view, "s1", "running", account="a")
    select_project(drawer_view, "p")
    assert drawer_buttons(drawer_view, "s1", "attach") and not drawer_buttons(drawer_view, "s1", "switch")

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

    original_tabs = drawer_view.workspace.tabs
    drawer_view.workspace.tabs = lambda: [Tab()]
    drawer_view.workspace.select = lambda tab_id: setattr(drawer_view, "_jumped", tab_id)
    drawer_view._refresh_project_drawer()
    assert drawer_view.work_drawer.rows()[0].tab_id == "tab-1" and drawer_buttons(drawer_view, "s1", "switch") and not drawer_buttons(drawer_view, "s1", "attach")
    drawer_buttons(drawer_view, "s1", "switch")[0].click()
    assert drawer_view._jumped == "tab-1"
    drawer_view.workspace.tabs = original_tabs


def test_the_new_work_button_opens_the_project_again(drawer_view, monkeypatch):
    opened = []
    monkeypatch.setattr(drawer_view, "open_terminal", lambda preset_id, *a, **k: opened.append(preset_id))
    select_project(drawer_view, "p")
    drawer_view.work_drawer.add.click()
    assert opened == ["p"]


# ------------------------------------------------------------------------------ bringing workspaces back
ENVS = [{"id": "gpu", "kind": "ssh"}, {"id": "local", "kind": "local"}]
DRAWER_ACCOUNTS = [{"id": "a", "tool": "claude", "auth": "api"}, {"id": "b", "tool": "codex", "auth": "api"}]


def plan(sessions, open_ids=frozenset(), accounts=DRAWER_ACCOUNTS):
    return [(s.action, s.session["id"]) for s in env_state.restore_plan("p", sessions, ENVS, accounts, open_ids)]


def ses(sid, status, account="a", env="gpu", minutes_ago=0, **extra):
    return drawer_sess(sid, status, minutes_ago=minutes_ago, account_id=account, environment_id=env, **extra)


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


def test_the_attach_button_restarts_stopped_workspaces_and_replaces_their_records(drawer_view, monkeypatch):
    add(drawer_view, "live", "detached")
    add(drawer_view, "dead", "ended", account="b", minutes_ago=7)
    add(drawer_view, "older", "ended", account="b", minutes_ago=70)
    drawer_view.reload()
    select_project(drawer_view, "p")
    asked = []
    monkeypatch.setattr(drawer_view, "_confirm", lambda title, message, **k: asked.append(message) or True)
    drawer_view.attach_project()
    calls = drawer_view.service.calls
    assert ("attach", {"session_id": "live"}) in calls
    launch = next(c for c in calls if c[0] == "launch")[1]
    assert launch == {"preset_id": "p", "environment_id": "gpu", "account_id": "b", "resume": True}      # same service, account and host
    assert "重新連線 Claude（Alpha）" in asked[0] and "重新開啟 Codex（Beta）" in asked[0]
    ids = {s["id"] for s in drawer_view.store.list("sessions")}
    assert "dead" not in ids and "live" in ids and "older" in ids                          # the restarted record was replaced


def test_nothing_to_bring_back_says_why(drawer_view, monkeypatch):
    notes = []
    monkeypatch.setattr(drawer_view, "_notice", notes.append)
    select_project(drawer_view, "p")
    drawer_view.attach_project()
    assert "還沒有工作階段" in notes[-1]
    add(drawer_view, "up", "connecting")
    drawer_view.reload()
    drawer_view.attach_project()
    assert "都已經開著" in notes[-1]
    assert drawer_view.service.calls == []


def test_a_stopped_row_offers_restart_and_the_drawer_recalls_everything(drawer_view, monkeypatch):
    add(drawer_view, "live", "detached")
    add(drawer_view, "dead", "failed", account="b", minutes_ago=7)
    select_project(drawer_view, "p")
    assert drawer_buttons(drawer_view, "dead", "restart") and not drawer_buttons(drawer_view, "live", "restart")
    assert drawer_view.work_drawer.restore.text() == "還原全部（2）" and not drawer_view.work_drawer.restore.isHidden()
    monkeypatch.setattr(drawer_view, "_confirm", lambda *a, **k: True)
    drawer_buttons(drawer_view, "dead", "restart")[0].click()
    assert [c[0] for c in drawer_view.service.calls] == ["launch"]
    drawer_view.service.calls.clear()
    add(drawer_view, "dead2", "ended", account="b", minutes_ago=9)
    drawer_view.reload()
    drawer_view.work_drawer.restore.click()
    assert sorted(c[0] for c in drawer_view.service.calls) == ["attach", "launch"]


def test_a_workspace_of_a_deleted_project_or_account_cannot_be_restarted(drawer_view, monkeypatch):
    notes = []
    monkeypatch.setattr(drawer_view, "_notice", notes.append)
    assert drawer_view._relaunch({"id": "z", "preset_id": "gone", "status": "ended"}) is False
    assert drawer_view._relaunch({"id": "z", "preset_id": "p", "account_id": "gone", "environment_id": "gpu", "status": "ended"}) is False
    assert len(notes) == 2 and drawer_view.service.calls == []


def test_the_workspace_page_offers_the_recall_for_a_project(drawer_view, monkeypatch):
    add(drawer_view, "dead", "ended", account="b")
    drawer_view.reload()
    assert drawer_view.workspace.launcher.recoverable == {"p": 1}
    row_buttons = [b.text() for b in drawer_view.workspace.launcher.findChildren(QAbstractButton) if b.isVisibleTo(drawer_view.workspace.launcher)]
    assert any("還原 1" in t for t in row_buttons)
    drawer_view.workspace.launcher.restore.emit("p")                          # the card's button reaches the window's recall
    assert [c[0] for c in drawer_view.service.calls] == ["launch"]
    drawer_view.reload()
    assert drawer_view.workspace.launcher.recoverable == {}                  # the stopped record was replaced by the new one's launch


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


def test_the_workspace_has_no_open_on_another_host_button(drawer_view):
    add(drawer_view, "dead", "ended", account="b")
    drawer_view.reload()
    assert not hasattr(drawer_view.workspace.launcher, "more")
    assert not hasattr(drawer_view.workspace, "_launcher_menu")


def test_a_workspace_from_before_the_binding_is_not_restarted_elsewhere(drawer_view, monkeypatch):
    drawer_view.store.save("environments", {"id": "box", "name": "Box", "kind": "ssh", "ssh_target": "box"})
    add(drawer_view, "stray", "ended", account="b")
    drawer_view.store.save("sessions", {**drawer_view.store.get("sessions", "stray"), "environment_id": "box"})
    drawer_view.reload()
    select_project(drawer_view, "p")
    assert drawer_view._restore_steps("p") == []
    assert not drawer_buttons(drawer_view, "stray", "restart")
    notes = []
    monkeypatch.setattr(drawer_view, "_notice", notes.append)
    assert drawer_view._relaunch(drawer_view.store.get("sessions", "stray")) is False and notes
