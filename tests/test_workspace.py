"""Workspace: terminals as tabs / grid, grouped by project, leave-without-ending, saved workspaces, shell sessions."""
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QWidget

from vibe_launcher import runtime, terminal
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow
from vibe_launcher.workspace_page import TerminalArea, WorkspacePage

ACCOUNT = {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"}


def iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class FakeTerminal(QWidget):
    """Same surface as TerminalWidget, no browser and no process."""

    exited = Signal(int)
    output_drained = Signal()
    title_changed = Signal(str)
    chord = Signal(str)
    activity = Signal()
    ready = Signal()

    def __init__(self):
        super().__init__()
        self.command = self.cwd = self.env = None
        self.sent: list[bytes] = []
        self.detaches = 0
        self.terminated = False
        self.disposed = False
        self.running = True
        self.focus_calls = 0
        self.is_output_drained = True

    def start(self, command, *, cwd=None, env=None):
        self.command, self.cwd, self.env = command, cwd, env

    def is_running(self):
        return self.running

    def detach_tmux(self):
        self.detaches += 1
        self.sent.append(b"\x02d")

    def terminate(self):
        self.terminated = True

    def dispose(self):
        self.disposed = True

    def focus_terminal(self):
        self.focus_calls += 1

    def finish(self, code=0):
        self.running = False
        self.exited.emit(code)

    def drain(self):
        self.is_output_drained = True
        self.output_drained.emit()


# ----------------------------------------------------------------------------- store: workspaces


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save("accounts", ACCOUNT)
    store.save("presets", {"id": "p1", "name": "前端", "account_id": "a", "environment_id": "local", "path": "C:/p1"})
    return store


def test_workspace_records_are_validated_and_cleaned(store):
    saved = store.save("workspaces", {"name": "日常", "slots": [{"preset_id": "p1", "kind": "agent", "account_id": "a"},
                                                              {"preset_id": "p1", "kind": "shell", "environment_id": "local"}]})
    assert saved["layout"] == "tabs"
    assert saved["slots"][0] == {"preset_id": "p1", "kind": "agent", "account_id": "a"}
    assert saved["slots"][1] == {"preset_id": "p1", "kind": "shell", "environment_id": "local"}
    assert store.get("workspaces", saved["id"])["name"] == "日常"
    for bad in ({"name": ""}, {"name": "x", "layout": "floating"}, {"name": "x", "slots": "no"},
                {"name": "x", "slots": [{"preset_id": "p1", "kind": "root"}]},
                {"name": "x", "slots": [{"preset_id": ""}]},
                {"name": "x", "slots": [{"preset_id": "p1", "command": "rm -rf"}]},
                {"name": "x", "slots": [{"preset_id": "p1"}] * 17},
                {"name": "x", "slots": [{"preset_id": "../../etc"}]}):
        with pytest.raises(ValueError):
            store.save("workspaces", bad)


def test_workspaces_survive_a_project_being_deleted_and_travel_with_exported_config(store, tmp_path):
    store.save("workspaces", {"id": "w1", "name": "日常", "slots": [{"preset_id": "p1", "kind": "shell"}]})
    store.delete("presets", "p1")                     # no foreign key: the slot just points at nothing
    assert store.get("workspaces", "w1")["slots"][0]["preset_id"] == "p1"
    store.save("presets", {"id": "p1", "name": "前端", "account_id": "a", "environment_id": "local", "path": "C:/p1"})
    path = tmp_path / "config.json"
    store.export_config(path)
    other = Store(tmp_path / "other.db")
    other.import_config(path)
    assert [w["name"] for w in other.list("workspaces")] == ["日常"]


def test_sessions_know_their_mode(store):
    base = {"id": "s", "preset_id": "p1", "account_id": "a", "environment_id": "local", "name": "n", "path": "p"}
    assert store.save("sessions", {**base, "mode": "shell"})["mode"] == "shell"
    with pytest.raises(ValueError):
        store.save("sessions", {**base, "mode": "root"})


# ----------------------------------------------------------------------------- service: shell mode, overrides, spawn


def test_shell_launch_needs_no_account_in_the_request(store, tmp_path, monkeypatch):
    spawned = []
    sid = LauncherService(store).start("launch", preset_id="p1", mode="shell", spawn=lambda args, ctx: spawned.append((args, ctx)))
    job = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    assert job["request"]["mode"] == "shell" and "account" not in job["request"]
    session = store.get("sessions", sid)
    assert session["mode"] == "shell" and session["account_id"] == "a", "kept only to satisfy the record's reference"
    (args, ctx), = spawned
    assert args[1:4] == ["-I", "-m", "vibe_launcher.runner"] and ctx["session_id"] == sid and ctx["mode"] == "shell"


def test_an_explicit_account_wins_over_the_saved_one_but_the_environment_stays_the_projects(store, tmp_path):
    store.save("accounts", {"id": "c", "name": "Claude", "tool": "claude", "auth": "subscription"})
    store.save("environments", {"id": "srv", "name": "S", "kind": "ssh", "ssh_target": "srv"})
    project_env = store.get("presets", "p1")["environment_id"]
    LauncherService(store).start("launch", preset_id="p1", account_id="c", environment_id=project_env, spawn=lambda *a: None)
    job = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    assert job["request"]["account"]["tool"] == "claude" and job["request"]["environment_id"] == project_env
    session = store.list("sessions")[0]
    assert (session["account_id"], session["environment_id"]) == ("c", project_env)
    other = "srv" if project_env != "srv" else "local"
    with pytest.raises(ValueError, match="已綁定環境"):
        LauncherService(store).start("launch", preset_id="p1", account_id="c", environment_id=other, spawn=lambda *a: None)


def test_default_launch_is_unchanged_and_shell_is_launch_only(store, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    LauncherService(store).start("launch", preset_id="p1")
    assert store.list("sessions")[0]["mode"] == "agent"
    with pytest.raises(ValueError):
        LauncherService(store).start("doctor", preset_id="p1", mode="shell")
    with pytest.raises(ValueError):
        LauncherService(store).start("launch", preset_id="p1", mode="root")


def test_a_failed_spawn_cleans_up(store, tmp_path):
    def boom(args, ctx):
        raise OSError("no terminal")
    with pytest.raises(OSError):
        LauncherService(store).start("launch", preset_id="p1", spawn=boom)
    assert store.list("sessions")[0]["status"] == "failed"
    assert not list((tmp_path / "jobs").glob("*.json")), "the job file must not be left behind"


# ----------------------------------------------------------------------------- runtime: shell mode


def test_validate_request_knows_the_modes():
    runtime.validate_request({"action": "launch", "mode": "shell"})
    with pytest.raises(ValueError):
        runtime.validate_request({"action": "launch", "mode": "root"})


def test_shell_environment_drops_launcher_plumbing():
    env = runtime.shell_env({"PATH": "x", "HOME": "h", "VIBE_SSH_ID": "e", "VIBE_EMBEDDED": "1", "SSH_ASKPASS": "a", "SSH_ASKPASS_REQUIRE": "force",
                             "SSH_AUTH_SOCK": "keep", "EDITOR": "vim"})
    assert env == {"PATH": "x", "HOME": "h", "SSH_AUTH_SOCK": "keep", "EDITOR": "vim"}


def test_shell_command_is_the_users_login_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    shell = tmp_path / "zsh"
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setattr(runtime.os, "access", lambda *a: True)
    assert runtime.shell_command() == [str(shell), "-l"]
    monkeypatch.setenv("SHELL", "relative/zsh")
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/usr/bin/" + name)
    assert runtime.shell_command() == ["/usr/bin/bash", "-l"], "an unusable $SHELL falls back to bash"


def test_shell_session_needs_no_agent_cli(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "emit", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/usr/bin/tmux" if name == "tmux" else (_ for _ in ()).throw(RuntimeError("missing")))
    runtime.doctor({"action": "launch", "mode": "shell", "persistent": True}, None)     # only tmux is looked for
    assert "codex" not in capsys.readouterr().out


def test_a_shell_launch_skips_authentication_and_runs_the_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "doctor", lambda *a: None)
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    monkeypatch.setattr(runtime, "authenticate", lambda *a, **k: pytest.fail("a shell has no account to authenticate"))
    monkeypatch.setattr(runtime, "check_settings", lambda *a, **k: pytest.fail("no account settings for a shell"))
    monkeypatch.setattr(runtime, "cli_command", lambda *a, **k: pytest.fail("a shell must not start the agent CLI"))
    monkeypatch.setattr(runtime, "shell_command", lambda: ["/bin/bash", "-l"])
    started = []

    class Shell:
        pid = 55

        def poll(self):
            return 0

        def wait(self):
            return 0
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)) or Shell())
    request = {"action": "launch", "mode": "shell", "persistent": False, "session_id": "s", "environment_id": "host",
               "path": str(tmp_path)}
    assert runtime.perform(request, tmp_path / "state") == runtime.EXIT_SESSION_ENDED
    (cmd, kw), = started
    assert cmd == ["/bin/bash", "-l"] and kw["cwd"] == str(tmp_path.resolve())
    assert "VIBE_SSH_ID" not in kw["env"]


class FakeTmux:
    """A tmux that knows windows, their @vibe-session marks and sessions, enough for the launch/attach/stop paths."""

    def __init__(self, version=b"tmux 3.6"):
        self.calls, self.windows, self.next, self.version = [], {}, 1, version

    def __call__(self, *args, capture=False):
        self.calls.append(args)
        if args[0] == "-V":
            return subprocess.CompletedProcess(args, 0, self.version + b"\n", b"")
        if args[0] in {"new-window", "new-session"}:
            window, self.next = f"@{self.next}", self.next + 1
            self.windows[window] = ""
            return subprocess.CompletedProcess(args, 0, (window + "\n").encode(), b"")
        if args[0] == "set-option" and "-w" in args and "@vibe-session" in args:
            self.windows[args[args.index("-t") + 1]] = args[-1]
        if args[0] == "kill-window":
            self.windows.pop(args[-1], None)
        if args[0] == "list-windows":
            lines = "".join(f"{w} {s}\n" for w, s in self.windows.items())
            return subprocess.CompletedProcess(args, 0, lines.encode(), b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")


def launch_with_fake_tmux(tmp_path, monkeypatch, version=b"tmux 3.6", **extra):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "doctor", lambda *a: None)
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    monkeypatch.setattr(runtime, "authenticate", lambda *a, **k: {})
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/usr/bin/" + name)
    fake = FakeTmux(version)
    monkeypatch.setattr(runtime, "tmux", fake)
    request = {"action": "launch", "account": ACCOUNT, "session_id": "s", "environment_id": "host", "path": str(tmp_path),
               "nonce": "abcdef0123", **extra}
    runtime.perform(request, tmp_path / "state")
    return fake


