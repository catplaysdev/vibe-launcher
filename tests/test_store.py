import json

import pytest

from vibe_launcher.store import Store


def test_initializes_local_environment_and_round_trips_records(tmp_path):
    store = Store(tmp_path / "launcher.db")
    local = store.get("environments", "local")
    assert (local["name"], local["kind"], local["ssh_target"]) == ("本機", "local", "")

    account = store.save(
        "accounts",
        {"name": "Codex", "tool": "codex", "auth": "subscription", "model": ""},
    )
    preset = store.save(
        "presets",
        {
            "name": "專案", "account_id": account["id"], "environment_id": "local",
            "path": "C:/work/project", "favorite": True,
        },
    )
    assert store.get("accounts", account["id"]) == account
    assert store.get("presets", preset["id"]) == preset


def test_rejects_invalid_account_and_unsafe_url(tmp_path):
    store = Store(tmp_path / "launcher.db")
    with pytest.raises(ValueError):
        store.save("accounts", {"name": "x", "tool": "unknown", "auth": "api", "model": "m"})
    with pytest.raises(ValueError):
        store.save(
            "accounts",
            {"name": "x", "tool": "codex", "auth": "api", "model": "m", "base_url": "https://u:p@example.test"},
        )


def test_rejects_invalid_ssh_and_project_paths(tmp_path):
    store = Store(tmp_path / "launcher.db")
    with pytest.raises(ValueError):
        store.save("environments", {"name": "bad", "kind": "ssh", "ssh_target": "-oProxyCommand=x"})
    account = store.save("accounts", {"name": "Codex", "tool": "codex", "auth": "subscription"})
    with pytest.raises(ValueError):
        store.save(
            "presets",
            {"name": "bad", "account_id": account["id"], "environment_id": "local", "path": "bad\npath"},
        )


def test_an_environment_in_use_is_protected_but_an_account_is_only_a_suggestion(tmp_path):
    store = Store(tmp_path / "launcher.db")
    account = store.save("accounts", {"name": "Codex", "tool": "codex", "auth": "subscription"})
    env = store.save("environments", {"name": "Box", "kind": "ssh", "ssh_target": "box"})
    preset = store.save(
        "presets",
        {"name": "P", "account_id": account["id"], "environment_id": env["id"], "path": "/p"},
    )
    with pytest.raises(ValueError, match="P"):
        store.delete("environments", env["id"])             # a project still lives there
    store.delete("accounts", account["id"])                  # the project merely loses its suggested account
    assert store.get("presets", preset["id"])["account_id"] == ""
    store.delete("presets", preset["id"])
    store.delete("environments", env["id"])
    with pytest.raises(KeyError):
        store.get("environments", env["id"])


