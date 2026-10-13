import os
import subprocess
from pathlib import Path

import pytest

from vibe_launcher import runtime


def test_older_codex_launch_omits_unsupported_daemon_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/snap/bin/codex")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"Codex CLI\nOptions:\n --model MODEL\n", b""))
    account = {"id": "a", "tool": "codex", "auth": "subscription"}
    assert "--no-daemon" not in runtime.cli_command(account, "launch", tmp_path)


def test_current_codex_keeps_daemon_isolation_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/bin/codex")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"Codex CLI\nOptions:\n --no-daemon\n", b""))
    account = {"id": "a", "tool": "codex", "auth": "subscription"}
    assert "--no-daemon" in runtime.cli_command(account, "launch", tmp_path)


def test_executable_symlink_keeps_dispatch_name(tmp_path, monkeypatch):
    target = tmp_path / "snap.exe"
    target.write_bytes(b"fake executable")
    alias = tmp_path / "codex.exe"
    alias.symlink_to(target)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert runtime.trusted_executable("codex") == str(alias)
    assert runtime.trusted_executable(str(alias)) == str(alias)


@pytest.mark.parametrize("exists", [True, False])
def test_stop_missing_snapshot_checks_exact_tmux_window(tmp_path, monkeypatch, exists):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    calls = []
    def fake_tmux(*args, **kwargs):
        calls.append(args)
        if args[0] == "list-windows":
            # "s2" must not be mistaken for "s"
            return subprocess.CompletedProcess(args, 0, b"@4 s2\n" + (b"@7 s\n" if exists else b""), b"")
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(runtime, "tmux", fake_tmux)
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    request = {"action": "stop", "session_id": "s", "environment_id": "host"}
    assert runtime.perform(request, tmp_path) == 0
    assert calls[0][0] == "list-windows"
    assert (("kill-window", "-t", "@7") in calls) is exists
    assert not any(c[0] == "kill-window" and c[-1] == "@4" for c in calls)


def test_tmux_permission_error_is_not_treated_as_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"error connecting to /tmp/socket (Permission denied)"))
    with pytest.raises(RuntimeError, match="Cannot verify"):
        runtime.perform({"action": "stop", "session_id": "s", "environment_id": "host"}, tmp_path)


def test_missing_attach_snapshot_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    with pytest.raises(RuntimeError, match="session record"):
        runtime.perform({"action": "attach", "session_id": "s", "environment_id": "host"}, tmp_path)


def test_stop_intent_prevents_launch_after_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "doctor", lambda *a: None)
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: pytest.fail("must not create a worker"))
    def authenticate(request, root):
        runtime.write_json(runtime.session_path(root, "s").with_suffix(".stop"), {"stop_requested": True})
        return {}
    monkeypatch.setattr(runtime, "authenticate", authenticate)
    request = {"action": "launch", "session_id": "s", "environment_id": "host", "path": str(tmp_path),
               "account": {"id": "a", "tool": "codex", "auth": "subscription"}}
    assert runtime.perform(request, tmp_path / "state") == 0


def test_worker_preserves_stop_requested_before_start(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("stopped worker must not launch CLI"))
    request = {"session_id": "s", "environment_id": "host", "path": str(tmp_path),
               "account": {"id": "a", "tool": "codex", "auth": "subscription"}}
    runtime.write_json(runtime.session_path(tmp_path, "s").with_suffix(".stop"), {"stop_requested": True})
    assert runtime.run_worker(request, tmp_path, {"PATH": "test"}) == 0
