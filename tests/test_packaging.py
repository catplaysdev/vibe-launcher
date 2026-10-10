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
            "body": body if body is not None else "- Faster terminals.\n",
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
    shown = json.loads((data_root / updates.PROGRESS_NAME).read_text(encoding="utf-8"))
    assert shown["state"] == "launched" and shown["step"] == 4 and len(shown["stages"]) == 4 and shown["to"] == "0.2.0"
    assert shown["from"] == "0.1.0" and shown["total"] == shown["done"] == len(installer), "the download size is reported"
    assert shown["versions"] == ["0.2.0"]
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
    shown = json.loads((data_root / updates.PROGRESS_NAME).read_text(encoding="utf-8"))
    assert shown["state"] == "failed" and shown["step"] == 3 and shown["message"], "the window can say what went wrong"


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
    assert 'Name: "{app}\_internal"' in iss and "[InstallDelete]" in iss, "an update must not leave the old version's metadata behind"
    spec = (root / "packaging" / "vibe-launcher.spec").read_text(encoding="utf-8")
    assert "upx=False" in spec and "console=False" in spec and "console=True" in spec and "runtime.py" in spec
    assert "collect_all" not in spec, "collect only what the launcher imports; the build must stay small"


def test_the_packaging_procedure_is_a_tracked_skill():
    root = Path(__file__).resolve().parents[1]
    skill = (root / ".agents" / "skills" / "package-release" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: package-release\ndescription: ") and "packaging/build.ps1" in skill
    assert "package-release" in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_the_exe_icon_is_the_in_app_logo_on_its_round_plate(qapp):
    import importlib.util
    import struct

    from PySide6.QtGui import QImage

    spec = importlib.util.spec_from_file_location("make_icon", SRC.parents[1] / "packaging" / "make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ico = module.build_ico()
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, len(module.SIZES))
    image = QImage.fromData(module.png_bytes(64), "PNG")
    assert image.pixelColor(0, 0).alpha() == 0                      # corners are transparent: a round plate, not a white square
    assert image.pixelColor(8, 32).alpha() == 255 and image.pixelColor(8, 32).name() != "#ffffff"


def test_changelog_notes_come_in_two_languages_and_can_be_copied():
    sample = chr(10).join(["## [0.3.0] - 2026-10-12", "", "- 中文一", "- 中文二", "", "### English", "", "- English one", "- English two", ""])
    parsed = updates.parse_changelog(sample)["0.3.0"]
    assert parsed["notes"] == ["中文一", "中文二"] and parsed["notes_en"] == ["English one", "English two"]
    assert updates.notes_for(parsed, "zh-TW") == parsed["notes"] and updates.notes_for(parsed, "ja") == parsed["notes_en"]
    assert updates.notes_for({"notes": ["只有中文"]}, "en") == ["English release notes are unavailable for this version."]
    text = updates.release_text([{"version": "0.3.0", "date": "2026-10-12", **parsed}])
    assert text.startswith("v0.3.0 (2026-10-12)") and "- 中文一" in text and "[English]" in text and "- English two" in text
    shipped = updates.parse_changelog((Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8"),
                                      include_unreleased=True)
    assert shipped["Unreleased"]["notes"] and shipped["Unreleased"]["notes_en"]
    assert shipped["0.2.1"]["notes_en"], "the latest release carries English notes"


def test_the_update_window_follows_the_helper_progress_file(qapp, qtbot, tmp_path):
    from vibe_launcher.update_dialog import UpdateDialog
    releases = [{"version": "0.3.0", "date": "2026-10-12", "notes": ["中文"], "notes_en": ["English"]}]
    dialog = UpdateDialog(["unused"], tmp_path, releases)
    qtbot.addWidget(dialog)
    stages = ["Look", "Back up", "Download", "Install"]
    base = {"stages": stages, "from": "0.2.1", "to": "0.3.0", "state": "running"}

    def show(**fields):
        runtime.write_json(tmp_path / updates.PROGRESS_NAME, {**base, **fields})
        dialog.poll()

    show(step=3, done=5 * 1024 * 1024, total=20 * 1024 * 1024, detail="Vibe-Launcher-Setup-0.3.0.exe")
    assert dialog.bar.maximum() == 100 and dialog.bar.value() == 25 and "5.0 MB" in dialog.detail.text()
    assert dialog.step_labels[0].text().startswith("✓") and dialog.step_labels[2].text().startswith("●")
    assert dialog.versions.text() == "v0.2.1 → v0.3.0" and not dialog.close_button.isEnabled()
    launched = []
    dialog.launched.connect(lambda: launched.append(True))
    show(step=4, state="launched")
    assert dialog.bar.maximum() == 0, "indeterminate while the installer replaces files"
    assert launched == [True] and "0" in dialog.message.text() and not dialog.close_button.isEnabled()
    assert dialog.step_labels[3].text().startswith("●"), "the install step is still running while the installer works"
    dialog._launched_at -= 200                    # the installer never closed the launcher: the user is not trapped
    dialog.poll()
    assert dialog.close_button.isEnabled() and dialog.folder_button.isVisibleTo(dialog)
    dialog._launched_at = __import__("time").monotonic()
    show(step=3, state="failed", message="No network")
    assert dialog.step_labels[2].text().startswith("✕") and dialog.message.text() == "No network" and dialog.close_button.isEnabled()
    dialog.copy_notes()
    from PySide6.QtGui import QGuiApplication
    assert "中文" in QGuiApplication.clipboard().text() and "English" in QGuiApplication.clipboard().text()
    assert dialog.grab().width() > 0


@pytest.mark.parametrize("system,expected", [("zh_TW", "中文"), ("zh-Hant", "中文"), ("zh_HK", "中文"),
                                            ("zh_MO", "中文"), ("zh_CN", "English"), ("zh-Hans-TW", "English"),
                                            ("en_US", "English"), ("ja_JP", "English"), ("de_DE", "English")])
def test_update_notes_follow_system_language_not_interface(monkeypatch, system, expected):
    from vibe_launcher import i18n
    i18n.set_language("en" if expected == "中文" else "zh-TW")
    monkeypatch.setattr(updates, "_system_language", lambda: system)
    assert updates.notes_for({"notes": ["中文"], "notes_en": ["English"]}) == [expected]


def test_windows_notes_use_display_language(monkeypatch):
    import ctypes
    import os
    if os.name != "nt":
        pytest.skip("Windows display language")
    monkeypatch.setattr(ctypes.windll.kernel32, "GetUserDefaultUILanguage", lambda: 0x0404)
    assert updates._system_language() == "zh_TW"
    monkeypatch.setattr(ctypes.windll.kernel32, "GetUserDefaultUILanguage", lambda: 0x0804)
    assert updates.notes_for({"notes": ["中文"], "notes_en": ["English"]}) == ["English"]


@pytest.mark.parametrize("reported", ["running", "done", "current", "launched"])
def test_update_helper_failure_overrides_progress_success(qapp, qtbot, tmp_path, reported):
    from types import SimpleNamespace
    from vibe_launcher.update_dialog import UpdateDialog
    dialog = UpdateDialog(["unused"], tmp_path, [])
    qtbot.addWidget(dialog)
    dialog._process = SimpleNamespace(poll=lambda: 7)
    started = []
    dialog.launched.connect(lambda: started.append(True))
    runtime.write_json(tmp_path / updates.PROGRESS_NAME, {"state": reported, "step": 4, "stages": []})
    dialog.poll()
    assert dialog._state == "failed" and "7" in dialog.message.text()
    assert dialog.close_button.isEnabled() and not started


def test_update_success_waits_for_helper_exit(qapp, qtbot, tmp_path):
    from types import SimpleNamespace
    from vibe_launcher.update_dialog import UpdateDialog
    dialog = UpdateDialog(["unused"], tmp_path, [])
    qtbot.addWidget(dialog)
    dialog._process = SimpleNamespace(poll=lambda: None)
    runtime.write_json(tmp_path / updates.PROGRESS_NAME, {"state": "done", "step": 4, "stages": []})
    dialog.poll()
    assert dialog._state == "running" and not dialog.close_button.isEnabled()
    dialog._process = SimpleNamespace(poll=lambda: 0)
    dialog.poll()
    assert dialog._state == "done" and dialog.close_button.isEnabled()


def test_both_update_screens_show_system_selected_notes(qapp, qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from vibe_launcher import i18n
    from vibe_launcher.about import ReleaseCard
    from vibe_launcher.update_dialog import UpdateDialog
    i18n.set_language("zh-TW")
    monkeypatch.setattr(updates, "_system_language", lambda: "ja_JP")
    release = {"version": "0.2.2", "date": "", "notes": ["中文資訊"], "notes_en": ["English notes"]}
    for widget in (ReleaseCard(release), UpdateDialog(["unused"], tmp_path, [release])):
        qtbot.addWidget(widget)
        texts = [label.text() for label in widget.findChildren(QLabel)]
        assert "• English notes" in texts and "• 中文資訊" not in texts
