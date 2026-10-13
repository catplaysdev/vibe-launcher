"""Managed Claude Code: hook events, quota from the status line, transcript handoff and the supervisor's safe point."""
import io
import json
import sys
import time

import pytest

from vibe_launcher import runtime, selfcmd


@pytest.fixture(autouse=True)
def english(restore_controls):
    """These tests read English messages (the suite otherwise runs with the Traditional Chinese runtime catalog)."""
    from vibe_launcher import i18n, runtime as engine
    i18n.set_language("en")
    engine._MESSAGES = {}

REAL_SUPPORTED = runtime.managed_claude_supported      # conftest replaces it for every test

ACCOUNT_A = {"id": "account-a", "name": "Account A", "tool": "claude", "auth": "subscription", "environment_id": "local"}
ACCOUNT_B = {"id": "account-b", "name": "Account B", "tool": "claude", "auth": "subscription", "environment_id": "local"}


def hook(monkeypatch, kind, data, **env):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(data).encode())))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert runtime.claude_hook([kind]) == 0


def test_hook_records_only_the_fields_it_needs(tmp_path, monkeypatch):
    events = tmp_path / "events"
    hook(monkeypatch, "event", {"hook_event_name": "Stop", "session_id": "s", "background_tasks": [{"id": 1}],
                                "last_assistant_message": "secret answer", "tool_input": {"command": "rm"}},
         VIBE_CLAUDE_CONTROL=str(events), VIBE_CLAUDE_GENERATION="3")
    [path] = list(events.glob("*.json"))
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event == {"hook_event_name": "Stop", "session_id": "s", "background_tasks": 1, "generation": "3"}


