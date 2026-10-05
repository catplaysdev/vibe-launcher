import base64
import io
import json

import pytest

from vibe_launcher import askpass, runner, runtime
from vibe_launcher.store import Store


def setup(tmp_path, status="connecting"):
    store = Store(tmp_path / "state.db")
    account = store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"})
    env = store.save("environments", {"id": "host", "name": "H", "kind": "ssh", "ssh_target": "user@server"})
    store.save("sessions", {"id": "s", "name": "S", "account_id": "a", "environment_id": "host", "path": "/tmp", "status": status})
    return store, env, {"action": "launch", "nonce": "n", "session_id": "s", "environment_id": "host", "account": account}


def fake_ssh(monkeypatch, exit_code):
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])

    class Remote:
        stdout = io.BytesIO(b"Python: 3.12\ncodex: MISSING\n")

        def wait(self):
            return exit_code
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Remote())


def test_remote_prestart_failure_is_failed_not_unknown(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    fake_ssh(monkeypatch, runtime.EXIT_NOT_STARTED)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "failed"


def test_other_remote_failures_stay_unknown_because_work_may_be_running(tmp_path, monkeypatch):
    for code in (1, 255, 130):
        store, env, request = setup(tmp_path / str(code))
        fake_ssh(monkeypatch, code)
        runner.run_remote(store, request, env)
        assert store.get("sessions", "s")["status"] == "unknown", code


def test_prestart_exit_code_only_applies_to_launch(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path, status="running")
    request["action"] = "attach"
    fake_ssh(monkeypatch, runtime.EXIT_NOT_STARTED)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "unknown"


def remote_main(monkeypatch, request, perform):
    monkeypatch.setattr(runtime.sys, "argv", ["runtime", base64.urlsafe_b64encode(json.dumps(request).encode()).decode()])
    monkeypatch.setattr(runtime, "perform", perform)
    return runtime.main()


def test_remote_main_reports_not_started_when_no_worker_was_created(monkeypatch, capsys):
    def missing_tool(request, root):
        raise RuntimeError("Missing required tools: codex")
    assert remote_main(monkeypatch, {"action": "launch"}, missing_tool) == runtime.EXIT_NOT_STARTED
    assert "Missing required tools" in capsys.readouterr().err


def test_remote_main_keeps_exit_1_once_a_worker_exists_or_for_other_actions(monkeypatch):
    def crash_after_worker(request, root):
        runtime._worker_created = True
        raise RuntimeError("boom")
    assert remote_main(monkeypatch, {"action": "launch"}, crash_after_worker) == 1

    def crash(request, root):
        raise RuntimeError("boom")
    assert remote_main(monkeypatch, {"action": "doctor"}, crash) == 1
    assert remote_main(monkeypatch, {"action": "attach"}, crash) == 1