def test_terminals_of_one_workspace_are_windows_of_its_tmux_session(tmp_path, monkeypatch):
    fake = launch_with_fake_tmux(tmp_path, monkeypatch, workspace_id="ws1")
    created = fake.calls[0]
    assert created[0] == "new-window" and created[created.index("-t") + 1] == "=vibe-ws-ws1:"
    assert created[created.index("-n") + 1] == tmp_path.name + " · codex"
    # the workspace session did not exist yet (the fake still answers), then the window is marked as this terminal's
    assert ("set-option", "-w", "-t", "@1", "@vibe-session", "s") in fake.calls
    assert not any(c[0] == "new-session" and "-s" in c and c[c.index("-s") + 1] == "vibe-s" for c in fake.calls)


def test_a_workspace_session_is_created_when_its_first_terminal_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    fake = FakeTmux()
    original = fake.__call__

    def missing_group(*args, capture=False):
        if args[0] == "new-window" and not fake.windows:
            fake.calls.append(args)
            return subprocess.CompletedProcess(args, 1, b"", b"can't find session: vibe-ws-default")
        return original(*args, capture=capture)
    monkeypatch.setattr(runtime, "tmux", missing_group)
    assert runtime.tmux_new_window("vibe-ws-default", "p · codex", "/w", "worker") == "@1"
    assert [c[0] for c in fake.calls] == ["new-window", "new-session"]
    assert fake.calls[1][fake.calls[1].index("-s") + 1] == "vibe-ws-default"


def test_tmux_sessions_tell_the_app_when_attached_and_accept_ctrl_b(tmp_path, monkeypatch):
    fake = launch_with_fake_tmux(tmp_path, monkeypatch)
    view = "vibe-s-abcdef01"
    assert ("link-window", "-a", "-s", "@1", "-t", "=" + view + ":") in fake.calls
    assert ("set-option", "-s", "focus-events", "on") in fake.calls       # the launcher's own tmux server only
    options = [c for c in fake.calls if c[0] == "set-option" and "-w" not in c and "-s" not in c]
    assert any("prefix2" in c and "C-b" in c for c in options)
    assert any("set-titles-string" in c and "vibe:#S" in c for c in options)
    # set-option takes a pane target: tmux answers "no such session" for "=name" and only matches "=name:" exactly.
    assert options and all(c[c.index("-t") + 1] == "=" + view + ":" for c in options)
    # the view disappears when the tab leaves; the option is set only once attached (earlier, tmux destroys it at once)
    attach = next(c for c in fake.calls if c[0] == "attach-session")
    assert attach == ("attach-session", "-t", "=" + view, ";", "set-option", "-t", "=" + view + ":", "destroy-unattached", "on")
    assert ("kill-window", "-t", "@2") in fake.calls          # the placeholder the view was opened on goes at once


def test_older_tmux_attaches_on_a_placeholder_that_takes_the_terminals_late_answer(tmp_path, monkeypatch):
    # tmux 3.4 types the terminal's answer to its device-attributes query ("61;6;7;...c") into the window
    fake = launch_with_fake_tmux(tmp_path, monkeypatch, version=b"tmux 3.4")
    view = "vibe-s-abcdef01"
    assert ("link-window", "-a", "-d", "-s", "@1", "-t", "=" + view + ":") in fake.calls     # the placeholder stays shown
    assert ("kill-window", "-t", "@2") not in fake.calls
    attach = next(c for c in fake.calls if c[0] == "attach-session")
    assert attach[-4:-1] == (";", "run-shell", "-b")
    assert attach[-1] == "sleep 1; /usr/bin/tmux -L vibe-launcher kill-window -t @2"


def test_stop_kills_only_the_terminals_window(tmp_path, monkeypatch):
    fake = launch_with_fake_tmux(tmp_path, monkeypatch, workspace_id="ws1")
    fake.windows["@9"] = "other"
    runtime.perform({"action": "stop", "session_id": "s", "environment_id": "host"}, tmp_path / "state")
    assert ("kill-window", "-t", "@1") in fake.calls
    assert not any(c[0] == "kill-session" and "vibe-ws" in " ".join(c) for c in fake.calls)
    assert fake.windows == {"@9": "other"}


# ----------------------------------------------------------------------------- the page


PROJECTS = [{"id": "p1", "name": "前端", "account_id": "a", "environment_id": "srv", "path": "/w/web", "persistent": True},
            {"id": "p2", "name": "後端", "account_id": "c", "environment_id": "gpu", "path": "/w/api", "persistent": True},
            {"id": "p3", "name": "本機專案", "account_id": "a", "environment_id": "local", "path": "C:/p"}]
ACCOUNTS = [{"id": "a", "name": "我的 Codex", "tool": "codex"}, {"id": "c", "name": "我的 Claude", "tool": "claude"}]
ENVIRONMENTS = [{"id": "srv", "name": "伺服器", "kind": "ssh"}, {"id": "gpu", "name": "GPU 站", "kind": "ssh"},
                {"id": "local", "name": "本機", "kind": "local"}]


@pytest.fixture
def page(qtbot):
    view = WorkspacePage(terminal_factory=FakeTerminal)
    qtbot.addWidget(view, before_close_func=lambda w: w.shutdown())
    view.resize(1200, 700)
    view.show()
    view.set_data(PROJECTS, ACCOUNTS, ENVIRONMENTS, [])
    messages = []
    view.notify.connect(lambda text, tone: messages.append((text, tone)))
    view.messages = messages
    view._confirm = lambda *a, **k: True
    return view


def open_tab(page, preset="p1", mode="agent", account="a", environment="srv", session="s1", action="launch"):
    return page.spawn(["python", "-m", "runner"], {"action": action, "mode": mode, "preset_id": preset, "account_id": account,
                                                  "environment_id": environment, "session_id": session})


def open_login(page, operation="login-1", **extra):
    return page.spawn(["python", "-m", "runner"], {
        "action": "login", "account_id": "a", "environment_id": "local",
        "operation_id": operation, "login_operation": True, **extra})


@pytest.mark.parametrize("success_first", [True, False])
@pytest.mark.parametrize("environment", ["local", "srv"])
def test_verified_login_closes_only_after_success_and_zero_exit(page, success_first, environment):
    tab = open_login(page, environment_id=environment)
    if success_first:
        page.confirm_login("login-1", "authenticated")
        assert tab.info.id in page._tabs
        tab.terminal.finish()
    else:
        tab.terminal.finish()
        assert tab.info.id in page._tabs
        page.confirm_login("login-1", "authenticated")
    assert tab.info.id not in page._tabs and tab.terminal.disposed
    assert len(page.messages) == 1 and "登入成功" in page.messages[0][0]
    page.confirm_login("login-1", "authenticated")
    assert len(page.messages) == 1


def test_login_waits_for_last_browser_ack(page):
    tab = open_login(page)
    tab.terminal.is_output_drained = False
    page.confirm_login("login-1", "authenticated")
    tab.terminal.finish()
    assert tab.info.id in page._tabs
    tab.terminal.drain()
    assert tab.info.id not in page._tabs


@pytest.mark.parametrize("status,code", [("authenticated", 1), ("authenticated", -1),
                                         ("failed", 0), ("unknown", 0), ("", 0)])
def test_failed_or_unknown_login_stays_visible(page, status, code):
    tab = open_login(page)
    page.confirm_login("login-1", status)
    tab.terminal.finish(code)
    assert tab.info.id in page._tabs and not page.messages


def test_login_confirmation_never_closes_other_operations(page):
    one = open_login(page, "one")
    two = open_login(page, "two", operation_title="GitHub 登入", operation_account_name="GH")
    normal = open_login(page, "normal", login_operation=False)
    for tab in (one, two, normal):
        tab.terminal.finish()
    page.confirm_login("wrong", "authenticated")
    assert len(page.tabs()) == 3
    page.confirm_login("normal", "authenticated")
    assert normal.info.id in page._tabs
    page.confirm_login("two", "authenticated")
    assert one.info.id in page._tabs and two.info.id not in page._tabs
    assert two.info.kind == "operation"
    page.confirm_login("one", "configured")
    assert one.info.id not in page._tabs and "設定完成" in page.messages[-1][0]


def test_manual_close_before_login_confirmation_does_not_report_success(page):
    tab = open_login(page)
    page.close_tab(tab.info.id, confirm=False)
    page.confirm_login("login-1", "authenticated")
    tab.terminal.finish()
    assert tab.info.id not in page._tabs and not page.messages


def test_spawn_runs_the_job_in_an_embedded_terminal_and_labels_it(page):
    tab = open_tab(page)
    term = tab.terminal
    assert term.command == ["python", "-m", "runner"] and term.env["VIBE_EMBEDDED"] == "1" and term.env["PYTHONUTF8"] == "1"
    assert (tab.info.project, tab.info.title, tab.info.environment, tab.info.tool) == ("前端", "Codex", "伺服器", "codex")
    assert tab.info.tmux, "an SSH project that keeps running uses tmux, so it can be left without ending"
    shell = open_tab(page, mode="shell", account="", session="s2")
    assert (shell.info.title, shell.info.tool, shell.info.kind) == ("終端機", "shell", "shell")
    local = open_tab(page, preset="p3", environment="local", session="s3")
    assert not local.info.tmux and local.info.local
    direct = open_tab(page, preset="p2", account="c", environment="gpu", session="s4")
    assert direct.info.title == "Claude" and page.selected_id() == direct.info.id, "the newest terminal is shown"


def test_rail_groups_terminals_by_project_and_follows_selection(page):
    first = open_tab(page, session="a1")
    second = open_tab(page, mode="shell", account="", session="a2")
    other = open_tab(page, preset="p2", account="c", environment="gpu", session="b1")
    rows = page.rail.rows()
    assert rows == [("header", "前端 · 2"), ("tab", first.info.id), ("tab", second.info.id), ("header", "後端 · 1"), ("tab", other.info.id)]
    page.select(first.info.id)
    assert page.rail.currentItem().data(Qt.ItemDataRole.UserRole) == first.info.id
    page.rail.setCurrentRow(4)
    assert page.selected_id() == other.info.id, "clicking the rail switches the terminal"