def test_hook_never_fails_the_cli(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert runtime.claude_hook(["event"]) == 0


def test_status_line_quota_is_cached_like_other_quota(tmp_path, monkeypatch):
    path = tmp_path / "quota" / "account-a__local.json"
    limits = {"five_hour": {"used_percentage": 23.5, "resets_at": 4102444800},
              "seven_day": {"used_percentage": 41, "resets_at": 4102444900}}
    hook(monkeypatch, "status", {"rate_limits": limits}, VIBE_CLAUDE_QUOTA=str(path), VIBE_CLAUDE_ACCOUNT="account-a",
         VIBE_CLAUDE_ENVIRONMENT="local")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["tool"] == "claude" and [w["used_percent"] for w in snapshot["windows"]] == [23.5, 41]
    moment = runtime._quota_timestamp(snapshot["checked_at"])
    assert runtime.quota_state(snapshot, "local", moment + 30, 60, 10)[0] == 59
    assert runtime.quota_state(snapshot, "local", moment + 3700, 60, 10) == (None, None)     # an hour old: unknown
    snapshot["windows"][0]["resets_at"] = int(moment) - 1                                        # a window has reset
    assert runtime.quota_state(snapshot, "local", moment + 30, 60, 10) == (None, None)


def test_settings_add_hooks_and_wrap_the_users_status_line(tmp_path):
    settings = runtime.claude_settings("helper", {"command": "x", "padding": 2})
    assert set(settings["hooks"]) == set(runtime.CLAUDE_EVENTS)
    entry = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    # Work-starting events block until recorded, and a hook that fails or times out blocks the action.
    assert entry == {"type": "command", "command": "helper event PreToolUse", "timeout": 10, "onFailure": "block"}
    for name in ("UserPromptSubmit", "SessionStart", "SubagentStart", "PermissionRequest"):
        assert settings["hooks"][name][0]["hooks"][0]["onFailure"] == "block"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["async"] is True
    assert settings["statusLine"] == {"type": "command", "command": "helper status", "padding": 2}
    project, profile = tmp_path / "project", tmp_path / "profile"
    (project / ".claude").mkdir(parents=True)
    profile.mkdir()
    assert runtime.claude_user_statusline(profile, project) is None
    (profile / "settings.json").write_text('{"statusLine": {"type": "command", "command": "user line"}}', encoding="utf-8")
    assert runtime.claude_user_statusline(profile, project)["command"] == "user line"
    (project / ".claude" / "settings.json").write_text('{"statusLine": {"type": "command", "command": "project line"}}', encoding="utf-8")
    assert runtime.claude_user_statusline(profile, project)["command"] == "project line"


def test_status_hook_records_quota_and_still_shows_the_users_line(tmp_path, monkeypatch, capfdbinary):
    path = tmp_path / "quota.json"
    limits = {"five_hour": {"used_percentage": 3, "resets_at": 4102444800}}
    hook(monkeypatch, "status", {"rate_limits": limits}, VIBE_CLAUDE_QUOTA=str(path), VIBE_CLAUDE_STATUSLINE="echo vibe-user-line")
    assert path.exists() and b"vibe-user-line" in capfdbinary.readouterr().out


def test_hook_command_runs_in_bash_and_powershell_without_quotes():
    command = selfcmd.hook_command("claude_hook")
    assert command.endswith("vibe_launcher.claude_hook") or command.endswith("claude_hook")
    assert "\\" not in command and (" " not in command.split(" -I ")[0] or command.startswith("powershell"))


def test_transcript_and_its_files_move_to_the_target_profile(tmp_path):
    source, target = tmp_path / "a", tmp_path / "b"
    folder = source / "projects" / "C--work"
    runtime._write_private(folder / "s1.jsonl", b'{"type":"user"}\n')
    runtime._write_private(folder / "s1" / "subagents" / "agent.jsonl", b"{}\n")
    runtime._write_private(source / "file-history" / "s1" / "v1", b"old")
    runtime._write_private(source / "todos" / "s1-agent.json", b"[]")
    runtime._write_private(target / "projects" / "C--work" / "s1.jsonl", b"stale")
    runtime.copy_claude_session(source, target, "s1")
    assert (target / "projects" / "C--work" / "s1.jsonl").read_bytes() == b'{"type":"user"}\n'
    assert (target / "projects" / "C--work" / "s1" / "subagents" / "agent.jsonl").exists()
    assert (target / "file-history" / "s1" / "v1").exists() and (target / "todos" / "s1-agent.json").exists()
    with pytest.raises(runtime.CodexControlError):
        runtime.copy_claude_session(source, target, "missing")


def test_account_level_capabilities_are_read_by_name_only(tmp_path):
    profile, project = tmp_path / "p", tmp_path / "work"
    runtime._write_private(profile / "settings.json", json.dumps({"enabledPlugins": {"a@m": True, "b@m": False}}).encode())
    runtime._write_private(profile / ".claude.json", json.dumps({
        "mcpServers": {"docs": {"type": "http", "url": "https://example.com", "headers": {"Authorization": "x"}},
                       "local": {"command": "node"}},
        "projects": {str(project).replace("\\", "/"): {"mcpServers": {"db": {"type": "sse"}}}}}).encode())
    plugins, servers = runtime.claude_capabilities(profile, project)
    assert plugins == {"a@m"} and servers == {"docs": "http", "local": "stdio", "db": "sse"}


class Process:
    def __init__(self):
        self.code = None
        self.pid = 4321
    def poll(self):
        return self.code
    def wait(self, timeout=None):
        return self.code


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    spawned = []

    def popen(argv, **kwargs):
        spawned.append((argv, kwargs["env"]["VIBE_CLAUDE_GENERATION"]))
        return Process()
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime, "prepare", lambda account, root: {})
    monkeypatch.setattr(runtime, "stop_process", lambda process: setattr(process, "code", 0))
    request = {"session_id": "terminal", "environment_id": "local", "account": ACCOUNT_A, "path": str(tmp_path)}
    accounts = {"account-a": ACCOUNT_A, "account-b": ACCOUNT_B}
    item = runtime.ClaudeSupervisor(request, tmp_path, accounts, lambda account, args, prompt, live: [account, *args] + ([prompt] if prompt else []))
    item.start()
    return item, spawned


def opened(item, session, model="claude-sonnet-5-5"):
    """What Claude Code 2.1.296 reports when a session starts or resumes: SessionStart (no model), then the status line."""
    emit(item, "SessionStart", session_id=session)
    if model is not None:
        emit(item, "VibeStatus", session_id=session, model=model)


def emit(item, name, **values):
    runtime.write_json(item._events / ("%020d-1.json" % time.time_ns()),
                       dict(values, hook_event_name=name, generation=str(item._generation)))


