"""A packaged build starts its own helpers and updates itself through GitHub Releases; both must stay testable from source."""
import hashlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_launcher import entry, runtime, selfcmd, updates

SRC = Path(__file__).resolve().parents[1] / "src" / "vibe_launcher"


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    install = tmp_path / "Vibe Launcher"
    install.mkdir()
    (install / selfcmd.GUI_NAME).write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / selfcmd.GUI_NAME))
    monkeypatch.setattr(sys, "_MEIPASS", str(install / "_internal"), raising=False)
    return install


def test_helper_commands_from_source_keep_the_python_module_form():
    program, args = selfcmd.command("runner", "job.json")[0], selfcmd.command("runner", "job.json")[1:]
    assert args == ["-I", "-m", "vibe_launcher.runner", "job.json"] and Path(program).name.lower() in {"python.exe", "python"}
    assert selfcmd.gui_launch()[1] == ["-m", "vibe_launcher.ui"]
    assert selfcmd.helper_environment()[1] == "-I -m vibe_launcher.askpass"
    with pytest.raises(ValueError):
        selfcmd.command("os")


def test_a_packaged_build_runs_helpers_through_its_console_program(frozen):
    command = selfcmd.command("updates", "apply-release", "--data", "d")
    assert command == [str(frozen / selfcmd.CLI_NAME), "updates", "apply-release", "--data", "d"]
    assert selfcmd.gui_launch() == (str(frozen / selfcmd.GUI_NAME), [])
    assert selfcmd.helper_environment() == (str(frozen / selfcmd.CLI_NAME), "askpass")
    assert selfcmd.bundle_dir() == frozen / "_internal"
    assert updates.repo_root() is None


def test_runtime_source_is_read_from_the_bundled_data_file(frozen):
    (frozen / "_internal" / "vibe_launcher").mkdir(parents=True)
    (frozen / "_internal" / "vibe_launcher" / "runtime.py").write_bytes(b"# bundled\n")
    assert selfcmd.runtime_source() == b"# bundled\n"


def test_only_selfcmd_builds_helper_command_lines():
    pattern = re.compile(r'"-m",\s*"vibe_launcher|with_name\("python(w)?\.exe"\)')
    offenders = [path.name for path in SRC.glob("*.py") if path.name not in {"selfcmd.py", "runtime.py"}
                 and pattern.search(path.read_text(encoding="utf-8"))]
    assert offenders == [], "build helper commands with selfcmd.command()"


def test_entry_runs_a_helper_like_its_own_main(monkeypatch):
    seen = []
    fake = type(sys)("vibe_launcher.runner")
    fake.main = lambda: seen.append(list(sys.argv)) or 7
    monkeypatch.setitem(sys.modules, "vibe_launcher.runner", fake)
    assert entry.main(["runner", "job.json"]) == 7 and seen == [["runner", "job.json"]]
    assert entry.main(["nonsense"]) == 2


# --------------------------------------------------------------------------------- release channel


def release(version, *, assets=True, draft=False, prerelease=False, body=None):
    prefix = f"https://github.com/{updates.RELEASE_REPO}/releases/download/v{version}/"
    names = [f"Vibe-Launcher-Setup-{version}.exe", updates.SUMS_NAME] if assets else []
    return {"tag_name": f"v{version}", "draft": draft, "prerelease": prerelease, "published_at": "2026-10-07T01:02:03Z",
            "body": body if body is not None else f"- Faster terminals.\n### 設定變更\n- 變更: terminal_font_size（預設改為 13）\n",
            "assets": [{"name": n, "browser_download_url": prefix + n} for n in names]}


def listing(*items):
    return json.dumps(list(items)).encode()