def test_switching_between_terminals_keeps_every_connection_open(page):
    a, b, c = open_tab(page, session="a1"), open_tab(page, session="a2"), open_tab(page, session="a3")
    page.step(1)
    assert page.selected_id() == a.info.id
    page.step(-1)
    assert page.selected_id() == c.info.id
    page.goto(2)
    assert page.selected_id() == b.info.id
    page.goto(9)
    assert page.selected_id() == b.info.id
    assert all(t.alive() and not t.terminal.terminated for t in (a, b, c))
    assert b.terminal.focus_calls >= 0


def test_account_labels_survive_title_changes_and_refresh_on_rename(page):
    tab = open_tab(page)
    assert tab.pane.title.text() == "Codex · 我的 Codex"
    tab.terminal.title_changed.emit("vibe:attached")
    assert "我的 Codex" in page.rail.currentItem().toolTip()
    accounts = [dict(a, name="工程帳號") if a["id"] == "a" else a for a in ACCOUNTS]
    page.set_data(PROJECTS, accounts, ENVIRONMENTS, [])
    assert tab.pane.title.text() == "Codex · 工程帳號"
    assert "工程帳號" in page.rail.currentItem().toolTip()
    shell = open_tab(page, mode="shell")
    assert shell.pane.title.text() == "終端機"


def test_move_terminal_changes_grid_navigation_and_saved_slot_order(page, store):
    first = open_tab(page)
    second = open_tab(page, preset="p2", account="c")
    third = open_tab(page, session="s3")
    page.set_layout("grid")
    page.move_up_button.click()
    assert page.tabs() == [first, third, second]
    page.move_up_button.click()
    assert page.tabs() == [third, first, second]
    assert not page.move_up_button.isEnabled()
    assert page.area.panes == [third.pane, first.pane, second.pane]
    assert [value for kind, value in page.rail.rows() if kind == "tab"] == page._order
    assert page.area.pane_rects()[third.pane].topLeft().isNull()
    assert page.selected_id() == third.info.id
    record = store.save("workspaces", {"name": "排列", "layout": "grid", "slots": page.slots()})
    assert [s["preset_id"] for s in record["slots"]] == ["p1", "p1", "p2"]
    page.goto(3)
    assert page.selected_id() == second.info.id and not page.move_down_button.isEnabled()
    assert all(t.alive() and not t.terminal.terminated for t in page.tabs())


def test_chords_from_inside_a_terminal_are_routed(page, qtbot):
    a, b = open_tab(page, session="a1"), open_tab(page, session="a2")
    palette = []
    page.palette_requested.connect(lambda: palette.append(1))
    b.terminal.chord.emit("prev")
    assert page.selected_id() == a.info.id
    a.terminal.chord.emit("goto:2")
    assert page.selected_id() == b.info.id
    a.terminal.chord.emit("palette")
    assert palette == [1]


def test_status_comes_from_the_session_record_once_the_terminal_ended(page):
    tab = open_tab(page)
    assert page.status_for(tab)[1] == "unknown"
    page.update_sessions([{"id": "s1", "status": "connecting"}])
    assert page.status_for(tab) == ("連線中", "info"), "the local terminal alone cannot prove remote execution"
    tab.terminal.finish(0)
    page.update_sessions([{"id": "s1", "status": "detached"}])
    assert page.status_for(tab) == ("背景執行", "info")
    assert "背景執行" in tab.pane.ended_label.text() and "重新連線" in tab.pane.ended_label.text()
    page.update_sessions([{"id": "s1", "status": "failed"}])
    assert page.status_for(tab) == ("失敗", "danger")


def test_layout_modes_and_geometry(page):
    assert TerminalArea.grid_shape(1) == (1, 1) and TerminalArea.grid_shape(2) == (2, 1) and TerminalArea.grid_shape(4) == (2, 2)
    assert TerminalArea.grid_shape(5) == (3, 2) and TerminalArea.grid_shape(7) == (3, 3)
    tabs = [open_tab(page, session=f"t{i}") for i in range(4)]
    page.area.resize(1000, 600)
    shown = [t.pane for t in tabs if page.area.on_screen(t.pane)]
    assert shown == [tabs[-1].pane], "tab mode shows exactly one terminal"
    assert all(t.pane.size() == page.area.size() for t in tabs), "parked terminals keep their size (no redraw on switch)"
    page.set_layout("grid")
    rects = page.area.pane_rects()
    assert len(rects) == 4 and all(page.area.on_screen(t.pane) for t in tabs)
    rectangles = list(rects.values())
    for i, one in enumerate(rectangles):
        assert page.area.rect().contains(one)
        assert all(not one.intersects(two) for two in rectangles[i + 1:]), "side-by-side terminals must not overlap"
    assert page.layout_bar.current() == "grid"


def test_switching_terminals_does_not_restyle_or_hide_them(page, monkeypatch):
    tabs = [open_tab(page, session=f"t{i}") for i in range(3)]
    page.area.resize(800, 500)
    restyled, hidden = [], []
    for tab in tabs:
        monkeypatch.setattr(tab.pane, "setStyleSheet", lambda *a, p=tab.pane: restyled.append(p))
        monkeypatch.setattr(tab.pane, "hide", lambda p=tab.pane: hidden.append(p))
    for tab in tabs + tabs:
        page.select(tab.info.id)
    assert not restyled and not hidden
    assert [t.pane for t in tabs if page.area.on_screen(t.pane)] == [tabs[-1].pane]
    page.set_layout("grid")
    page.select(tabs[0].info.id)
    assert [t.pane.property("active") for t in tabs] == [True, False, False]


# ----------------------------------------------------------------------------- leaving without ending


def test_detach_waits_for_control_confirmation_not_title_or_terminal_exit(page):
    tab = open_tab(page)
    requests = []
    page.detach_requested.connect(requests.append)
    tab.terminal.title_changed.emit("vibe:forged")
    page.detach(tab.info.id)
    assert requests == [tab.info.id] and tab.terminal.sent == []
    assert not tab.terminal.terminated
    tab.terminal.finish(0)
    assert page.tab(tab.info.id) is tab and not page.messages
    page.confirm_detach(tab.info.id, {"status": "detached"})
    assert page.tab(tab.info.id) is None and tab.terminal.disposed
    assert any("仍在伺服器背景執行" in text for text, tone in page.messages)


def test_detach_explains_itself_when_it_cannot_work(page):
    waiting = open_tab(page, session="w")                      # still logging in: tmux is not attached yet
    page.detach(waiting.info.id)
    page.confirm_detach(waiting.info.id, {"status": "unknown"})
    assert waiting.terminal.sent == [] and page.messages[-1][1] == "warn"
    direct = open_tab(page, preset="p3", environment="local", session="d")
    page.detach(direct.info.id)
    assert direct.terminal.sent == [] and "沒有使用 tmux" in page.messages[-1][0]
    assert direct.alive(), "nothing was ended"


def test_closing_a_tmux_terminal_detaches_instead_of_killing(page):
    tab = open_tab(page)
    tab.terminal.title_changed.emit("vibe:vibe-s1")
    page.close_tab(tab.info.id)
    assert tab.terminal.sent == [] and not tab.terminal.terminated
    tab.terminal.finish(80)
    page.confirm_detach(tab.info.id, {"status": "detached"})
    assert page.tab(tab.info.id) is None


def test_closing_before_attach_does_not_kill_unverified_work(page):
    tab = open_tab(page)
    page.close_tab(tab.info.id)
    assert not tab.terminal.terminated and tab.terminal.sent == []
    page.confirm_detach(tab.info.id, {"status": "unknown"})
    assert page.tab(tab.info.id) is tab and not tab.closing


def test_closing_a_terminal_that_would_lose_work_asks_first(page):
    direct = open_tab(page, preset="p3", environment="local", session="d")
    asked = []
    page._confirm = lambda title, message, **kw: asked.append((title, message, kw)) or False
    page.close_tab(direct.info.id)
    assert asked and asked[0][2]["danger"] is True and not direct.terminal.terminated
    page._confirm = lambda *a, **k: True
    page.close_tab(direct.info.id)
    assert direct.terminal.terminated


def test_a_stuck_detach_keeps_the_terminal_and_allows_retry(page):
    tab = open_tab(page)
    tab.terminal.title_changed.emit("vibe:vibe-s1")
    page.detach(tab.info.id)
    page.confirm_detach(tab.info.id, {"status": "unknown"})
    assert not tab.terminal.terminated and not tab.closing
    assert page.tab(tab.info.id) is tab and page.messages[-1][1] == "warn"


def test_an_ended_terminal_keeps_its_output_and_offers_to_reopen(page):
    tab = open_tab(page)
    tab.terminal.finish(1)
    assert page.tab(tab.info.id) is not None and not tab.terminal.disposed, "scrollback stays readable"
    assert tab.pane.ended_bar.isVisibleTo(tab.pane) and tab.pane.reopen_button.isVisibleTo(tab.pane)
    asked = []
    page.launch_requested.connect(lambda *args: asked.append(args))
    tab.pane.reopen_button.click()
    assert asked == [("p1", "agent", "a", "")] and page.tab(tab.info.id) is None      # "" = on the project's own environment


# ----------------------------------------------------------------------------- opening things, quickly


def test_quick_open_picks_a_matching_account_per_tool(page):
    project = PROJECTS[0]                                       # saved with the Codex account
    assert page.launcher.tool_account(project, "codex")["id"] == "a"
    assert page.launcher.tool_account(project, "claude")["id"] == "c", "any Claude account will do for another tool"
    page.launcher.set_data(PROJECTS, [ACCOUNTS[0]], ENVIRONMENTS)
    assert page.launcher.tool_account(project, "claude") is None


def launcher_row(page):
    """The first live quick-open row (replaced rows are marked disposed at once and deleted on the next loop turn)."""
    return next(w for w in page.launcher.host.findChildren(QWidget) if w.property("surface") == "card" and not w.property("disposed"))


def test_quick_open_buttons_launch_the_right_thing(page):
    requests = []
    page.launch_requested.connect(lambda *args: requests.append(args))
    row = launcher_row(page)
    buttons = {b.text(): b for b in row.findChildren(type(page.workspace_button)) if b.text()}
    buttons["Claude"].click()
    buttons["終端機"].click()
    buttons["Codex"].click()
    # rows are ordered favourites/recent/name; the first row is 前端 (p1) or 後端 (p2): check the shapes, not the order
    kinds = [(r[1], bool(r[2])) for r in requests]
    assert kinds == [("agent", True), ("shell", False), ("agent", True)]