def test_safe_point_needs_a_settled_turn_without_tools_or_subagents(supervisor):
    item, spawned = supervisor
    assert spawned[0][0][:2] == ["account-a", "--session-id"]
    assert not item.tick()["can_switch"]                      # nothing reported yet
    emit(item, "SessionStart", session_id=item._session, model="haiku", effort="low", permission_mode="default")
    assert item.tick()["can_switch"]
    emit(item, "UserPromptSubmit")
    emit(item, "PreToolUse", tool_use_id="t1")
    assert not item.tick()["can_switch"]
    emit(item, "PostToolUse", tool_use_id="t1")
    emit(item, "SubagentStart", agent_id="a1")
    emit(item, "Stop", background_tasks=0)
    assert not item.tick()["can_switch"]                      # a subagent still runs
    emit(item, "SubagentStop", agent_id="a1")
    emit(item, "Stop", background_tasks=1)
    assert not item.tick()["can_switch"]                      # a background task still runs
    emit(item, "UserPromptSubmit")
    emit(item, "Stop", background_tasks=0)
    snapshot = item.tick()
    assert snapshot["can_switch"] and snapshot["live"] == {"model": "haiku", "effort": "low", "permission_mode": "default"}


def test_switch_ends_the_cli_moves_the_transcript_and_resumes_the_same_session(supervisor, tmp_path):
    item, spawned = supervisor
    session = item._session
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_A) / "projects" / "C--work" / (session + ".jsonl"), b"{}\n")
    opened(item, session)
    emit(item, "UserPromptSubmit")
    emit(item, "StopFailure", error="rate_limit")
    item.tick()
    item.request_switch("account-b")
    snapshot = item.tick()
    # Started, but not yet the active account: it has to report the session first.
    assert snapshot["account"] == "account-a" and snapshot["state"] == "switching" and snapshot["pending_account"] == "account-b"
    assert spawned[-1][0] == ["account-b", "--resume", session] and spawned[-1][1] == "2"
    assert (runtime.profile_dir(tmp_path, ACCOUNT_B) / "projects" / "C--work" / (session + ".jsonl")).exists()
    assert not runtime.claude_handoff_lock(item._events).exists()
    emit(item, "SessionStart", session_id=session)
    assert item.tick()["account"] == "account-a"                 # the status line has not reported the model yet
    emit(item, "VibeStatus", session_id=session, model="claude-sonnet-5-5")
    snapshot = item.tick()
    assert snapshot["account"] == "account-b" and snapshot["thread_id"] == session and snapshot["pending_account"] is None
    assert snapshot["continuation_ready"] and snapshot["last_error_kind"] == "usageLimitExceeded"
    item.resume_work()
    assert spawned[-1][0] == ["account-b", "--resume", session, runtime.CLAUDE_CONTINUE]


def test_old_generation_events_and_user_exit(supervisor):
    item, _ = supervisor
    runtime.write_json(item._events / "00000000000000000001-1.json", {"hook_event_name": "UserPromptSubmit", "generation": "0"})
    emit(item, "SessionStart", session_id=item._session)
    assert item.tick()["can_switch"]                          # the stale prompt from another run was ignored
    item._process.code = 0
    assert item.finished() == 0


def test_account_differences_are_offered_to_the_user(supervisor, tmp_path, monkeypatch):
    item, _ = supervisor
    request = {"session_id": "terminal", "environment_id": "local", "account": ACCOUNT_A, "path": str(tmp_path / "work")}
    controller = runtime.ManagedClaudeSession(request, tmp_path, item)
    controller.accounts.update({"account-b": ACCOUNT_B})
    monkeypatch.setattr(runtime, "check_login", lambda *args: True)
    monkeypatch.setattr(runtime, "sync_user_data", lambda *args, **kwargs: {})
    for account, plugins in ((ACCOUNT_A, {"review@m": True}), (ACCOUNT_B, {})):
        runtime._write_private(runtime.profile_dir(tmp_path, account) / "settings.json",
                               json.dumps({"enabledPlugins": plugins, "theme": "dark"}).encode())
    with pytest.raises(runtime.CodexHandoffBlocked) as blocked:
        controller.validate_target("account-b", {})
    assert blocked.value.gaps == [{"kind": "plugin", "name": "review"}]
    controller._allow_gaps.add("account-b")
    controller.validate_target("account-b", {})
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_B) / "settings.json",
                           json.dumps({"enabledPlugins": {}, "theme": "light"}).encode())
    with pytest.raises(runtime.CodexControlError, match="settings"):
        controller.validate_target("account-b", {})
    # A default model only one profile sets is overridden by the conversation's own model at the restart.
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_B) / "settings.json",
                           json.dumps({"enabledPlugins": {}, "theme": "dark", "model": "opus"}).encode())
    with pytest.raises(runtime.CodexControlError, match="settings"):
        controller.validate_target("account-b", {})
    controller.validate_target("account-b", {"model": "claude-haiku-5-5"})
    controller._closed.set()


