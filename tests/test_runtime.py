"""The runtime and the jobs around it: launches, lifecycle, helpers' integrity, the service, start-up.

- runtime
- lifecycle
- production integration
- service
- prestart failure
- concurrent launchers: Several terminals start at once (opening a workspace), so launcher processes share one data folder.
- snap stop
"""
import base64
import io
import json
import lzma
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from vibe_launcher import askpass, runner, runtime
from vibe_launcher.runner import record_event
from vibe_launcher.runtime import build_cli, isolated_env, profile_dir, validate_request
from vibe_launcher.service import SHARED_GITHUB, _compressed_runtime, LauncherService, remote_command, ssh_args
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow


# --- runtime ----------------------------------------------------------------------------------------------------------
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
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)   # CI machines have no Codex/Claude installed
    monkeypatch.setattr(runtime, "prepare", lambda *args: {})
    monkeypatch.setattr(runtime, "check_login", lambda *args: False)
    monkeypatch.setattr(runtime.subprocess, "call", lambda *args, **kwargs: 1)
    events = []
    monkeypatch.setattr(runtime, "emit", lambda req, event: events.append(event))
    with pytest.raises(RuntimeError, match="Login incomplete"):
        runtime.authenticate({"account": account()}, tmp_path)
    assert events[-1]["auth_status"] == "unknown"


def test_tmux_attaches_as_utf8_and_keeps_c_locale_only_for_parsed_output(monkeypatch):
    import subprocess
    from vibe_launcher import runtime
    calls = []
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs["env"])) or subprocess.CompletedProcess(argv, 0))
    runtime.tmux("attach-session", "-t", "x")
    runtime.tmux("has-session", "-t", "x", capture=True)
    assert calls[0][0][:2] == ["/usr/bin/tmux", "-u"] and calls[0][1].get("LC_ALL") != "C"
    assert calls[1][1]["LC_ALL"] == "C"


@pytest.mark.parametrize("env, expected", [
    ({}, ["-u", "LC_ALL", "LC_CTYPE=C.UTF-8"]),
    ({"LANG": "en_US.UTF-8"}, ["-u", "LC_ALL"]),
    ({"LC_ALL": "zh_TW.utf8"}, []),
    ({"LC_ALL": "C"}, ["LC_CTYPE=C.UTF-8"]),
])
def test_tmux_worker_gets_a_utf8_locale(env, expected):
    from vibe_launcher import runtime
    assert runtime.utf8_locale(env) == expected


def test_a_tmux_worker_prints_no_events_into_the_window_the_user_sees(capsys):
    runtime.emit({"action": "worker", "nonce": "n"}, {"status": "running"})
    assert capsys.readouterr().out == ""
    runtime.emit({"action": "list-sessions", "nonce": "n"}, {"status": "running"})
    assert "VIBE_EVENT_n:" in capsys.readouterr().out


# --- lifecycle --------------------------------------------------------------------------------------------------------
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


@pytest.mark.parametrize("account", [sample(), dict(sample(), tool="claude"), dict(sample(), tool="claude", auth="api", model="m")])
def test_bootstrap_deploys_equivalent_feature_source_with_integrity_check(tmp_path, account):
    import ast
    import base64
    import shlex
    import lzma
    import hashlib
    from vibe_launcher.service import _shipped_node, remote_feature
    request = {"action": "launch", "account": account, "path": "/home/me/a ' ; $(bad)"}
    args = shlex.split(remote_command(request))
    tree = ast.parse(args[3])
    strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    sources = []
    for value in strings:
        try:
            sources.append(lzma.decompress(base64.b85decode(value)))
        except Exception:
            pass
    if len(args) == 5:
        # The compact transport: the helper travels as its own argument, 14 bits per character.
        codes = [ord(char) - 0x4E00 for char in args[4]]
        raw = b"".join((codes[i] << 42 | codes[i + 1] << 28 | codes[i + 2] << 14 | codes[i + 3]).to_bytes(7, "big")
                       for i in range(0, len(codes), 4))
        size = next(node.upper.value for node in ast.walk(tree) if isinstance(node, ast.Slice)
                    and isinstance(node.upper, ast.Constant))
        sources.append(lzma.decompress(raw[:size]))
    assert len(sources) == 1
    feature = remote_feature(request)
    assert feature == ("managed" if runtime.switch_supported(account) else "cli")
    original = ast.parse(Path(runtime.__file__).read_bytes())
    original.body = [node for node in original.body if _shipped_node(node, feature)]
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
    assert len(remote_command(request)) <= 28000
    # A damaged helper exits with "not started" (78) before anything runs; not an assert, which -O would drop.
    checks = [node for node in ast.walk(tree) if isinstance(node, ast.IfExp)]
    assert len(checks) == 1
    assert ast.unparse(checks[0]) == f"sys.exit(78) if hashlib.sha256(b).hexdigest() != '{digest}' else None"
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
        return subprocess.CompletedProcess(args, 0, b"@1\n" if args[0] in {"new-window", "new-session"} else b"", b"")
    monkeypatch.setattr(runtime, "tmux", fake_tmux)
    request = {"action": "launch", "account": sample(), "session_id": "s", "environment_id": "host", "path": str(tmp_path)}
    runtime.perform(request, tmp_path / "state")
    assert calls[0][:3] == ("new-window", "-t", "=vibe-ws-default:")
    assert calls[0][calls[0].index("-c") + 1] == str(tmp_path.resolve())
    snapshot = runtime.session_path(tmp_path / "state", "s").read_text(encoding="utf-8")
    assert "fake-secret" not in snapshot
    assert "fake-secret" not in str(calls)
    assert "PATH=" in calls[0][-1]