def test_only_installable_newer_published_releases_are_offered(monkeypatch):
    monkeypatch.setattr(updates, "_read", lambda url, limit: listing(
        release("0.3.0"), release("0.2.0"), release("0.1.0"), release("0.9.0", assets=False), release("0.8.0", draft=True),
        release("0.7.0", prerelease=True), {"tag_name": "nightly"}))
    found = updates._latest_releases(updates.RELEASE_REPO, (0, 1, 0))
    assert [r["version"] for r in found] == ["0.3.0", "0.2.0"]
    assert found[0]["notes"] == ["Faster terminals."]
    assert found[0]["settings_changes"] == [{"kind": "changed", "key": "terminal_font_size", "detail": "預設改為 13"}]


def test_a_release_with_files_from_another_place_is_ignored(monkeypatch):
    bad = release("0.3.0")
    bad["assets"][0]["browser_download_url"] = "https://example.com/Vibe-Launcher-Setup-0.3.0.exe"
    monkeypatch.setattr(updates, "_read", lambda url, limit: listing(bad))
    assert updates._latest_releases(updates.RELEASE_REPO, (0, 1, 0)) == []


def test_packaged_check_reports_and_caches_like_the_git_channel(frozen, monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(updates, "_read", lambda url, limit: listing(release("0.2.0")))
    result = updates.check(None, tmp_path)
    assert result["status"] == "update_available" and result["latest"] == "0.2.0"
    assert updates.load_cached(tmp_path)["releases"][0]["version"] == "0.2.0"
    monkeypatch.setattr(updates, "_read", lambda url, limit: (_ for _ in ()).throw(OSError("offline")))
    assert updates.check(None, tmp_path)["status"] == "unavailable"


def test_only_github_addresses_are_trusted():
    assert updates._trusted("https://api.github.com/repos/x/y/releases")
    assert updates._trusted("https://release-assets.githubusercontent.com/a/b")
    for url in ("http://github.com/x", "https://evilgithub.com/x", "https://github.com.evil.example/x", "file:///c:/x", "https://example.com/"):
        assert not updates._trusted(url)
    with pytest.raises(ValueError):
        updates._open("https://example.com/Vibe-Launcher-Setup-0.2.0.exe")


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_a_wrong_checksum_leaves_nothing_behind(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "_open", lambda url: FakeResponse(b"installer"))
    target = tmp_path / "setup.exe"
    with pytest.raises(ValueError):
        updates._download("https://github.com/x", target, hashlib.sha256(b"other").hexdigest())
    assert list(tmp_path.iterdir()) == []
    updates._download("https://github.com/x", target, hashlib.sha256(b"installer").hexdigest())
    assert target.read_bytes() == b"installer" and [p.name for p in tmp_path.iterdir()] == ["setup.exe"]
    with pytest.raises(ValueError):
        updates._download("https://github.com/x", tmp_path / "big.exe", hashlib.sha256(b"installer").hexdigest(), limit=3)


def test_checksum_lookup():
    good = "a" * 64
    sums = f"{good}  Vibe-Launcher-Setup-0.2.0.exe\n{'b' * 64} *other.exe\n".encode()
    assert updates._checksum(sums, "Vibe-Launcher-Setup-0.2.0.exe") == good
    assert updates._checksum(sums, "other.exe") == "b" * 64
    with pytest.raises(ValueError):
        updates._checksum(sums, "missing.exe")
    with pytest.raises(ValueError):
        updates._checksum(b"nothex  Vibe-Launcher-Setup-0.2.0.exe", "Vibe-Launcher-Setup-0.2.0.exe")


def test_apply_release_backs_up_downloads_verifies_then_starts_the_installer(frozen, monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "data"
    runtime.private_dir(data_root)
    (data_root / "ui-state.json").write_text("{}", encoding="utf-8")
    installer = b"MZ installer"
    sums = f"{hashlib.sha256(installer).hexdigest()}  Vibe-Launcher-Setup-0.2.0.exe\n".encode()
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(updates, "_read", lambda url, limit: listing(release("0.2.0")) if "api.github.com" in url else sums)
    monkeypatch.setattr(updates, "_open", lambda url: FakeResponse(installer))
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: started.append(args))
    old = data_root / "updates"
    runtime.private_dir(old)
    (old / "leftover.exe").write_bytes(b"old")
    assert updates.apply_release(data_root) == 0
    output = capsys.readouterr().out
    assert "terminal_font_size" in output and "已備份" in output
    (target,) = started
    assert target[0].endswith("Vibe-Launcher-Setup-0.2.0.exe") and "/CLOSEAPPLICATIONS" in target and "/SILENT" in target and "/RESTARTAPPLICATIONS" not in target
    assert [p.name for p in old.iterdir()] == ["Vibe-Launcher-Setup-0.2.0.exe"], "earlier downloads are removed"
    assert list((data_root / "backups").iterdir()), "user data was backed up first"


def test_apply_release_stops_cleanly_when_the_download_cannot_be_verified(frozen, monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "data"
    runtime.private_dir(data_root)
    sums = f"{'0' * 64}  Vibe-Launcher-Setup-0.2.0.exe\n".encode()
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(updates, "_read", lambda url, limit: listing(release("0.2.0")) if "api.github.com" in url else sums)
    monkeypatch.setattr(updates, "_open", lambda url: FakeResponse(b"tampered"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("an unverified installer must never run"))
    assert updates.apply_release(data_root) == 4
    assert not list((data_root / "updates").glob("*.exe"))


# --------------------------------------------------------------------------------- release pipeline


def load_script(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## [Unreleased]

- Next thing.

## [0.2.0] - 2026-10-07

- Faster terminals.

### 設定變更

- 變更: terminal_font_size（預設改為 13）

## [0.1.0] - 2026-09-01

- First.
"""


def test_the_release_body_is_the_versions_changelog_section_and_the_updater_reads_it_back():
    notes = load_script("release_notes")
    body = notes.section(CHANGELOG, "0.2.0")
    assert body.startswith("- Faster terminals.") and "First." not in body and "Next thing" not in body
    assert notes.section(CHANGELOG, "9.9.9") is None
    parsed = updates.parse_changelog("## [0.2.0] - 2026-10-07\n" + body)["0.2.0"]
    assert parsed["notes"] == ["Faster terminals."]
    assert parsed["settings_changes"] == [{"kind": "changed", "key": "terminal_font_size", "detail": "預設改為 13"}]
    assert notes.project_version('[project]\nname = "x"\nversion = "1.2.3"\n') == "1.2.3"
    assert notes.main(["release_notes.py", "check", "not-a-tag"]) == 2


def test_installer_file_names_agree_between_the_script_the_pipeline_and_the_updater():
    root = Path(__file__).resolve().parents[1]
    iss = (root / "packaging" / "installer.iss").read_text(encoding="utf-8")
    flow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build = (root / "packaging" / "build.ps1").read_text(encoding="utf-8")
    assert "OutputBaseFilename=Vibe-Launcher-Setup-{#AppVersion}" in iss and "Vibe-Launcher-Setup-*.exe" in flow
    assert updates.SUMS_NAME in flow and updates.SUMS_NAME in build
    assert "PrivilegesRequired=lowest" in iss and "CloseApplications=yes" in iss, "the silent update needs no administrator"
    spec = (root / "packaging" / "vibe-launcher.spec").read_text(encoding="utf-8")
    assert "upx=False" in spec and "console=False" in spec and "console=True" in spec and "runtime.py" in spec
    assert "collect_all" not in spec, "collect only what the launcher imports; the build must stay small"


def test_the_packaging_procedure_is_a_tracked_skill():
    root = Path(__file__).resolve().parents[1]
    skill = (root / ".agents" / "skills" / "package-release" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: package-release\ndescription: ") and "packaging/build.ps1" in skill
    assert "package-release" in (root / "AGENTS.md").read_text(encoding="utf-8")