def test_a_pool_holds_one_cli(tmp_path):
    with pytest.raises(ValueError):
        runtime.validate_control_accounts([ACCOUNT_A, dict(ACCOUNT_B, tool="codex")], "local")
    assert runtime.validate_control_accounts([ACCOUNT_A, ACCOUNT_B], "local") == {"account-a", "account-b"}


def test_simulated_quota_failure_marks_a_settled_turn(supervisor):
    item, _ = supervisor
    assert not item.simulate_quota_failure()                  # nothing settled yet
    emit(item, "SessionStart", session_id=item._session)
    emit(item, "UserPromptSubmit")
    emit(item, "Stop", background_tasks=0)
    item.tick()
    assert item.simulate_quota_failure() and item.snapshot()["last_error_kind"] == "usageLimitExceeded"


def test_account_synced_and_cli_internal_skills_are_neither_shared_nor_compared(tmp_path):
    first, second = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("one", "two"))
    for account, synced in ((first, "org-a_skill"), (second, "org-b_skill")):
        profile = runtime.profile_dir(tmp_path, account)
        (profile / "skills" / "synced" / synced).mkdir(parents=True)
        (profile / "skills" / "synced" / synced / "SKILL.md").write_text(synced, encoding="utf-8")
        (profile / "skills" / ".trash" / synced).mkdir(parents=True)                 # Claude's removed-skill bin
        (profile / "skills" / ".trash" / synced / "SKILL.md").write_text(synced, encoding="utf-8")
        (profile / "skills" / "mine").mkdir(parents=True)
        (profile / "skills" / "mine" / "SKILL.md").write_text("shared", encoding="utf-8")
    assert runtime.claude_profile_fingerprint(first, tmp_path) == runtime.claude_profile_fingerprint(second, tmp_path)
    files = runtime._shareable_files(runtime.profile_dir(tmp_path, first), "claude")
    assert files == [runtime.Path("skills/mine/SKILL.md")]
    (runtime.profile_dir(tmp_path, second) / "skills" / "mine" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert runtime.claude_profile_fingerprint(first, tmp_path) != runtime.claude_profile_fingerprint(second, tmp_path)


def test_a_handoff_target_skips_the_first_run_wizard_and_keeps_the_folder_trusted(tmp_path):
    source, target = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("from", "to"))
    project = "/home/user/projects/hello"
    runtime.write_json(runtime.profile_dir(tmp_path, source) / ".claude.json", {
        "hasCompletedOnboarding": True, "projects": {project: {"hasTrustDialogAccepted": True}}})
    runtime.write_json(runtime.profile_dir(tmp_path, target) / ".claude.json", {
        "oauthAccount": {"emailAddress": "user@example.com"}, "projects": {"/elsewhere": {"hasTrustDialogAccepted": False}}})
    runtime.carry_claude_onboarding(source, target, tmp_path, project)
    data = runtime.json.loads(runtime.read_private(runtime.profile_dir(tmp_path, target) / ".claude.json"))
    assert data["hasCompletedOnboarding"] is True
    assert data["projects"][project] == {"hasTrustDialogAccepted": True}
    assert data["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": False}       # other folders untouched
    assert data["oauthAccount"] == {"emailAddress": "user@example.com"}


