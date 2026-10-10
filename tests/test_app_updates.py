"""Launcher self-update: newer tags only, changelog parsing, safe apply, backups and cache."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vibe_launcher import runtime, updates
from vibe_launcher.store import Store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

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


def test_lists_only_newer_versions_newest_first(repos):
    result = updates.check(repos["clone"], repos["data"])
    assert result["status"] == "update_available" and result["current"] == "0.1.0" and result["latest"] == "0.3.0"
    assert [r["version"] for r in result["releases"]] == ["0.3.0", "0.2.0"]
    newest, older = result["releases"]
    assert newest["notes"] == ["New sidebar.", "Wider cards."] and newest["date"] == "2026-01-03"
    assert older["notes"] == ["Faster terminals."]
    assert updates.load_cached(repos["data"]) == result


def test_current_version_reports_no_releases(repos, monkeypatch):
    monkeypatch.setattr(updates, "current_version", lambda: "0.3.0")
    result = updates.check(repos["clone"], repos["data"])
    assert result["status"] == "current" and result["releases"] == []


def test_fetch_failure_and_non_git_are_unavailable(repos, tmp_path):
    git(repos["clone"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = updates.check(repos["clone"], repos["data"])
    assert result["status"] == "unavailable" and result["releases"] == [] and "missing" not in result["message"]
    (tmp_path / "plain").mkdir()
    assert updates.check(tmp_path / "plain", repos["data"])["status"] == "unavailable"
    assert updates.check(None, repos["data"], fetch=False)["status"] == "unavailable"


def test_parse_changelog_skips_unreleased_unless_asked():
    text = changelog("0.3.0")
    assert set(updates.parse_changelog(text)) == {"0.1.0", "0.2.0", "0.3.0"}
    unreleased = updates.parse_changelog(text, include_unreleased=True)["Unreleased"]
    assert unreleased["notes"] == ["Not yet."]


def test_dirty_tree_is_detected(repos):
    clone = repos["clone"]
    assert updates.working_tree_clean(clone) == (True, "")
    (clone / "untracked.txt").write_text("x", encoding="utf-8")
    assert updates.working_tree_clean(clone)[0]
    (clone / "CHANGELOG.md").write_text("changed", encoding="utf-8")
    clean, message = updates.working_tree_clean(clone)
    assert not clean and "CHANGELOG.md" in message


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


def test_apply_stops_on_dirty_tree(repos, capsys):
    (repos["clone"] / "CHANGELOG.md").write_text("local edit", encoding="utf-8")
    assert updates.apply(repos["clone"], repos["data"]) == 2
    assert not (repos["data"] / "backups").exists()


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


def test_version_comes_from_pyproject():
    import vibe_launcher
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{vibe_launcher.__version__}"' in text
    assert json.dumps(updates.current_version())
