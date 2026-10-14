"""User data in profiles: native discovery, shared extensions, project history and imports.

- native user data: Native user data is discovered automatically in the execution environment; credentials stay private.
- shared extensions: A launcher profile is isolated, but the user's own skills / hooks / agents must still work inside it.
- project history share: Conversations of one project follow it across the launcher's accounts of the same tool (copies, never credentials).
- history import
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from vibe_launcher import runtime
from vibe_launcher.store import Store


# --- native user data -------------------------------------------------------------------------------------------------
CLAUDE = {"id": "a", "tool": "claude", "auth": "subscription"}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def link_dir(link, target):
    """A directory link the way users make them: a junction on Windows (no rights needed), a symlink elsewhere."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory links unavailable")


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_automatic_refresh_uses_native_data(tmp_path, tool):
    account = {"id": "a", "tool": tool, "auth": "subscription"}
    home, root = tmp_path / "home", tmp_path / "launcher"
    source = runtime.user_config_dir(tool, home)
    write(source / "skills/review/SKILL.md", "# User skill")
    write(source / "auth.json", "must not copy")
    relative = "projects/site/conversation.jsonl" if tool == "claude" else "sessions/rollout-example.jsonl"
    first = {"type": "user", "message": {"content": "hello"}} if tool == "claude" else {
        "type": "session_meta", "payload": {"id": "example", "cwd": "/work/site"}}
    transcript = json.dumps(first) + "\n"
    write(source / relative, transcript)
    summary = runtime.sync_user_data(account, root, "/work/site", home)
    target = runtime.profile_dir(root, account)
    assert (target / "skills/review/SKILL.md").read_text() == "# User skill"
    assert (target / relative).read_text() == transcript
    assert not (target / "auth.json").exists()
    assert summary["history"]["copied"] == 1
    assert source.joinpath(relative).read_text() == transcript
    assert runtime.sync_user_data(account, root, "/work/site", home)["history"]["copied"] == 0


def test_removed_extensions_are_cleaned_only_if_unmodified(tmp_path):
    account = {"id": "a", "tool": "claude", "auth": "subscription"}
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir("claude", home)
    old = write(source / "skills/old/SKILL.md", "old")
    edited = write(source / "skills/edited/SKILL.md", "original")
    runtime.sync_user_data(account, root, "/work/site", home)
    target = runtime.profile_dir(root, account)
    write(target / "skills/edited/SKILL.md", "personal changes")
    write(target / "skills/own/SKILL.md", "own")
    old.unlink()
    edited.unlink()
    summary = runtime.sync_user_data(account, root, "/work/site", home)
    assert summary["extensions"]["removed"] == 1
    assert not (target / "skills/old/SKILL.md").exists()
    assert (target / "skills/edited/SKILL.md").read_text() == "personal changes"
    assert (target / "skills/own/SKILL.md").read_text() == "own"


def test_project_plugin_scope_and_local_disable_are_honored(tmp_path, monkeypatch):
    source = tmp_path / "home/.claude"
    project = tmp_path / "site"
    project.mkdir()
    plugin = source / "plugins/cache/project-plugin"
    write(plugin / "plugin.json", "{}")
    write(source / "plugins/installed_plugins.json", json.dumps({"plugins": {"demo@market": [
        {"scope": "project", "projectPath": str(project), "installPath": str(plugin)}]}}))
    write(source / "settings.json", "{}")
    monkeypatch.setattr(runtime, "user_config_dir", lambda *args: source)
    account = {"tool": "claude", "share_extensions": True}
    assert runtime.user_plugin_dirs(account, project=project) == [str(plugin)]
    assert runtime.user_plugin_dirs(account, project=tmp_path / "other") == []
    write(project / ".claude/settings.local.json", '{"enabledPlugins": {"demo@market": false}}')
    assert runtime.user_plugin_dirs(account, project=project) == []


def test_codex_existing_index_is_used_without_copying_paginated_rollouts(tmp_path, monkeypatch):
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir("codex", home)
    write(source / "state_5.sqlite", "native state")
    write(source / "sessions/rollout-indexed.jsonl", json.dumps({
        "type": "session_meta", "payload": {"history_mode": "paginated"}}) + "\n")
    monkeypatch.setattr(runtime, "user_config_dir", lambda *args: source)
    account = {"id": "a", "tool": "codex", "auth": "subscription"}
    command = runtime.build_cli(account, "launch", root)
    assert "sqlite_home=" + json.dumps(str(source)) in command
    assert runtime.isolated_env(account, root, {})["CODEX_HOME"] == str(runtime.profile_dir(root, account))
    assert runtime.sync_user_data(account, root, "/work/site")["history"]["skipped"] == 0
    assert not (runtime.profile_dir(root, account) / "sessions").exists()


def test_native_transcript_updates_preserve_a_locally_continued_conversation(tmp_path):
    root, home = tmp_path / "data", tmp_path / "home"
    account = {"id": "a", "tool": "claude", "auth": "subscription"}
    source = runtime.user_config_dir("claude", home) / "projects/site/session.jsonl"
    original = '{"type":"user","message":{"content":"first"}}\n'
    write(source, original)
    runtime.sync_user_data(account, root, "/work/site", home)
    target = runtime.profile_dir(root, account) / "projects/site/session.jsonl"
    extended = original + '{"type":"user","message":{"content":"second"}}\n'
    write(source, extended)
    runtime.sync_user_data(account, root, "/work/site", home)
    assert target.read_text() == extended
    write(target, extended + '{"type":"user","message":{"content":"local"}}\n')
    write(source, extended + '{"type":"user","message":{"content":"native"}}\n')
    runtime.sync_user_data(account, root, "/work/site", home)
    assert '"local"' in target.read_text() and '"native"' not in target.read_text()