def test_an_untrusted_folder_is_not_trusted_for_the_target(tmp_path):
    source, target = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("from", "to"))
    runtime.write_json(runtime.profile_dir(tmp_path, source) / ".claude.json", {"projects": {}})
    runtime.write_json(runtime.profile_dir(tmp_path, target) / ".claude.json", {"oauthAccount": {"x": 1}})
    runtime.carry_claude_onboarding(source, target, tmp_path, "/w")
    data = runtime.json.loads(runtime.read_private(runtime.profile_dir(tmp_path, target) / ".claude.json"))
    assert "projects" not in data and data["hasCompletedOnboarding"] is True


def test_one_time_notices_the_person_saw_are_not_shown_again_on_the_target(tmp_path):
    source, target = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("from", "to"))
    runtime.write_json(runtime.profile_dir(tmp_path, source) / ".claude.json", {
        "hasSeenAutoModeEntryWarning": True, "hasSeenAutoDefaultNotice": True, "lastReleaseNotesSeen": "2.1.296",
        "hasSeenSomethingElse": False, "oauthAccount": {"emailAddress": "source@example.com"}, "userID": "source"})
    runtime.write_json(runtime.profile_dir(tmp_path, target) / ".claude.json", {
        "oauthAccount": {"emailAddress": "target@example.com"}, "hasCompletedOnboarding": True, "hasSeenAutoDefaultNotice": True})
    runtime.carry_claude_onboarding(source, target, tmp_path, "/w")
    data = runtime.json.loads(runtime.read_private(runtime.profile_dir(tmp_path, target) / ".claude.json"))
    assert data["hasSeenAutoModeEntryWarning"] is True and data["lastReleaseNotesSeen"] == "2.1.296"
    assert "hasSeenSomethingElse" not in data                                   # only what was really seen
    assert data["oauthAccount"] == {"emailAddress": "target@example.com"} and "userID" not in data   # identity stays


def test_a_switch_before_anything_was_said_starts_the_conversation_fresh_on_the_target(supervisor, tmp_path):
    item, spawned = supervisor
    session = item._session
    opened(item, session)
    assert item.tick()["can_switch"]
    item.request_switch("account-b")                    # no transcript yet: --resume would find no conversation
    item.tick()
    assert spawned[-1][0] == ["account-b", "--session-id", session]
    opened(item, session)
    snapshot = item.tick()
    assert snapshot["account"] == "account-b" and snapshot["thread_id"] == session
    emit(item, "UserPromptSubmit")
    emit(item, "Stop", background_tasks=0)
    item.tick()
    item.request_switch("account-a")                     # once something was said, later starts resume it
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_B) / "projects" / "C--work" / (session + ".jsonl"), b"{}\n")
    item.tick()
    assert spawned[-1][0] == ["account-a", "--resume", session]


# --- synchronous gate: a prompt or tool is either known to the handoff or refused -------------------------------------
def run_hook(monkeypatch, events, name, generation="1", **values):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(dict(values, hook_event_name=name)).encode())))
    monkeypatch.setenv("VIBE_CLAUDE_CONTROL", str(events))
    monkeypatch.setenv("VIBE_CLAUDE_GENERATION", generation)
    return runtime.claude_hook(["event", name])


def test_a_prompt_during_the_handoff_lock_is_refused_and_does_not_count(supervisor, monkeypatch, capsys):
    item, _ = supervisor
    emit(item, "SessionStart", session_id=item._session)
    item.tick()
    assert item._quiesce()                          # the handoff holds the lock
    assert run_hook(monkeypatch, item._events, "UserPromptSubmit", str(item._generation)) == 2
    assert run_hook(monkeypatch, item._events, "PreToolUse", str(item._generation), tool_use_id="t9") == 2
    assert "handoff" in capsys.readouterr().err
    assert run_hook(monkeypatch, item._events, "Stop", str(item._generation)) == 0     # ending work is never held
    item._release()
    item._read_events()
    assert item.snapshot()["can_switch"] and not item._tools
    assert run_hook(monkeypatch, item._events, "UserPromptSubmit", str(item._generation)) == 0    # lock released