def test_buttons_are_disabled_when_there_is_no_account_for_the_tool(page):
    page.launcher.set_data(PROJECTS, [ACCOUNTS[0]], ENVIRONMENTS)
    row = launcher_row(page)
    buttons = {b.text(): b for b in row.findChildren(type(page.workspace_button)) if b.text()}
    assert buttons["Codex"].isEnabled() and not buttons["Claude"].isEnabled() and buttons["終端機"].isEnabled()
    assert "沒有可用的 Claude 帳號" in buttons["Claude"].toolTip()


def test_search_filters_the_quick_open_list(page):
    assert {p["name"] for p in page.launcher.visible_projects()} == {"前端", "後端", "本機專案"}
    page.launcher.search.setText("api")
    assert [p["name"] for p in page.launcher.visible_projects()] == ["後端"]


def test_switch_options_offer_other_agents_and_a_plain_shell_but_never_another_host(page):
    tab = open_tab(page)                                        # 前端 · Codex on 伺服器
    options = page.switch_options(tab)
    labels = [o[0] for o in options]
    assert any(label.startswith("開啟 Codex") for label in labels) and any(label.startswith("開啟 Claude") for label in labels)
    assert any("終端機（不執行" in label for label in labels)
    assert not any("在「" in label for label in labels), "a project only ever opens on the host it is bound to"
    assert {o[4] for o in options} == {"srv"}


def test_workspace_slots_capture_only_reopenable_terminals(page):
    open_tab(page, session="a1")
    open_tab(page, mode="shell", account="", session="a2")
    page.spawn(["python"], {"action": "login", "account_id": "a", "environment_id": "srv"})
    # A Codex terminal also keeps its session: reopening it finds its exact conversation and saved quota flow.
    assert page.slots() == [{"preset_id": "p1", "kind": "agent", "account_id": "a", "environment_id": "srv", "session_id": "a1"},
                            {"preset_id": "p1", "kind": "shell", "environment_id": "srv"}]


def test_shutdown_ends_everything_and_frees_the_pages(page):
    tabs = [open_tab(page, session=f"t{i}") for i in range(3)]
    assert page.running_count() == 3
    page.shutdown()
    assert page.tabs() == [] and all(t.terminal.terminated and t.terminal.disposed for t in tabs)
    assert page.right.currentWidget() is page.empty


# ----------------------------------------------------------------------------- window integration


class SpawnService:
    supports_spawn = True

    def __init__(self):
        self.calls = []
        self.fail = None

    def start(self, action, **kwargs):
        self.calls.append((action, dict(kwargs)))
        if self.fail:
            error, self.fail = self.fail, None
            raise error
        spawn = kwargs.get("spawn")
        if spawn:
            spawn(["python", "-m", "runner"], {"action": action, "mode": kwargs.get("mode", "agent"), "session_id": f"s{len(self.calls)}",
                                             "preset_id": kwargs.get("preset_id"), "account_id": kwargs.get("account_id") or "a",
                                             "environment_id": kwargs.get("environment_id") or "srv"})
        return f"s{len(self.calls)}"


@pytest.fixture
def window(tmp_path, qtbot):
    store = Store(tmp_path / "gui.db")
    store.save("accounts", {"id": "a", "name": "我的 Codex", "tool": "codex", "auth": "subscription"})
    store.save("accounts", {"id": "c", "name": "我的 Claude", "tool": "claude", "auth": "subscription"})
    store.save("environments", {"id": "srv", "name": "伺服器", "kind": "ssh", "ssh_target": "srv"})
    store.save("presets", {"id": "p1", "name": "前端", "account_id": "a", "environment_id": "srv", "path": "/w/web", "ask_account": False})
    store.save("presets", {"id": "p2", "name": "後端", "account_id": "c", "environment_id": "srv", "path": "/w/api", "ask_account": False})
    service = SpawnService()
    view = MainWindow(store, service, terminal_factory=FakeTerminal)
    view._confirm = lambda *a, **k: True
    # pytest-qt closes widgets *before* fixture teardown, and a window with live terminals asks before closing,
    # so end the terminals first.
    qtbot.addWidget(view, before_close_func=lambda w: w.workspace.shutdown())
    view.resize(1280, 800)
    view.show()
    return view, store, service


def test_projects_open_in_the_workspace_when_the_built_in_terminal_is_available(window):
    view, store, service = window
    view.show_page("projects")
    view.launch_project()
    action, kwargs = service.calls[-1]
    assert action == "launch" and kwargs["spawn"] == view.workspace.spawn
    assert view.current_page() == "workspace" and len(view.workspace.tabs()) == 1
    assert view.workspace.tabs()[0].info.title == "Codex"


def test_ssh_only_button_opens_a_shell_without_the_agent(window):
    view, store, service = window
    view.show_page("projects")
    view.open_shell()
    action, kwargs = service.calls[-1]
    assert action == "launch" and kwargs["mode"] == "shell" and kwargs["preset_id"] == view._selected_project
    assert view.workspace.tabs()[0].info.kind == "shell"


def test_attach_and_login_also_use_the_workspace_but_checks_stay_external(window):
    view, store, service = window
    view._invoke("attach", session_id="x")
    view._invoke("doctor", environment_id="srv")
    assert "spawn" in service.calls[0][1] and "spawn" not in service.calls[1][1]


def test_falls_back_to_an_external_window_if_the_built_in_terminal_cannot_start(window):
    view, store, service = window
    service.fail = OSError("ConPTY unavailable")
    view._invoke("launch", preset_id="p1")
    assert len(service.calls) == 2 and "spawn" in service.calls[0][1] and "spawn" not in service.calls[1][1]
    # a refusal from validation is not a terminal problem: do not retry it elsewhere
    service.calls.clear()
    service.fail = ValueError("Select an account")
    view._invoke("launch", preset_id="p1")
    assert len(service.calls) == 1


def test_external_terminals_can_be_chosen_and_the_choice_is_remembered(window, tmp_path, qtbot):
    view, store, service = window
    view.toggle_external_terminal()
    view._invoke("launch", preset_id="p1")
    assert "spawn" not in service.calls[-1][1] and view.workspace.tabs() == []
    again = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(again, before_close_func=lambda w: w.workspace.shutdown())
    assert again.prefs.external_terminal is True
    view.toggle_external_terminal()
    assert view.prefs.external_terminal is False


def test_saving_and_opening_a_workspace(window, monkeypatch):
    view, store, service = window
    from vibe_launcher import ui
    view.open_terminal("p1", "agent")
    view.open_terminal("p2", "shell")
    view.workspace.set_layout("grid")
    answers = {"name": "全部開起來"}

    class Dialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return ui.QDialog.DialogCode.Accepted

        def values(self):
            return dict(answers)
    monkeypatch.setattr(ui, "RecordDialog", Dialog)
    view.save_workspace()
    saved, = store.list("workspaces")
    assert saved["name"] == "全部開起來" and saved["layout"] == "grid" and len(saved["slots"]) == 2
    # saving under the same name updates it instead of duplicating it
    view.save_workspace()
    assert len(store.list("workspaces")) == 1

    view.workspace.shutdown()
    service.calls.clear()
    view.workspace.set_layout("tabs")
    view.open_workspace(saved["id"])
    assert [c[1].get("mode", "agent") for c in service.calls] == ["agent", "shell"]
    assert len(view.workspace.tabs()) == 2 and view.workspace.area.mode == "grid"


def test_opening_a_workspace_skips_projects_that_no_longer_exist(window):
    view, store, service = window
    store.save("workspaces", {"id": "w", "name": "舊的", "slots": [{"preset_id": "p1", "kind": "agent"}, {"preset_id": "gone", "kind": "agent"}]})
    view.reload()
    view.open_workspace("w")
    assert len(service.calls) == 1
    assert any("略過" in t.text.text() for t in view.toasts.toasts)


def test_desktops_keep_multiple_projects_layout_focus_and_processes(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": [
        {"preset_id": "p1", "kind": "agent", "account_id": "a"},
        {"preset_id": "p2", "kind": "shell"}]})
    store.save("workspaces", {"id": "two", "name": "Two", "slots": []})
    view.reload()
    view.open_workspace("one")
    first, second = view.workspace.current_tabs()
    first.pane.composer.edit.setPlainText("Keep this draft")
    view.workspace.set_layout("grid")
    view.workspace.select(first.info.id)
    area = view.workspace.area
    view.open_workspace("two")
    assert view.workspace.current_tabs() == []
    assert view.workspace.right.currentWidget() is view.workspace.empty
    view.open_terminal("p2", "shell")
    third, = view.workspace.current_tabs()
    assert view.workspace.area is not area
    for _ in range(3):
        view.open_workspace("one")
        assert view.workspace.area is area and area.mode == "grid"
        assert view.workspace.selected_id() == first.info.id
        assert view.workspace.current_tabs() == [first, second]
        assert first.pane.composer.edit.toPlainText() == "Keep this draft"
        view.open_workspace("two")
        assert view.workspace.current_tabs() == [third]
    assert len(service.calls) == 3 and view.workspace.running_count() == 3
    assert all(not t.terminal.terminated for t in view.workspace.tabs())
    assert store.get("workspaces", "one")["layout"] == "grid"
    view.workspace.select(first.info.id)
    assert view.workspace.workspace_id == "one"
    view.workspace.goto(2)
    assert view.workspace.selected_id() == second.info.id


def test_repeated_open_and_attach_do_not_create_more_sessions(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "shell"}]})
    view.reload()
    view.open_workspace("one")
    tab, = view.workspace.tabs()
    view.open_workspace("")
    view._invoke("attach", session_id=tab.info.session_id)
    view.open_workspace("one")
    assert len(service.calls) == 1
    assert view.workspace.workspace_id == "one"


def test_launches_name_the_workspace_whose_tmux_session_they_join(window, qtbot):
    view, store, service = window
    view.open_terminal("p1", "shell")
    default_id = service.calls[-1][1]["workspace_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", default_id)
    store.save("workspaces", {"id": "two", "name": "Two", "slots": []})
    view.reload()
    view.open_workspace("two")
    view.open_terminal("p2", "shell")
    assert service.calls[-1][1]["workspace_id"] == "two"
    # the default desktop keeps its id across restarts, so its terminals are found in the same tmux session again
    again = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(again, before_close_func=lambda w: w.workspace.shutdown())
    assert again.prefs.default_workspace_id == default_id


