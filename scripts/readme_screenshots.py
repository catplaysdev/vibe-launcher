"""Render the README screenshots from made-up sample data (no real accounts, hosts, paths or brand logos).

    .venv/Scripts/python.exe scripts/readme_screenshots.py            # English and Traditional Chinese
    .venv/Scripts/python.exe scripts/readme_screenshots.py --lang en  # one language

Pictures go to docs/images/<name>-<lang>.png. Everything runs offscreen with widget.grab() (never the real screen),
in a throwaway data folder under .local-test/, with the brand icon folder switched off so only generic glyphs show.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
SIZE = (1280, 800)

TERMINAL_LINES = {          # kept short: a terminal in the grid is narrow
    "codex": [
        ("prompt", "user@server:~/site-analysis$ "), ("text", "codex\n"),
        ("dim", "  model: example-model\n\n"),
        ("accent", "> "), ("text", "Why does test_loader.py fail?\n\n"),
        ("info", "• "), ("text", "Reading tests/test_loader.py\n"),
        ("info", "• "), ("text", "Running pytest -q\n"),
        ("error", "  2 failed"), ("dim", ", "), ("ok", "18 passed\n\n"),
        ("text", "The loader expects UTC timestamps,\nbut the samples use local time.\nFix: parse them with tzinfo.\n"),
    ],
    "claude": [
        ("prompt", "user@server:~/docs-portal$ "), ("text", "claude\n"),
        ("dim", "  restored from tmux · 2 tasks\n\n"),
        ("accent", "> "), ("text", "Add a dark theme toggle\n\n"),
        ("info", "• "), ("text", "Edited Header.tsx (+24 −3)\n"),
        ("info", "• "), ("text", "Edited theme.css (+41)\n"),
        ("ok", "✓ "), ("text", "npm test — 36 passed\n\n"),
        ("dim", "Waiting for your next instruction…\n"),
    ],
}


def iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def seed(store) -> None:
    """Sample records: generic names, documentation addresses and paths only."""
    store.save("environments", {"id": "build", "name": "build-server", "kind": "ssh", "ssh_target": "user@build-server",
                                "ssh_auth_mode": "key"})
    store.save("environments", {"id": "lab", "name": "gpu-lab", "kind": "ssh", "ssh_target": "user@192.0.2.10",
                                "ssh_auth_mode": "interactive"})
    accounts = [("work", "Work", "codex", "local"), ("personal", "Personal", "claude", "build"),
                ("lab", "Lab", "codex", "build")]
    for account_id, name, tool, environment in accounts:
        store.save("accounts", {"id": account_id, "name": name, "tool": tool, "auth": "subscription",
                                "environment_id": environment})
        store.save("auth_states", {"id": f"{account_id}:{environment}", "status": "logged_in", "checked_at": iso(1)})
    projects = [
        ("site-analysis", "Site analysis", "build", "/home/user/projects/site-analysis", ["analysis", "python"], True, 0.2),
        ("docs-portal", "Docs portal", "build", "/home/user/projects/docs-portal", ["frontend"], False, 0.5),
        ("web-frontend", "Web frontend", "local", r"C:\Projects\web-frontend", ["frontend"], True, 3),
        ("data-pipeline", "Data pipeline", "lab", "/home/user/projects/data-pipeline", ["python"], False, 26),
    ]
    for project_id, name, environment, path, tags, favorite, hours in projects:
        store.save("presets", {"id": project_id, "name": name, "environment_id": environment, "path": path,
                               "tags": tags, "favorite": favorite, "last_used": iso(hours)})
    sessions = [
        ("s1", "site-analysis", "lab", "build", "running", 0.2, "/home/user/projects/site-analysis"),
        ("s2", "site-analysis", "personal", "build", "detached", 0.4, "/home/user/projects/site-analysis"),
        ("s3", "docs-portal", "personal", "build", "running", 0.5, "/home/user/projects/docs-portal"),
        ("s4", "web-frontend", "work", "local", "ended", 3, r"C:\Projects\web-frontend"),
        ("s5", "data-pipeline", "lab", "lab", "unknown", 26, "/home/user/projects/data-pipeline"),
    ]
    for session_id, project, account, environment, status, hours, path in sessions:
        store.save("sessions", {"id": session_id, "preset_id": project, "account_id": account, "environment_id": environment,
                                "name": project, "path": path, "status": status, "updated_at": iso(hours), "persistent": True})


def fake_terminal(tool: str):
    """A terminal-looking panel with a short, made-up CLI exchange (no process, no browser)."""
    from html import escape
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QVBoxLayout
    from vibe_launcher import theme
    from test_workspace import FakeTerminal
    colours = {"prompt": theme.C.terminal, "text": theme.C.text, "dim": theme.C.text_muted, "accent": theme.C.accent,
               "info": theme.C.info, "ok": theme.C.success, "error": theme.C.danger}
    terminal = FakeTerminal()
    layout = QVBoxLayout(terminal)
    layout.setContentsMargins(14, 10, 10, 10)
    html = "".join(f'<span style="color:{colours[kind]}">{escape(text)}</span>' for kind, text in TERMINAL_LINES[tool])
    label = QLabel(f'<pre style="font-family: Cascadia Mono, Consolas, monospace; font-size: 13px">{html}</pre>')
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    layout.addWidget(label, 1)
    terminal.setStyleSheet(f"background: {theme.C.bg};")
    return terminal


def render(lang: str) -> list[Path]:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog
    from vibe_launcher import icons, project_icons
    from vibe_launcher.project_icon_dialog import ProjectIconDialog
    from vibe_launcher.service import LauncherService
    from vibe_launcher.store import Store
    from vibe_launcher.ui import MainWindow
    from vibe_launcher.workspace_page import TabInfo

    data = ROOT / ".local-test" / "readme-shots" / lang
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    icons.BRAND_DIR = data / "no-brand-icons"           # generic glyphs only: no third-party logos in the pictures
    app = QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFont
    app.setFont(QFont(["Segoe UI", "Microsoft JhengHei UI"], 9))   # what Windows gives widgets that set no family
    store = Store(data / "launcher.db")
    seed(store)
    window = MainWindow(store, LauncherService(store), terminal_factory=lambda: fake_terminal("codex"))
    window.timer.stop()
    window.prefs.project_icons.update({
        "site-analysis": {"kind": "glyph", "name": "activity", "color": "#5EEAD4"},
        "docs-portal": {"kind": "glyph", "name": "layers", "color": "#A78BFA"},
        "web-frontend": {"kind": "glyph", "name": "monitor", "color": "#60A5FA"},
        "data-pipeline": {"kind": "glyph", "name": "cpu", "color": "#FB923C"},
    })
    window._refresh_project_icons()
    window.resize(*SIZE)
    window.show()
    app.processEvents()
    written = []

    def shot(widget, name: str) -> None:
        app.processEvents()
        path = OUT / f"{name}-{lang}.png"
        widget.grab().save(str(path))
        written.append(path)

    window.show_page("home")
    shot(window, "home")

    window.show_page("projects")
    for row in range(window.project_list.count()):
        if window.project_list.item(row).data(Qt.ItemDataRole.UserRole) == "site-analysis":
            window.project_list.setCurrentRow(row)
    shot(window, "projects")

    window.show_page("workspace")
    window.workspace.set_layout("grid")
    for index, (tool, project, account, environment) in enumerate((("codex", "Site analysis", "Lab", "build-server"),
                                                                    ("claude", "Docs portal", "Personal", "build-server"))):
        tab = window.workspace.add_terminal(TabInfo(id=f"t{index}", title=tool.capitalize(), project=project,
                                                    environment=environment, kind="agent", tool=tool, account_name=account,
                                                    tmux=True), fake_terminal(tool))
        tab.pane.set_composer_visible(index == 1)
    shot(window, "workspace")

    window.show_page("environments")
    window.env_page.select("build")
    shot(window, "environments")

    dialog = ProjectIconDialog(window, "Site analysis", project_icons.entry_for("site-analysis"))
    dialog.show()
    shot(dialog, "project-icon")
    dialog.reject()
    window.workspace.shutdown()
    window.close()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", choices=("en", "zh-TW"))
    args = parser.parse_args()
    if not args.lang:              # one process per language: the UI language is fixed when the app starts
        for lang in ("en", "zh-TW"):
            subprocess.run([sys.executable, __file__, "--lang", lang], check=True)
        return 0
    os.environ.update(QT_QPA_PLATFORM="offscreen", QT_QPA_FONTDIR=os.environ.get("QT_QPA_FONTDIR", "C:/Windows/Fonts"),
                      QT_SCALE_FACTOR="1.5", VIBE_LAUNCHER_LANGUAGE=args.lang,
                      VIBE_LAUNCHER_HOME=str(ROOT / ".local-test" / "readme-shots" / "home"))
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    OUT.mkdir(parents=True, exist_ok=True)
    for path in render(args.lang):
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