def test_a_hook_that_cannot_record_blocks_only_work_starting_events(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    events = blocker / "events"                     # cannot be created: recording fails
    assert run_hook(monkeypatch, events, "UserPromptSubmit") == 2
    assert run_hook(monkeypatch, events, "PreToolUse", tool_use_id="t") == 2
    assert run_hook(monkeypatch, events, "Stop") == 0
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert runtime.claude_hook(["event", "PreToolUse"]) == 2          # unreadable input of a gated event


def test_a_lock_left_by_a_dead_supervisor_does_not_block(tmp_path, monkeypatch):
    events = tmp_path / "events"
    runtime.write_json(runtime.claude_handoff_lock(events), {"pid": 999999})
    monkeypatch.setattr(runtime, "process_alive", lambda pid: False)
    assert run_hook(monkeypatch, events, "UserPromptSubmit") == 0


def test_work_that_starts_while_the_target_is_checked_keeps_the_source(supervisor):
    item, spawned = supervisor
    opened(item, item._session)
    item.tick()
    item._validate_target = lambda target, live: emit(item, "UserPromptSubmit")      # a prompt slips in meanwhile
    item.request_switch("account-b")
    snapshot = item.tick()
    assert len(spawned) == 1 and item._process.code is None          # the source CLI was never stopped
    assert snapshot["account"] == "account-a" and snapshot["pending_account"] == "account-b"
    assert snapshot["state"] == "running" and not runtime.claude_handoff_lock(item._events).exists()


def test_a_late_tool_event_found_under_the_lock_keeps_the_source(supervisor):
    item, spawned = supervisor
    emit(item, "SessionStart", session_id=item._session)
    item.tick()
    emit(item, "PreToolUse", tool_use_id="late")        # recorded before the lock, not yet read
    item.request_switch("account-b")
    item._safe = lambda: True                            # the controller still believed the session idle
    item.tick()
    assert len(spawned) == 1 and item._process.code is None and item._account == "account-a"


def failing_target(supervisor, monkeypatch, code=None, raises=False):
    item, spawned = supervisor
    original = runtime.subprocess.Popen

    def popen(argv, **kwargs):
        if argv[0] == "account-b" and raises:
            raise OSError("cannot start")
        process = original(argv, **kwargs)
        if argv[0] == "account-b":
            process.code = code
        return process
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    opened(item, item._session)
    item.tick()
    assert item._live["model"] == "claude-sonnet-5-5"                   # the source's model, learned from its status line
    item.request_switch("account-b")
    item.tick()
    return item, spawned


@pytest.mark.parametrize("code, raises", [(1, False), (None, True)])
def test_a_target_that_fails_to_start_brings_back_the_source(supervisor, monkeypatch, code, raises):
    item, spawned = failing_target(supervisor, monkeypatch, code, raises)
    snapshot = item.tick()
    assert spawned[-1][0][0] == "account-a" and snapshot["account"] == "account-a"
    assert snapshot["pending_account"] is None and "original account" in snapshot["reason"]
    assert item._process.code is None                    # only the restarted source runs
    emit(item, "SessionStart", session_id=item._session)
    assert item.tick()["state"] == "idle"


def test_a_target_that_never_reports_the_session_is_replaced_by_the_source(supervisor, monkeypatch):
    item, spawned = failing_target(supervisor, monkeypatch)
    target = item._process
    assert item.tick()["account"] == "account-a"             # still waiting for the target
    item._verifying["until"] = 0
    snapshot = item.tick()
    assert target.code == 0 and spawned[-1][0][0] == "account-a" and snapshot["account"] == "account-a"


@pytest.mark.parametrize("reported, committed", [("claude-opus-5-5", False), ("claude-sonnet-5-5", True), (None, False),
                                                 ("claude-sonnet-5", False), ("sonnet", False), ("Claude-Sonnet-5-5", False)])
def test_a_target_with_another_or_no_model_is_not_committed(supervisor, monkeypatch, reported, committed):
    """Exactly the source's full model id: a near id ("claude-sonnet-5"), an alias or a missing model is not verified."""
    item, spawned = failing_target(supervisor, monkeypatch)            # the source ran "sonnet"
    opened(item, item._session, reported)
    if reported is None:
        item._verifying["until"] = 0                         # no status line report within the limit
    snapshot = item.tick()
    assert (snapshot["account"] == "account-b") is committed and spawned[-1][0][0] == ("account-b" if committed else "account-a")


def test_when_even_the_source_cannot_start_the_checkpoint_needs_the_user(supervisor, monkeypatch):
    item, spawned = failing_target(supervisor, monkeypatch, code=1)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("gone")))
    snapshot = item.tick()
    assert snapshot["state"] == "needs_attention" and snapshot["account"] == "account-a" and not item._ours
    assert item.finished() == 1                          # the worker ends instead of waiting forever