def test_background_desktop_closes_only_its_own_terminal(window):
    view, store, service = window
    view.open_terminal("p1", "shell")
    background, = view.workspace.tabs()
    store.save("workspaces", {"id": "two", "name": "Two", "slots": []})
    view.reload()
    view.open_workspace("two")
    view.open_terminal("p2", "shell")
    foreground, = view.workspace.current_tabs()
    background.terminal.finish()
    view.workspace.close_tab(background.info.id)
    assert view.workspace.current_tabs() == [foreground]
    assert view.workspace.area.panes == [foreground.pane]
    assert view.workspace.selected_id() == foreground.info.id


def test_deleted_workspace_keeps_live_terminals_accessible(window, monkeypatch):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "shell"}]})
    view.reload()
    view.open_workspace("one")
    tab, = view.workspace.tabs()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.delete_workspace("one")
    assert not store.list("workspaces")
    view.open_workspace("")
    view.workspace.select(tab.info.id)
    assert view.workspace.current_tabs() == [tab]
    assert not tab.terminal.terminated
    assert view.workspace.workspace_picker.currentData() == "one"


def test_new_desktop_is_empty_and_does_not_move_existing_terminals(window, monkeypatch):
    from vibe_launcher import ui
    view, store, service = window
    view.open_terminal("p1", "shell")
    tab, = view.workspace.tabs()

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return ui.QDialog.DialogCode.Accepted

        def values(self):
            return {"name": "Review"}

    monkeypatch.setattr(ui, "RecordDialog", Dialog)
    view.new_workspace()
    record, = store.list("workspaces")
    assert record["slots"] == [] and record["name"] == "Review"
    assert view.workspace.workspace_id == record["id"]
    assert view.workspace.current_tabs() == []
    view.open_workspace("")
    assert view.workspace.current_tabs() == [tab]


def test_saved_desktop_reattaches_existing_remote_work(window):
    view, store, service = window
    store.save("accounts", {**store.get("accounts", "a"), "environment_id": "srv"})
    store.save("sessions", {"id": "remote", "preset_id": "p1", "account_id": "a", "environment_id": "srv",
                            "name": "Project", "path": "/w/web", "status": "detached", "persistent": True})
    store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "agent", "account_id": "a"}]})
    view.reload()
    view.open_workspace("one")
    assert service.calls[0][0] == "attach" and service.calls[0][1]["session_id"] == "remote"
    assert len(service.calls) == 1


def test_failed_restore_does_not_erase_saved_slots_on_switch(window):
    view, store, service = window
    record = store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "shell"}]})
    view.reload()
    service.fail = ValueError("Cannot start this terminal")
    view.open_workspace("one")
    assert view.workspace.current_tabs() == []
    view.open_workspace("")
    assert store.get("workspaces", "one") == record


def test_external_terminal_mode_does_not_create_an_empty_saved_desktop(window):
    view, store, service = window
    record = store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "shell"}]})
    view.reload()
    view.toggle_external_terminal()
    view.open_workspace("one")
    assert view.workspace.workspace_id == "" and not service.calls
    assert store.get("workspaces", "one") == record


def test_deleting_a_workspace_asks_first(window, monkeypatch):
    view, store, service = window
    store.save("workspaces", {"id": "w", "name": "舊的", "slots": [{"preset_id": "p1", "kind": "agent"}]})
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: False)
    view.delete_workspace("w")
    assert store.list("workspaces")
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.delete_workspace("w")
    assert store.list("workspaces") == []


def test_command_palette_can_switch_terminals_and_open_projects(window):
    view, store, service = window
    view.open_terminal("p1", "agent")
    view.open_terminal("p2", "shell")
    titles = [c.title for c in view._commands()]
    assert any(t.startswith("切換到 前端 · Codex") for t in titles) and any(t.startswith("切換到 後端 · 終端機") for t in titles)
    assert "開啟 前端 · 終端機" in titles and "開啟 後端 · Codex／Claude" in titles
    assert "工作區：並排檢視" in titles and "改用外部終端機視窗" in titles
    switch = next(c for c in view._commands() if c.title.startswith("切換到 前端"))
    view.show_page("projects")
    switch.callback()
    assert view.current_page() == "workspace" and view.workspace.tab(view.workspace.selected_id()).info.project == "前端"


def test_closing_the_app_asks_when_terminals_are_connected(window, monkeypatch):
    view, store, service = window
    view.open_terminal("p1", "agent")
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda title, message, **kw: asked.append((message, kw)) or False)
    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    view.closeEvent(event)
    assert not event.isAccepted() and "tmux" in asked[0][0] and asked[0][1]["danger"]
    assert view.workspace.running_count() == 1, "declining keeps everything connected"
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    event = QCloseEvent()
    view.closeEvent(event)
    assert event.isAccepted() and view.workspace.tabs() == []


def test_closing_with_only_local_terminals_says_so_and_ends_their_sessions(window, monkeypatch):
    view, store, service = window
    store.save("presets", {"id": "p3", "name": "本機專案", "account_id": "a", "environment_id": "local", "path": "C:/w/local",
                           "ask_account": False})
    view.reload()
    view.open_terminal("p3", "agent", "a", "local")
    tab = view.workspace.tabs()[0]
    assert tab.info.local and not tab.info.tmux
    store.save("sessions", {"id": tab.info.session_id, "preset_id": "p3", "account_id": "a", "environment_id": "local",
                            "name": "本機專案", "path": "C:/w/local", "status": "running", "updated_at": runtime.now()})
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda title, message, **kw: asked.append(message) or True)
    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    view.closeEvent(event)
    assert event.isAccepted() and "tmux" not in asked[0]
    assert store.get("sessions", tab.info.session_id)["status"] == "ended"


def test_closing_to_the_tray_keeps_terminals_and_quit_uses_the_normal_close(window, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    from vibe_launcher import tray as tray_module
    view, store, service = window
    view.open_terminal("p1", "agent")
    monkeypatch.setattr(tray_module, "available", lambda: True)
    monkeypatch.setattr(tray_module.TrayIcon, "show", lambda self: None)
    monkeypatch.setattr(tray_module.TrayIcon, "showMessage", lambda self, *a, **k: None)
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda title, message, **kw: asked.append(message) or True)
    view.prefs.minimize_to_tray = True
    event = QCloseEvent()
    view.closeEvent(event)
    assert not event.isAccepted() and not asked and view.workspace.running_count() == 1, "hidden, nothing ended or asked"
    view._quit_from_tray()                  # the tray's Quit goes through the usual confirmation and shutdown
    assert asked and view.workspace.tabs() == []


def test_closing_without_the_tray_option_or_a_tray_is_unchanged(window, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    from vibe_launcher import tray as tray_module
    view, store, service = window
    monkeypatch.setattr(tray_module, "available", lambda: False)
    view.prefs.minimize_to_tray = True      # no tray on this platform: fall back to quitting
    event = QCloseEvent()
    view.closeEvent(event)
    assert event.isAccepted()


def test_the_workspace_is_a_page_with_a_shortcut(window):
    view, store, service = window
    assert view.PAGES[1] == "workspace" and "workspace" in view.nav_buttons
    view.show_page("workspace")
    assert view.current_page() == "workspace"


# ----------------------------------------------------------------------------- the terminal widget (no browser needed)


def test_the_page_template_is_filled_in_and_locked_down():
    html = terminal.terminal_html()
    # Placeholders look like __NAME__ (Qt's inlined qwebchannel.js has its own __id__ / __proto__).
    assert not re.search(r"__[A-Z0-9_]+__", html), "every placeholder must be substituted"
    assert "Content-Security-Policy" in html and "connect-src 'none'" in html and "default-src 'none'" in html
    assert "windowsPty" in html and "conpty" in html
    assert html.count("<script") >= 5


def test_webgl_renderer_is_opt_in(monkeypatch):
    monkeypatch.delenv("VIBE_TERMINAL_WEBGL", raising=False)
    assert "if (false)" in terminal.terminal_html()
    monkeypatch.setenv("VIBE_TERMINAL_WEBGL", "1")
    assert "if (true)" in terminal.terminal_html()


def test_packaged_terminal_assets_exist():
    for name in ("xterm.js", "xterm.css", "addon-fit.js", "addon-unicode11.js", "addon-webgl.js", "LICENSE-xterm.txt"):
        assert (terminal.ASSETS / "xterm" / name).is_file(), name


def test_chords_the_page_claims_do_not_include_common_terminal_keys():
    html = (terminal.ASSETS / "terminal.html").read_text(encoding="utf-8")
    chords = html[html.index("function chordName"):html.index("new QWebChannel")]
    assert "KeyB" not in chords and "'KeyC'" not in chords, "Ctrl+B (tmux) and Ctrl+C (interrupt) must reach the program"


@pytest.mark.skipif(__import__("os").environ.get("VIBE_TEST_WEBENGINE") != "1", reason="opt-in: starts a real Chromium")
def test_real_terminal_widget_runs_a_program_and_reports_its_exit(qtbot):
    import sys
    widget = terminal.TerminalWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 500)
    widget.show()
    exits = []
    widget.exited.connect(exits.append)
    widget.start([sys.executable, "-c", "import sys; print('hello from a real terminal'); sys.exit(4)"])
    qtbot.waitUntil(lambda: bool(exits), timeout=20000)
    assert exits == [4]
    widget.dispose()


@pytest.mark.skipif(__import__("os").environ.get("VIBE_TEST_WEBENGINE") != "1", reason="opt-in: starts a real Chromium")
def test_real_terminal_takes_input_bar_text_and_follows_a_theme_switch(qtbot):
    import sys
    from vibe_launcher import theme
    widget = terminal.TerminalWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 500)
    widget.show()
    exits = []
    widget.exited.connect(exits.append)
    program = ("import sys\n"
               "line = sys.stdin.readline().strip()\n"
               "sys.exit(0 if line == '你好 world' else 3)\n")
    widget.start([sys.executable, "-X", "utf8", "-c", program], env=dict(__import__("os").environ, PYTHONUTF8="1"))
    qtbot.waitUntil(lambda: widget._page_ready and widget.pid != 0, timeout=20000)
    theme.apply_theme("github-light")
    terminal.retheme_all()
    terminal.configure(font_size=16)
    qtbot.wait(300)
    widget.submit("你好 world")
    qtbot.waitUntil(lambda: bool(exits), timeout=20000)
    theme.apply_theme(theme.DEFAULT_THEME)
    terminal.configure(font_size=14)
    assert exits == [0], "text from the input bar reaches the program intact (UTF-8, then Enter)"
    widget.dispose()


