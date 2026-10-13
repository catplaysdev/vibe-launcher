import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vibe_launcher import harness, runtime


def request(**updates):
    return {"account": {"id": "ai", "tool": "deepseek-harness", "environment_id": "ssh1"},
            "environment_id": "ssh1", "harness_operation": "start", "harness_port": 3080,
            "local_port": 4080, **updates}


def test_shared_identity_separates_accounts_and_ignores_project():
    one = request(path="/work/a")
    assert runtime.harness_identity(one) == runtime.harness_identity(request(path="/work/b"))
    assert runtime.harness_identity(one) != runtime.harness_identity(request(github_account={"id": "gh", "environment_id": "ssh1"}))
    with pytest.raises(ValueError):
        runtime.harness_identity(request(environment_id="ssh2"))
    with pytest.raises(ValueError):
        runtime.harness_identity(request(github_account={"id": "gh", "environment_id": "ssh2"}))


def test_environment_has_no_inherited_tokens_or_personal_home(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/usr/bin/" + name)
    parent = {"HOME": "/users/me", "PATH": "/usr/bin", "GH_TOKEN": "secret", "GITHUB_TOKEN": "secret",
              "DEEPSEEK_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret", "NODE_OPTIONS": "secret"}
    env = runtime.harness_env(request(), tmp_path, parent)
    assert "secret" not in json.dumps(env)
    assert env["HOME"] == env["DSH_HOME"]
    assert env["GH_CONFIG_DIR"].endswith("github-unbound")
    assert env["GIT_CONFIG_VALUE_0"] == ""
    selected = runtime.harness_env(request(github_account={"id": "gh", "environment_id": "ssh1"}), tmp_path, parent)
    assert selected["GH_CONFIG_DIR"] != env["GH_CONFIG_DIR"]
    assert selected["GIT_CONFIG_COUNT"] == "2"
    assert selected["GH_CONFIG_DIR"].replace("\\", "/") in selected["GIT_CONFIG_VALUE_1"]
    assert parent["GH_TOKEN"] == "secret"


def test_initial_telemetry_defaults_preserve_later_user_changes(tmp_path):
    home = tmp_path / "home"
    runtime.harness_prepare(home)
    patch = home / "cordis.patch.yml"
    text = patch.read_text(encoding="utf-8")
    assert "enabled: false" in text and "mode: DISABLED" in text
    patch.write_text("user settings", encoding="utf-8")
    runtime.harness_prepare(home)
    assert patch.read_text() == "user settings"


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *a: None))
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(runtime, "harness_listener_owned", lambda *a: None)


def test_start_reuses_owned_service_and_refuses_port_changes(tmp_path, monkeypatch, remote):
    key = runtime.harness_identity(request())
    runtime.write_json(tmp_path / "web-services" / key / "service.json",
                       {"service_key": key, "token": "a" * 32, "port": 3080, "local_port": 4080})
    monkeypatch.setattr(runtime, "harness_pane", lambda *a: 123)
    monkeypatch.setattr(runtime, "harness_listener_owned", lambda *a: True)
    monkeypatch.setattr(runtime, "harness_tmux", lambda *a: pytest.fail("must not restart service"))
    assert runtime.harness_action(request(), tmp_path)["status"] == "running"
    with pytest.raises(ValueError, match="連接埠"):
        runtime.harness_action(request(local_port=4081), tmp_path)
    monkeypatch.setattr(runtime, "harness_listener_owned", lambda *a: False)
    with pytest.raises(RuntimeError, match="未知服務"):
        runtime.harness_action(request(), tmp_path)


def test_status_does_not_create_profile_and_ownership_errors_propagate(tmp_path, monkeypatch, remote):
    monkeypatch.setattr(runtime, "harness_pane", lambda *a: None)
    assert runtime.harness_action(request(harness_operation="status"), tmp_path)["status"] == "stopped"
    assert not (tmp_path / "profiles").exists()
    def unknown(*args):
        raise RuntimeError("unknown")
    monkeypatch.setattr(runtime, "harness_pane", unknown)
    with pytest.raises(RuntimeError, match="unknown"):
        runtime.harness_action(request(harness_operation="status"), tmp_path)


def test_tmux_unknown_is_not_stopped(monkeypatch):
    monkeypatch.setattr(runtime, "harness_tmux", lambda *a: subprocess.CompletedProcess(a, 1, "", "permission denied"))
    with pytest.raises(RuntimeError, match="無法確認"):
        runtime.harness_pane("harness-a", "a" * 32)


