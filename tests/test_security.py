import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_launcher import runtime, runner, ssh_security
from vibe_launcher.service import ssh_args
from vibe_launcher.store import Store


def records(tmp_path):
    store = Store(tmp_path / "state.db")
    account = store.save("accounts", {"id": "account", "name": "A", "tool": "codex", "auth": "subscription"})
    env = store.save("environments", {"id": "host", "name": "Host", "kind": "ssh", "ssh_target": "work"})
    for sid in ("one", "two"):
        store.save("sessions", {"id": sid, "account_id": account["id"], "environment_id": "host", "name": sid,
                                "path": "/tmp", "status": "queued"})
    return store, account, env


def test_terminal_cannot_forge_auth_or_other_session(tmp_path, monkeypatch):
    store, account, env = records(tmp_path)
    request = {"action": "launch", "account": account, "environment_id": "host", "session_id": "one", "nonce": "public"}
    forged = b'VIBE_EVENT_public:{"session_id":"two","status":"ended","auth_status":"authenticated"}\n'
    class FakeSSH:
        stdout = io.BytesIO(forged)
        def wait(self):
            return 0
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeSSH())
    assert runner.run_remote(store, request, env) == 0
    assert store.get("sessions", "two")["status"] == "queued"
    assert store.get("sessions", "one")["status"] == "unknown"
    assert store.list("auth_states") == []


def test_event_session_binding_validation_and_terminal_state(tmp_path):
    store, account, env = records(tmp_path)
    request = {"action": "launch", "account": account, "environment_id": "host", "session_id": "one"}
    for event in ({"session_id": "two", "status": "ended"}, {"status": []}, {"pid": True, "status": "running"}, {"status": "invented"}):
        runner.record_event(store, request, event)
    assert all(s["status"] == "queued" for s in store.list("sessions"))
    runner.record_event(store, request, {"status": "ended"})
    runner.record_event(store, request, {"status": "running"})
    assert store.get("sessions", "one")["status"] == "ended"


def test_failed_report_does_not_commit_partial_states(tmp_path, monkeypatch):
    store, account, env = records(tmp_path)
    class FakeSSH:
        stdout = io.BytesIO(b'VIBE_EVENT_n:{"session_id":"one","status":"ended"}\n')
        def wait(self):
            return 255
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeSSH())
    runner.run_remote(store, {"action": "list-sessions", "environment_id": "host", "nonce": "n"}, env)
    assert store.get("sessions", "one")["status"] == "unknown"


def test_real_openssh_policy_overrides_unsafe_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/config").write_text("Host work\n HostName 127.0.0.1\n User test\n StrictHostKeyChecking no\n ForwardAgent yes\n ForwardX11 yes\n LocalForward 0.0.0.0:8765 localhost:8765\n ProxyJump gateway\n")
    runtime.protect_path(tmp_path / ".ssh/config")
    env = {"id": "host", "ssh_target": "work", "ssh_auth_mode": "password"}
    result = ssh_security.resolve(tmp_path / "state", env)
    assert result["stricthostkeychecking"] in {"ask", "2"}
    assert result["forwardagent"] == "no"
    assert result["forwardx11"] == "no"
    assert result["clearallforwardings"] == "yes"
    assert result["proxyjump"] == "gateway"
    assert "localforward" not in result
    args = ssh_args("work", {"action": "doctor"}, root=tmp_path / "state", environment=env)
    assert Path(args[0]).is_absolute()
    assert "PreferredAuthentications=password" in args
    assert "KbdInteractiveAuthentication=no" in args
    with pytest.raises(ValueError, match="without configured forwards"):
        ssh_args("work", {"action": "login", "account": {"tool": "codex"}, "login_method": "browser"}, root=tmp_path / "state", environment=env)


def test_api_key_does_not_follow_endpoint_or_revision(tmp_path, monkeypatch):
    account = {"id": "api", "tool": "codex", "auth": "api", "model": "m", "base_url": "https://one.example", "credential_revision": "one"}
    vault = {}
    def secret(account, value=None):
        if value is not None:
            vault[account["id"]] = value
        return vault.get(account["id"])
    monkeypatch.setattr(runtime, "WINDOWS", True)
    monkeypatch.setattr(runtime, "windows_secret", secret)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runtime.getpass, "getpass", lambda *a: "synthetic-key")
    assert runtime.api_key(account, tmp_path) == "synthetic-key"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert runtime.api_key(account, tmp_path) == "synthetic-key"
    for changed in ({**account, "base_url": "https://two.example"}, {**account, "credential_revision": "two"}):
        with pytest.raises(RuntimeError, match="interactive"):
            runtime.api_key(changed, tmp_path)


def test_linked_private_file_is_rejected_without_overwrite(tmp_path):
    target = tmp_path / "outside"
    target.write_text("untouched")
    link = tmp_path / "linked"
    os.link(target, link)
    with pytest.raises(ValueError, match="hard links"):
        runtime.write_json(link, {"overwrite": True})
    assert target.read_text() == "untouched"


def test_database_link_rejected_before_sqlite_opens(tmp_path):
    target = tmp_path / "outside"
    target.write_text("not a database")
    link = tmp_path / "db"
    os.link(target, link)
    with pytest.raises(ValueError, match="hard links"):
        Store(link)
    assert target.read_text() == "not a database"


def test_symbolic_data_directory_rejected(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Windows developer mode/symlink privilege unavailable")
    with pytest.raises(ValueError, match="symlinks"):
        runtime.private_dir(link / "helper")
    assert not (target / "helper").exists()


def test_project_executable_and_relative_path_cannot_hijack_cli(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "codex.exe").write_bytes(b"not an executable")
    (project / "codex.cmd").write_text("echo untrusted")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project)]))
    with pytest.raises(RuntimeError, match="native executable"):
        runtime.trusted_executable("codex")


def test_failed_ssh_never_promotes_pending_password(tmp_path, monkeypatch):
    store, account, env = records(tmp_path)
    promoted, cleaned = [], []
    class Rejected:
        stdout = io.BytesIO(b"")
        def wait(self):
            return 255
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    monkeypatch.setattr(runner.askpass, "ssh_environment", lambda *a: {"VIBE_SSH_ROOT": str(tmp_path), "VIBE_SSH_ATTEMPT": "fake"})
    monkeypatch.setattr(runner.askpass, "password_operation", lambda *a, **k: promoted.append(True))
    monkeypatch.setattr(runner.askpass, "cleanup", lambda env: cleaned.append(env))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Rejected())
    request = {"action": "launch", "account": account, "environment_id": "host", "session_id": "one", "nonce": "n"}
    assert runner.run_remote(store, request, env) == 255
    assert not promoted
    assert len(cleaned) == 1
