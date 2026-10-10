"""System tray icon: lets the window be closed to the tray while terminals and SSH work keep running.

Only created when the user turns on "Keep running in the system tray" and the platform has a tray.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import icons
from .i18n import tr
from .theme import C


def available() -> bool:
    return QSystemTrayIcon.isSystemTrayAvailable()


class TrayIcon(QSystemTrayIcon):
    def __init__(self, parent, *, on_open: Callable[[], None], on_quit: Callable[[], None]) -> None:
        super().__init__(icons.logo_icon(C.accent), parent)
        self.setToolTip("Vibe Launcher")
        self._menu = QMenu()
        open_action = QAction(tr("Open Vibe Launcher"), self._menu)
        open_action.triggered.connect(on_open)
        quit_action = QAction(tr("Quit"), self._menu)
        quit_action.triggered.connect(on_quit)
        self._menu.addAction(open_action)
        self._menu.addSeparator()
        self._menu.addAction(quit_action)
        self.setContextMenu(self._menu)
        self.activated.connect(lambda reason: on_open() if reason in (
            QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
