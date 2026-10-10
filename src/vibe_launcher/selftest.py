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
    from . import __version__, i18n, icons, runtime, selfcmd, terminal
    checks: dict[str, object] = {"version": __version__, "frozen": selfcmd.frozen()}
    checks["version_known"] = __version__ != "0.0.0"
    checks["runtime_source"] = selfcmd.runtime_source().startswith(b'"""') or len(selfcmd.runtime_source()) > 10000
    source_code = compile(selfcmd.runtime_source(), "runtime.py", "exec")
    shipped_function = next(c for c in source_code.co_consts if getattr(c, "co_name", "") == "sync_extensions")
    checks["runtime_matches_source"] = runtime.sync_extensions.__code__.co_code == shipped_function.co_code
    from .service import HISTORY_ONLY
    checks["native_user_data"] = callable(runtime.sync_user_data) and {"sync_user_data", "native_codex_state"} <= HISTORY_ONLY
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
        if runtime.WINDOWS:
            # Verify discovery without the PATH a terminal or installer supplies.
            home = Path(folder) / "native-home"
            native_bin = home / ".local" / "bin"
            native_bin.mkdir(parents=True)
            for tool in ("codex", "claude"):
                (native_bin / (tool + ".exe")).write_bytes(b"discovery-only")
            previous_path = os.environ.get("PATH")
            try:
                os.environ["PATH"] = runtime.augmented_path("", runtime.user_tool_dirs(home))
                checks["native_cli_discovery"] = all(
                    Path(runtime.trusted_executable(tool)) == native_bin / (tool + ".exe")
                    for tool in ("codex", "claude"))
            finally:
                if previous_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = previous_path
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
