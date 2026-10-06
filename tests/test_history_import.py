import json
import os

import pytest

from vibe_launcher import runtime


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def transcript(tool, mode="legacy"):
    record = {"type": "session_meta", "payload": {"id": "test", "history_mode": mode}}
    if tool == "claude":
        record = {"type": "user", "message": {"content": "test"}}
    return (json.dumps(record) + "\n").encode()


@pytest.mark.parametrize("tool,relative", [
    ("codex", "sessions/2026/10/05/rollout-test.jsonl"),
    ("claude", "projects/test/session.jsonl"),
])
def test_opt_in_snapshot_never_overwrites_or_copies_other_data(tmp_path, tool, relative):
    account = {"id": "a", "tool": tool}
    home, root = tmp_path / "home", tmp_path / "data"
    source = runtime.user_config_dir(tool, home)
    original = write(source / relative, transcript(tool))
    write(source / "history.jsonl", b"prompt history\n")
    write(source / "auth.json", b"fake credential")
    write(source / "state_5.sqlite", b"database")
    assert runtime.sync_history(account, root, home)["copied"] == 0
    assert not root.exists()
    account["import_history"] = True
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
