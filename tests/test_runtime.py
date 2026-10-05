import shlex
from pathlib import Path

import pytest

from vibe_launcher.runtime import build_cli, isolated_env, validate_request, profile_dir
from vibe_launcher.service import remote_command
from vibe_launcher import runtime
from vibe_launcher.service import ssh_args


def account(**updates):
    return dict(id="a" * 32, name="Work", tool="codex", auth="subscription",
                model="", base_url="", **updates)


def test_subscription_cleans_provider_environment_without_mutating_parent(tmp_path):
    parent = {"PATH": "bin", "OPENAI_API_KEY": "secret", "ANTHROPIC_AUTH_TOKEN": "secret",
              "CLAUDE_CODE_USE_BEDROCK": "1", "CODEX_HOME": "old", "HOME": "home"}
    env = isolated_env(account(), tmp_path, parent)
    assert env["HOME"] == "home"
    assert env["CODEX_HOME"] == str(profile_dir(tmp_path, account()))
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert parent["OPENAI_API_KEY"] == "secret"


def test_profile_ids_cannot_escape_root(tmp_path):
    item = account()
    item["id"] = "../../escape"
    with pytest.raises(ValueError):
        profile_dir(tmp_path, item)


def test_codex_api_explicit_endpoint_and_no_shared_daemon(tmp_path):
    item = account()
    item.update(auth="api", model="custom-model", base_url="https://gateway.example/v1")
    cmd = build_cli(item, "launch", tmp_path)
    assert "--no-daemon" in cmd
    assert "custom-model" in cmd
    assert 'model_provider="vibe"' in cmd
    assert 'model_providers.vibe.wire_api="responses"' in cmd
    assert not any("secret" in part for part in cmd)


def test_ssh_command_keeps_request_out_of_shell_syntax():
    request = {"action": "launch", "account": account(), "path": "/tmp/中文 ' ; $(touch bad)"}
    command = remote_command(request)
    parts = shlex.split(command)
    assert parts[:3] == ["/usr/bin/python3", "-I", "-c"]
    assert "$(touch bad)" not in command
    assert len(command) < 28000


def test_invalid_operation_rejected():
    with pytest.raises(ValueError):
        validate_request({"action": "shell", "command": "anything"})


def test_claude_subscription_uses_official_login(tmp_path):
    item = account()
    item["tool"] = "claude"
    assert build_cli(item, "login", tmp_path)[1:] == ["auth", "login", "--claudeai"]


def test_subscription_forces_official_provider(tmp_path):
    cmd = build_cli(account(), "launch", tmp_path)
    assert 'model_provider="openai"' in cmd
    assert 'forced_login_method="chatgpt"' in cmd


def test_api_key_env_from_other_session_is_removed(tmp_path):
    assert "VIBE_API_KEY" not in isolated_env(account(), tmp_path, {"VIBE_API_KEY": "other"})


def test_browser_callback_is_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    request = {"action": "login", "account": account(), "login_method": "browser"}
    args = ssh_args("work", request, root=tmp_path / "state")
    assert "127.0.0.1:1455:127.0.0.1:1455" in args
    assert "ExitOnForwardFailure=yes" in args
    with pytest.raises(ValueError):
        ssh_args("-oProxyCommand=bad", request)


def test_missing_path_never_authenticates_or_launches(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "doctor", lambda *args: None)
    monkeypatch.setattr(runtime, "authenticate", lambda *args: pytest.fail("must validate cwd first"))
    with pytest.raises(ValueError, match="does not exist"):
        runtime.perform({"action": "launch", "account": account(), "path": str(tmp_path / "absent")}, tmp_path)


def test_project_auth_override_is_rejected_without_disclosing_secret(tmp_path):
    import json
    item = account()
    item["tool"] = "claude"
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude/settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "secret-value"}}))
    with pytest.raises(ValueError) as error:
        runtime.check_settings(item, tmp_path / "data", project)
    assert "secret-value" not in str(error.value)


def test_failed_login_does_not_start_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "prepare", lambda *args: {})
    monkeypatch.setattr(runtime, "check_login", lambda *args: False)
    monkeypatch.setattr(runtime.subprocess, "call", lambda *args, **kwargs: 1)
    events = []
    monkeypatch.setattr(runtime, "emit", lambda req, event: events.append(event))
    with pytest.raises(RuntimeError, match="Login incomplete"):
        runtime.authenticate({"account": account()}, tmp_path)
    assert events[-1]["auth_status"] == "unknown"