def test_missing_pane_with_replaced_listener_is_unknown(tmp_path, monkeypatch, remote):
    monkeypatch.setattr(runtime, "harness_pane", lambda *a: None)
    monkeypatch.setattr(runtime, "harness_listener_owned", lambda *a: False)
    for operation in ("status", "stop"):
        result = runtime.harness_action(request(harness_operation=operation), tmp_path)
        assert result["status"] == "unknown"
    with pytest.raises(RuntimeError, match="未知服務"):
        runtime.harness_action(request(), tmp_path)


def test_local_windows_service_rejected(tmp_path):
    if runtime.WINDOWS:
        with pytest.raises(ValueError, match="SSH Linux"):
            runtime.harness_action(request(), tmp_path)


def test_ports_require_real_integer():
    for port in (True, "3080", 80, 65536):
        with pytest.raises(ValueError):
            runtime.harness_port(port)


def test_tunnel_shutdown_only_stops_owned_local_process(tmp_path, monkeypatch):
    calls = []
    process = SimpleNamespace(pid=123, poll=lambda: None, terminate=lambda: calls.append("terminate"), wait=lambda **k: None)
    monkeypatch.setattr(harness, "listener_owned", lambda *a: True)
    manager = harness.TunnelManager(tmp_path)
    manager._processes["a" * 32] = (process, 3080, 4080, "ssh1")
    assert manager.status("a" * 32) == "connected"
    manager.shutdown()
    assert calls == ["terminate"]
    assert manager.status("a" * 32) == "disconnected"


def test_start_checks_dependencies_and_does_not_install(tmp_path, monkeypatch, remote):
    panes = iter((None, 123))
    monkeypatch.setattr(runtime, "harness_pane", lambda *a: next(panes))
    monkeypatch.setattr(runtime, "harness_listener_owned", lambda *a: None)
    calls = []
    monkeypatch.setattr(runtime, "harness_tmux", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(runtime, "harness_env", lambda *a: {"DSH_HOME": str(tmp_path / "home"), "GH_CONFIG_DIR": str(tmp_path / "gh")})
    result = runtime.harness_action(request(), tmp_path)
    assert result["status"] == "starting"
    assert [call[0] for call in calls] == ["new-session", "set-option"]
    assert calls[1][2].startswith("=") and calls[1][2].endswith(":")     # options need "=name:" for an exact match
    script = (tmp_path / "web-services" / result["service_key"] / "start.sh").read_text()
    assert "--host 127.0.0.1" in script and "--no-open" in script
    assert "127.0.0.1:4080" in script


def test_stop_confirms_listener_exit(tmp_path, monkeypatch, remote):
    key = runtime.harness_identity(request())
    runtime.write_json(tmp_path / "web-services" / key / "service.json",
                       {"service_key": key, "token": "a" * 32, "port": 3080, "local_port": 4080})
    panes = iter((123, None))
    listeners = iter((True, None))
    monkeypatch.setattr(runtime, "harness_pane", lambda *a: next(panes))
    monkeypatch.setattr(runtime, "harness_listener_owned", lambda *a: next(listeners))
    monkeypatch.setattr(runtime, "harness_tmux", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    assert runtime.harness_action(request(harness_operation="stop"), tmp_path)["status"] == "stopped"


def test_tunnel_rejects_inherited_forwarding_before_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.ssh_security, "resolve", lambda *a: {})
    monkeypatch.setattr(harness.ssh_security, "base_args", lambda *a: ["ssh"])
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "localforward localhost:9000 host:9000", ""))
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    manager = harness.TunnelManager(tmp_path)
    with pytest.raises(ValueError, match="forwarding"):
        manager.open("a" * 32, {"kind": "ssh", "id": "ssh1", "ssh_target": "alias"}, 3080, 40999)


def test_tunnel_password_mode_is_explicitly_rejected(tmp_path):
    manager = harness.TunnelManager(tmp_path)
    with pytest.raises(ValueError, match="密碼與 OTP"):
        manager.open("a" * 32, {"kind": "ssh", "id": "ssh1", "ssh_target": "alias", "ssh_auth_mode": "password"}, 3080, 4080)


def test_local_listener_owner_is_checked_without_connecting():
    import os
    import socket
    if not runtime.WINDOWS:
        pytest.skip("Windows API acceptance")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert harness.listener_owned(port, os.getpid())
        assert not harness.listener_owned(port, os.getpid() + 1)