def open_check(page, operation="op-1", environment="local"):
    return page.spawn(["python", "-m", "runner"], {
        "action": "github", "account_id": "", "environment_id": environment, "operation_id": operation,
        "login_operation": False, "operation_title": "GitHub · status", "operation_account_name": "工作"})


def test_finished_background_operation_closes_itself(page):
    tab = open_check(page)
    assert tab.info.background
    tab.terminal.is_output_drained = False
    tab.terminal.finish()
    assert tab.info.id in page._tabs                       # output still arriving
    tab.terminal.drain()
    assert tab.info.id not in page._tabs and tab.terminal.disposed


def test_failed_background_operation_stays_and_is_replaced_by_a_retry(page):
    first = open_check(page, "op-1")
    first.terminal.finish(1)
    assert first.info.id in page._tabs                     # the output explains the failure
    second = open_check(page, "op-2")
    assert first.info.id not in page._tabs and second.info.id in page._tabs
    other = open_check(page, "op-3", environment="srv")
    assert second.info.id in page._tabs and other.info.id in page._tabs   # a different target is not a copy


def test_agent_and_update_terminals_never_close_on_their_own(page):
    agent = open_tab(page)
    update = open_tab(page, session="", action="update-tool")
    for tab in (agent, update):
        tab.terminal.finish()
    assert agent.info.id in page._tabs and update.info.id in page._tabs


def test_unsent_input_bar_text_returns_with_the_project(page, tmp_path):
    page.draft_root = tmp_path
    tab = open_tab(page, preset="p3", environment="local", account="c", session="s9")
    tab.pane.composer.edit.setPlainText("還沒送出的指令")
    tab.pane.draft.save()
    page.close_tab(tab.info.id, confirm=False)
    again = open_tab(page, preset="p3", environment="local", account="c", session="s10")
    assert again.pane.composer.edit.toPlainText() == "還沒送出的指令"
    assert again.pane.composer_visible()
    again.pane.composer.edit.clear()
    again.pane.draft.save()
    third = open_tab(page, preset="p3", environment="local", account="c", session="s11")
    assert third.pane.composer.edit.toPlainText() == ""


def test_splitter_handle_follows_the_theme_not_the_system_palette(window):
    view = window[0]
    view.show_page("workspace")
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication
    from vibe_launcher import theme
    palette = QApplication.palette()
    dark_system = QPalette(palette)
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button, QPalette.ColorRole.Base):
        dark_system.setColor(role, QColor("#000000"))
    QApplication.setPalette(dark_system)                 # a dark Windows paints unstyled handles from this
    try:
        view.apply_appearance({"theme": "solarized-light"}, save=False)
        handle = view.workspace.splitter.handle(1)
        assert handle.grab().toImage().pixelColor(handle.width() // 2, 40).name() == QColor(theme.C.bg).name()
    finally:
        QApplication.setPalette(palette)


def test_agent_terminals_can_refresh_the_projects_conversations_from_other_accounts(window, monkeypatch):
    view, store, service = window
    view.open_terminal("p1")
    tab = view.workspace.tabs()[0]
    assert tab.pane.history_button.isVisibleTo(tab.pane)
    calls = []
    monkeypatch.setattr(view, "_start_operation", lambda action, kwargs, callback: calls.append((action, kwargs)))
    tab.pane.history_button.click()
    assert calls == [("import-history", {"preset_id": "p1", "account_id": "a", "environment_id": "srv",
                                          "operation": {"project_history": True}})]
    view.open_terminal("p1", kind="shell")
    shell = [t for t in view.workspace.tabs() if t.info.kind == "shell"][0]
    assert not shell.pane.history_button.isVisibleTo(shell.pane)


# ----------------------------------------------------------------------------- workspace names, icons, Home and Projects
class FakeIconDialog:
    """Stands in for the name-and-icon dialog: records how it was opened and answers with ``answer``."""
    answer = {"name": "工地", "entry": {"kind": "glyph", "name": "crane", "color": ""}}
    opened = []

    def __init__(self, parent, name, entry, **kwargs):
        FakeIconDialog.opened.append((name, entry, kwargs))

    def exec(self):
        from vibe_launcher import ui
        return ui.QDialog.DialogCode.Accepted

    def result_name(self):
        return self.answer["name"]

    def result_entry(self, root):
        return self.answer["entry"]


@pytest.fixture
def icon_dialog(monkeypatch):
    from vibe_launcher import project_icon_dialog
    FakeIconDialog.opened = []
    FakeIconDialog.answer = {"name": "工地", "entry": {"kind": "glyph", "name": "crane", "color": ""}}
    monkeypatch.setattr(project_icon_dialog, "ProjectIconDialog", FakeIconDialog)
    return FakeIconDialog


def test_the_default_desktop_gets_its_own_name_and_icon(window, icon_dialog):
    view, store, service = window
    view.edit_workspace("")
    assert icon_dialog.opened[0][2]["glyph"] == "briefcase" and icon_dialog.opened[0][2]["name_label"]
    assert view.prefs.default_workspace_name == "工地" and view.prefs.workspace_icons["@default"]["name"] == "crane"
    assert view.workspace.workspace_picker.itemText(0) == "工地"
    assert view.home_page.workspace_card_widgets()[0].name.text() == "工地"
    assert not store.list("workspaces"), "the default desktop stays a view preference, not a record"


def test_renaming_a_saved_workspace_keeps_its_terminals_and_slots(window, icon_dialog):
    view, store, service = window
    slots = [{"preset_id": "p1", "kind": "shell"}]
    store.save("workspaces", {"id": "one", "name": "One", "slots": slots})
    view.reload()
    icon_dialog.answer = {"name": "Site", "entry": None}
    view.edit_workspace("one")
    saved = store.get("workspaces", "one")
    assert saved["name"] == "Site" and saved["slots"] == slots and "one" not in view.prefs.workspace_icons
    assert "One" in icon_dialog.opened[0][2]["taken"] or "預設工作區" in icon_dialog.opened[0][2]["taken"]


def test_a_deleted_workspace_takes_its_icon_with_it(window, monkeypatch):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": []})
    view.prefs.workspace_icons["one"] = {"kind": "glyph", "name": "crane", "color": ""}
    view.reload()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.delete_workspace("one")
    assert "one" not in view.prefs.workspace_icons


def test_the_menu_and_palette_offer_every_desktop_and_editing(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": []})
    view.reload()
    texts = [action.text for action in view.workspace.workspace_menu_actions()]
    assert "編輯名稱與圖示…" in texts
    titles = [c.title for c in view._commands()]
    assert "切換工作區：預設工作區" in titles and "切換工作區：One" in titles
    assert "編輯目前工作區的名稱與圖示…" in titles


def test_home_shows_a_card_per_desktop_and_switches_on_click(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "shell"}]})
    view.reload()
    view.open_terminal("p2", "shell")
    default, one = view.home_page.workspace_card_widgets()
    assert "1 個專案 · 1 個開啟 · 1 個執行中" in default.caption.text()
    assert "1 個專案 · 0 個開啟" in one.caption.text(), "a saved desktop not opened yet counts its saved projects"
    one.clicked.emit("one")
    assert view.workspace.workspace_id == "one" and view.current_page() == "workspace"


def test_projects_show_filter_and_open_into_workspaces(window):
    from vibe_launcher.ui import NO_WORKSPACE
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "agent", "account_id": "a"}]})
    view.reload()
    view.show_page("projects")
    rows = {view.project_list.item(i).data(Qt.ItemDataRole.UserRole): view.project_list.item(i).text()
            for i in range(view.project_list.count())}
    assert "工作區：One" in rows["p1"] and "工作區" not in rows["p2"]
    box = view.project_workspace_filter
    box.setCurrentIndex(box.findData("one"))
    assert [view.project_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(view.project_list.count())] == ["p1"]
    box.setCurrentIndex(box.findData(NO_WORKSPACE))
    assert [view.project_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(view.project_list.count())] == ["p2"]
    view.clear_project_filters()
    assert view.project_list.count() == 2
    # opening p1 "in One" restores One's saved terminal for it and does not start a second one
    view._selected_project = "p1"
    view.open_project_in_workspace("one")
    assert view.workspace.workspace_id == "one" and len(service.calls) == 1
    # opening p2 there adds it to that desktop
    view._selected_project = "p2"
    view.open_project_in_workspace("one")
    assert [t.info.preset_id for t in view.workspace.current_tabs()] == ["p1", "p2"]
    view._show_project_workspaces("p2")
    layout = view.project_workspaces
    chips = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget().property("workspaceChip") is not None]
    assert [chip.property("workspaceChip") for chip in chips] == ["one"]
    view.open_workspace("")
    chips[0].click()
    assert view.workspace.workspace_id == "one"


def test_open_in_a_new_workspace_and_external_terminal_mode(window, monkeypatch):
    from vibe_launcher import ui
    view, store, service = window

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return ui.QDialog.DialogCode.Accepted

        def values(self):
            return {"name": "Review"}

    monkeypatch.setattr(ui, "RecordDialog", Dialog)
    view._selected_project = "p2"
    view.open_project_in_workspace(None)
    record, = store.list("workspaces")
    tab, = view.workspace.current_tabs()
    assert view.workspace.workspace_id == record["id"] and tab.info.workspace_id == record["id"] and tab.info.preset_id == "p2"
    view.toggle_external_terminal()
    calls = len(service.calls)
    view.open_project_in_workspace("")
    assert len(service.calls) == calls and view.workspace.workspace_id == record["id"]


