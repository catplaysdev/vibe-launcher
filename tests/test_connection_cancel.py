import io
import pytest
from pathlib import Path

from vibe_launcher import askpass, runner, ssh_security
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow


def setup(tmp_path):
    store = Store(tmp_path / "state.db")
    account = store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"})
    env = store.save("environments", {"id": "host", "name": "H", "kind": "ssh", "ssh_target": "user@server"})
    store.save("sessions", {"id": "s", "name": "S", "account_id": "a", "environment_id": "host", "path": "/tmp", "status": "unknown"})
    return store, env, {"action": "launch", "nonce": "n", "session_id": "s", "environment_id": "host", "account": account}


def test_config_alias_with_underscore_is_accepted(tmp_path):
    store = Store(tmp_path / "db")
    assert store.save("environments", {"name": "Lab", "kind": "ssh", "ssh_target": "lab-target_A"})["ssh_target"] == "lab-target_A"


def test_windows_fingerprint_prompt_is_not_a_password():
    prompt = "The authenticity of host 'example' can't be established.\nED25519 key fingerprint is SHA256:example.\nAre you sure you want to continue connecting (yes/no/[fingerprint])?"
    assert askpass.is_host_confirmation(prompt)
    assert not askpass.is_host_confirmation("Password:")
    assert not askpass.is_host_confirmation("OTP (yes/no)?")


def test_cancel_before_auth_is_removable_and_stop_success_is_ended(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    context = {"VIBE_SSH_ROOT": str(tmp_path), "VIBE_SSH_ATTEMPT": "attempt"}
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: context)
    monkeypatch.setattr(askpass, "cleanup", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    class Cancelled:
        stdout = io.BytesIO(b"")
        def wait(self):
            return 255
    askpass.mark_cancelled(context)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Cancelled())
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "failed"
    store.delete("sessions", "s")


def test_successful_remote_stop_updates_gui_state(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    request["action"] = "stop"
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    class Stopped:
        stdout = io.BytesIO(b"")
        def wait(self):
            return 0
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Stopped())
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "ended"


def test_unknown_record_can_be_forgotten_without_remote_stop(tmp_path, monkeypatch, qtbot):
    store, env, request = setup(tmp_path)
    view = MainWindow(store, object())
    qtbot.addWidget(view)
    view.set_sessions_grouping("time")         # one flat list: row 0 is the record
    view.sessions_list.setCurrentRow(0)
    monkeypatch.setattr(view, "_notice", lambda text: pytest.fail(text))
    messages = []
    def confirm(title, text, **kwargs):
        messages.append(text)
        assert kwargs.get("danger"), "removing an unknown session must use the destructive confirmation"
        return True
    monkeypatch.setattr(view, "_confirm", confirm)
    view.remove_session()
    assert store.list("sessions") == []
    assert "不會停止" in messages[0]


def test_identity_files_and_jump_are_inherited(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".ssh").mkdir()
    config = tmp_path / ".ssh/config"
    config.write_text("Host lab-target_A\n HostName 192.0.2.1\n IdentityFile ~/.ssh/existing-one\n IdentityFile ~/.ssh/existing-two\n ProxyJump lab-jump_A\nHost lab-jump_A\n HostName 192.0.2.2\n", encoding="utf-8")
    from vibe_launcher.runtime import protect_path
    protect_path(config)
    settings = ssh_security.resolve(tmp_path / "data", {"id": "lab", "ssh_target": "lab-target_A"})
    assert settings["proxyjump"] == "lab-jump_A"
    assert settings["identityfiles"] == ["~/.ssh/existing-one", "~/.ssh/existing-two"]
