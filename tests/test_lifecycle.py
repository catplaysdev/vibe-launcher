import json
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_launcher import runtime
from vibe_launcher.runner import record_event
from vibe_launcher.service import SHARED_GITHUB, remote_command


def sample():
    return {"id": "test-profile", "name": "Test", "tool": "codex", "auth": "subscription",
            "model": "", "base_url": ""}


def test_real_child_receives_isolated_cwd_and_environment(tmp_path, monkeypatch):
    project = tmp_path / "中文 ' ; $(untrusted)"
    project.mkdir()
    root = tmp_path / "state"
    account = sample()
    runtime.private_dir(runtime.profile_dir(root, account))
    script = "import os,json,pathlib; pathlib.Path('result.json').write_text(json.dumps({'cwd':os.getcwd(),'home':os.environ['CODEX_HOME'],'other':os.environ.get('OPENAI_API_KEY')}))"
    monkeypatch.setattr(runtime, "build_cli", lambda *args: [sys.executable, "-c", script])
    events = []
    monkeypatch.setattr(runtime, "emit", lambda request, event: events.append(event))
    env = runtime.isolated_env(account, root, {**__import__('os').environ, "OPENAI_API_KEY": "fake-inherited"})
    request = {"action": "worker", "account": account, "session_id": "session1", "environment_id": "local", "path": str(project)}
    assert runtime.run_worker(request, root, env) == 0
    result = json.loads((project / "result.json").read_text())
    assert Path(result["cwd"]) == project
    assert result["home"] == str(runtime.profile_dir(root, account))
    assert result["other"] is None
    assert [event["status"] for event in events] == ["running", "ended"]


def test_failed_child_persists_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "build_cli", lambda *args: [sys.executable, "-c", "raise SystemExit(7)"])
    monkeypatch.setattr(runtime, "emit", lambda *args: None)
    request = {"action": "worker", "account": sample(), "session_id": "s", "environment_id": "local", "path": str(tmp_path)}
    assert runtime.run_worker(request, tmp_path, dict(__import__('os').environ)) == 7
    assert json.loads(runtime.session_path(tmp_path, "s").read_text())["status"] == "failed"


def test_ctrl_c_in_cli_does_not_abandon_supervisor(tmp_path, monkeypatch):
    import time
    real_sleep = time.sleep
    interrupted = []
    def interrupt_once(seconds):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        real_sleep(seconds)
    monkeypatch.setattr(runtime.time, "sleep", interrupt_once)
    monkeypatch.setattr(runtime, "build_cli", lambda *args: [sys.executable, "-c", "import time; time.sleep(0.2)"])
    monkeypatch.setattr(runtime, "emit", lambda *args: None)
    request = {"account": sample(), "session_id": "ctrlc", "environment_id": "local", "path": str(tmp_path)}
    assert runtime.run_worker(request, tmp_path, dict(__import__('os').environ)) == 0


def test_bootstrap_deploys_equivalent_feature_source_with_integrity_check(tmp_path):
    import ast
    import base64
    import shlex
    import lzma
    import hashlib
    request = {"action": "launch", "account": sample(), "path": "/home/me/a ' ; $(bad)"}
    args = shlex.split(remote_command(request))
    tree = ast.parse(args[3])
    strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    sources = []
    for value in strings:
        try:
            sources.append(lzma.decompress(base64.b85decode(value)))
        except Exception:
            pass
    assert len(sources) == 1
    original = ast.parse(Path(runtime.__file__).read_bytes())
    original.body = [node for node in original.body if not (
        isinstance(node, (ast.FunctionDef, ast.ClassDef)) and
        (node.name.startswith("harness_") or
         (node.name.startswith("github_") and node.name not in SHARED_GITHUB)))]
    # Omit documentation and type hints, as the transport does; all executable
    # statements and argument defaults in the selected feature must still match.
    for node in ast.walk(original):
        if isinstance(node, ast.arg):
            node.annotation = None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.returns = None
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                node.body.pop(0)
                if not node.body:
                    node.body.append(ast.Pass())
    equivalent = ast.dump(original, include_attributes=False) == ast.dump(ast.parse(sources[0]), include_attributes=False)
    assert equivalent, "Transport changed executable statements or argument defaults"
    digest = hashlib.sha256(sources[0]).hexdigest()
    assert digest in strings
    checks = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    assert len(checks) == 1
    assert ast.unparse(checks[0]) == f"assert hashlib.sha256(b).hexdigest() == '{digest}'"
    assert any("runtime-" + digest + ".py" == value for value in strings)
    assert request["path"] not in args[3]


def test_refresh_unreachable_never_marks_remote_ended(tmp_path, monkeypatch):
    from vibe_launcher.runner import run_remote
    from vibe_launcher.store import Store
    import io
    monkeypatch.setattr("vibe_launcher.runner.ssh_args", lambda *a, **k: ["fake-ssh"])
    store = Store(tmp_path / "db.sqlite")
    account = store.save("accounts", sample())
    environment = store.save("environments", {"id": "host", "name": "Host", "kind": "ssh", "ssh_target": "host"})
    session = store.save("sessions", {"id": "session", "account_id": account["id"], "environment_id": "host",
                                      "name": "A", "path": "/tmp", "status": "running"})
    class Disconnected:
        stdout = io.BytesIO(b"")
        def wait(self):
            return 255
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Disconnected())
    assert run_remote(store, {"action": "list-sessions", "environment_id": "host", "nonce": "test"}, environment) == 255
    assert store.get("sessions", session["id"])["status"] == "unknown"


def test_remote_tmux_snapshot_does_not_contain_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "doctor", lambda *args: None)
    monkeypatch.setattr(runtime, "authenticate", lambda *args: {"VIBE_API_KEY": "fake-secret"})
    monkeypatch.setattr(runtime, "emit", lambda *args: None)
    calls = []
    def fake_tmux(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(runtime, "tmux", fake_tmux)
    request = {"action": "launch", "account": sample(), "session_id": "s", "environment_id": "host", "path": str(tmp_path)}
    runtime.perform(request, tmp_path / "state")
    assert calls[0][:4] == ("new-session", "-d", "-s", "vibe-s")
    assert calls[0][5] == str(tmp_path.resolve())
    snapshot = runtime.session_path(tmp_path / "state", "s").read_text(encoding="utf-8")
    assert "fake-secret" not in snapshot
    assert "fake-secret" not in str(calls)
    assert "PATH=" in calls[0][-1]
