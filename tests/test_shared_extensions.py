"""A launcher profile is isolated, but the user's own skills / hooks / agents must still work inside it."""
import json
import os

import pytest

from vibe_launcher import runtime

CLAUDE = {"id": "c1", "name": "C", "tool": "claude", "auth": "subscription"}
CODEX = {"id": "x1", "name": "X", "tool": "codex", "auth": "subscription"}


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    base = tmp_path / "home" / ".claude"
    _write(base / "skills" / "review" / "SKILL.md", "# review")
    _write(base / "agents" / "planner.md", "# planner")
    _write(base / "commands" / "ship.md", "# ship")
    _write(base / "hooks" / "gate.cmd", "echo hi")
    _write(base / "CLAUDE.md", "@RTK.md")
    _write(base / "RTK.md", "rtk")
    _write(base / ".credentials.json", '{"token": "SECRET"}')
    _write(base / "skills" / "auth.json", "SECRET")
    _write(base / "history.jsonl", "private history")
    _write(base / "projects" / "p" / "log.jsonl", "private")
    _write(base / "settings.json", json.dumps({
        "model": "claude-x", "theme": "dark",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}]},
        "permissions": {"allow": ["Bash(ls)"]},
        "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1", "ANTHROPIC_API_KEY": "SECRET",
                "CLAUDE_CODE_USE_BEDROCK": "1", "MY_TOKEN": "SECRET", "TEAM_FLAG": "on"},
        "apiKeyHelper": "get-key"}))
    return tmp_path / "home"


def test_skills_agents_hooks_and_notes_reach_the_profile(tmp_path, home):
    root = tmp_path / "data"
    summary = runtime.sync_extensions(CLAUDE, root, home)
    profile = runtime.profile_dir(root, CLAUDE)
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review"
    assert (profile / "agents" / "planner.md").is_file()
    assert (profile / "commands" / "ship.md").is_file()
    assert (profile / "hooks" / "gate.cmd").is_file()
    assert (profile / "CLAUDE.md").is_file() and (profile / "RTK.md").is_file()
    assert summary["copied"] >= 6