def test_the_sessions_drawer_groups_open_sessions_by_workspace(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": []})
    view.reload()
    view.open_terminal("p1", "agent")
    first = view.workspace.current_tabs()[0]
    view.open_workspace("one")
    view.open_terminal("p1", "agent")
    second = view.workspace.current_tabs()[0]
    for tab in (first, second):
        store.save("sessions", {"id": tab.info.session_id, "preset_id": "p1", "account_id": "a", "environment_id": "srv",
                                "name": "前端", "path": "/w/web", "status": "running", "updated_at": runtime.now()})
    store.save("sessions", {"id": "old", "preset_id": "p1", "account_id": "a", "environment_id": "srv", "name": "前端",
                            "path": "/w/web", "status": "ended", "updated_at": runtime.now()})
    view.reload()
    view.show_page("projects")
    view._show_project("p1")
    groups = [w.property("workGroup") for w in view.work_drawer.findChildren(QObject) if w.property("workGroup") is not None]
    assert groups == ["預設工作區", "One", ""]
    assert "預設工作區、One" in view.work_drawer.summary.text()


# ----------------------------------------------------------------------------- reload, send confirmation, autosave
def test_the_input_bar_asks_before_sending_until_told_not_to(window, monkeypatch):
    view, store, service = window
    view.open_terminal("p1", "agent")
    tab = view.workspace.tabs()[0]
    sent = []
    tab.terminal.submit = sent.append
    answers = [(False, False), (True, True)]
    monkeypatch.setattr(view.workspace, "_confirm_skippable", lambda *a, **k: answers.pop(0))
    edit = tab.pane.composer.edit
    edit.setPlainText("run the tests")
    edit.send()
    assert sent == [] and edit.toPlainText() == "run the tests", "declined: nothing sent, the text stays"
    edit.send()
    assert sent == ["run the tests"] and view.prefs.confirm_send is False
    edit.setPlainText("again")
    edit.send()                                            # no more questions (answers is empty now)
    assert sent == ["run the tests", "again"]


def test_reload_restarts_a_terminal_in_place_and_continues_the_conversation(window, monkeypatch):
    view, store, service = window
    view.open_terminal("p1", "agent")
    view.open_terminal("p2", "shell")
    first, second = view.workspace.current_tabs()
    first.info.tmux, first.info.local = False, True        # a local terminal: it ends with its window
    store.save("sessions", {"id": first.info.session_id, "preset_id": "p1", "account_id": "a", "environment_id": "local",
                            "name": "前端", "path": "C:/p1", "status": "running", "persistent": False})
    monkeypatch.setattr(view.workspace, "_confirm_skippable", lambda *a, **k: (True, False))
    service.calls.clear()
    first.pane.reload_button.click()
    assert [c[0] for c in service.calls] == ["launch"] and service.calls[0][1]["resume"] is True
    tabs = view.workspace.current_tabs()
    assert len(tabs) == 2 and tabs[1] is second and tabs[0] is not first, "the new terminal takes the old one's place"
    assert view.prefs.confirm_reload is True
    assert store.get("sessions", first.info.session_id)["status"] == "ended", "the replaced run is recorded as over"


def test_reload_of_a_tmux_terminal_reattaches(window, monkeypatch):
    view, store, service = window
    view.open_terminal("p1", "agent")
    tab = view.workspace.current_tabs()[0]
    tab.info.tmux = True
    monkeypatch.setattr(view.workspace, "_confirm_skippable", lambda *a, **k: (True, False))
    service.calls.clear()
    view.workspace.reload_tab(tab.info.id)
    assert service.calls[0][0] == "attach" and service.calls[0][1]["session_id"] == tab.info.session_id


def test_reload_of_a_tmux_terminal_whose_cli_ended_starts_the_conversation_again(window, monkeypatch):
    """Ctrl+C ended the CLI and with it the tmux session: attaching would fail, so the conversation is resumed instead."""
    view, store, service = window
    view.open_terminal("p1", "agent")
    tab = view.workspace.current_tabs()[0]
    tab.info.tmux = True
    tab.exit_code = 79
    view.workspace._sessions[tab.info.session_id] = {"id": tab.info.session_id, "status": "ended"}
    monkeypatch.setattr(view.workspace, "_confirm_skippable", lambda *a, **k: (True, False))
    service.calls.clear()
    view.workspace.reload_tab(tab.info.id)
    assert service.calls[0][0] == "launch" and service.calls[0][1].get("resume") is True


def test_desktops_are_saved_as_terminals_open_and_close(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": []})
    view.reload()
    view.open_terminal("p1", "agent")
    assert [s["preset_id"] for s in view.prefs.default_workspace["slots"]] == ["p1"]
    view.open_workspace("one")
    view.open_terminal("p2", "shell")
    assert [s["preset_id"] for s in store.get("workspaces", "one")["slots"]] == ["p2"]
    assert view.prefs.last_workspace == "one"
    tab = view.workspace.current_tabs()[0]
    tab.terminal.finish()
    view.workspace.close_tab(tab.info.id)
    assert store.get("workspaces", "one")["slots"] == [], "closing a terminal on purpose takes it out"
    view.open_workspace("")
    view.workspace.shutdown()                              # closing the launcher is not "closing every terminal"
    assert [s["preset_id"] for s in view.prefs.default_workspace["slots"]] == ["p1"]


def test_after_a_restart_the_last_desktop_offers_its_terminals(tmp_path, qtbot, window):
    view, store, service = window
    view.open_terminal("p1", "agent")
    view.workspace.shutdown()
    again = MainWindow(store, service, terminal_factory=FakeTerminal)
    again._confirm = lambda *a, **k: True
    qtbot.addWidget(again, before_close_func=lambda w: w.workspace.shutdown())
    assert again.workspace.restore_button.isVisibleTo(again.workspace) and "1" in again.workspace.restore_button.text()
    assert again.workspace.tabs() == [], "nothing starts by itself"
    service.calls.clear()
    again.workspace.restore_button.click()
    assert service.calls[0][0] == "launch" and service.calls[0][1]["resume"] is True
    assert not again.workspace.restore_button.isVisibleTo(again.workspace)


def test_project_running_follows_the_open_terminals(window):
    view, store, service = window
    store.save("presets", {"id": "p3", "name": "本機專案", "account_id": "a", "environment_id": "local", "path": "C:/w/l",
                           "ask_account": False})
    store.save("sessions", {"id": "stale", "preset_id": "p3", "account_id": "a", "environment_id": "local", "name": "本機專案",
                            "path": "C:/w/l", "status": "running", "updated_at": runtime.now()})
    view.reload()
    view.show_page("projects")
    badges = {view.project_list.item(i).data(Qt.ItemDataRole.UserRole): view.project_list.item(i).data(0x0101)
              for i in range(view.project_list.count())}
    assert badges["p3"][0] != "運行中", "no terminal is open for it"
    assert view.home_page.running.value.text() == "0"


def test_finished_local_runs_leave_no_record_once_their_terminal_is_closed(window):
    view, store, service = window
    store.save("sessions", {"id": "old", "preset_id": "p1", "account_id": "a", "environment_id": "local", "name": "前端",
                            "path": "/w/web", "status": "ended", "updated_at": runtime.now()})
    view.reload()
    view.open_terminal("p2", "shell")
    assert not [s for s in store.list("sessions") if s["id"] == "old"]


def test_restoring_a_project_returns_to_its_workspace(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "p1", "kind": "agent", "account_id": "a"}]})
    view.reload()
    view.restore_project("p1")
    assert view.workspace.workspace_id == "one" and service.calls[-1][1].get("resume") is True
    calls = len(service.calls)
    view.open_workspace("")
    view.restore_project("p1")
    assert view.workspace.workspace_id == "one" and len(service.calls) == calls, "an open terminal is shown, not reopened"


# ----------------------------------------------------------------------------- review fixes
def restart(store, service, qtbot):
    again = MainWindow(store, service, terminal_factory=FakeTerminal)
    again._confirm = lambda *a, **k: True
    qtbot.addWidget(again, before_close_func=lambda w: w.workspace.shutdown())
    return again


def test_restore_preview_cancellation_starts_nothing_and_keeps_slots(window):
    view, store, service = window
    slots = [{"preset_id": "p1", "kind": "agent", "account_id": "a"}]
    previews = []
    view._confirm = lambda title, message, **kw: previews.append(message) or False
    service.calls.clear()
    view._restore_slots("", slots)
    assert not service.calls and view._pending_slots[""] == slots
    assert previews and "重新啟動程式" in previews[0]


def test_detach_uses_operation_channel_and_original_attachment(window):
    view, store, service = window
    view.open_terminal("p1", "agent")
    tab = view.workspace.current_tabs()[0]
    tab.info.attachment_nonce = "attachment"
    view.workspace.detach(tab.info.id)
    action, kwargs = service.calls[-1]
    assert action == "detach" and kwargs["session_id"] == tab.info.session_id
    assert kwargs["operation"] == {"attachment_nonce": "attachment"}
    assert tab.terminal.sent == [] and not tab.terminal.terminated


def test_the_default_desktop_survives_a_restart_on_another_desktop(window, qtbot):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": []})
    view.reload()
    view.open_terminal("p1", "agent")
    view.open_workspace("one")
    view.open_terminal("p2", "shell")
    view.workspace.shutdown()
    again = restart(store, service, qtbot)
    assert again.workspace.workspace_id == "one"
    again.open_terminal("p2", "shell")                    # a save happens: the default desktop must not be emptied
    assert [s["preset_id"] for s in again.prefs.default_workspace["slots"]] == ["p1"]
    again.open_workspace("")
    assert "1" in again.workspace.restore_button.text() and again.workspace.restore_button.isVisibleTo(again.workspace)


def test_separate_window_mode_keeps_the_saved_default_desktop(window, qtbot):
    view, store, service = window
    view.open_terminal("p1", "agent")
    view.workspace.shutdown()
    view.prefs.external_terminal = True
    view.prefs.save()
    again = restart(store, service, qtbot)
    again.workspace.set_layout("grid")                    # anything that saves
    assert [s["preset_id"] for s in again.prefs.default_workspace["slots"]] == ["p1"]
    assert not again.workspace.restore_button.isVisibleTo(again.workspace)


def test_a_workspace_with_a_deleted_project_still_saves_and_failures_wait(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": [{"preset_id": "gone", "kind": "agent"},
                                                                  {"preset_id": "p2", "kind": "shell"}]})
    view.reload()
    service.fail = ValueError("Cannot start this terminal")
    view.open_workspace("one")
    assert [s["preset_id"] for s in store.get("workspaces", "one")["slots"]] == ["p2"], "the failed one waits, the deleted one goes"
    assert view.workspace.restore_button.isVisibleTo(view.workspace)
    view.open_terminal("p1", "agent")
    assert [s["preset_id"] for s in store.get("workspaces", "one")["slots"]] == ["p1", "p2"], "still saved as you go"


def test_restoring_does_not_open_a_second_copy_of_an_open_terminal(window, qtbot):
    view, store, service = window
    view.open_terminal("p1", "agent")
    view.workspace.shutdown()
    again = restart(store, service, qtbot)
    again.open_terminal("p1", "agent")
    service.calls.clear()
    again.restore_pending("")
    assert service.calls == [] and len(again.workspace.current_tabs()) == 1


