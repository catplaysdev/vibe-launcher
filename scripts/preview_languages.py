"""Render language settings with synthetic data and widget.grab()."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / ".local-test" / "languages-preview"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["VIBE_LAUNCHER_LANGUAGE"] = "en"
os.environ["VIBE_LAUNCHER_HOME"] = str(DEST / "data")
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from PySide6.QtWidgets import QApplication
from vibe_launcher import i18n
from vibe_launcher.kit import dispose_widget
from vibe_launcher.service import LauncherService
from vibe_launcher.settings_dialog import SettingsDialog
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow
from test_workspace import FakeTerminal


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    store = Store(DEST / "data" / "preview.db")
    window = MainWindow(store, LauncherService(store), terminal_factory=FakeTerminal)
    window.timer.stop()
    window.show()
    for locale in i18n.LANGUAGES:
        i18n.set_language(locale)
        window.prefs.language = locale
        dialog = SettingsDialog(window)
        dialog.resize(680, 520)
        dialog.show()
        app.processEvents()
        dialog.grab().save(str(DEST / (locale + "-general.png")))
        dialog.show_page("keyboard")
        app.processEvents()
        dialog.grab().save(str(DEST / (locale + "-keyboard.png")))
        dialog.reject()
        dispose_widget(dialog)
        app.processEvents()
    window.workspace.shutdown()
    window.close()
    print(DEST)


if __name__ == "__main__":
    main()
