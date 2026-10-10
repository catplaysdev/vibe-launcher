"""Native user data is discovered automatically in the execution environment; credentials stay private."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from vibe_launcher import runtime

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