# --- production integration -------------------------------------------------------------------------------------------
def test_github_export_requires_revalidation_and_preserves_separation(tmp_path):
    source = Store(tmp_path / 'source.sqlite3')
    gh = source.save('github_accounts', {'id': 'gh', 'name': 'GitHub', 'environment_id': 'local', 'login': 'user', 'status': 'authenticated', 'credential_storage': 'unverified'})
    project = source.save('presets', {'id': 'p', 'name': 'Repo', 'path': 'C:/repo', 'environment_id': 'local', 'github_account_id': gh['id'], 'github_repo': 'https://github.com/owner/repo', 'github_remote': 'upstream', 'github_branch': 'main', 'github_access': 'accessible'})
    destination = tmp_path / 'export.json'
    source.export_config(destination)
    output = json.loads(destination.read_text(encoding='utf-8'))
    assert output['github_accounts'] == [{'id': 'gh', 'name': 'GitHub', 'environment_id': 'local'}]
    assert 'github_access' not in output['presets'][0]
    target = Store(tmp_path / 'target.sqlite3')
    target.import_config(destination)
    assert target.get('github_accounts', 'gh')['status'] == ''
    assert target.get('presets', 'p')['github_remote'] == 'upstream'
    ai = target.save('accounts', {'id': 'ai', 'name': 'AI', 'tool': 'claude', 'auth': 'subscription'})
    target.save('presets', {**target.get('presets', 'p'), 'account_id': ai['id']})
    assert target.get('presets', 'p')['github_account_id'] == 'gh'


def test_foreign_github_binding_rejected_and_live_service_prevents_delete(tmp_path):
    store = Store(tmp_path / 'data.sqlite3')
    store.save('environments', {'id': 'remote', 'name': 'SSH', 'kind': 'ssh', 'ssh_target': 'example'})
    gh = store.save('github_accounts', {'id': 'gh', 'name': 'GH', 'environment_id': 'remote'})
    with pytest.raises(ValueError, match='another environment'):
        store.save('presets', {'name': 'wrong', 'environment_id': 'local', 'path': 'C:/x', 'github_account_id': gh['id']})
    store.save('accounts', {'id': 'h', 'name': 'Harness', 'tool': 'deepseek-harness', 'auth': 'subscription', 'environment_id': 'remote'})
    store.save('web_services', {'id': 'service', 'service_key': 'service', 'tool': 'deepseek-harness', 'account_id': 'h', 'github_account_id': 'gh', 'environment_id': 'remote', 'status': 'unknown', 'port': 3080, 'local_port': 3080})
    with pytest.raises(ValueError, match='共享'):
        store.delete('github_accounts', 'gh')


