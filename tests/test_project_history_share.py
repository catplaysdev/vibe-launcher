"""Conversations of one project follow it across the launcher's accounts of the same tool (copies, never credentials)."""
import json

import pytest

from vibe_launcher import runtime
from vibe_launcher.store import Store

PROJECT = "/work/site"
WIN_PROJECT = "C:" + chr(92) + "Users" + chr(92) + "user" + chr(92) + "site"


def write(path, data):
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
    mine = write(profile(tmp_path, "codex", "a") / "sessions/2026/10/05/rollout-1.jsonl", rollout(PROJECT))
    write(profile(tmp_path, "codex", "a") / "sessions/2026/10/05/rollout-2.jsonl", rollout("/work/other"))
    write(profile(tmp_path, "codex", "a") / "auth.json", b"fake credential")
    write(profile(tmp_path, "codex", "a") / "history.jsonl", b"prompts")
    runtime.private_dir(profile(tmp_path, "codex", "b"))

    summary = runtime.sync_project_history(b, tmp_path, PROJECT)
    assert (summary["copied"], summary["sources"]) == (1, 1)
    target = profile(tmp_path, "codex", "b")
    assert (target / "sessions/2026/10/05/rollout-1.jsonl").read_bytes() == mine.read_bytes()
    assert not (target / "sessions/2026/10/05/rollout-2.jsonl").exists()
    assert not (target / "auth.json").exists() and not (target / "history.jsonl").exists()
    assert runtime.sync_project_history(b, tmp_path, PROJECT)["copied"] == 0      # repeating adds nothing
    write(profile(tmp_path, "codex", "a") / "sessions/2026/10/06/rollout-3.jsonl", rollout(PROJECT))
    assert runtime.sync_project_history(b, tmp_path, PROJECT)["copied"] == 1      # the refresh picks up newer ones
    write(profile(tmp_path, "codex", "b") / "sessions/2026/10/07/rollout-4.jsonl", rollout(PROJECT))
    assert runtime.sync_project_history(a, tmp_path, PROJECT)["copied"] == 1      # and it works both ways


def test_claude_conversations_follow_the_project_folder(tmp_path):
    folder = runtime.claude_project_folder(WIN_PROJECT)
    assert folder == "C--Users-user-site"
    write(profile(tmp_path, "claude", "a") / "projects" / folder / "s1.jsonl", claude_line())
    write(profile(tmp_path, "claude", "a") / "projects" / folder / "s1" / "subagents" / "x.jsonl", claude_line())
    write(profile(tmp_path, "claude", "a") / "projects/other/s2.jsonl", claude_line())
    runtime.private_dir(profile(tmp_path, "claude", "b"))
    summary = runtime.sync_project_history({"id": "b", "tool": "claude"}, tmp_path, WIN_PROJECT)
    assert summary["copied"] == 2
    assert (profile(tmp_path, "claude", "b") / "projects" / folder / "s1.jsonl").exists()
    assert not (profile(tmp_path, "claude", "b") / "projects/other").exists()


def test_other_tools_and_missing_profiles_are_left_alone(tmp_path):
    write(profile(tmp_path, "claude", "a") / "projects/x/s.jsonl", claude_line())
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