def test_credentials_and_history_never_travel(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(CLAUDE, root, home)
    profile = runtime.profile_dir(root, CLAUDE)
    assert not (profile / ".credentials.json").exists()
    assert not (profile / "skills" / "auth.json").exists()
    assert not (profile / "history.jsonl").exists()
    assert not (profile / "projects").exists()
    assert "SECRET" not in "".join(p.read_text(encoding="utf-8", errors="ignore") for p in profile.rglob("*") if p.is_file())


def test_settings_keep_hooks_but_drop_routing_and_secrets(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(CLAUDE, root, home)
    settings = json.loads((runtime.profile_dir(root, CLAUDE) / "settings.json").read_text(encoding="utf-8"))
    assert "SessionStart" in settings["hooks"] and settings["permissions"]["allow"] == ["Bash(ls)"]
    assert settings["env"] == {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1", "TEAM_FLAG": "on"}
    assert settings["model"] == "claude-x" and "apiKeyHelper" not in settings
    # the launcher's own guard against provider overrides must still accept what we wrote
    runtime.check_settings(CLAUDE, root, tmp_path)


def test_resync_follows_the_source_but_spares_the_profiles_own_files(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(CLAUDE, root, home)
    profile = runtime.profile_dir(root, CLAUDE)
    _write(profile / "skills" / "mine" / "SKILL.md", "profile only")
    (home / ".claude" / "agents" / "planner.md").unlink()
    _write(home / ".claude" / "skills" / "review" / "SKILL.md", "# review v2")
    again = runtime.sync_extensions(CLAUDE, root, home)
    assert (profile / "agents" / "planner.md").exists()
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review v2"
    assert (profile / "skills" / "mine" / "SKILL.md").read_text(encoding="utf-8") == "profile only"
    assert again["removed"] == 0
    assert runtime.sync_extensions(CLAUDE, root, home)["copied"] == 0     # nothing changed, nothing rewritten


def test_a_hook_removed_from_the_users_settings_is_preserved_in_the_profile(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(CLAUDE, root, home)
    path = home / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["hooks"]
    path.write_text(json.dumps(data), encoding="utf-8")
    runtime.sync_extensions(CLAUDE, root, home)
    settings = json.loads((runtime.profile_dir(root, CLAUDE) / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" in settings and "permissions" in settings


def test_claude_personal_settings_and_deletions_survive_resync(tmp_path, home):
    root = tmp_path / "data"
    runtime.sync_extensions(CLAUDE, root, home)
    settings_path = runtime.profile_dir(root, CLAUDE) / "settings.json"
    personal = json.loads(settings_path.read_text(encoding="utf-8"))
    personal["model"] = "personal-model"
    personal["permissions"] = {"allow": ["Read"]}
    personal["env"]["TEAM_FLAG"] = "personal"
    del personal["hooks"]
    _write(settings_path, json.dumps(personal))
    source_path = home / ".claude" / "settings.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source.update(model="new-model", theme="light")
    source["env"].update(TEAM_FLAG="new", NEW_FLAG="yes")
    _write(source_path, json.dumps(source))
    for _ in range(2):
        runtime.sync_extensions(CLAUDE, root, home)
        actual = json.loads(settings_path.read_text(encoding="utf-8"))
        assert actual["model"] == "personal-model"
        assert actual["permissions"] == {"allow": ["Read"]}
        assert "hooks" not in actual
        assert actual["theme"] == "light"
        assert actual["env"]["TEAM_FLAG"] == "personal"
        assert actual["env"]["NEW_FLAG"] == "yes"


def test_codex_follows_source_until_personally_edited(tmp_path):
    pytest.importorskip("tomllib")
    home, root = tmp_path / "home", tmp_path / "data"
    source = home / ".codex" / "config.toml"
    profile = runtime.profile_dir(root, CODEX) / "config.toml"
    _write(source, 'model = "first"\napproval_policy = "on-request"\n')
    runtime.sync_extensions(CODEX, root, home)
    _write(source, 'model = "second"\napproval_policy = "never"\n')
    runtime.sync_extensions(CODEX, root, home)
    assert '"second"' in profile.read_text(encoding="utf-8")
    personal = b'# keep comment\nmodel = "mine"\n[projects.local]\ntrust_level = "trusted"\n'
    profile.write_bytes(personal)
    _write(source, 'model = "third"\n')
    for _ in range(2):
        runtime.sync_extensions(CODEX, root, home)
        assert profile.read_bytes() == personal


@pytest.mark.parametrize("account", [CLAUDE, CODEX])
def test_account_model_overrides_shared_model(tmp_path, account, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)   # CI machines have no Codex/Claude installed
    command = runtime.cli_command(dict(account, auth="api", model="account-model"), "run", tmp_path)
    assert command[command.index("--model") + 1] == "account-model"


def test_links_in_the_users_folder_are_not_followed(tmp_path, home):
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE", encoding="utf-8")
    link = home / ".claude" / "skills" / "linked.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    root = tmp_path / "data"
    runtime.sync_extensions(CLAUDE, root, home)
    assert not (runtime.profile_dir(root, CLAUDE) / "skills" / "linked.md").exists()


def test_codex_config_keeps_mcp_and_features_but_not_provider_or_login():
    pytest.importorskip("tomllib")
    text = '''
model = "gpt-x"
model_provider = "other"
forced_login_method = "chatgpt"
approval_policy = "on-request"

[model_providers.other]
base_url = "https://example.invalid"

[projects."C:\\\\work"]
trust_level = "trusted"

[features]
multi_agent = true

[mcp_servers.docs]
command = "npx"
args = ["-y", "docs-mcp"]
'''
    import tomllib
    out = tomllib.loads(runtime.shared_codex_config(text))
    assert out == {"model": "gpt-x", "approval_policy": "on-request", "features": {"multi_agent": True},
                   "mcp_servers": {"docs": {"command": "npx", "args": ["-y", "docs-mcp"]}}}


def test_codex_profile_gets_skills_agents_and_a_reduced_config(tmp_path):
    pytest.importorskip("tomllib")
    home = tmp_path / "home"
    _write(home / ".codex" / "AGENTS.md", "# agents")
    _write(home / ".codex" / "skills" / "s" / "SKILL.md", "# s")
    _write(home / ".codex" / "auth.json", "SECRET")
    _write(home / ".codex" / "config.toml", 'model_provider = "x"\n[features]\nmulti_agent = true\n')
    root = tmp_path / "data"
    runtime.sync_extensions(CODEX, root, home)
    profile = runtime.profile_dir(root, CODEX)
    assert (profile / "AGENTS.md").is_file() and (profile / "skills" / "s" / "SKILL.md").is_file()
    assert not (profile / "auth.json").exists()
    config = (profile / "config.toml").read_text(encoding="utf-8")
    assert "multi_agent = true" in config and "model_provider" not in config


def test_enabled_user_plugins_are_passed_by_path(tmp_path):
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"
    on, off = plugins / "cache" / "on", plugins / "cache" / "off"
    _write(on / "plugin.json", "{}")
    _write(off / "plugin.json", "{}")
    _write(plugins / "installed_plugins.json", json.dumps({"version": 2, "plugins": {
        "on@m": [{"scope": "user", "installPath": str(on)}],
        "off@m": [{"scope": "user", "installPath": str(off)}],
        "gone@m": [{"scope": "user", "installPath": str(plugins / "cache" / "gone")}],
        "elsewhere@m": [{"scope": "user", "installPath": str(tmp_path)}]}}))
    _write(home / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"off@m": False}}))
    assert runtime.user_plugin_dirs(CLAUDE, home) == [str(on)]
    assert runtime.user_plugin_dirs(CODEX, home) == []


def test_explicit_import_preserves_personal_extension_edits_and_deletions(tmp_path, home):
    root = tmp_path / "data"
    account = dict(CLAUDE, share_extensions=False)
    assert runtime.manual_import(account, root, "settings", home)["status"] == "imported"
    profile = runtime.profile_dir(root, account)
    skill = profile / "skills" / "review" / "SKILL.md"
    _write(skill, "personal")
    (profile / "agents" / "planner.md").unlink()
    _write(home / ".claude" / "skills" / "review" / "SKILL.md", "source changed")
    runtime.manual_import(account, root, "settings", home)
    assert skill.read_text(encoding="utf-8") == "personal"
    assert not (profile / "agents" / "planner.md").exists()


def test_first_import_does_not_overwrite_existing_profile_extensions(tmp_path, home):
    root = tmp_path / "data"
    skill = runtime.profile_dir(root, CLAUDE) / "skills" / "review" / "SKILL.md"
    _write(skill, "existing")
    runtime.manual_import(CLAUDE, root, "settings", home)
    assert skill.read_text(encoding="utf-8") == "existing"


def test_launch_discovers_current_plugins_without_an_import_snapshot(tmp_path, monkeypatch):
    root, home = tmp_path / "data", tmp_path / "home"
    plugin = home / ".claude" / "plugins" / "cache" / "test"
    _write(plugin / "plugin.json", "{}")
    _write(home / ".claude" / "plugins" / "installed_plugins.json",
           json.dumps({"plugins": {"test@m": [{"installPath": str(plugin)}]}}))
    _write(home / ".claude" / "settings.json", "{}")
    monkeypatch.setattr(runtime, "user_config_dir", lambda *args: home / ".claude")
    assert runtime.build_cli(CLAUDE, "launch", root) == ["claude", "--plugin-dir", str(plugin)]
    _write(home / ".claude/settings.json", '{"enabledPlugins": {"test@m": false}}')
    assert runtime.build_cli(CLAUDE, "launch", root) == ["claude"]


def test_import_rejects_unknown_kind_or_tool(tmp_path):
    with pytest.raises(ValueError):
        runtime.manual_import(CLAUDE, tmp_path, "automatic")
    with pytest.raises(ValueError):
        runtime.manual_import(dict(CLAUDE, tool="unknown"), tmp_path, "settings")
    with pytest.raises(ValueError):
        runtime.build_cli(dict(CLAUDE, tool="unknown"), "login", tmp_path)


def test_codex_update_retains_removed_keys_and_nested_tables(tmp_path):
    import tomllib
    root, home = tmp_path / "data", tmp_path / "home"
    source = home / ".codex" / "config.toml"
    _write(source, 'model = "first"\napproval_policy = "on-request"\n[features]\nmulti_agent = true\n')
    runtime.manual_import(CODEX, root, "settings", home)
    _write(source, 'model = "second"\n[features]\nfast = true\n')
    runtime.manual_import(CODEX, root, "settings", home)
    actual = tomllib.loads((runtime.profile_dir(root, CODEX) / "config.toml").read_text(encoding="utf-8"))
    assert actual == {"model": "second", "approval_policy": "on-request",
                      "features": {"multi_agent": True, "fast": True}}


@pytest.mark.parametrize("account, filename", [(CLAUDE, "settings.json"), (CODEX, "config.toml")])
def test_invalid_source_settings_report_failure_without_replacing_profile(tmp_path, account, filename):
    root, home = tmp_path / "data", tmp_path / "home"
    source = runtime.user_config_dir(account["tool"], home) / filename
    target = runtime.profile_dir(root, account) / filename
    _write(source, "[invalid")
    _write(target, "personal content")
    with pytest.raises(ValueError, match="source"):
        runtime.manual_import(account, root, "settings", home)
    assert target.read_text(encoding="utf-8") == "personal content"


def test_a_launch_refresh_copies_what_fits_and_removes_nothing(tmp_path, home, monkeypatch):
    root = tmp_path / "data"
    runtime.sync_extensions(CLAUDE, root, home, follow_source=True)
    monkeypatch.setattr(runtime, "SHARE_MAX_FILES", 2)
    summary = runtime.sync_extensions(CLAUDE, root, home, follow_source=True)
    profile = runtime.profile_dir(root, CLAUDE)
    assert summary["limited"] and summary["removed"] == 0
    assert (profile / "commands" / "ship.md").is_file() and (profile / "hooks" / "gate.cmd").is_file()
    with pytest.raises(ValueError, match="Too many"):
        runtime.manual_import(CLAUDE, root, "settings", home)
