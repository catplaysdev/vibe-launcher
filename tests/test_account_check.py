"""Account compatibility before a quota plan is saved: what the worker would refuse at a handoff, found ahead of time."""
import json

import pytest

from vibe_launcher import account_check, runtime

A = {"id": "a", "name": "A", "tool": "claude", "auth": "subscription", "environment_id": "local"}
B = dict(A, id="b", name="B")
X = {"id": "x", "name": "X", "tool": "codex", "auth": "subscription", "environment_id": "local"}
Y = dict(X, id="y", name="Y")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Profiles under data/, the user's own files under native-home/ (conftest); sign-in is reported, never read."""
    monkeypatch.setattr(runtime, "check_login", lambda account, root, env: account["id"] != "signed-out")
    return tmp_path / "data"


def native(tmp_path, tool="claude"):
    return tmp_path / "native-home" / (".claude" if tool == "claude" else ".codex")


def keys(report, accepted=None):
    return {(finding.account_id, finding.key) for finding in report.open(accepted)}


def test_profiles_refreshed_from_the_same_user_files_are_interchangeable(tmp_path, root):
    write(native(tmp_path) / "settings.json", {"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"})
    write(native(tmp_path) / "skills" / "review" / "SKILL.md", "# review")
    report = account_check.check([A, B], "a", root, tmp_path)
    assert report.findings == [] and report.reference == "a" and set(report.signature) == {"a", "b"}


def test_a_personal_edit_is_found_and_aligned_to_the_reference_with_a_backup(tmp_path, root):
    write(native(tmp_path) / "settings.json", {"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"})
    write(native(tmp_path) / "skills" / "review" / "SKILL.md", "# review")
    account_check.check([A, B], "a", root, tmp_path)
    profile = runtime.profile_dir(root, B)
    write(profile / "skills" / "review" / "SKILL.md", "# edited in B only")
    settings = json.loads((profile / "settings.json").read_text(encoding="utf-8"))
    write(profile / "settings.json", dict(settings, theme="light", model="opus"))
    report = account_check.check([A, B], "a", root, tmp_path)           # the refresh keeps personal edits
    assert keys(report) == {("b", "settings:skills/review/SKILL.md"), ("b", "settings:settings.json")}
    assert account_check.check([A, B], "b", root, tmp_path).reference == "b"
    changed = account_check.align(B, A, root)
    assert set(changed) == {"skills/review/SKILL.md", "settings.json"}
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review"
    aligned = json.loads((profile / "settings.json").read_text(encoding="utf-8"))
    assert aligned["theme"] == "dark" and aligned["model"] == "opus"     # its own default model is not the layer
    [saved] = (root / "alignment-backups" / "b").rglob("SKILL.md")
    assert saved.read_text(encoding="utf-8") == "# edited in B only"
    assert account_check.check([A, B], "a", root, tmp_path).findings == []   # and the next refresh keeps it aligned
    write(native(tmp_path) / "skills" / "review" / "SKILL.md", "# review, updated by the user")
    account_check.check([A, B], "a", root, tmp_path)
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review, updated by the user"


def codex_answers(plugins=(), apps=None, mcp=None):
    return [{"marketplaces": [{"plugins": [{"id": item, "version": "1", "installed": True, "enabled": True}
                                           for item in plugins]}]},
            {"apps": [{"id": key, "runtimeName": name, "enabled": True, "callable": True} for key, name in (apps or {}).items()]},
            {"data": [{"name": key, "authStatus": state} for key, state in (mcp or {}).items()]}]


@pytest.fixture
def codex(monkeypatch):
    answers = {}
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)
    monkeypatch.setattr(runtime, "rpc_requests", lambda command, env, calls, cwd=None, timeout=30: answers[cwd.name])
    return answers


