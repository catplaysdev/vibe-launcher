import json

from vibe_launcher.store import Store


def test_environment_has_optional_ttl_and_export_disables_it(tmp_path):
    store = Store(tmp_path / "db")
    env = store.save("environments", {"name": "Host", "kind": "ssh", "ssh_target": "work", "ssh_auth_mode": "password", "remember_seconds": 900})
    assert env["remember_seconds"] == 900
    path = tmp_path / "config.json"
    store.export_config(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert next(e for e in data["environments"] if e["id"] == env["id"])["remember_seconds"] == 0


def test_ttl_expiry_and_host_binding(monkeypatch):
    from vibe_launcher import askpass
    vault = {}
    monkeypatch.setattr(askpass, "windows_secret", lambda account, value=None: vault.setdefault(account["id"], value) if value else vault.get(account["id"]))
    monkeypatch.setattr(askpass, "delete_secret", lambda account: vault.pop(account["id"], None))
    profile = {"tool": "ssh", "id": "work"}
    askpass.save_password(profile, "private", "host1", 900, clock=100)
    assert askpass.cached_password(profile, "host1", 900, clock=999) == "private"
    assert askpass.cached_password(profile, "host2", 900, clock=999) is None
    assert askpass.cached_password(profile, "host1", 900, clock=1000) is None
    assert vault == {}


def test_only_exact_target_password_prompt_can_reuse_secret():
    from vibe_launcher.askpass import is_password_prompt
    expected = "alice@real-host's password:"
    assert is_password_prompt(expected, expected)
    for prompt in ("Password:", "Verification code:", "alice@jump's password:", "Enter passphrase for key:", "OTP password:"):
        assert not is_password_prompt(prompt, expected)


def test_disabled_uses_original_terminal_without_touching_vault(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    monkeypatch.setattr(askpass.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no SSH resolution")))
    assert askpass.ssh_environment({"id": "host", "ssh_target": "work", "remember_seconds": 0}, tmp_path) is None


def test_native_bridge_ignores_cwd_and_pythonpath_module_hijack(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from vibe_launcher.askpass import build_bridge
    package = tmp_path / "vibe_launcher"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "askpass.py").write_text("print('synthetic-test-response')", encoding="utf-8")
    bridge = build_bridge(tmp_path)
    env = {**os.environ, "VIBE_SSH_PYTHON": sys.executable, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run([str(bridge.resolve()), "test prompt"], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 1  # Real isolated module rejects missing launch context.
    assert result.stdout == b""
    assert result.stderr == b""


def test_forget_button_clears_only_selected_environment(tmp_path, qtbot, monkeypatch):
    from vibe_launcher import askpass
    from vibe_launcher.ui import MainWindow
    store = Store(tmp_path / "ui.db")
    store.save("environments", {"id": "host", "name": "Host", "kind": "ssh", "ssh_target": "work", "remember_seconds": 900})
    cleared = []
    monkeypatch.setattr(askpass, "delete_secret", lambda profile: cleared.append(profile))
    view = MainWindow(store, object())
    qtbot.addWidget(view)
    view.environments_list.setCurrentRow(0)
    view.forget_ssh_password()
    assert cleared == [{"tool": "ssh", "id": "host"}]


def test_native_bridge_propagates_cancel_without_output(tmp_path):
    import os
    import subprocess
    import sys
    from vibe_launcher.askpass import build_bridge
    package = tmp_path / "vibe_launcher"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "askpass.py").write_text("raise SystemExit(1)", encoding="utf-8")
    result = subprocess.run([str(build_bridge(tmp_path).resolve()), "OTP"], cwd=tmp_path,
                            env={**os.environ, "VIBE_SSH_PYTHON": sys.executable, "PYTHONPATH": str(tmp_path)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 1
    assert result.stdout == b""


def cache_context(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store = Store(tmp_path / "db.sqlite")
    host = store.save("environments", {"id": "host", "name": "Host", "kind": "ssh", "ssh_target": "work", "ssh_auth_mode": "password", "remember_seconds": 900})
    vault = {}
    def secret(profile, value=None):
        if value is not None:
            vault[profile["id"]] = value
        return vault.get(profile["id"])
    monkeypatch.setattr(askpass, "windows_secret", secret)
    monkeypatch.setattr(askpass, "delete_secret", lambda profile: vault.pop(profile["id"], None))
    monkeypatch.setattr(askpass.ssh_security, "host_binding", lambda *a: "verified-host-fingerprint")
    with askpass.security_lock(tmp_path, "host") as (_, epoch):
        pass
    env = {"VIBE_SSH_ROOT": str(tmp_path), "VIBE_SSH_ID": "host", "VIBE_SSH_ATTEMPT": "attempt",
           "VIBE_SSH_DATABASE": str(store.path), "VIBE_SSH_EPOCH": epoch,
           "VIBE_SSH_CONFIG_EPOCH": host["credential_epoch"], "VIBE_SSH_CACHE": "1", "VIBE_SSH_SETTINGS": "{}"}
    return store, host, vault, env


def test_password_only_promoted_after_authenticated_channel(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic-中文")
    assert "host" not in vault
    assert askpass.password_operation(env) is None
    askpass.password_operation(env, promote=True)
    assert askpass.password_operation(env) == "synthetic-中文"
    assert "attempt" not in vault


def test_lock_invalidates_pending_and_old_windows(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    askpass.revoke_password("host", tmp_path)
    askpass.password_operation(env, promote=True)
    assert "host" not in vault
    before = dict(vault)
    askpass.password_operation(env, "old-window-input")
    assert vault == before
    assert askpass.password_operation(env) is None


def test_policy_disable_then_reenable_never_revives_old_window(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    host["remember_seconds"] = 0
    host = store.save("environments", host)
    host["remember_seconds"] = 900
    store.save("environments", host)
    askpass.password_operation(env, promote=True)
    assert "host" not in vault
    assert askpass.password_operation(env) is None


def test_otp_or_unverified_host_never_uses_cache(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    askpass.password_operation(env, promote=True)
    assert askpass.password_operation({**env, "VIBE_SSH_CACHE": "0"}) is None
    monkeypatch.setattr(askpass.ssh_security, "host_binding", lambda *a: None)
    assert askpass.password_operation(env) is None


def test_replaced_host_binding_never_receives_saved_password(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    askpass.password_operation(env, promote=True)
    monkeypatch.setattr(askpass.ssh_security, "host_binding", lambda *a: "different-host-fingerprint")
    assert askpass.password_operation(env) is None


def test_existing_bridge_binary_is_never_reused(tmp_path):
    from vibe_launcher.askpass import build_bridge
    first = build_bridge(tmp_path)
    first.write_bytes(b"replaced executable")
    second = build_bridge(tmp_path)
    assert first != second
    assert second.read_bytes().startswith(b"MZ")