def test_transport_cache_integrity_feature_dependencies_and_length_gate():
    source = Path(runtime.__file__).read_bytes()
    _compressed_runtime.cache_clear()
    for feature in ('cli', 'github', 'harness'):
        digest, compressed = _compressed_runtime(source, feature)
        module = lzma.decompress(base64.b85decode(compressed))
        assert __import__('hashlib').sha256(module).hexdigest() == digest
        namespace = {'__name__': 'proof'}
        exec(compile(module, 'runtime-proof.py', 'exec'), namespace)
        assert 'github_env' in namespace and 'github_session_env' in namespace and 'github_helper' in namespace
        if feature == 'harness':
            assert 'github_helper' in namespace and 'harness_action' in namespace
    _compressed_runtime(source, 'cli')
    assert _compressed_runtime.cache_info().hits == 1
    with pytest.raises(ValueError, match='長度'):
        # Incompressible: the compact transport compresses the request, so repetition alone no longer overflows.
        remote_command({'action': 'doctor', 'path': ''.join(__import__('hashlib').sha256(bytes([i % 256, i // 256])).hexdigest()
                                                            for i in range(2000))})


def test_poll_unchanged_skips_render_and_failure_visible(tmp_path, qtbot, monkeypatch):
    class Service:
        def start(self, *args, **kwargs):
            return 'op'
    store = Store(tmp_path / 'gui.sqlite3')
    view = MainWindow(store, Service())
    qtbot.addWidget(view, before_close_func=lambda w: w.workspace.shutdown())
    view.timer.stop()
    view._poll()
    calls = []
    for name in ('_render_projects', '_render_sessions', '_render_workspace', '_render_records', '_render_environments'):
        monkeypatch.setattr(view, name, lambda *args, **kwargs: calls.append(args))
    view._poll()
    assert not calls
    monkeypatch.setattr(store, 'list_many', lambda *args: (_ for _ in ()).throw(OSError('secret-path')))
    view._poll()
    assert '更新失敗' in view.statusBar().currentMessage()
    assert 'secret-path' not in (tmp_path / 'diagnostics/launcher.log').read_text(encoding='utf-8')


def test_harness_explicit_unbound_ignores_projects_github(tmp_path):
    store = Store(tmp_path / 'jobs.sqlite3')
    store.save('environments', {'id': 'ssh', 'name': 'SSH', 'kind': 'ssh', 'ssh_target': 'example'})
    store.save('accounts', {'id': 'h', 'name': 'H', 'tool': 'deepseek-harness', 'auth': 'subscription', 'environment_id': 'ssh'})
    store.save('github_accounts', {'id': 'gh', 'name': 'GH', 'environment_id': 'ssh'})
    store.save('presets', {'id': 'p', 'name': 'P', 'path': '/repo', 'environment_id': 'ssh', 'github_account_id': 'gh'})
    LauncherService(store).start('harness', preset_id='p', account_id='h', github_account_id='', operation={'harness_operation': 'status'}, spawn=lambda *args: None)
    job = json.loads(next((tmp_path / 'jobs').glob('*.json')).read_text(encoding='utf-8'))
    assert 'github_account' not in job['request']


@pytest.mark.parametrize('action', ['github', 'harness', 'import-settings', 'import-history'])
def test_operations_dispatch_before_cli_doctor(tmp_path, monkeypatch, action):
    account = {'id': 'ai', 'name': 'AI', 'tool': 'codex', 'auth': 'subscription'}
    if action == 'harness':
        account.update(tool='deepseek-harness', environment_id='ssh')
    request = {'action': action, 'environment_id': 'ssh', 'operation_id': 'op', 'nonce': 'n'}
    if action == 'github':
        request['github_account'] = {'id': 'gh', 'environment_id': 'ssh'}
    else:
        request['account'] = account
    monkeypatch.setattr(runtime, 'doctor', lambda *args: pytest.fail('CLI doctor must not handle a Web/GitHub/import operation'))
    result = {'status': 'done'}
    monkeypatch.setattr(runtime, 'github_action', lambda *args: result)
    monkeypatch.setattr(runtime, 'harness_action', lambda *args: result)
    monkeypatch.setattr(runtime, 'manual_import', lambda *args: result)
    assert runtime.perform(request, tmp_path) == 0
    output = json.loads((tmp_path / 'results/op.json').read_text(encoding='utf-8'))
    assert output['result'] == result and output['action'] == action


def test_github_job_runner_reports_account_without_ai_profile(tmp_path, qtbot, monkeypatch):
    from vibe_launcher import runner
    store = Store(tmp_path / 'gui.sqlite3')
    gh = store.save('github_accounts', {'id': 'gh', 'name': 'My GitHub', 'environment_id': 'local'})
    service = LauncherService(store)
    view = MainWindow(store, service)
    qtbot.addWidget(view, before_close_func=lambda w: w.workspace.shutdown())
    view.timer.stop()
    args = []
    operation_id = service.start('github', github_account_id='gh', operation={'github_operation': 'status'}, spawn=lambda argv, ctx: args.extend(argv))
    monkeypatch.setattr(runner.sys, 'argv', ['runner', args[-1]])
    monkeypatch.setenv('VIBE_EMBEDDED', '1')
    monkeypatch.setattr(runtime, 'github_action', lambda request, root: {'status': 'authenticated', 'login': 'my-user', 'credential_storage': 'unverified'})
    monkeypatch.setattr(runtime, 'doctor', lambda *args: pytest.fail('GitHub must not ask for an AI account'))
    monkeypatch.setattr(runtime, 'emit', runtime.emit)
    assert runner.main() == 0
    results = []
    view._operations[operation_id] = {'callback': results.append, 'action': 'github', 'since': __import__('time').monotonic()}
    view._poll_operations()
    assert results == [{'status': 'authenticated', 'login': 'my-user', 'credential_storage': 'unverified'}]
    assert store.list('sessions') == []


# --- service ----------------------------------------------------------------------------------------------------------
def setup(store):
    account = store.save("accounts", {"name": "A", "tool": "codex", "auth": "subscription"})
    preset = store.save("presets", {"name": "Project", "account_id": account["id"],
                                    "environment_id": "local", "path": "C:/Project"})
    return account, preset


def test_launch_creates_immutable_nonsecret_request(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    account, preset = setup(store)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    LauncherService(store).start("launch", preset_id=preset["id"])
    job = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    account["name"] = "Changed"
    store.save("accounts", account)
    assert job["request"]["account"]["name"] == "A"
    assert job["request"]["path"] == "C:/Project"
    assert store.list("sessions")[0]["status"] == "queued"
    assert calls[0][1]["creationflags"] == subprocess.CREATE_NEW_CONSOLE


def test_status_events_do_not_mix_hosts(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    account, preset = setup(store)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: None)
    LauncherService(store).start("launch", preset_id=preset["id"])
    session = store.list("sessions")[0]
    request = {"account": account, "environment_id": "other", "session_id": session["id"]}
    record_event(store, request, {"status": "ended"})
    assert store.get("sessions", session["id"])["status"] == "queued"
    request["environment_id"] = "local"
    record_event(store, request, {"status": "ready", "auth_status": "configured"})
    assert store.get("sessions", session["id"])["status"] == "queued"
    assert store.list("auth_states")[0]["status"] == "configured"


def test_cancelled_login_does_not_stay_waiting(tmp_path):
    store = Store(tmp_path / "state.db")
    account, _ = setup(store)
    request = {"account": account, "environment_id": "local", "action": "login"}
    record_event(store, request, {"auth_status": "waiting_auth"})
    record_event(store, request, {"status": "unknown"})
    assert store.list("auth_states")[0]["status"] == "unknown"


# --- prestart failure -------------------------------------------------------------------------------------------------
def prestart_setup(tmp_path, status="connecting"):
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
    store, env, request = prestart_setup(tmp_path)
    fake_ssh(monkeypatch, runtime.EXIT_NOT_STARTED)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "failed"


def test_other_remote_failures_stay_unknown_because_work_may_be_running(tmp_path, monkeypatch):
    for code in (1, 255, 130):
        store, env, request = prestart_setup(tmp_path / str(code))
        fake_ssh(monkeypatch, code)
        runner.run_remote(store, request, env)
        assert store.get("sessions", "s")["status"] == "unknown", code


def test_prestart_exit_code_only_applies_to_launch(tmp_path, monkeypatch):
    store, env, request = prestart_setup(tmp_path, status="running")
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


# --- concurrent launchers ---------------------------------------------------------------------------------------------
def test_private_path_check_tolerates_files_that_come_and_go(tmp_path):
    target = tmp_path / "state.sqlite3-wal"
    stop = threading.Event()
    errors = []

    def churn():
        while not stop.is_set():
            try:
                target.write_text("x", encoding="utf-8")
                target.unlink()
            except OSError:
                pass

    worker = threading.Thread(target=churn, daemon=True)
    worker.start()
    try:
        for _ in range(4000):
            try:
                runtime.assert_private_path(target)
            except Exception as error:  # a vanished file must never surface as an error
                errors.append(repr(error))
                break
    finally:
        stop.set()
        worker.join(2)
    assert errors == []


def test_the_safety_checks_still_reject_what_they_should(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    runtime.assert_private_path(real)
    runtime.assert_private_path(tmp_path / "missing.txt")
    link = tmp_path / "hard.txt"
    try:
        os.link(real, link)
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(ValueError, match="hard links"):
        runtime.assert_private_path(real)


def test_many_launchers_can_use_one_store_at_once(tmp_path):
    path = tmp_path / "shared.sqlite3"
    Store(path).save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"})
    errors = []

    def launcher(n):
        try:
            store = Store(path)
            for i in range(25):
                store.list("accounts")
                store.save("sessions", {"id": f"s{n}x{i}", "account_id": "a", "environment_id": "local", "name": "n",
                                        "path": "p", "status": "running"})
        except Exception as error:
            errors.append(repr(error))

    threads = [threading.Thread(target=launcher, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert len(Store(path).list("sessions")) == 6 * 25


# --- snap stop --------------------------------------------------------------------------------------------------------
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