@pytest.mark.parametrize("version, supported", [(b"2.1.295 (Claude Code)", True), (b"2.1.294 (Claude Code)", False),
                                                (b"unknown", False)])
def test_safe_management_needs_a_claude_code_that_blocks_failed_hooks(tmp_path, monkeypatch, version, supported):
    def run(argv, **kwargs):
        output = b"--session-id --settings --resume" if argv[-1] == "--help" else version
        return runtime.subprocess.CompletedProcess(argv, 0, output)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert REAL_SUPPORTED("claude", {}, tmp_path) is supported


@pytest.mark.parametrize("reported", ["another-session", None])
def test_a_target_that_opened_another_conversation_is_rolled_back(supervisor, monkeypatch, reported):
    """Regression: a target reporting a different (or no) session id was accepted as a successful switch."""
    item, spawned = failing_target(supervisor, monkeypatch)
    expected = item._session
    emit(item, "SessionStart", **({"session_id": reported} if reported else {}))
    emit(item, "VibeStatus", session_id=reported or "x", model="claude-sonnet-5-5")
    snapshot = item.tick()
    assert snapshot["account"] == "account-a" and snapshot["pending_account"] is None
    assert spawned[-1][0][0] == "account-a" and snapshot["thread_id"] == expected
    assert "original account" in snapshot["reason"]


def test_a_backlog_beyond_one_batch_postpones_the_handoff(supervisor, monkeypatch):
    """Regression: with more events queued than one read takes, a PreToolUse left unread let the handoff stop the CLI."""
    item, spawned = supervisor
    opened(item, item._session)
    item.tick()
    monkeypatch.setattr(runtime, "CLAUDE_EVENT_BATCH", 3)
    for index in range(6):                       # one batch in tick, one under the lock, the tool still unread
        runtime.write_json(item._events / ("%020d-1.json" % index), {"hook_event_name": "PostModelSwitch",
                                                                       "generation": str(item._generation)})
    runtime.write_json(item._events / ("%020d-1.json" % 9), {"hook_event_name": "PreToolUse", "tool_use_id": "late",
                                                               "generation": str(item._generation)})
    item._safe = lambda: True                    # the controller had not yet seen the backlog
    item.request_switch("account-b")
    snapshot = item.tick()
    assert len(spawned) == 1 and item._process.code is None and snapshot["account"] == "account-a"
    assert not runtime.claude_handoff_lock(item._events).exists() and snapshot["pending_account"] == "account-b"
    del item._safe
    item.tick()
    item.tick()                                  # the rest of the queue is read: a tool is running, still no switch
    assert "late" in item._tools and len(spawned) == 1


def test_the_status_line_records_session_and_model_once_per_change(tmp_path, monkeypatch):
    events = tmp_path / "control" / "events"
    data = {"session_id": "s1", "model": {"id": "claude-sonnet-5-5", "display_name": "Sonnet"}}
    for _ in range(2):
        hook(monkeypatch, "status", data, VIBE_CLAUDE_CONTROL=str(events), VIBE_CLAUDE_GENERATION="2")
    hook(monkeypatch, "status", dict(data, model={"id": "claude-opus-5-5"}), VIBE_CLAUDE_CONTROL=str(events),
         VIBE_CLAUDE_GENERATION="2")
    recorded = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(events.glob("*.json"))]
    assert recorded == [{"session_id": "s1", "model": "claude-sonnet-5-5", "hook_event_name": "VibeStatus", "generation": "2"},
                        {"session_id": "s1", "model": "claude-opus-5-5", "hook_event_name": "VibeStatus", "generation": "2"}]


def test_a_target_whose_status_line_reports_another_session_is_rolled_back(supervisor, monkeypatch):
    item, spawned = failing_target(supervisor, monkeypatch)
    emit(item, "SessionStart", session_id=item._session)
    emit(item, "VibeStatus", session_id="another-session", model="claude-sonnet-5-5")
    snapshot = item.tick()
    assert snapshot["account"] == "account-a" and spawned[-1][0][0] == "account-a"