def test_export_import_only_nonsecret_config_and_is_atomic(tmp_path):
    source = Store(tmp_path / "source.db")
    account = source.save(
        "accounts",
        {"name": "API", "tool": "claude", "auth": "api", "model": "model-x", "base_url": "http://localhost:8000", "allow_insecure_loopback": True},
    )
    source.save("auth_states", {"id": f"{account['id']}:local", "status": "ok", "checked_at": "now"})
    source.save("sessions", {"preset_id": "", "account_id": account["id"], "environment_id": "local", "name": "secret-session", "path": "C:/p"})
    exported = tmp_path / "config.json"
    source.export_config(exported)
    data = json.loads(exported.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert "sessions" not in data and "auth_states" not in data
    assert "secret-session" not in exported.read_text(encoding="utf-8")

    target = Store(tmp_path / "target.db")
    target.import_config(exported)
    assert target.get("accounts", account["id"])["model"] == "model-x"
    assert target.list("sessions") == []

    before = target.list("accounts")
    exported.write_text(json.dumps({"version": 1, "accounts": [{"id": "x", "name": "x", "token": "secret"}], "environments": [], "presets": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        target.import_config(exported)
    assert target.list("accounts") == before


def test_rejects_import_with_broken_references(tmp_path):
    store = Store(tmp_path / "launcher.db")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"version": 1, "accounts": [], "environments": [], "presets": [{"id": "p", "name": "p", "account_id": "missing", "environment_id": "local", "path": "C:/p", "favorite": False}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        store.import_config(config)
    assert store.list("presets") == []
@pytest.mark.parametrize("kind,original,changed", [
    ("accounts", {"name": "a", "tool": "codex", "auth": "subscription"}, {"auth": "api", "model": "m"}),
    ("environments", {"name": "h", "kind": "ssh", "ssh_target": "host-a"}, {"ssh_target": "host-b"}),
])
def test_identity_cannot_be_retargeted(tmp_path, kind, original, changed):
    store = Store(tmp_path / "identity.db")
    record = store.save(kind, original)
    with pytest.raises(ValueError, match="identity"):
        store.save(kind, {**record, **changed})


def test_account_id_cannot_be_a_path(tmp_path):
    store = Store(tmp_path / "identity.db")
    with pytest.raises(ValueError):
        store.save("accounts", {"id": "../../escape", "name": "a", "tool": "codex", "auth": "subscription"})


def test_import_cannot_retarget_identity(tmp_path):
    store = Store(tmp_path / "identity.db")
    record = store.save("environments", {"name": "h", "kind": "ssh", "ssh_target": "host-a"})
    path = tmp_path / "import.json"
    store.export_config(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    next(env for env in payload["environments"] if env["id"] == record["id"])["ssh_target"] = "host-b"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        store.import_config(path)


@pytest.mark.parametrize("url,allow,valid", [
    ("https://api.example.test/v1", False, True),
    ("http://localhost:8000/v1", True, True),
    ("http://127.0.0.8:8000", True, True),
    ("http://[::1]:8000", True, True),
    ("http://localhost:8000", False, False),
    ("http://example.test", True, False),
    ("https://u:p@example.test", False, False),
    ("https://example.test?key=x", False, False),
    ("https://example.test:bad", False, False),
    ("https://example.test\nX-Evil: yes", False, False),
])
def test_api_url_requires_https_except_opted_in_loopback(tmp_path, url, allow, valid):
    store = Store(tmp_path / "urls.db")
    account = {
        "name": "API", "tool": "codex", "auth": "api", "model": "m",
        "base_url": url, "allow_insecure_loopback": allow,
    }
    if valid:
        store.save("accounts", account)
    else:
        with pytest.raises(ValueError):
            store.save("accounts", account)


def test_account_endpoint_rotates_revision_and_clears_auth_state_even_when_changed_back(tmp_path):
    store = Store(tmp_path / "revision.db")
    account = store.save("accounts", {
        "name": "API", "tool": "codex", "auth": "api", "model": "m",
        "base_url": "https://one.example",
    })
    old_revision = account["credential_revision"]
    store.save("auth_states", {"id": f"{account['id']}:local", "status": "ok"})
    changed = store.save("accounts", {**account, "base_url": "https://two.example"})
    assert changed["credential_revision"] != old_revision
    assert store.list("auth_states") == []
    changed_back = store.save("accounts", {**changed, "base_url": "https://one.example"})
    assert changed_back["credential_revision"] != old_revision


def test_ssh_security_policy_rotates_epoch_and_revoke_preserves_remember_policy(tmp_path):
    store = Store(tmp_path / "ssh-security.db")
    environment = store.save("environments", {
        "name": "SSH", "kind": "ssh", "ssh_target": "host", "ssh_auth_mode": "password",
        "remember_seconds": 3600,
    })
    original_epoch = environment["credential_epoch"]
    changed = store.save("environments", {**environment, "ssh_auth_mode": "key"})
    assert changed["credential_epoch"] != original_epoch
    assert changed["remember_seconds"] == 0
    revoked = store.revoke_ssh(environment["id"])
    assert revoked["credential_epoch"] != changed["credential_epoch"]
    assert revoked["remember_seconds"] == 0
    with pytest.raises(ValueError):
        store.revoke_ssh("local")


def test_import_drops_internal_revisions_and_disables_password_memory(tmp_path):
    source = Store(tmp_path / "import-source.db")
    account = source.save("accounts", {
        "name": "API", "tool": "claude", "auth": "api", "model": "m",
        "base_url": "https://api.example",
    })
    environment = source.save("environments", {
        "name": "SSH", "kind": "ssh", "ssh_target": "host", "ssh_auth_mode": "password",
        "remember_seconds": 3600,
    })
    exported = tmp_path / "safe-config.json"
    source.export_config(exported)
    raw = json.loads(exported.read_text(encoding="utf-8"))
    account_row = next(row for row in raw["accounts"] if row["id"] == account["id"])
    environment_row = next(row for row in raw["environments"] if row["id"] == environment["id"])
    assert "credential_revision" not in account_row
    assert "credential_epoch" not in environment_row
    assert environment_row["remember_seconds"] == 0

    target = Store(tmp_path / "import-target.db")
    target.import_config(exported)
    imported_account = target.get("accounts", account["id"])
    imported_environment = target.get("environments", environment["id"])
    assert imported_account["credential_revision"] != account["credential_revision"]
    assert imported_environment["credential_epoch"] != environment["credential_epoch"]
    assert imported_environment["remember_seconds"] == 0

    account_row["credential_revision"] = "forged"
    exported.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        target.import_config(exported)


def test_legacy_ssh_remember_setting_is_cleared_on_migration(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE accounts (id TEXT PRIMARY KEY, payload TEXT NOT NULL); CREATE TABLE environments (id TEXT PRIMARY KEY, payload TEXT NOT NULL); CREATE TABLE presets (id TEXT PRIMARY KEY, payload TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id), environment_id TEXT NOT NULL REFERENCES environments(id)); CREATE TABLE sessions (id TEXT PRIMARY KEY, payload TEXT NOT NULL, preset_id TEXT REFERENCES presets(id), account_id TEXT NOT NULL REFERENCES accounts(id), environment_id TEXT NOT NULL REFERENCES environments(id)); CREATE TABLE auth_states (id TEXT PRIMARY KEY, payload TEXT NOT NULL);")
    connection.execute("INSERT INTO environments VALUES (?, ?)", ("host", json.dumps({"id": "host", "name": "SSH", "kind": "ssh", "ssh_target": "host", "remember_seconds": 3600})))
    connection.commit()
    connection.close()
    migrated = Store(path).get("environments", "host")
    assert migrated["ssh_auth_mode"] == "interactive"
    assert migrated["remember_seconds"] == 0
