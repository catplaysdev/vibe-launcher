import base64
import json
import lzma
from pathlib import Path

import pytest

from vibe_launcher import runtime
from vibe_launcher.service import LauncherService, _compressed_runtime, remote_command
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow


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