def test_project_refresh_includes_native_conversations(tmp_path):
    account = {"id": "a", "tool": "codex"}
    home, root = tmp_path / "home", tmp_path / "data"
    source = runtime.user_config_dir("codex", home)
    for name, cwd in [("mine", "/work/site"), ("other", "/work/other")]:
        write(source / f"sessions/rollout-{name}.jsonl", json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n")
    assert runtime.sync_project_history(account, root, "/work/site", home)["copied"] == 1
    target = runtime.profile_dir(root, account)
    assert (target / "sessions/rollout-mine.jsonl").exists()
    assert not (target / "sessions/rollout-other.jsonl").exists()


def test_native_skill_directory_links_are_materialized_as_regular_files(tmp_path):
    account = {"id": "a", "tool": "claude", "auth": "subscription"}
    root, home = tmp_path / "data", tmp_path / "home"
    skill = tmp_path / "shared-skill"
    write(skill / "SKILL.md", "Shared skill")
    base = runtime.user_config_dir("claude", home) / "skills"
    base.mkdir(parents=True)
    try:
        (base / "review").symlink_to(skill, target_is_directory=True)
        (skill / "loop").symlink_to(skill, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    runtime.sync_user_data(account, root, "/work/site", home)
    target = runtime.profile_dir(root, account) / "skills/review/SKILL.md"
    assert target.read_text() == "Shared skill"
    runtime.assert_private_path(target)


def test_skill_alias_cannot_import_a_credential_file(tmp_path):
    account = {"id": "a", "tool": "claude", "auth": "subscription"}
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir("claude", home)
    credential = write(source / "auth.json", "credential fixture")
    skill = source / "skills/alias"
    skill.mkdir(parents=True)
    try:
        (skill / "SKILL.md").symlink_to(credential)
    except OSError:
        pytest.skip("symlinks unavailable")
    runtime.sync_user_data(account, root, "/work/site", home)
    assert not (runtime.profile_dir(root, account) / "skills/alias/SKILL.md").exists()


def test_cleanup_never_uses_a_forged_manifest_to_delete_credentials(tmp_path):
    import hashlib
    account = {"id": "a", "tool": "claude", "auth": "subscription"}
    root, home = tmp_path / "data", tmp_path / "home"
    target = runtime.profile_dir(root, account)
    credential = write(target / "auth.json", "credential fixture")
    digest = hashlib.sha256(credential.read_bytes()).hexdigest()
    write(target / runtime.SHARE_MANIFEST, json.dumps({"files": ["auth.json"], "file_hashes": {"auth.json": digest}}))
    runtime.sync_user_data(account, root, "/work/site", home)
    assert credential.read_text() == "credential fixture"


def test_a_live_profile_transcript_is_not_replaced_by_a_second_launch(tmp_path):
    account = {"id": "a", "tool": "claude", "auth": "subscription"}
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir("claude", home) / "projects/site/session.jsonl"
    original = '{"type":"user"}\n'
    write(source, original)
    runtime.sync_user_data(account, root, "/work/site", home)
    write(source, original + '{"type":"assistant"}\n')
    write(root / "sessions/other.json", json.dumps({"session_id": "other", "account": account, "status": "unknown"}))
    runtime.sync_user_data(account, root, "/work/site", home, session_id="current")
    target = runtime.profile_dir(root, account) / "projects/site/session.jsonl"
    assert target.read_text() == original


# ------------------------------------------------------------------ a refresh never keeps the CLI from opening
def test_links_in_the_profile_are_left_untouched_and_reported(tmp_path):
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir("claude", home)
    write(source / "skills/review/SKILL.md", "native skill")
    write(source / "commands/ship.md", "ship")
    elsewhere = write(tmp_path / "elsewhere/mine.md", "kept").parent
    target = runtime.profile_dir(root, CLAUDE)
    target.mkdir(parents=True)
    link_dir(target / "skills", elsewhere)
    summary = runtime.sync_user_data(CLAUDE, root, "/work/site", home)
    assert summary["failed"] == [] and summary["extensions"]["linked"] == ["skills"]
    assert (target / "commands/ship.md").read_text() == "ship"
    assert [p.name for p in elsewhere.iterdir()] == ["mine.md"]          # nothing was written through the link


def test_cleanup_never_deletes_through_a_profile_link(tmp_path):
    root, home = tmp_path / "data", tmp_path / "home"
    write(runtime.user_config_dir("claude", home) / "CLAUDE.md", "notes")
    elsewhere = tmp_path / "elsewhere"
    victim = write(elsewhere / "old/SKILL.md", "outside")
    target = runtime.profile_dir(root, CLAUDE)
    target.mkdir(parents=True)
    link_dir(target / "skills", elsewhere)
    write(target / runtime.SHARE_MANIFEST, json.dumps({"files": ["skills/old/SKILL.md"], "file_hashes": {
        "skills/old/SKILL.md": hashlib.sha256(b"outside").hexdigest()}}))
    runtime.sync_user_data(CLAUDE, root, "/work/site", home)
    assert victim.read_text() == "outside"


def worker_request(tmp_path):
    return {"action": "worker", "account": dict(CLAUDE), "session_id": "s", "environment_id": "local", "path": str(tmp_path)}


def test_the_cli_still_starts_when_the_refresh_fails(tmp_path, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise ValueError("Launcher data cannot use symlinks or reparse points")
    monkeypatch.setattr(runtime, "_MESSAGES", {})
    monkeypatch.setattr(runtime, "sync_user_data", broken)
    monkeypatch.setattr(runtime, "build_cli", lambda *args: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(runtime, "emit", lambda *args: None)
    assert runtime.run_worker(worker_request(tmp_path), tmp_path / "state", dict(os.environ)) == 0
    assert "could not be refreshed" in capsys.readouterr().out


def test_profile_links_are_named_in_the_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "_MESSAGES", {})
    monkeypatch.setattr(runtime, "sync_user_data", lambda *a, **k: {"extensions": {"linked": ["CLAUDE.md", "skills"]},
                                                                     "history": {}, "failed": []})
    monkeypatch.setattr(runtime, "build_cli", lambda *args: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(runtime, "emit", lambda *args: None)
    runtime.run_worker(worker_request(tmp_path), tmp_path / "state", dict(os.environ))
    out = capsys.readouterr().out
    assert "CLAUDE.md, skills" in out and "could not be refreshed" not in out


def test_a_cli_that_cannot_start_is_recorded_as_failed(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(runtime, "build_cli", lambda *args: ["vibe-launcher-missing-cli-for-tests"])
    monkeypatch.setattr(runtime, "emit", lambda request, event: events.append(event))
    root = tmp_path / "state"
    with pytest.raises(RuntimeError):
        runtime.run_worker(worker_request(tmp_path), root, dict(os.environ))
    assert events[-1]["status"] == "failed"
    assert json.loads((root / "sessions/s.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_user_settings_with_a_bom_and_ordinary_permissions_are_read(tmp_path):
    root, home = tmp_path / "data", tmp_path / "home"
    settings = write(runtime.user_config_dir("claude", home) / "settings.json", b"\xef\xbb\xbf" + json.dumps({"theme": "dark"}).encode())
    os.chmod(settings, 0o644)
    summary = runtime.sync_user_data(CLAUDE, root, "/work/site", home)
    assert summary["failed"] == [] and summary["extensions"]["settings"] == ["theme"]
    assert json.loads((runtime.profile_dir(root, CLAUDE) / "settings.json").read_text(encoding="utf-8"))["theme"] == "dark"


def test_broken_user_settings_leave_the_profile_alone_but_do_not_stop_the_refresh(tmp_path):
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir("claude", home)
    write(source / "settings.json", "[broken")
    write(source / "agents/planner.md", "planner")
    target = write(runtime.profile_dir(root, CLAUDE) / "settings.json", "personal")
    summary = runtime.sync_user_data(CLAUDE, root, "/work/site", home)
    assert summary["extensions"]["skipped"] == 1 and target.read_text() == "personal"
    assert (target.parent / "agents/planner.md").read_text() == "planner"


def test_unexpected_plugin_settings_do_not_break_the_launch(tmp_path, monkeypatch):
    source = tmp_path / "home/.claude"
    write(source / "plugins/installed_plugins.json", json.dumps({"plugins": {}}))
    write(source / "settings.json", json.dumps({"enabledPlugins": [1, 2]}))
    monkeypatch.setattr(runtime, "user_config_dir", lambda tool, home=None: source)
    assert runtime.user_plugin_dirs({"tool": "claude"}) == []
    assert runtime.build_cli(CLAUDE, "launch", tmp_path / "data") == ["claude"]


def test_a_linked_codex_home_is_not_shared_and_does_not_break_the_launch(tmp_path, monkeypatch):
    real = write(tmp_path / "dotfiles/codex/state_5.sqlite", "native state").parent
    home = tmp_path / "home"
    home.mkdir()
    link_dir(home / ".codex", real)
    monkeypatch.setattr(runtime, "user_config_dir", lambda tool, h=None: home / (".codex" if tool == "codex" else ".claude"))
    assert runtime.native_codex_state() is None
    command = runtime.build_cli({"id": "x", "tool": "codex", "auth": "subscription"}, "launch", tmp_path / "data")
    assert not any("sqlite_home" in part for part in command)


def test_unchanged_conversations_are_not_read_again(tmp_path, monkeypatch):
    root, home = tmp_path / "data", tmp_path / "home"
    source = write(runtime.user_config_dir("claude", home) / "projects/site/a.jsonl", '{"type":"user"}\n')
    assert runtime.sync_user_data(CLAUDE, root, "/work/site", home)["history"]["copied"] == 1
    opened, real_open = [], os.open
    monkeypatch.setattr(os, "open", lambda path, *a, **k: opened.append(str(path)) or real_open(path, *a, **k))
    assert runtime.sync_user_data(CLAUDE, root, "/work/site", home)["history"]["copied"] == 0
    assert not [path for path in opened if path.endswith(".jsonl")]
    write(source, '{"type":"user"}\n{"type":"assistant"}\n')
    assert runtime.sync_user_data(CLAUDE, root, "/work/site", home)["history"]["copied"] == 1
    assert (runtime.profile_dir(root, CLAUDE) / "projects/site/a.jsonl").read_text().count("\n") == 2


def test_unchanged_extensions_are_not_read_again(tmp_path, monkeypatch):
    root, home = tmp_path / "data", tmp_path / "home"
    skill = write(runtime.user_config_dir("claude", home) / "skills/review/SKILL.md", "skill")
    runtime.sync_user_data(CLAUDE, root, "/work/site", home)
    reads, real = [], runtime.read_private_bytes
    monkeypatch.setattr(runtime, "read_private_bytes", lambda path, *a, **k: reads.append(Path(path).name) or real(path, *a, **k))
    runtime.sync_user_data(CLAUDE, root, "/work/site", home)
    assert "SKILL.md" not in reads
    write(skill, "changed skill")
    runtime.sync_user_data(CLAUDE, root, "/work/site", home)
    assert (runtime.profile_dir(root, CLAUDE) / "skills/review/SKILL.md").read_text() == "changed skill"

def test_restoring_continues_the_last_conversation_only_when_there_is_one(tmp_path, monkeypatch):
    root = tmp_path / "data"
    project = tmp_path / "site"
    project.mkdir()
    monkeypatch.setattr(runtime, "user_config_dir", lambda tool, home=None: tmp_path / "home" / ("." + tool))
    assert runtime.build_cli(CLAUDE, "launch", root, project=project, resume=True) == ["claude"]
    write(runtime.profile_dir(root, CLAUDE) / "projects" / runtime.claude_project_folder(str(project)) / "a.jsonl", "{}\n")
    assert runtime.build_cli(CLAUDE, "launch", root, project=project, resume=True) == ["claude", "--continue"]
    assert runtime.build_cli(CLAUDE, "launch", root, project=project) == ["claude"], "a fresh launch starts fresh"
    codex = {"id": "x", "tool": "codex", "auth": "subscription"}
    assert runtime.build_cli(codex, "launch", root, project=project, resume=True)[-2:] != ["resume", "--last"]
    write(runtime.profile_dir(root, codex) / "sessions/2026/10/09/rollout-1.jsonl",
          json.dumps({"type": "session_meta", "payload": {"cwd": str(project)}}) + "\n")
    assert runtime.build_cli(codex, "launch", root, project=project, resume=True)[-2:] == ["resume", "--last"]


def test_the_resume_flag_is_only_for_launches():
    base = {"action": "launch", "session_id": "s", "environment_id": "local"}
    runtime.validate_request(dict(base, resume=True))
    for bad in (dict(base, resume="yes"), dict(base, action="stop", resume=True)):
        with pytest.raises(ValueError):
            runtime.validate_request(bad)

def test_plugin_settings_above_the_project_folder_are_ignored(tmp_path, monkeypatch):
    source = tmp_path / "home/.claude"
    plugin = source / "plugins/cache/demo"
    write(plugin / "plugin.json", "{}")
    write(source / "plugins/installed_plugins.json", json.dumps({"plugins": {"demo@m": [{"scope": "user", "installPath": str(plugin)}]}}))
    write(source / "settings.json", "{}")
    project = tmp_path / "shared/site"
    project.mkdir(parents=True)
    write(tmp_path / "shared/.claude/settings.json", '{"enabledPlugins": {"demo@m": false}}')   # someone else's folder
    monkeypatch.setattr(runtime, "user_config_dir", lambda tool, home=None: source)
    assert runtime.user_plugin_dirs({"tool": "claude"}, project=project) == [str(plugin)]


# --- shared extensions ------------------------------------------------------------------------------------------------
EXTENSIONS_CLAUDE = {"id": "c1", "name": "C", "tool": "claude", "auth": "subscription"}
CODEX = {"id": "x1", "name": "X", "tool": "codex", "auth": "subscription"}


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    base = tmp_path / "home" / ".claude"
    _write(base / "skills" / "review" / "SKILL.md", "# review")
    _write(base / "agents" / "planner.md", "# planner")
    _write(base / "commands" / "ship.md", "# ship")
    _write(base / "hooks" / "gate.cmd", "echo hi")
    _write(base / "CLAUDE.md", "@RTK.md")
    _write(base / "RTK.md", "rtk")
    _write(base / ".credentials.json", '{"token": "SECRET"}')
    _write(base / "skills" / "auth.json", "SECRET")
    _write(base / "history.jsonl", "private history")
    _write(base / "projects" / "p" / "log.jsonl", "private")
    _write(base / "settings.json", json.dumps({
        "model": "claude-x", "theme": "dark", "autoContinueAtUsageLimit": False,
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}]},
        "permissions": {"allow": ["Bash(ls)"]},
        "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1", "ANTHROPIC_API_KEY": "SECRET",
                "CLAUDE_CODE_USE_BEDROCK": "1", "MY_TOKEN": "SECRET", "TEAM_FLAG": "on"},
        "apiKeyHelper": "get-key"}))
    return tmp_path / "home"


def test_skills_agents_hooks_and_notes_reach_the_profile(tmp_path, home):
    root = tmp_path / "data"
    summary = runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    profile = runtime.profile_dir(root, EXTENSIONS_CLAUDE)
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review"
    assert (profile / "agents" / "planner.md").is_file()
    assert (profile / "commands" / "ship.md").is_file()
    assert (profile / "hooks" / "gate.cmd").is_file()
    assert (profile / "CLAUDE.md").is_file() and (profile / "RTK.md").is_file()
    assert summary["copied"] >= 6


def test_credentials_and_history_never_travel(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    profile = runtime.profile_dir(root, EXTENSIONS_CLAUDE)
    assert not (profile / ".credentials.json").exists()
    assert not (profile / "skills" / "auth.json").exists()
    assert not (profile / "history.jsonl").exists()
    assert not (profile / "projects").exists()
    assert "SECRET" not in "".join(p.read_text(encoding="utf-8", errors="ignore") for p in profile.rglob("*") if p.is_file())


def test_settings_keep_hooks_but_drop_routing_and_secrets(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    settings = json.loads((runtime.profile_dir(root, EXTENSIONS_CLAUDE) / "settings.json").read_text(encoding="utf-8"))
    assert "SessionStart" in settings["hooks"] and settings["permissions"]["allow"] == ["Bash(ls)"]
    assert settings["env"] == {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1", "TEAM_FLAG": "on"}
    assert settings["model"] == "claude-x" and "apiKeyHelper" not in settings
    assert settings["autoContinueAtUsageLimit"] is False       # the user's choice about unattended continuation
    # the launcher's own guard against provider overrides must still accept what we wrote
    runtime.check_settings(EXTENSIONS_CLAUDE, root, tmp_path)


def test_resync_follows_the_source_but_spares_the_profiles_own_files(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    profile = runtime.profile_dir(root, EXTENSIONS_CLAUDE)
    _write(profile / "skills" / "mine" / "SKILL.md", "profile only")
    (home / ".claude" / "agents" / "planner.md").unlink()
    _write(home / ".claude" / "skills" / "review" / "SKILL.md", "# review v2")
    again = runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    assert (profile / "agents" / "planner.md").exists()
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review v2"
    assert (profile / "skills" / "mine" / "SKILL.md").read_text(encoding="utf-8") == "profile only"
    assert again["removed"] == 0
    assert runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)["copied"] == 0     # nothing changed, nothing rewritten


def test_a_hook_removed_from_the_users_settings_is_preserved_in_the_profile(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    path = home / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["hooks"]
    path.write_text(json.dumps(data), encoding="utf-8")
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    settings = json.loads((runtime.profile_dir(root, EXTENSIONS_CLAUDE) / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" in settings and "permissions" in settings


def test_claude_personal_settings_and_deletions_survive_resync(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    settings_path = runtime.profile_dir(root, EXTENSIONS_CLAUDE) / "settings.json"
    personal = json.loads(settings_path.read_text(encoding="utf-8"))
    personal["model"] = "personal-model"
    personal["permissions"] = {"allow": ["Read"]}
    personal["env"]["TEAM_FLAG"] = "personal"
    del personal["hooks"]
    _write(settings_path, json.dumps(personal))
    source_path = home / ".claude" / "settings.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source.update(model="new-model", theme="light")
    source["env"].update(TEAM_FLAG="new", NEW_FLAG="yes")
    _write(source_path, json.dumps(source))
    for _ in range(2):
        runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
        actual = json.loads(settings_path.read_text(encoding="utf-8"))
        assert actual["model"] == "personal-model"
        assert actual["permissions"] == {"allow": ["Read"]}
        assert "hooks" not in actual
        assert actual["theme"] == "light"
        assert actual["env"]["TEAM_FLAG"] == "personal"
        assert actual["env"]["NEW_FLAG"] == "yes"


def test_codex_follows_source_until_personally_edited(tmp_path):
    pytest.importorskip("tomllib")
    home, root = tmp_path / "home", tmp_path / "data"
    source = home / ".codex" / "config.toml"
    profile = runtime.profile_dir(root, CODEX) / "config.toml"
    _write(source, 'model = "first"\napproval_policy = "on-request"\n')
    runtime.sync_extensions(CODEX, root, home)
    _write(source, 'model = "second"\napproval_policy = "never"\n')
    runtime.sync_extensions(CODEX, root, home)
    assert '"second"' in profile.read_text(encoding="utf-8")
    personal = b'# keep comment\nmodel = "mine"\n[projects.local]\ntrust_level = "trusted"\n'
    profile.write_bytes(personal)
    _write(source, 'model = "third"\n')
    for _ in range(2):
        runtime.sync_extensions(CODEX, root, home)
        assert profile.read_bytes() == personal


@pytest.mark.parametrize("account", [EXTENSIONS_CLAUDE, CODEX])
def test_account_model_overrides_shared_model(tmp_path, account, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)   # CI machines have no Codex/Claude installed
    command = runtime.cli_command(dict(account, auth="api", model="account-model"), "run", tmp_path)
    assert command[command.index("--model") + 1] == "account-model"


def test_links_in_the_users_folder_are_not_followed(tmp_path, home):
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE", encoding="utf-8")
    link = home / ".claude" / "skills" / "linked.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    root = tmp_path / "data"
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home)
    assert not (runtime.profile_dir(root, EXTENSIONS_CLAUDE) / "skills" / "linked.md").exists()


def test_codex_config_keeps_mcp_and_features_but_not_provider_or_login():
    pytest.importorskip("tomllib")
    text = '''
model = "gpt-x"
model_provider = "other"
forced_login_method = "chatgpt"
approval_policy = "on-request"

[model_providers.other]
base_url = "https://example.invalid"

[projects."C:\\\\work"]
trust_level = "trusted"

[features]
multi_agent = true

[mcp_servers.docs]
command = "npx"
args = ["-y", "docs-mcp"]
'''
    import tomllib
    out = tomllib.loads(runtime.shared_codex_config(text))
    assert out == {"model": "gpt-x", "approval_policy": "on-request", "features": {"multi_agent": True},
                   "mcp_servers": {"docs": {"command": "npx", "args": ["-y", "docs-mcp"]}}}


def test_codex_profile_gets_skills_agents_and_a_reduced_config(tmp_path):
    pytest.importorskip("tomllib")
    home = tmp_path / "home"
    _write(home / ".codex" / "AGENTS.md", "# agents")
    _write(home / ".codex" / "skills" / "s" / "SKILL.md", "# s")
    _write(home / ".codex" / "auth.json", "SECRET")
    _write(home / ".codex" / "config.toml", 'model_provider = "x"\n[features]\nmulti_agent = true\n')
    root = tmp_path / "data"
    runtime.sync_extensions(CODEX, root, home)
    profile = runtime.profile_dir(root, CODEX)
    assert (profile / "AGENTS.md").is_file() and (profile / "skills" / "s" / "SKILL.md").is_file()
    assert not (profile / "auth.json").exists()
    config = (profile / "config.toml").read_text(encoding="utf-8")
    assert "multi_agent = true" in config and "model_provider" not in config


def test_enabled_user_plugins_are_passed_by_path(tmp_path):
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"
    on, off = plugins / "cache" / "on", plugins / "cache" / "off"
    _write(on / "plugin.json", "{}")
    _write(off / "plugin.json", "{}")
    _write(plugins / "installed_plugins.json", json.dumps({"version": 2, "plugins": {
        "on@m": [{"scope": "user", "installPath": str(on)}],
        "off@m": [{"scope": "user", "installPath": str(off)}],
        "gone@m": [{"scope": "user", "installPath": str(plugins / "cache" / "gone")}],
        "elsewhere@m": [{"scope": "user", "installPath": str(tmp_path)}]}}))
    _write(home / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"off@m": False}}))
    assert runtime.user_plugin_dirs(EXTENSIONS_CLAUDE, home) == [str(on)]
    assert runtime.user_plugin_dirs(CODEX, home) == []


def test_explicit_import_preserves_personal_extension_edits_and_deletions(tmp_path, home):
    root = tmp_path / "data"
    account = dict(EXTENSIONS_CLAUDE, share_extensions=False)
    assert runtime.manual_import(account, root, "settings", home)["status"] == "imported"
    profile = runtime.profile_dir(root, account)
    skill = profile / "skills" / "review" / "SKILL.md"
    _write(skill, "personal")
    (profile / "agents" / "planner.md").unlink()
    _write(home / ".claude" / "skills" / "review" / "SKILL.md", "source changed")
    runtime.manual_import(account, root, "settings", home)
    assert skill.read_text(encoding="utf-8") == "personal"
    assert not (profile / "agents" / "planner.md").exists()


def test_first_import_does_not_overwrite_existing_profile_extensions(tmp_path, home):
    root = tmp_path / "data"
    skill = runtime.profile_dir(root, EXTENSIONS_CLAUDE) / "skills" / "review" / "SKILL.md"
    _write(skill, "existing")
    runtime.manual_import(EXTENSIONS_CLAUDE, root, "settings", home)
    assert skill.read_text(encoding="utf-8") == "existing"


def test_launch_discovers_current_plugins_without_an_import_snapshot(tmp_path, monkeypatch):
    root, home = tmp_path / "data", tmp_path / "home"
    plugin = home / ".claude" / "plugins" / "cache" / "test"
    _write(plugin / "plugin.json", "{}")
    _write(home / ".claude" / "plugins" / "installed_plugins.json",
           json.dumps({"plugins": {"test@m": [{"installPath": str(plugin)}]}}))
    _write(home / ".claude" / "settings.json", "{}")
    monkeypatch.setattr(runtime, "user_config_dir", lambda *args: home / ".claude")
    assert runtime.build_cli(EXTENSIONS_CLAUDE, "launch", root) == ["claude", "--plugin-dir", str(plugin)]
    _write(home / ".claude/settings.json", '{"enabledPlugins": {"test@m": false}}')
    assert runtime.build_cli(EXTENSIONS_CLAUDE, "launch", root) == ["claude"]


def test_import_rejects_unknown_kind_or_tool(tmp_path):
    with pytest.raises(ValueError):
        runtime.manual_import(EXTENSIONS_CLAUDE, tmp_path, "automatic")
    with pytest.raises(ValueError):
        runtime.manual_import(dict(EXTENSIONS_CLAUDE, tool="unknown"), tmp_path, "settings")
    with pytest.raises(ValueError):
        runtime.build_cli(dict(EXTENSIONS_CLAUDE, tool="unknown"), "login", tmp_path)


def test_codex_update_retains_removed_keys_and_nested_tables(tmp_path):
    import tomllib
    root, home = tmp_path / "data", tmp_path / "home"
    source = home / ".codex" / "config.toml"
    _write(source, 'model = "first"\napproval_policy = "on-request"\n[features]\nmulti_agent = true\n')
    runtime.manual_import(CODEX, root, "settings", home)
    _write(source, 'model = "second"\n[features]\nfast = true\n')
    runtime.manual_import(CODEX, root, "settings", home)
    actual = tomllib.loads((runtime.profile_dir(root, CODEX) / "config.toml").read_text(encoding="utf-8"))
    assert actual == {"model": "second", "approval_policy": "on-request",
                      "features": {"multi_agent": True, "fast": True}}


@pytest.mark.parametrize("account, filename", [(EXTENSIONS_CLAUDE, "settings.json"), (CODEX, "config.toml")])
def test_invalid_source_settings_report_failure_without_replacing_profile(tmp_path, account, filename):
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir(account["tool"], home) / filename
    target = runtime.profile_dir(root, account) / filename
    _write(source, "[invalid")
    _write(target, "personal content")
    with pytest.raises(ValueError, match="source"):
        runtime.manual_import(account, root, "settings", home)
    assert target.read_text(encoding="utf-8") == "personal content"


def test_a_launch_refresh_copies_what_fits_and_removes_nothing(tmp_path, home, monkeypatch):
    root = tmp_path / "data"
    runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home, follow_source=True)
    monkeypatch.setattr(runtime, "SHARE_MAX_FILES", 2)
    summary = runtime.sync_extensions(EXTENSIONS_CLAUDE, root, home, follow_source=True)
    profile = runtime.profile_dir(root, EXTENSIONS_CLAUDE)
    assert summary["limited"] and summary["removed"] == 0
    assert (profile / "commands" / "ship.md").is_file() and (profile / "hooks" / "gate.cmd").is_file()
    with pytest.raises(ValueError, match="Too many"):
        runtime.manual_import(EXTENSIONS_CLAUDE, root, "settings", home)


# --- project history share --------------------------------------------------------------------------------------------
PROJECT = "/work/site"
WIN_PROJECT = "C:" + chr(92) + "Users" + chr(92) + "user" + chr(92) + "site"


def share_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def rollout(cwd):
    line = {"type": "session_meta", "payload": {"id": "t", "cwd": cwd, "history_mode": "legacy"}}
    return (json.dumps(line) + "\n" + json.dumps({"type": "event"}) + "\n").encode()


def claude_line():
    return (json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n").encode()


def profile(root, tool, name):
    return root / "profiles" / tool / name


def test_codex_only_this_projects_conversations_are_copied_and_credentials_are_not(tmp_path):
    a, b = {"id": "a", "tool": "codex"}, {"id": "b", "tool": "codex"}
    mine = share_write(profile(tmp_path, "codex", "a") / "sessions/2026/10/05/rollout-1.jsonl", rollout(PROJECT))
    share_write(profile(tmp_path, "codex", "a") / "sessions/2026/10/05/rollout-2.jsonl", rollout("/work/other"))
    share_write(profile(tmp_path, "codex", "a") / "auth.json", b"fake credential")
    share_write(profile(tmp_path, "codex", "a") / "history.jsonl", b"prompts")
    runtime.private_dir(profile(tmp_path, "codex", "b"))

    summary = runtime.sync_project_history(b, tmp_path, PROJECT)
    assert (summary["copied"], summary["sources"]) == (1, 1)
    target = profile(tmp_path, "codex", "b")
    assert (target / "sessions/2026/10/05/rollout-1.jsonl").read_bytes() == mine.read_bytes()
    assert not (target / "sessions/2026/10/05/rollout-2.jsonl").exists()
    assert not (target / "auth.json").exists() and not (target / "history.jsonl").exists()
    assert runtime.sync_project_history(b, tmp_path, PROJECT)["copied"] == 0      # repeating adds nothing
    share_write(profile(tmp_path, "codex", "a") / "sessions/2026/10/06/rollout-3.jsonl", rollout(PROJECT))
    assert runtime.sync_project_history(b, tmp_path, PROJECT)["copied"] == 1      # the refresh picks up newer ones
    share_write(profile(tmp_path, "codex", "b") / "sessions/2026/10/07/rollout-4.jsonl", rollout(PROJECT))
    assert runtime.sync_project_history(a, tmp_path, PROJECT)["copied"] == 1      # and it works both ways


def test_claude_conversations_follow_the_project_folder(tmp_path):
    folder = runtime.claude_project_folder(WIN_PROJECT)
    assert folder == "C--Users-user-site"
    share_write(profile(tmp_path, "claude", "a") / "projects" / folder / "s1.jsonl", claude_line())
    share_write(profile(tmp_path, "claude", "a") / "projects" / folder / "s1" / "subagents" / "x.jsonl", claude_line())
    share_write(profile(tmp_path, "claude", "a") / "projects/other/s2.jsonl", claude_line())
    runtime.private_dir(profile(tmp_path, "claude", "b"))
    summary = runtime.sync_project_history({"id": "b", "tool": "claude"}, tmp_path, WIN_PROJECT)
    assert summary["copied"] == 2
    assert (profile(tmp_path, "claude", "b") / "projects" / folder / "s1.jsonl").exists()
    assert not (profile(tmp_path, "claude", "b") / "projects/other").exists()


def test_other_tools_and_missing_profiles_are_left_alone(tmp_path):
    share_write(profile(tmp_path, "claude", "a") / "projects/x/s.jsonl", claude_line())
    assert runtime.sync_project_history({"id": "b", "tool": "codex"}, tmp_path, PROJECT)["copied"] == 0
    assert runtime.sync_project_history({"id": "b", "tool": "codex"}, tmp_path, "")["copied"] == 0


def test_the_account_flag_is_stored_and_must_be_boolean(tmp_path):
    store = Store(tmp_path / "launcher.db")
    store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"})
    assert store.get("accounts", "a")["share_project_history"] is False
    store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription", "share_project_history": True})
    assert store.get("accounts", "a")["share_project_history"] is True
    with pytest.raises(ValueError):
        store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription", "share_project_history": "yes"})


def test_the_refresh_request_is_accepted_only_as_a_history_import():
    base = {"action": "import-history", "nonce": "n" * 32, "language": "en", "environment_id": "local",
            "account": {"id": "a", "tool": "codex", "auth": "subscription"}, "path": PROJECT, "project_history": True}
    runtime.validate_request(dict(base))
    with pytest.raises(ValueError):
        runtime.validate_request({**base, "project_history": "yes"})
    with pytest.raises(ValueError):
        runtime.validate_request({**base, "action": "launch"})


def test_the_service_builds_a_project_history_job(tmp_path):
    from vibe_launcher.service import LauncherService
    store = Store(tmp_path / "launcher.db")
    store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"})
    store.save("presets", {"id": "p", "name": "Site", "environment_id": "local", "path": str(tmp_path)})
    jobs = []
    LauncherService(store).start("import-history", preset_id="p", account_id="a", environment_id="local",
                                 operation={"project_history": True}, spawn=lambda args, context: jobs.append(args))
    assert jobs, "the job was handed to the terminal"
    with pytest.raises(ValueError):
        LauncherService(store).start("import-history", preset_id="p", account_id="a", operation={"surprise": 1},
                                     spawn=lambda args, context: None)


# --- history import ---------------------------------------------------------------------------------------------------
def transcript(tool, mode="legacy"):
    record = {"type": "session_meta", "payload": {"id": "test", "history_mode": mode}}
    if tool == "claude":
        record = {"type": "user", "message": {"content": "test"}}
    return (json.dumps(record) + "\n").encode()


@pytest.mark.parametrize("tool,relative", [
    ("codex", "sessions/2026/10/05/rollout-test.jsonl"),
    ("claude", "projects/test/session.jsonl"),
])
def test_snapshot_never_overwrites_or_copies_other_data(tmp_path, tool, relative):
    account = {"id": "a", "tool": tool, "import_history": True}
    home, root = tmp_path / "home", tmp_path / "data"
    source = runtime.user_config_dir(tool, home)
    original = write(source / relative, transcript(tool))
    write(source / "history.jsonl", b"prompt history\n")
    write(source / "auth.json", b"fake credential")
    write(source / "state_5.sqlite", b"database")
    assert runtime.sync_history(account, root, home) == {"copied": 1, "skipped": 0, "limited": False}
    profile = runtime.profile_dir(root, account)
    assert (profile / relative).read_bytes() == original.read_bytes()
    assert {p.relative_to(profile).as_posix() for p in profile.rglob("*") if p.is_file()} == {relative}
    (profile / relative).write_bytes(b"local continuation\n")
    assert runtime.sync_history(account, root, home)["copied"] == 0
    assert (profile / relative).read_bytes() == b"local continuation\n"
    assert original.read_bytes() == transcript(tool)


def test_incomplete_paginated_and_oversize_transcripts_are_not_imported(tmp_path):
    account = {"id": "a", "tool": "codex", "import_history": True}
    home, root = tmp_path / "home", tmp_path / "data"
    base = home / ".codex" / "sessions"
    write(base / "rollout-incomplete.jsonl", transcript("codex").rstrip())
    write(base / "rollout-invalid.jsonl", transcript("codex") + b"not json\n")
    write(base / "rollout-paginated.jsonl", transcript("codex", "paginated"))
    huge = write(base / "rollout-huge.jsonl", b"")
    with huge.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    assert runtime.sync_history(account, root, home)["skipped"] == 4
    assert not root.exists()


def test_hardlinked_source_and_target_are_rejected(tmp_path):
    account = {"id": "a", "tool": "claude", "import_history": True}
    home, root = tmp_path / "home", tmp_path / "data"
    source = write(home / ".claude/projects/p/session.jsonl", transcript("claude"))
    os.link(source, tmp_path / "linked-source")
    assert runtime.sync_history(account, root, home)["skipped"] == 1
    (tmp_path / "linked-source").unlink()
    target = write(runtime.profile_dir(root, account) / "projects/p/session.jsonl", b"keep")
    os.link(target, tmp_path / "linked-target")
    assert runtime.sync_history(account, root, home)["skipped"] == 1
    assert target.read_bytes() == b"keep"


def test_atomic_publication_preserves_racing_destination(tmp_path, monkeypatch):
    account = {"id": "a", "tool": "claude", "import_history": True}
    home, root = tmp_path / "home", tmp_path / "data"
    write(home / ".claude/projects/p/session.jsonl", transcript("claude"))
    target = runtime.profile_dir(root, account) / "projects/p/session.jsonl"
    operation = "rename" if os.name == "nt" else "link"
    real_operation = getattr(os, operation)

    def race(source, destination):
        target.write_bytes(b"new conversation")
        return real_operation(source, destination)

    monkeypatch.setattr(os, operation, race)
    assert runtime.sync_history(account, root, home)["skipped"] == 1
    assert target.read_bytes() == b"new conversation"
    assert not list(target.parent.glob("*.tmp"))


def test_unsafe_source_directory_is_not_traversed(tmp_path, monkeypatch):
    account = {"id": "a", "tool": "claude", "import_history": True}
    home, root = tmp_path / "home", tmp_path / "data"
    source = write(home / ".claude/projects/linked/session.jsonl", transcript("claude"))
    real_guard = runtime.assert_private_path

    def reject_link(path):
        if path == source.parent:
            raise ValueError("Launcher data cannot use symlinks or reparse points")
        real_guard(path)

    monkeypatch.setattr(runtime, "assert_private_path", reject_link)
    assert runtime.sync_history(account, root, home)["skipped"] == 1
    assert not root.exists()


def test_prepare_never_scans_external_settings_or_history(tmp_path, monkeypatch):
    account = {"id": "a", "tool": "claude", "auth": "subscription", "import_history": True}
    def forbidden(*args):
        raise AssertionError("Daily launch must not scan import sources")
    monkeypatch.setattr(runtime, "sync_extensions", forbidden)
    monkeypatch.setattr(runtime, "sync_history", forbidden)
    monkeypatch.setattr(runtime, "user_config_dir", forbidden)
    monkeypatch.setattr(runtime, "isolated_env", lambda *args: {})
    assert runtime.prepare(account, tmp_path) == {}
