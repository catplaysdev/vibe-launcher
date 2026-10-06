"""``vibe-launcher-cli --selftest``: start the program headlessly and check what a packaged build can silently lose.

Used by the release workflow (and by hand) after PyInstaller: missing data files, a Qt module that was trimmed too far,
helper command lines, the version. Prints one JSON object and returns 0 only when every check passed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def run() -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from . import __version__, i18n, icons, selfcmd, terminal
    checks: dict[str, object] = {"version": __version__, "frozen": selfcmd.frozen()}
    checks["version_known"] = __version__ != "0.0.0"
    checks["runtime_source"] = selfcmd.runtime_source().startswith(b'"""') or len(selfcmd.runtime_source()) > 10000
    checks["locales"] = all(i18n.catalog(code) for code in i18n.LANGUAGES)
    checks["helper_command"] = selfcmd.command("runner", "x")[-2:] == ["runner", "x"] if selfcmd.frozen() else True
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    checks["logo"] = not icons.logo_icon("#38bdf8").isNull()
    checks["terminal_page"] = (terminal.ASSETS / "terminal.html").is_file() and (terminal.ASSETS / "xterm" / "xterm.js").is_file()
    checks["terminal_available"] = bool(terminal.available())
    from .service import LauncherService
    from .store import Store
    from .ui import MainWindow
    with tempfile.TemporaryDirectory() as folder:
        os.environ["VIBE_LAUNCHER_HOME"] = folder
        store = Store(Path(folder) / "vibe-launcher.sqlite3")
        window = MainWindow(store, LauncherService(store))
        checks["window"] = bool(window.windowTitle())
        checks["gpu_primer"] = getattr(window, "_gpu_primer", None) is not None or not checks["terminal_available"]
        window.workspace.shutdown()
        window.deleteLater()
    app.processEvents()
    checks["ok"] = all(v for k, v in checks.items() if k not in {"version", "frozen"})
    return checks


def main() -> int:
    try:
        result = run()
    except Exception as exc:     # report, never crash silently: CI reads this output
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 1