def test_a_source_whose_model_is_not_proven_keeps_its_account(supervisor, tmp_path):
    """Regression: the source (account default Haiku) had not reported its model yet; the switch used the target's
    default (Opus) and committed. Without the source's own model report nothing is stopped and the user is told."""
    item, spawned = supervisor
    item._accounts = {"account-a": dict(ACCOUNT_A, model="claude-haiku-5-5"), "account-b": dict(ACCOUNT_B, model="claude-opus-5-5")}
    emit(item, "SessionStart", session_id=item._session)          # no model, no status line yet
    item.tick()
    item.request_switch("account-b")
    snapshot = item.tick()
    assert len(spawned) == 1 and item._process.code is None and snapshot["account"] == "account-a"
    assert snapshot["pending_account"] == "account-b" and "current model" in snapshot["reason"]
    emit(item, "VibeStatus", session_id=item._session, model="claude-haiku-5-5")
    item.tick()                                                   # proven now: the target starts on the source's model
    argv = spawned[-1][0]
    assert argv[0] == "account-b" and item._verifying["expected_model"] == "claude-haiku-5-5"
    emit(item, "SessionStart", session_id=item._session)
    emit(item, "VibeStatus", session_id=item._session, model="claude-opus-5-5")    # the target's own default
    assert item.tick()["account"] == "account-a" and spawned[-1][0][0] == "account-a"


def test_a_model_switch_needs_a_new_report_before_handing_off(supervisor):
    item, spawned = supervisor
    opened(item, item._session)
    emit(item, "PostModelSwitch", model="opus")                   # an alias: not the evidence itself
    item.tick()
    item.request_switch("account-b")
    item.tick()
    assert len(spawned) == 1 and item.snapshot()["pending_account"] == "account-b"
    emit(item, "VibeStatus", session_id=item._session, model="claude-opus-5-5")
    item.tick()
    assert spawned[-1][0][0] == "account-b" and item._verifying["expected_model"] == "claude-opus-5-5"


def recording_commands(item):
    started = []
    item._command_for = lambda account, args, prompt, live: started.append((account, dict(live))) or [account, *args]
    return started


@pytest.mark.parametrize("target_starts", [True, False])
def test_settings_changed_during_the_target_check_are_confirmed_before_stopping(supervisor, monkeypatch, target_starts):
    """Regression: the user switched Sonnet -> Opus while the target was checked; the target started on Opus but was
    verified against the stale Sonnet, failed, and the rollback restarted Sonnet, undoing the user's choice."""
    item, spawned = supervisor
    started = recording_commands(item)
    opened(item, item._session, "claude-sonnet-5-5")
    item.tick()

    def user_switches_model(target, live):
        assert live["model"] == "claude-sonnet-5-5"
        emit(item, "PostModelSwitch", model="opus")
        emit(item, "VibeStatus", session_id=item._session, model="claude-opus-5-5")
    item._validate_target = user_switches_model
    item.request_switch("account-b")
    snapshot = item.tick()
    # Read under the lock: the snapshot changed, so the source keeps running and the lock is released.
    assert not started and item._process.code is None and snapshot["account"] == "account-a"
    assert snapshot["pending_account"] == "account-b" and not runtime.claude_handoff_lock(item._events).exists()
    checked = []
    item._validate_target = lambda target, live: checked.append(live["model"])
    if not target_starts:
        original = item._command_for
        item._command_for = lambda account, args, prompt, live: (_ for _ in ()).throw(OSError("no")) if account == "account-b" \
            else original(account, args, prompt, live)
    item.tick()                                          # checked again with the new settings, then handed off
    assert checked == ["claude-opus-5-5"]
    if target_starts:
        assert started == [("account-b", started[0][1])] and started[0][1]["model"] == "claude-opus-5-5"
        assert item._verifying["expected_model"] == "claude-opus-5-5"
        opened(item, item._session, "claude-opus-5-5")
        assert item.tick()["account"] == "account-b"
    else:                                                # the rollback keeps the user's Opus, never the stale Sonnet
        assert started[-1][0] == "account-a" and started[-1][1]["model"] == "claude-opus-5-5"
        assert item.snapshot()["account"] == "account-a"
