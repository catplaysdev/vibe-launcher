"""About and updates: version, newer releases (only those above the installed one) and their notes."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import icons, updates
from .i18n import tr
from .kit import dispose_widget, label, later, make_button, set_tone
from .theme import C, S, theme_bus

SPONSOR_URL = "https://buymeacoffee.com/catplaysdev"
LICENSES = (("xterm.js", "MIT"), ("PySide6 / Qt", "LGPLv3"), ("Lucide icons", "ISC"))


class ReleaseCard(QFrame):
    def __init__(self, release: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("surface", "well")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(S.md, S.md, S.md, S.md)
        layout.setSpacing(S.xs)
        head = QHBoxLayout()
        head.addWidget(label(f"v{release.get('version', '')}", "subsection"))
        head.addWidget(label(release.get("date", ""), "meta"))
        head.addStretch(1)
        self.copy_button = make_button(tr("Copy"), "ghost", "copy", small=True, tip=tr("Copy this version's notes in both languages"),
                                       callback=lambda: copy_notes([release], self.copy_button))
        head.addWidget(self.copy_button)
        layout.addLayout(head)
        for note in updates.notes_for(release)[:20]:
            layout.addWidget(label("• " + note, wrap=True))
        for child in self.findChildren(QLabel):
            child.setTextFormat(Qt.TextFormat.PlainText)
            child.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)


def copy_notes(releases: list[dict[str, Any]], button) -> None:
    """Put the notes (Chinese and English) on the clipboard so they can be pasted into a translator."""
    QGuiApplication.clipboard().setText(updates.release_text(releases))
    button.setText(tr("Copied"))
    later(button, 1500, lambda: button.setText(tr("Copy")))


class AboutPanel(QWidget):
    def __init__(self, window, parent: QWidget | None = None):
        super().__init__(parent)
        self.window = window
        self._shown: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(S.md)
        head = QHBoxLayout()
        head.setSpacing(S.md)
        self.logo = QLabel()
        head.addWidget(self.logo)
        names = QVBoxLayout()
        names.addWidget(label("Vibe Launcher", "dialogTitle"))
        self.version = label("", "muted")
        names.addWidget(self.version)
        head.addLayout(names, 1)
        layout.addLayout(head)

        layout.addWidget(label(tr("Updates"), "subsection"))
        self.status = label("", wrap=True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.check_button = make_button(tr("Check now"), "secondary", "refresh", callback=lambda: window.check_updates(force=True))
        self.update_button = make_button(tr("Update…"), "primary", "download", callback=window.apply_update)
        row.addWidget(self.check_button)
        row.addWidget(self.update_button)
        self.copy_all = make_button(tr("Copy all notes"), "ghost", "copy", callback=lambda: copy_notes(self._shown, self.copy_all))
        row.addWidget(self.copy_all)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(label(tr("Your settings are backed up before updating and existing values are never changed."), "meta", wrap=True))
        self.releases = QVBoxLayout()
        self.releases.setSpacing(S.sm)
        layout.addLayout(self.releases)

        layout.addSpacing(S.sm)
        layout.addWidget(label(tr("Support"), "subsection"))
        sponsor = make_button(tr("Buy me a coffee"), "secondary", "heart",
                              callback=lambda: QDesktopServices.openUrl(QUrl(SPONSOR_URL)))
        layout.addWidget(sponsor, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(S.sm)
        layout.addWidget(label(tr("Third-party software"), "subsection"))
        layout.addWidget(label("  ·  ".join(f"{name} ({kind})" for name, kind in LICENSES), "meta", wrap=True))

        signal = getattr(window, "update_report_changed", None)
        if signal is not None:
            signal.connect(self.refresh)
        theme_bus().changed.connect(self._paint_logo)
        self._paint_logo()
        self.refresh()

    def _paint_logo(self) -> None:
        self.logo.setPixmap(icons.logo_pixmap(56, icons.LOGO_LINE, C.accent, plate=icons.LOGO_PLATE,
                                              dpr=max(2.0, self.devicePixelRatioF())))

    def refresh(self, *_) -> None:
        report = self.window.update_report() or {}
        current = self.window.app_version()          # what is installed, not what an earlier check recorded
        self.version.setText(tr("Version {version}", version=current))
        busy = bool(getattr(self.window, "_update_busy", False))
        status = report.get("status")
        releases = report.get("releases", []) if status == "update_available" else []
        if busy:
            text, tone = tr("Checking for updates…"), "muted"
        elif status == "update_available":
            text, tone = tr("{count} newer version(s) available, latest v{latest}.", count=len(releases),
                            latest=report.get("latest", "")), "accent"
        elif status == "current":
            text, tone = tr("You are on the latest version."), "ok"
        elif status == "unavailable":
            text, tone = report.get("message") or tr("Cannot check for updates right now."), "warn"
        else:
            text, tone = tr("Not checked yet."), "muted"
        self.status.setText(text)
        set_tone(self.status, tone)
        self.check_button.setEnabled(not busy)
        self.update_button.setVisible(bool(releases))
        self._shown = releases
        self.copy_all.setVisible(len(releases) > 1)
        while self.releases.count():
            item = self.releases.takeAt(0)
            if item.widget() is not None:
                dispose_widget(item.widget())
        for release in releases:
            self.releases.addWidget(ReleaseCard(release))
