"""Updates: the release report and the launcher's own update.

- updates
- app updates: Launcher self-update: newer tags only, changelog parsing, safe apply, backups and cache.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from vibe_launcher import runtime, updates
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store


# --- updates ----------------------------------------------------------------------------------------------------------
def report(tool="codex", installed="0.114.0"):
    return {"tool":tool,"installed":installed,"latest":"0.160.0","path":"/snap/bin/codex", "source":"Snap",
            "status":"update_available","message":"","can_update":True,"checked_at":"test"}


def test_update_validates_tool_and_channel():
    with pytest.raises(ValueError):
        runtime.validate_request({"action":"update-tool","tool":"other"})
    with pytest.raises(ValueError):
        runtime.validate_request({"action":"update-tool","tool":"codex","snap_channel":"edge; bad"})


def test_cli_owned_updater_uses_exact_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "tool_output", lambda *a:"Commands:\n  update [options] Update tool\n")
    source, args, details = runtime.update_route("claude", str(tmp_path / "claude.exe"), tmp_path)
    assert args == [str(tmp_path / "claude.exe"), "update"]


def test_unknown_install_never_runs_install_script(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime,"tool_output",lambda *a:"Commands:\n  login\n")
    assert runtime.update_route("codex", str(tmp_path / "codex.exe"), tmp_path)[1] == []


def test_update_rechecks_path_and_version(tmp_path, monkeypatch):
    before = report()
    after = {**report(installed="0.160.0"), "path":"/new/bin/codex", "status":"current"}
    values = iter([before, after])
    monkeypatch.setattr(runtime,"inspect_tool",lambda *a:next(values))
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["/old/bin/codex","update"],{}))
    calls=[]
    monkeypatch.setattr(runtime.subprocess,"call",lambda args,**kw:calls.append(args) or 0)
    assert runtime.manage_tools({"action":"update-tool","tool":"codex","environment_id":"host","nonce":"n"},tmp_path)==0
    saved=json.loads((tmp_path/"tool-versions/host.json").read_text())['reports'][0]
    assert saved['path']=="/new/bin/codex"
    assert saved['installed']=="0.160.0"
    assert saved['previous']=="0.114.0"
    assert calls==[["/old/bin/codex","update"]]


def test_updater_success_unchanged_version_is_not_claimed_updated(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"inspect_tool",lambda *a:report())
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["tool","update"],{}))
    monkeypatch.setattr(runtime.subprocess,"call",lambda *a,**k:0)
    runtime.manage_tools({"action":"update-tool","tool":"codex","environment_id":"host","nonce":"n"},tmp_path)
    saved=json.loads((tmp_path/"tool-versions/host.json").read_text())['reports'][0]
    assert "unchanged" in saved['message']
    assert saved['status']=="update_available"


def test_cancelled_or_failed_updater_does_not_mark_current(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"inspect_tool",lambda *a:report())
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["tool","update"],{}))
    monkeypatch.setattr(runtime.subprocess,"call",lambda *a,**k:1)
    assert runtime.manage_tools({"action":"update-tool","tool":"codex","environment_id":"host","nonce":"n"},tmp_path)==1
    saved=json.loads((tmp_path/"tool-versions/host.json").read_text())['reports'][0]
    assert saved['status']=="update_failed"


def test_unreachable_registry_is_unknown(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"trusted_executable",lambda *a:"/bin/codex")
    monkeypatch.setattr(runtime,"tool_output",lambda *a:"codex-cli 0.114.0")
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["tool","update"],{}))
    monkeypatch.setattr(runtime,"upstream_version",lambda *a:(_ for _ in ()).throw(OSError()))
    value=runtime.inspect_tool("codex",tmp_path)
    assert value['status']=="unknown"
    assert value['latest']=="unknown"


def test_check_does_not_invoke_updater(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"inspect_tool",lambda tool,*a:report(tool))
    monkeypatch.setattr(runtime.subprocess,"call",lambda *a,**kw:pytest.fail("check must not update"))
    assert runtime.manage_tools({"action":"check-tools","environment_id":"host","nonce":"n"},tmp_path)==0


def test_service_update_is_environment_scoped(tmp_path,monkeypatch):
    store=Store(tmp_path/"db")
    monkeypatch.setattr(subprocess,"Popen",lambda *a,**kw:None)
    LauncherService(store).start("update-tool",environment_id="local",tool="codex")
    job=json.loads(next((tmp_path/"jobs").glob("*.json")).read_text(encoding="utf-8"))
    assert job['request']['tool']=="codex"
    assert 'account' not in job['request']
    assert store.list('sessions')==[]


# --- app updates ------------------------------------------------------------------------------------------------------
CHANGES = {
    "0.1.0": "- First release.\n",
    "0.2.0": "- Faster terminals.\n",
    "0.3.0": "- New sidebar.\n### Details\n- Wider cards.\n",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=user", "-c", "user.email=user@example.com",
                           "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def changelog(upto: str) -> str:
    versions = [v for v in CHANGES if tuple(map(int, v.split("."))) <= tuple(map(int, upto.split(".")))]
    return "# Changelog\n\n## [Unreleased]\n- Not yet.\n\n" + "".join(
        f"## [{v}] - 2026-01-0{i + 1}\n{CHANGES[v]}\n" for i, v in reversed(list(enumerate(versions))))


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """origin (bare) with v0.1.0..v0.3.0; the clone only knows v0.1.0 until it fetches."""
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")
    work, origin, clone = tmp_path / "work", tmp_path / "origin.git", tmp_path / "clone"
    work.mkdir()
    git(tmp_path, "init", "--bare", str(origin))
    git(work, "init")
    git(work, "remote", "add", "origin", str(origin))
    for index, version in enumerate(CHANGES):
        (work / "CHANGELOG.md").write_text(changelog(version), encoding="utf-8")
        git(work, "add", "CHANGELOG.md")
        git(work, "commit", "-m", "Release " + version)
        git(work, "tag", "v" + version)
        if index == 0:
            git(work, "push", "origin", "main", "v0.1.0")
            git(tmp_path, "clone", str(origin), str(clone))
    git(work, "tag", "v-not-a-version")
    git(work, "push", "origin", "main", "--tags")
    git(clone, "reset", "--hard", "v0.1.0")
    return {"work": work, "origin": origin, "clone": clone, "data": tmp_path / "data"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_lists_only_newer_versions_newest_first(repos):
    result = updates.check(repos["clone"], repos["data"])
    assert result["status"] == "update_available" and result["current"] == "0.1.0" and result["latest"] == "0.3.0"
    assert [r["version"] for r in result["releases"]] == ["0.3.0", "0.2.0"]
    newest, older = result["releases"]
    assert newest["notes"] == ["New sidebar.", "Wider cards."] and newest["date"] == "2026-01-03"
    assert older["notes"] == ["Faster terminals."]
    assert updates.load_cached(repos["data"]) == result


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_current_version_reports_no_releases(repos, monkeypatch):
    monkeypatch.setattr(updates, "current_version", lambda: "0.3.0")
    result = updates.check(repos["clone"], repos["data"])
    assert result["status"] == "current" and result["releases"] == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_fetch_failure_and_non_git_are_unavailable(repos, tmp_path):
    git(repos["clone"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = updates.check(repos["clone"], repos["data"])
    assert result["status"] == "unavailable" and result["releases"] == [] and "missing" not in result["message"]
    (tmp_path / "plain").mkdir()
    assert updates.check(tmp_path / "plain", repos["data"])["status"] == "unavailable"
    assert updates.check(None, repos["data"], fetch=False)["status"] == "unavailable"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_parse_changelog_skips_unreleased_unless_asked():
    text = changelog("0.3.0")
    assert set(updates.parse_changelog(text)) == {"0.1.0", "0.2.0", "0.3.0"}
    unreleased = updates.parse_changelog(text, include_unreleased=True)["Unreleased"]
    assert unreleased["notes"] == ["Not yet."]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_dirty_tree_is_detected(repos):
    clone = repos["clone"]
    assert updates.working_tree_clean(clone) == (True, "")
    (clone / "untracked.txt").write_text("x", encoding="utf-8")
    assert updates.working_tree_clean(clone)[0]
    (clone / "CHANGELOG.md").write_text("changed", encoding="utf-8")
    clean, message = updates.working_tree_clean(clone)
    assert not clean and "CHANGELOG.md" in message


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_apply_pulls_after_backup_and_reports_missing_uv(repos, monkeypatch, capsys):
    data = repos["data"]
    Store(data / "vibe-launcher.sqlite3")
    git(repos["clone"], "checkout", "-q", "main")
    git(repos["clone"], "reset", "--hard", "v0.1.0")
    updates.check(repos["clone"], data)
    real = runtime.trusted_executable

    def trusted(name):
        if name == "uv":
            raise RuntimeError("missing")
        return real(name)

    monkeypatch.setattr(runtime, "trusted_executable", trusted)
    assert updates.apply(repos["clone"], data) == 5
    output = capsys.readouterr().out
    assert "uv sync" in output
    assert "## [0.3.0]" in (repos["clone"] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert len(list((data / "backups").iterdir())) == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_apply_stops_on_dirty_tree(repos, capsys):
    (repos["clone"] / "CHANGELOG.md").write_text("local edit", encoding="utf-8")
    assert updates.apply(repos["clone"], repos["data"]) == 2
    assert not (repos["data"] / "backups").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_backup_copies_user_data_and_keeps_ten(tmp_path):
    store = Store(tmp_path / "vibe-launcher.sqlite3")
    store.save("accounts", {"id": "user", "name": "Example", "tool": "codex", "auth": "subscription"})
    runtime.write_json(tmp_path / "ui-state.json", {"theme": "deep-sea"})
    first = updates.backup_user_data(tmp_path, "0.1.0")
    assert {p.name for p in first.iterdir()} == {"vibe-launcher.sqlite3", "ui-state.json"}
    assert Store(first / "vibe-launcher.sqlite3").get("accounts", "user")["name"] == "Example"
    made = [updates.backup_user_data(tmp_path, "0.1.0") for _ in range(11)]
    left = sorted(p for p in (tmp_path / "backups").iterdir() if p.is_dir())
    assert len(left) == 10 and made[-1] in left and first not in left


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_cache_rejects_malformed_data(tmp_path):
    assert updates.load_cached(tmp_path) is None
    good = {"status": "current", "current": "0.1.0", "latest": "0.1.0", "releases": [], "checked_at": "t", "message": ""}
    runtime.write_json(tmp_path / updates.CACHE_NAME, good)
    assert updates.load_cached(tmp_path) == good
    for bad in ({**good, "status": "maybe"}, {**good, "releases": [{"version": "latest"}]},
                {**good, "releases": [{"version": "1.0.0", "date": "", "notes": [1]}]},
                {**good, "current": None}):
        runtime.write_json(tmp_path / updates.CACHE_NAME, bad)
        assert updates.load_cached(tmp_path) is None
    runtime.write_json(tmp_path / updates.CACHE_NAME, {"unexpected": True})
    assert updates.load_cached(tmp_path) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_version_comes_from_pyproject():
    import vibe_launcher
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{vibe_launcher.__version__}"' in text
    assert json.dumps(updates.current_version())