def test_opening_a_saved_terminal_by_hand_does_not_accumulate_restore_slots(window, qtbot):
    view, store, service = window
    view.open_terminal("p1", "agent")
    view.workspace.shutdown()
    for _ in range(3):
        view = restart(store, service, qtbot)
        assert len(view._pending_slots[""]) == 1
        view.open_terminal("p1", "agent")
        assert not view._pending_slots.get("")
        view.workspace.set_layout("grid")
        view._remember_workspaces()
        assert len(view.prefs.default_workspace["slots"]) == 1
        tab = view.workspace.current_tabs()[0]
        tab.terminal.finish(1)
        view.workspace.close_tab(tab.info.id)
        assert view.prefs.default_workspace["slots"] == []
        view.open_terminal("p1", "agent")
        view.workspace.shutdown()


def test_pending_slots_are_consumed_once_and_other_sessions_can_still_restore(window):
    view, store, service = window
    slots = [{"preset_id": "p1", "kind": "agent", "account_id": "a", "session_id": sid} for sid in ("first", "second")]
    view._pending_slots[""] = slots
    view.workspace.set_pending("", 2)
    view.open_terminal("p1", "agent")
    assert len(view._pending_slots[""]) == 1
    view._on_terminals_changed()
    view._remember_workspaces()
    assert len(view._pending_slots[""]) == 1
    view.restore_pending("")
    assert len(view.workspace.current_tabs()) == 2
    assert not view._pending_slots.get("")
    assert len(view.prefs.default_workspace["slots"]) == 2


@pytest.mark.parametrize("workspace_id", ["", "one"])
def test_discard_saved_terminals_is_confirmed_persistent_and_keeps_open_work(window, qtbot, workspace_id):
    view, store, service = window
    if workspace_id:
        store.save("workspaces", {"id": workspace_id, "name": "One", "slots": []})
        view.reload()
        view.open_workspace(workspace_id)
    view.open_terminal("p1", "agent")
    tab = view.workspace.current_tabs()[0]
    slots = [{"preset_id": "p2", "kind": "shell"}]
    view._pending_slots[workspace_id] = slots
    view.workspace.set_pending(workspace_id, 1)
    view._confirm = lambda *args, **kwargs: False
    view.discard_pending(workspace_id)
    assert view._pending_slots[workspace_id] == slots
    view._confirm = lambda *args, **kwargs: True
    calls = len(service.calls)
    action = next(action for action in view.workspace.workspace_menu_actions() if action.text == "丟棄待恢復終端…")
    assert action.enabled
    action.callback()
    assert not view._pending_slots.get(workspace_id) and workspace_id not in view.workspace._pending
    assert len(service.calls) == calls and tab.alive() and not tab.terminal.terminated
    saved = store.get("workspaces", workspace_id) if workspace_id else view.prefs.default_workspace
    assert [slot["preset_id"] for slot in saved["slots"]] == ["p1"]
    view.workspace.shutdown()
    again = restart(store, service, qtbot)
    assert [slot["preset_id"] for slot in again._pending_slots[workspace_id]] == ["p1"]


def test_duplicate_saved_slots_of_one_session_come_back_once_and_are_cleaned_when_saved(window):
    view, store, service = window
    one = {"preset_id": "p1", "kind": "agent", "account_id": "a", "session_id": "s1", "environment_id": "srv"}
    two = {**one, "session_id": "s2"}
    shell = {"preset_id": "p1", "kind": "shell", "environment_id": "srv", "terminal_profile": "bash"}
    view.prefs.default_workspace = {"layout": "tabs", "slots": [one, two, shell, dict(one), dict(two), dict(shell)]}
    view._restore_last_desktop()
    # A session is one terminal; shells have no identity, so they are never merged.
    assert [slot.get("session_id") for slot in view._pending_slots[""]] == ["s1", "s2", None, None]
    assert view.workspace._pending[""] == 4
    view._remember_workspaces()
    assert [slot.get("session_id") for slot in view.prefs.default_workspace["slots"]] == ["s1", "s2", None, None]


def test_discard_button_sits_next_to_restore_and_follows_pending_saved_terminals(window):
    view, store, service = window
    page = view.workspace
    assert page.restore_button.isHidden() and page.discard_button.isHidden()
    view._pending_slots[""] = [{"preset_id": "p1", "kind": "agent", "account_id": "a"}]
    page.set_pending("", 1)
    assert not page.restore_button.isHidden() and not page.discard_button.isHidden()
    asked = []
    view._confirm = lambda *args, **kwargs: asked.append(args) or True
    page.discard_button.click()
    assert asked and not view._pending_slots.get("") and page.discard_button.isHidden() and page.restore_button.isHidden()
    assert view.prefs.default_workspace["slots"] == []


def test_failed_restore_retries_do_not_accumulate_pending_entries(window):
    view, store, service = window
    view._pending_slots[""] = [{"preset_id": "p1", "kind": "agent", "account_id": "a"}]
    for _ in range(3):
        service.fail = ValueError("Cannot start this terminal")
        view.restore_pending("")
        assert len(view._pending_slots[""]) == 1
        assert len(view.prefs.default_workspace["slots"]) == 1


def test_unrelated_account_or_shell_does_not_consume_saved_agent_slots(window):
    view, store, service = window
    slots = [{"preset_id": "p1", "kind": "agent", "account_id": "other"}]
    view._pending_slots[""] = list(slots)
    view.open_terminal("p1", "agent")
    view.open_terminal("p1", "shell")
    assert view._pending_slots[""] == slots


def test_a_direct_ssh_run_without_its_terminal_is_not_shown_as_background(window):
    view, store, service = window
    store.save("sessions", {"id": "direct", "preset_id": "p1", "account_id": "a", "environment_id": "srv", "name": "前端",
                            "path": "/w/web", "status": "running", "persistent": False, "updated_at": runtime.now()})
    view.reload()
    status = {s["id"]: s["status"] for s in view._effective_sessions()}
    assert status["direct"] == "unknown"


def test_after_a_restart_projects_offer_to_resume(window, qtbot):
    view, store, service = window
    view.open_terminal("p1", "agent")
    view.workspace.shutdown()
    again = restart(store, service, qtbot)
    again.show_page("projects")
    badges = {again.project_list.item(i).data(Qt.ItemDataRole.UserRole): again.project_list.item(i).data(0x0101)
              for i in range(again.project_list.count())}
    assert badges["p1"][0] == "可恢復"
    from PySide6.QtWidgets import QPushButton
    assert "恢復" in {b.text() for b in again.findChildren(QPushButton)}, "Home offers Restore, not Open"
    service.calls.clear()
    again.restore_project("p1")
    assert service.calls[0][1].get("resume") is True and again.workspace.workspace_id == ""


def test_restoring_a_finished_terminal_restarts_it_on_its_own_desktop(window):
    view, store, service = window
    store.save("workspaces", {"id": "one", "name": "One", "slots": []})
    view.reload()
    view.open_workspace("one")
    view.open_terminal("p1", "agent")
    tab = view.workspace.current_tabs()[0]
    tab.info.tmux = False
    tab.terminal.finish()
    view.open_workspace("")
    view.restore_project("p1")
    assert view.workspace.workspace_id == "one"
    assert len(view.workspace.desktop_tabs("one")) == 1 and view.workspace.desktop_tabs("one")[0] is not tab
    assert view.workspace.desktop_tabs("") == []


def test_a_reload_that_cannot_start_keeps_the_old_terminal(window, monkeypatch):
    view, store, service = window
    view.open_terminal("p1", "agent")
    tab = view.workspace.current_tabs()[0]
    tab.info.tmux = False
    monkeypatch.setattr(view, "_notice", lambda *a, **k: None)
    service.fail = ValueError("Cannot start this terminal")
    view.reload_terminal(tab.info.id)
    assert view.workspace.current_tabs() == [tab] and tab.alive()


def test_deleting_a_workspace_names_its_tmux_session_and_ends_it_on_each_server(window, monkeypatch):
    view, store, service = window
    store.save("workspaces", {"id": "two", "name": "Two", "slots": []})
    store.save("sessions", {"id": "live", "name": "Web", "preset_id": "p1", "account_id": "a", "environment_id": "srv",
                            "path": "/w/web", "status": "detached", "workspace_id": "two"})
    store.save("sessions", {"id": "done", "name": "Web", "preset_id": "p1", "account_id": "a", "environment_id": "srv",
                            "path": "/w/web", "status": "ended", "workspace_id": "two"})
    view.reload()
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda title, message, **k: asked.append(message) or True)
    view.delete_workspace("two")
    assert "vibe-ws-two" in asked[0] and "伺服器" in asked[0]
    action, kwargs = service.calls[-1]
    assert action == "stop" and kwargs["session_ids"] == ["live"] and kwargs["environment_id"] == "srv"
    assert kwargs["workspace_id"] == "two"


def test_a_workspace_without_server_terminals_is_deleted_with_the_plain_question(window, monkeypatch):
    view, store, service = window
    store.save("workspaces", {"id": "two", "name": "Two", "slots": []})
    view.reload()
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda title, message, **k: asked.append(message) or True)
    view.delete_workspace("two")
    assert "tmux" not in asked[0] and not service.calls


def test_stopping_with_a_workspace_also_ends_its_tmux_session(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    fake = FakeTmux()
    monkeypatch.setattr(runtime, "tmux", fake)
    runtime.perform({"action": "stop", "session_ids": ["s"], "session_id": "s", "environment_id": "host",
                     "workspace_id": "ws1"}, tmp_path)
    assert fake.calls[-1] == ("kill-session", "-t", "=vibe-ws-ws1")


def test_a_server_launch_records_its_workspace_and_a_stop_carries_it(tmp_path):
    from vibe_launcher.service import LauncherService
    store = Store(tmp_path / "ws.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    store.save("accounts", {"id": "a", "name": "A", "tool": "claude", "auth": "api", "model": "m"})
    store.save("presets", {"id": "p", "name": "Site", "account_id": "a", "environment_id": "gpu", "path": "/w"})
    service = LauncherService(store)
    session_id = service.start("launch", preset_id="p", account_id="a", workspace_id="ws1", spawn=lambda *a: None)
    assert store.get("sessions", session_id)["workspace_id"] == "ws1"
    service.start("stop", session_ids=[session_id], workspace_id="ws1", spawn=lambda *a: None)
    jobs = sorted((tmp_path / "jobs").glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
    request = json.loads(runtime.read_private(jobs[-1]))["request"]
    assert request["action"] == "stop" and request["workspace_id"] == "ws1"
