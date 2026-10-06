"""Updates never silently alter user settings: newer data survives older builds, removals must be announced.

Snapshot helper (after adding a setting, or after listing a removal/change under `### 設定變更` in CHANGELOG.md):

    .venv/Scripts/python.exe tests/test_settings_compat.py --update-snapshot
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from vibe_launcher import runtime, settings, updates  # noqa: E402
from vibe_launcher.prefs import PREFS_VERSION, UiPrefs  # noqa: E402
from vibe_launcher.store import SCHEMA_VERSION, Store  # noqa: E402
from vibe_launcher.theme import THEMES  # noqa: E402

SNAPSHOT = Path(__file__).with_name("fixtures") / "settings_schema.json"
CHANGELOG = ROOT / "CHANGELOG.md"
HOW_TO_UPDATE = (".venv/Scripts/python.exe tests/test_settings_compat.py --update-snapshot "
                 "(removed or changed settings must first be listed under '### 設定變更' in the newest CHANGELOG.md section)")


def current_schema(tmp: Path) -> dict:
    prefs = UiPrefs(tmp / "missing" / "ui-state.json")
    defaults = prefs.snapshot()
    return {"ui_prefs": {k: (None if k in UiPrefs.DYNAMIC_DEFAULTS else v) for k, v in defaults.items()},
            "settings": sorted(settings.DEFAULT_SETTINGS)}


def announced_changes() -> set[str]:
    """Keys listed under 設定變更 in the Unreleased section and the newest released section."""
    releases = updates.parse_changelog(CHANGELOG.read_text(encoding="utf-8"), include_unreleased=True)
    numbered = [v for v in releases if v != "Unreleased"]
    sections = [releases.get("Unreleased")]
    if numbered:
        sections.append(releases[max(numbered, key=lambda v: tuple(map(int, v.split("."))))])
    keys = set()
    for section in filter(None, sections):
        for change in section["settings_changes"]:
            key = change["key"].strip("`")
            keys.add(key)
            for prefix in ("ui-state.json:", "ui-state.", "settings.json:", "settings."):
                if key.startswith(prefix):
                    keys.add(key[len(prefix):])
    return keys


def schema_problems(old: dict, new: dict, announced: set[str]) -> tuple[list[str], list[str]]:
    """(unannounced removals/changes, added keys)."""
    problems, added = [], []
    for key, value in old["ui_prefs"].items():
        if key not in new["ui_prefs"]:
            if key not in announced:
                problems.append(f"ui-state key removed: {key}")
        elif new["ui_prefs"][key] != value and key not in announced:
            problems.append(f"ui-state default changed: {key}: {value!r} -> {new['ui_prefs'][key]!r}")
    added += [f"ui-state.{k}" for k in new["ui_prefs"] if k not in old["ui_prefs"]]
    for key in old["settings"]:
        if key not in new["settings"] and key not in announced:
            problems.append(f"settings.json key removed: {key}")
    added += [f"settings.{k}" for k in new["settings"] if k not in old["settings"]]
    return problems, added


def test_settings_schema_snapshot(tmp_path):
    old = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    problems, added = schema_problems(old, current_schema(tmp_path), announced_changes())
    assert not problems, ("Settings were removed or their defaults changed without a CHANGELOG entry:\n  "
                          + "\n  ".join(problems) + "\nAdd them under '### 設定變更' (- 移除: key / - 變更: key（說明）), then run: "
                          + HOW_TO_UPDATE)
    assert not added, "New settings are fine; record them in the snapshot: " + ", ".join(added) + "\nRun: " + HOW_TO_UPDATE


def test_ui_prefs_keep_unknown_keys_and_unusable_values(tmp_path):
    path = tmp_path / "ui-state.json"
    runtime.write_json(path, {"version": PREFS_VERSION + 3, "theme": "future-theme", "future_option": {"a": [1, 2]},
                              "sidebar_collapsed": True, "toast_anchor": [0.25, 0.75]})
    prefs = UiPrefs(path)
    assert prefs.theme == "deep-sea" and prefs.sidebar_collapsed and prefs.toast_anchor == [0.25, 0.75]
    assert prefs.update_check is True and prefs._extra == {"future_option": {"a": [1, 2]}}
    prefs.cursor_blink = True
    prefs.save()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["future_option"] == {"a": [1, 2]}
    assert stored["theme"] == "future-theme"             # not touched here, so the newer build's value stays
    assert stored["version"] == PREFS_VERSION + 3 and stored["cursor_blink"] is True
    prefs.theme = next(t for t in THEMES if t != "deep-sea")
    prefs.save()
    assert json.loads(path.read_text(encoding="utf-8"))["theme"] == prefs.theme   # a real change is saved


def test_ui_prefs_validate_new_keys(tmp_path):
    path = tmp_path / "ui-state.json"
    runtime.write_json(path, {"toast_anchor": [2, 0.5], "update_check": False})
    prefs = UiPrefs(path)
    assert prefs.toast_anchor is None and prefs.update_check is False
    prefs.toast_anchor = (0.5, 0)
    prefs.save()
    assert UiPrefs(path).toast_anchor == [0.5, 0.0]


def test_newer_settings_json_loads_and_survives_save(tmp_path):
    runtime.write_json(tmp_path / "settings.json", {"version": 2, "shells": {"windows": "cmd", "linux": "bash"},
                                                    "plugins": [], "future": {"enabled": True}})
    data = settings.load_settings(tmp_path)
    assert data["shells"]["windows"] == "cmd" and data["future"] == {"enabled": True}
    # The settings dialog saves a version-1 payload without the unknown field; neither may be lost.
    settings.save_settings(tmp_path, {"version": 1, "shells": {"windows": "auto", "linux": "bash"}, "plugins": []})
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored == {"version": 2, "shells": {"windows": "auto", "linux": "bash"}, "plugins": [], "future": {"enabled": True}}


def test_unusable_settings_json_falls_back_without_touching_the_file(tmp_path):
    path = tmp_path / "settings.json"
    runtime.write_json(path, {"version": 3, "shells": {"windows": "future-shell"}, "plugins": []})
    before = path.read_bytes()
    assert settings.load_settings(tmp_path)["shells"] == settings.DEFAULT_SETTINGS["shells"]
    assert path.read_bytes() == before
    path.write_text("{broken", encoding="utf-8")
    assert settings.load_settings(tmp_path)["version"] == 1
    settings.save_settings(tmp_path, settings.DEFAULT_SETTINGS)
    assert list(tmp_path.glob("settings.unreadable-*.json"))       # the unreadable file was kept aside


def test_store_from_newer_version_is_read_only(tmp_path):
    path = tmp_path / "launcher.db"
    Store(path).save("accounts", {"id": "user", "name": "Example", "tool": "codex", "auth": "subscription"})
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    store = Store(path)
    assert store.readonly
    assert store.get("accounts", "user")["name"] == "Example"
    with pytest.raises(ValueError):
        store.save("accounts", {"id": "other", "name": "Other", "tool": "codex", "auth": "subscription"})
    with pytest.raises(ValueError):
        store.delete("accounts", "user")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_migration_backs_up_existing_database(tmp_path):
    path = tmp_path / "launcher.db"
    Store(path).save("accounts", {"id": "user", "name": "Example", "tool": "codex", "auth": "subscription"})
    assert not (tmp_path / "backups").exists()                   # a fresh database needs no backup
    Store(path)
    assert not (tmp_path / "backups").exists()                   # neither does an up-to-date one
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=1")
    assert not Store(path).readonly
    backups = list((tmp_path / "backups").glob(f"db-before-v{SCHEMA_VERSION}-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1


def update_snapshot() -> int:
    import tempfile
    old = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else {"ui_prefs": {}, "settings": []}
    with tempfile.TemporaryDirectory() as folder:
        new = current_schema(Path(folder))
    problems, added = schema_problems(old, new, announced_changes())
    if problems:
        print("Refusing to update: list these under '### 設定變更' in CHANGELOG.md first:\n  " + "\n  ".join(problems))
        return 1
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Snapshot updated" + (": added " + ", ".join(added) if added else ""))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--update-snapshot"]:
        sys.exit(update_snapshot())
    print(__doc__)
