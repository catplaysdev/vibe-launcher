"""Home: what is running, what needs attention, recent projects and a new version, in one glance.

Read-only: everything comes from records the window already loaded; buttons ask the window to act.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from . import env_state
from .i18n import environment_name, tr
from .kit import EmptyState, IconTile, PageHeader, StatusBadge, dispose_widget, label, make_button, set_tone
from .theme import S, section_hue

LIVE = {"connecting", "waiting_auth", "running", "detached"}
NEEDS_LOGIN = {"failed", "logged_out"}
RECENT = 6


class Stat(QFrame):
    """A count with its meaning; clicking it opens the page that lists those items."""

    clicked = Signal()

    def __init__(self, icon: str, hue: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("surface", "card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(S.md, S.md, S.md, S.md)
        row.setSpacing(S.md)
        self.tile = IconTile(36, 18)
        self.tile.set_icon(icon, hue)
        row.addWidget(self.tile)
        column = QVBoxLayout()
        column.setSpacing(0)
        self.value = label("0", "dialogTitle")
        self.caption = label("", "meta", wrap=True)
        column.addWidget(self.value)
        column.addWidget(self.caption)
        row.addLayout(column, 1)

    def set(self, value: int, caption: str, tone: str = "") -> None:
        self.value.setText(str(value))
        self.caption.setText(caption)
        set_tone(self.value, tone if value else "")
        self.setAccessibleName(f"{value} {caption}")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class HomePage(QWidget):
    page_requested = Signal(str)
    project_selected = Signal(str)        # show it on the Projects page
    open_requested = Signal(str)          # open a terminal for the project
    restore_requested = Signal(str)       # bring its running work back
    add_project_requested = Signal()
    updates_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._signature = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.xl)
        root.setSpacing(S.lg)
        root.addWidget(PageHeader("Vibe Launcher", tr("What is running and what needs you, at a glance."),
                                  icon="home", hue=section_hue("projects")))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setMinimumWidth(0)
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(S.lg)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self.update_card = QFrame()
        self.update_card.setProperty("surface", "card")
        update_row = QHBoxLayout(self.update_card)
        update_row.setContentsMargins(S.md, S.md, S.md, S.md)
        tile = IconTile(36, 18)
        tile.set_icon("arrow-up-circle", "accent")
        update_row.addWidget(tile)
        self.update_text = label("", wrap=True)
        update_row.addWidget(self.update_text, 1)
        update_row.addWidget(make_button(tr("See what's new"), "primary", callback=self.updates_requested.emit))
        self.update_card.hide()
        self.body.addWidget(self.update_card)

        stats = QGridLayout()
        stats.setSpacing(S.md)
        self.running = Stat("activity", section_hue("sessions"))
        self.attention = Stat("alert", "warning")
        self.unknown = Stat("help", "neutral")
        self.running.clicked.connect(lambda: self.page_requested.emit("sessions"))
        self.attention.clicked.connect(lambda: self.page_requested.emit("accounts"))
        self.unknown.clicked.connect(lambda: self.page_requested.emit("sessions"))
        self.stats = stats
        self._stat_columns = 0
        self.body.addLayout(stats)
        self._lay_out_stats()

        head = QHBoxLayout()
        head.addWidget(label(tr("Recent projects"), "section"), 1)
        head.addWidget(make_button(tr("All projects"), "ghost", "arrow-right", small=True,
                                   callback=lambda: self.page_requested.emit("projects")))
        self.body.addLayout(head)
        self.recent = QVBoxLayout()
        self.recent.setSpacing(S.sm)
        self.body.addLayout(self.recent)
        self.empty = EmptyState()
        self.empty.set_content("folder", tr("No projects yet"), tr("Add a project folder to open Codex or Claude in it."),
                               tr("Add project"))
        self.empty.primary.clicked.connect(self.add_project_requested.emit)
        self.body.addWidget(self.empty)

        head = QHBoxLayout()
        head.addWidget(label(tr("Environments"), "section"), 1)
        head.addWidget(make_button(tr("Manage"), "ghost", "arrow-right", small=True,
                                   callback=lambda: self.page_requested.emit("environments")))
        self.body.addLayout(head)
        self.environments = QHBoxLayout()
        self.environments.setSpacing(S.sm)
        self.environments.addStretch(1)
        self.body.addLayout(self.environments)
        self.body.addStretch(1)

    def _lay_out_stats(self) -> None:
        """Three across on a wide window, stacked on a narrow one."""
        columns = 3 if self.width() >= 760 else 1
        if columns == self._stat_columns:
            return
        self._stat_columns = columns
        for index, stat in enumerate((self.running, self.attention, self.unknown)):
            self.stats.removeWidget(stat)
            self.stats.addWidget(stat, index // columns, index % columns)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._lay_out_stats()

    # -- data -------------------------------------------------------------------------------------
    def set_data(self, records: dict[str, list[dict[str, Any]]], states: list[env_state.EnvState],
                 update: dict[str, Any] | None) -> None:
        sessions = records.get("sessions", [])
        statuses = [s.get("status", "unknown") for s in sessions]
        self.running.set(sum(1 for s in statuses if s in LIVE), tr("running or in the background"), "ok")
        needs = sum(1 for s in records.get("auth_states", []) if s.get("status") in NEEDS_LOGIN)
        needs += sum(1 for g in records.get("github_accounts", []) if g.get("status") in {"failed", "unavailable"})
        self.attention.set(needs, tr("accounts need signing in again"), "warn")
        self.unknown.set(statuses.count("unknown"), tr("work in an unknown state"), "warn")

        releases = (update or {}).get("releases", []) if (update or {}).get("status") == "update_available" else []
        self.update_card.setVisible(bool(releases))
        if releases:
            self.update_text.setText(tr("Version {latest} is available ({count} newer version(s)).",
                                        latest=update.get("latest", ""), count=len(releases)))

        environments = {e["id"]: e for e in records.get("environments", [])}
        accounts = {a["id"]: a for a in records.get("accounts", [])}
        projects = sorted(records.get("presets", []), key=lambda p: (str(p.get("last_used") or ""), bool(p.get("favorite"))),
                          reverse=True)[:RECENT]
        signature = repr(([(p.get("id"), p.get("name"), p.get("last_used"), env_state.project_status(p["id"], sessions))
                           for p in projects], [(s.id, s.tone, s.status_text) for s in states]))
        if signature == self._signature:
            return
        self._signature = signature
        self._clear(self.recent)
        for project in projects:
            self.recent.addWidget(self._project_row(project, sessions, environments, accounts))
        self.empty.setVisible(not projects)
        while self.environments.count() > 1:
            item = self.environments.takeAt(0)
            if item.widget() is not None:
                dispose_widget(item.widget())
        for state in env_state.sort_states(states):
            badge = StatusBadge(f"{state.name} · {state.status_text}", state.tone, "monitor" if state.kind == "local" else "server",
                                tip=state.status_tip, max_width=320)
            self.environments.insertWidget(self.environments.count() - 1, badge)

    @staticmethod
    def _clear(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                dispose_widget(item.widget())

    def _project_row(self, project: dict, sessions: list[dict], environments: dict, accounts: dict) -> QFrame:
        row = QFrame()
        row.setProperty("surface", "card")
        line = QHBoxLayout(row)
        line.setContentsMargins(S.md, S.sm + 2, S.md, S.sm + 2)
        line.setSpacing(S.md)
        account = accounts.get(project.get("account_id", ""), {})
        tile = IconTile(32, 16)
        tile.set_project(project["id"])               # the project's own icon (folder unless one was chosen)
        line.addWidget(tile)
        column = QVBoxLayout()
        column.setSpacing(0)
        name = label(str(project.get("name") or ""), "subsection", wrap=True)
        name.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(name)
        where = environment_name(environments.get(project.get("environment_id", ""), {}))
        detail = " · ".join(filter(None, (account.get("name", ""), where,
                                          env_state.format_relative(project.get("last_used", "")) if project.get("last_used") else "")))
        sub = label(detail, "meta", wrap=True)
        sub.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(sub)
        line.addLayout(column, 1)
        badge = env_state.project_badge(project["id"], sessions)
        if badge:
            line.addWidget(StatusBadge(badge[0], badge[1], "dot"))
        live = env_state.project_status(project["id"], sessions) in LIVE
        action = make_button(tr("Resume") if live else tr("Open"), "secondary" if live else "subtle",
                             "repeat" if live else "play", small=True,
                             callback=lambda *_, p=project["id"], r=live: (self.restore_requested if r else self.open_requested).emit(p))
        line.addWidget(action)
        details = make_button("", "ghost", "arrow-right", small=True, tip=tr("Show in Projects"),
                              callback=lambda *_, p=project["id"]: self.project_selected.emit(p))
        details.setAccessibleName(tr("Show in Projects"))
        line.addWidget(details)
        for child in row.findChildren(QLabel):
            child.setTextFormat(Qt.TextFormat.PlainText)
        return row