def test_codex_rules_must_match_but_account_capabilities_can_be_accepted(tmp_path, root, codex):
    codex["x"] = codex_answers(["pages@market", "docs@market"], {"gh": "GitHub"}, {"notion": "oAuth", "wiki": "unknown"})
    codex["y"] = codex_answers(["pages@market"], {}, {"notion": "notLoggedIn"})
    account_check.check([X, Y], "x", root, tmp_path)
    write(runtime.profile_dir(root, X) / "rules" / "default.rules", 'prefix_rule(pattern=["ls"])')
    write(runtime.profile_dir(root, Y) / "rules" / "default.rules", 'prefix_rule(pattern=["rm"])')
    report = account_check.check([X, Y], "x", root, tmp_path)
    assert keys(report) == {("y", "rules:default.rules"), ("y", "plugin:docs"), ("y", "app:GitHub"), ("y", "mcp:notion"),
                            ("y", "mcp_unknown:wiki")}             # the names the worker uses at a handoff
    assert next(f for f in report.findings if f.key == "plugin:docs").holders == ("x",)
    accepted = {"y": ["plugin:docs", "app:GitHub", "mcp:notion", "mcp_unknown:wiki"]}
    assert keys(report, accepted) == {("y", "rules:default.rules")}      # rules cannot be accepted, only aligned
    assert account_check.align(Y, X, root) == ["rules/default.rules"]
    assert (runtime.profile_dir(root, Y) / "rules" / "default.rules").read_text(encoding="utf-8") == 'prefix_rule(pattern=["ls"])'
    assert keys(account_check.check([X, Y], "x", root, tmp_path), accepted) == set()
    codex["x"] = codex_answers(["pages@market", "docs@market", "slides@market"], {"gh": "GitHub"}, {"notion": "oAuth"})
    assert keys(account_check.check([X, Y], "x", root, tmp_path), accepted) == {("y", "plugin:slides")}   # added later


def test_codex_shared_config_is_aligned_but_its_own_trust_and_login_stay(tmp_path, root, codex):
    codex["x"] = codex["y"] = codex_answers()
    account_check.check([X, Y], "x", root, tmp_path)
    write(runtime.profile_dir(root, X) / "config.toml", 'model = "m1"\n[features]\nweb = true\n')
    write(runtime.profile_dir(root, Y) / "config.toml",
          'model = "m2"\nforced_login_method = "chatgpt"\n[projects."C:/work"]\ntrust_level = "trusted"\n')
    assert keys(account_check.check([X, Y], "x", root, tmp_path, refresh=False)) == {("y", "settings:config.toml")}
    assert account_check.align(Y, X, root) == ["config.toml"]
    import tomllib
    aligned = tomllib.loads((runtime.profile_dir(root, Y) / "config.toml").read_text(encoding="utf-8"))
    assert aligned["model"] == "m1" and aligned["features"] == {"web": True}
    assert aligned["forced_login_method"] == "chatgpt" and aligned["projects"]["C:/work"]["trust_level"] == "trusted"
    assert keys(account_check.check([X, Y], "x", root, tmp_path, refresh=False)) == set()


def test_claude_plugins_and_mcp_servers_belong_to_each_account(tmp_path, root):
    account_check.check([A, B], "a", root, tmp_path)
    first, second = runtime.profile_dir(root, A), runtime.profile_dir(root, B)
    settings = json.loads((first / "settings.json").read_text(encoding="utf-8")) if (first / "settings.json").exists() else {}
    write(first / "settings.json", dict(settings, enabledPlugins={"pages@market": True}))
    write(first / ".claude.json", {"mcpServers": {"docs": {"type": "http", "url": "https://example.com/mcp"},
                                                  "local": {"command": "tool"}}})
    write(second / ".claude.json", {"mcpServers": {"docs": {"type": "http", "url": "https://example.com/mcp"}}})
    report = account_check.check([A, B], "a", root, tmp_path, refresh=False)
    assert keys(report) == {("b", "plugin:pages"), ("b", "mcp:local"), ("b", "mcp_unknown:docs"), ("a", "mcp_unknown:docs")}


def test_signed_out_accounts_and_changed_profiles(tmp_path, root, monkeypatch):
    signed_out = dict(B, id="signed-out")
    report = account_check.check([A, signed_out], "a", root, tmp_path)
    assert keys(report) == {("signed-out", "login:sign-in")}
    before = account_check.signature([A], root)
    assert account_check.signature([A], root) == before                  # nothing changed, nothing to check again
    write(runtime.profile_dir(root, A) / "skills" / "new" / "SKILL.md", "# new")
    assert account_check.signature([A], root) != before
    monkeypatch.setattr(runtime, "account_in_use", lambda root, account_id, session_id: True)
    with pytest.raises(ValueError):
        account_check.align(B, A, root)                                  # never under another terminal's feet
